"""Soft and hard spend caps, and the exception queue (ADR 0032)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import config, services, spend_exceptions
from app.access import AccessPolicy
from app.admin_auth import MemoryAuditStore
from app.auth import ApiKey
from app.config import Limits, Pricing, Tier
from app.spend_exceptions import Grants, MemoryExceptionStore
from tests.conftest import OPEN_POLICY, add_key
from tests.test_governance import decided, team_key
from tests.test_identity import SCIM, Idp, bearer, provision
from tests.test_identity import api as api  # the fixtures
from tests.test_identity import idp as idp

MSGS = [{"role": "user", "content": "hi"}]
POLICY = AccessPolicy.model_validate(
    {
        **OPEN_POLICY.model_dump(),
        "version": "spend-1",
        "group_roles": {"platform-admin": "platform-admin", "eng": "engineer"},
        "role_tiers": {"platform-admin": "standard", "engineer": "standard"},
        "spend_exceptions": {"approver_groups": ["platform-admin"], "max_usd": 50},
    }
)


@pytest.fixture(autouse=True)
def capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """$1 per token, a `capped` tier with a soft cap of $10 and a hard cap of $20, and a
    policy that names an approver group."""
    pricing = Pricing.model_validate({"models": {"chaos/ok": {"input": 1e6, "output": 1e6}}})
    monkeypatch.setattr(config, "pricing", pricing)
    tiers = {name: tier.model_dump() for name, tier in config.limits.tiers.items()}
    tiers["capped"] = {
        "requests_per_minute": 10**6,
        "tokens_per_minute": 10**9,
        "monthly_budget_usd": 20,
        "soft_budget_usd": 10,
        "allowed_aliases": ["*"],
        "concurrent_requests": 0,
    }
    monkeypatch.setattr(config, "limits", Limits.model_validate({"tiers": tiers}))
    monkeypatch.setattr(config, "access", POLICY)
    monkeypatch.setattr(services, "audit", MemoryAuditStore())


def headers(plaintext: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {plaintext}"}


@pytest.fixture
def caller() -> dict[str, str]:
    return headers(add_key("capped"))


@pytest.fixture
def approver() -> dict[str, str]:
    return team_key("platform-admin")


def chat(client: TestClient, who: dict[str, str]) -> httpx.Response:
    return client.post(
        "/v1/chat/completions", json={"model": "chaos/ok", "messages": MSGS}, headers=who
    )


def until_refused(client: TestClient, who: dict[str, str], most: int = 60) -> list[httpx.Response]:
    """Call until the hard cap refuses. → every response, the 429 last."""
    out = []
    for _ in range(most):
        out.append(chat(client, who))
        if out[-1].status_code == 429:
            return out
    raise AssertionError("never reached the cap")


def ask(
    client: TestClient, who: dict[str, str], amount: float = 30, reason: str = "month-end run"
) -> httpx.Response:
    return client.post(
        "/v1/spend/exceptions", json={"amount_usd": amount, "reason": reason}, headers=who
    )


def code(resp: httpx.Response) -> str:
    return str(resp.json()["error"]["code"])


# --- soft and hard caps ------------------------------------------------------------------


def test_past_the_soft_cap_requests_run_and_the_caller_is_told(
    client: TestClient, caller: dict[str, str]
) -> None:
    responses = until_refused(client, caller)
    served = responses[:-1]
    flags = [r.headers.get("x-gateway-budget") for r in served]
    assert flags[0] is None and flags[-1] == "soft-cap-exceeded"
    assert flags == sorted(flags, key=lambda f: f is not None)  # once past it, always told
    allowed = [d for d in decided() if d.decision == "allow"]
    assert [bool(d.detail.get("soft_cap_exceeded")) for d in allowed] == [
        f is not None for f in flags
    ]


def test_the_hard_cap_refuses_and_says_how_to_ask_for_more(
    client: TestClient, caller: dict[str, str]
) -> None:
    refused = until_refused(client, caller)[-1]
    assert code(refused) == "insufficient_quota"
    assert "POST /v1/spend/exceptions" in refused.json()["error"]["message"]
    assert (decided()[-1].decision, decided()[-1].code) == ("deny", "budget")


def test_the_soft_cap_alerts_once_per_caller_and_month(
    client: TestClient, caller: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    class Alerts:
        """The alert channel: the dedupe key is what makes an alert go out once."""

        def __init__(self) -> None:
            self.sent: dict[str, dict[str, Any]] = {}

        async def deliver(self, payload: dict[str, Any], dedupe: str, ttl: int) -> None:
            self.sent.setdefault(dedupe, payload)

    alerts = Alerts()
    monkeypatch.setattr(services, "alerts", alerts)
    until_refused(client, caller)
    soft = [(key, p) for key, p in alerts.sent.items() if p["level"] == "soft"]
    ((key, payload),) = soft
    assert key.endswith(f":{payload['period']}:soft") and "soft cap" in payload["text"]
    assert {p["level"] for p in alerts.sent.values()} >= {"soft", 100}


def test_a_soft_cap_must_be_below_the_hard_cap() -> None:
    base = {"requests_per_minute": 1, "tokens_per_minute": 1, "allowed_aliases": []}
    with pytest.raises(ValidationError, match="soft_budget_usd"):
        Tier.model_validate({**base, "monthly_budget_usd": 10, "soft_budget_usd": 11})
    assert Tier.model_validate({**base, "monthly_budget_usd": 10}).soft_budget_usd is None


def test_a_key_with_a_smaller_budget_of_its_own_has_no_soft_cap() -> None:
    def limits(**overrides: Any) -> Any:
        return ApiKey("k", "n", "p", "capped", overrides).limits(config.limits)

    assert limits().soft_budget_usd == 10
    assert limits(monthly_budget_usd=50).soft_budget_usd == 10
    assert limits(monthly_budget_usd=10).soft_budget_usd is None


# --- the exception queue -----------------------------------------------------------------


def test_an_approved_exception_raises_the_cap_for_the_month(
    client: TestClient, caller: dict[str, str], approver: dict[str, str]
) -> None:
    until_refused(client, caller)
    asked = ask(client, caller, 30)
    assert asked.status_code == 201
    pending = asked.json()
    assert (pending["status"], pending["amount_usd"]) == ("pending", 30)
    assert "spend_id" not in pending
    assert chat(client, caller).status_code == 429  # asking changes nothing

    approved = client.post(
        f"/v1/spend/exceptions/{pending['id']}/approve",
        json={"amount_usd": 25, "note": "ok until the 31st"},
        headers=approver,
    )
    assert approved.status_code == 200
    body = approved.json()
    assert (body["status"], body["granted_usd"]) == ("approved", 25)
    assert body["decided_by"].startswith("key:") and body["decided_at"]

    again = chat(client, caller)
    assert again.status_code == 200  # at once on this replica
    assert decided()[-1].detail["exception_usd"] == 25
    assert until_refused(client, caller)[-1].status_code == 429  # the new cap is a cap too
    spent = [d for d in decided() if d.action == "model:chaos/ok" and d.decision == "allow"]
    assert 20 < len(spent) * 3 < 20 + 25 + 15  # about $45 at a few dollars a request


def test_a_denied_exception_changes_nothing(
    client: TestClient, caller: dict[str, str], approver: dict[str, str]
) -> None:
    until_refused(client, caller)
    pending = ask(client, caller).json()
    denied = client.post(
        f"/v1/spend/exceptions/{pending['id']}/deny", json={"note": "use local"}, headers=approver
    )
    assert denied.status_code == 200
    assert (denied.json()["status"], denied.json()["granted_usd"]) == ("denied", None)
    assert chat(client, caller).status_code == 429
    assert ask(client, caller).status_code == 201  # they may ask again


def test_only_an_approver_group_decides_and_never_their_own_request(
    client: TestClient, caller: dict[str, str], approver: dict[str, str]
) -> None:
    theirs = ask(client, caller).json()["id"]
    engineer = team_key("eng")
    refused = client.post(f"/v1/spend/exceptions/{theirs}/approve", headers=engineer)
    assert (refused.status_code, code(refused)) == (403, "not_an_approver")
    assert code(client.post(f"/v1/spend/exceptions/{theirs}/approve", headers=caller)) == (
        "not_an_approver"
    )
    own = ask(client, approver).json()["id"]
    selfish = client.post(f"/v1/spend/exceptions/{own}/approve", headers=approver)
    assert (selfish.status_code, code(selfish)) == (403, "self_approval")
    denials = [d for d in decided() if d.decision == "deny"]
    assert [d.code for d in denials] == ["not_an_approver", "not_an_approver", "self_approval"]
    assert all(d.action == "spend_exception:approve" for d in denials)
    assert services.exceptions.entries[theirs].status == "pending"  # type: ignore[attr-defined]


def test_nobody_approves_when_the_policy_names_no_group(
    client: TestClient,
    caller: dict[str, str],
    approver: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(config, "access", OPEN_POLICY)
    theirs = ask(client, caller).json()["id"]
    assert code(client.post(f"/v1/spend/exceptions/{theirs}/approve", headers=approver)) == (
        "not_an_approver"
    )


def test_bad_requests(client: TestClient, caller: dict[str, str], approver: dict[str, str]) -> None:
    assert code(ask(client, caller, 51)) == "amount_too_large"  # the policy's max is 50
    assert ask(client, caller, 0).status_code == 400
    assert ask(client, caller, 5, "").status_code == 400
    assert ask(client, caller, 5, "x" * 501).status_code == 400
    first = ask(client, caller, 5).json()["id"]
    assert (ask(client, caller, 5).status_code, code(ask(client, caller, 5))) == (
        409,
        "exception_pending",
    )
    more = client.post(
        f"/v1/spend/exceptions/{first}/approve", json={"amount_usd": 6}, headers=approver
    )
    assert code(more) == "amount_too_large"
    assert client.post("/v1/spend/exceptions/nope/approve", headers=approver).status_code == 404
    assert (
        client.post(f"/v1/spend/exceptions/{'a' * 500}/deny", headers=approver).status_code == 404
    )
    assert client.post(f"/v1/spend/exceptions/{first}/approve", headers=approver).status_code == 200
    late = client.post(f"/v1/spend/exceptions/{first}/deny", headers=approver)
    assert (late.status_code, code(late)) == (409, "already_decided")
    anon = TestClient(client.app)
    assert (
        anon.post("/v1/spend/exceptions", json={"amount_usd": 1, "reason": "x"}).status_code == 401
    )


def test_callers_see_their_own_requests_and_approvers_see_all(
    client: TestClient, caller: dict[str, str], approver: dict[str, str]
) -> None:
    other = headers(add_key("capped"))
    mine = ask(client, caller, 5).json()["id"]
    theirs = ask(client, other, 7).json()["id"]
    seen = client.get("/v1/spend/exceptions", headers=caller).json()
    assert seen["approver"] is False and [e["id"] for e in seen["exceptions"]] == [mine]
    queue = client.get("/v1/spend/exceptions?status=pending", headers=approver).json()
    assert queue["approver"] is True
    assert {e["id"] for e in queue["exceptions"]} == {mine, theirs}
    client.post(f"/v1/spend/exceptions/{mine}/deny", headers=approver)
    left = client.get("/v1/spend/exceptions?status=pending", headers=approver).json()
    assert [e["id"] for e in left["exceptions"]] == [theirs]


def test_every_step_is_audited_without_the_reason(
    client: TestClient, caller: dict[str, str], approver: dict[str, str]
) -> None:
    pending = ask(client, caller, 30, "SECRET-PROJECT-NAME needs it").json()
    client.post(f"/v1/spend/exceptions/{pending['id']}/approve", headers=approver)
    asked, approved = decided()
    assert (asked.action, asked.decision, asked.data_class) == (
        "spend_exception:request",
        "allow",
        "none",
    )
    assert asked.detail == {
        "exception": pending["id"],
        "amount_usd": 30,
        "period": pending["period"],
    }
    assert (approved.action, approved.decision) == ("spend_exception:approve", "allow")
    assert approved.detail["subject"] == asked.subject and approved.detail["granted_usd"] == 30
    assert approved.subject != asked.subject
    assert "SECRET" not in str(asked.row()) + str(approved.row())
    audit = services.audit
    assert isinstance(audit, MemoryAuditStore)
    (entry,) = audit.entries
    assert (entry.action, entry.target) == ("spend_exception.approve", pending["id"])
    assert entry.operator == approved.subject and entry.detail["granted_usd"] == 30


def test_the_shipped_policy_names_an_approver_group() -> None:
    policy = config.load_access(Path("config"))
    assert policy.spend_exceptions.approver_groups == ["platform-admin"]
    assert "platform-admin" in policy.group_roles


# --- users from the identity provider ----------------------------------------------------


def test_a_platform_admin_user_approves_and_loses_the_right_with_the_group(
    api: TestClient,
    idp: Idp,
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    monkeypatch.setattr(config, "access", POLICY)
    provision(api, "alice@example.com", "eng")
    paula = provision(api, "paula@example.com", "platform-admin")
    alice_token, paula_token = idp.token("ext-alice"), idp.token("ext-paula")
    theirs = ask(api, bearer(alice_token)).json()
    assert theirs["subject"].startswith("user:") and theirs["subject_name"] == "alice@example.com"
    ok = api.post(f"/v1/spend/exceptions/{theirs['id']}/approve", headers=bearer(paula_token))
    assert ok.status_code == 200 and ok.json()["decided_by_name"] == "paula@example.com"

    another = ask(api, bearer(alice_token)).json()["id"]
    gid = api.get('/scim/v2/Groups?filter=displayName eq "platform-admin"', headers=SCIM).json()[
        "Resources"
    ][0]["id"]
    body = {"Operations": [{"op": "remove", "path": f'members[value eq "{paula}"]'}]}
    assert api.patch(f"/scim/v2/Groups/{gid}", json=body, headers=SCIM).status_code == 200
    late = api.post(f"/v1/spend/exceptions/{another}/approve", headers=bearer(paula_token))
    assert late.status_code == 403  # the same token, minutes later: no longer an approver


# --- the grant lookup --------------------------------------------------------------------


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


async def approved(store: MemoryExceptionStore, amount: float, spend_id: str = "k1") -> None:
    entry = spend_exceptions.new("key:k1", "bot", spend_id, "2026-10", amount, "r")
    assert await store.create(entry)
    assert await store.decide(entry.id, "approved", "user:p", "paula", amount, None)


async def test_grants_are_remembered_briefly_and_forgotten_on_approval() -> None:
    store, clock = MemoryExceptionStore(), Clock()
    grants = Grants(store, clock)
    assert await grants.total("k1", "2026-10") == 0
    await approved(store, 10)
    assert await grants.total("k1", "2026-10") == 0  # remembered: no query per refusal
    clock.now = Grants.TTL_SECONDS + 1
    assert await grants.total("k1", "2026-10") == 10
    await approved(store, 5)
    grants.forget("k1", "2026-10")  # what an approval on this replica does
    assert await grants.total("k1", "2026-10") == 15
    assert await grants.total("k1", "2026-11") == 0  # this month only
    assert await grants.total("k2", "2026-10") == 0


async def test_no_grant_when_the_store_cannot_answer() -> None:
    class Down(MemoryExceptionStore):
        async def granted(self, spend_id: str, period: str) -> float:
            raise ConnectionError("db down")

    assert await Grants(Down()).total("k1", "2026-10") == 0  # the cap holds


async def test_one_pending_request_per_caller_and_month_and_one_decision() -> None:
    store = MemoryExceptionStore()
    first = spend_exceptions.new("key:k1", "bot", "k1", "2026-10", 10, "r")
    assert await store.create(first)
    assert not await store.create(spend_exceptions.new("key:k1", "bot", "k1", "2026-10", 5, "r"))
    assert await store.create(spend_exceptions.new("key:k1", "bot", "k1", "2026-11", 5, "r"))
    assert await store.decide(first.id, "denied", "user:p", "paula", None, "no")
    assert await store.decide(first.id, "approved", "user:q", "quinn", 10, None) is None
    assert await store.granted("k1", "2026-10") == 0
