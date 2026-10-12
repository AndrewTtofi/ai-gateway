"""Decisions for external tool servers: POST /v1/authorize (ADR 0033)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, connectors, services
from app.access import AccessPolicy, check_references
from tests.test_governance import decided, team_key
from tests.test_identity import SCIM, Idp, bearer, provision
from tests.test_identity import api as api  # the fixtures
from tests.test_identity import idp as idp

TOOLS = ["jira_search", "jira_add_comment"]
POLICY = AccessPolicy.model_validate(
    {
        "version": "delegated-1",
        "data_classes": ["public", "internal", "confidential", "restricted"],
        "default_data_class": "internal",
        "group_roles": {"eng": "engineer", "compliance": "compliance", "everyone": "staff"},
        "role_tiers": {"engineer": "standard", "compliance": "standard", "staff": "dev"},
        "tier_rank": ["dev", "standard"],
        "models": {"*": {"retention": "none", "max_data_class": "restricted"}},
        "secondary_use_max_data_class": "internal",
        "connectors": {
            "atlassian": {
                "external": True,
                "min_tier": "standard",
                "max_data_class": "internal",
                "allowed_roles": ["engineer"],
                "tools": TOOLS,
            },
            "mock_records": {"min_tier": "standard", "max_data_class": "restricted"},
        },
        "delegated_decisions": {"client_groups": ["mcp-servers"]},
    }
)


@pytest.fixture(autouse=True)
def policy(idp: Idp, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setattr(config, "access", POLICY)


@pytest.fixture
def server_key() -> dict[str, str]:
    """The tool server's own gateway key, in the group allowed to ask."""
    return team_key("mcp-servers")


def ask(
    api: TestClient,  # noqa: F811
    key: dict[str, str],
    subject: str = "ext-alice",
    tool: str = "jira_search",
    connector: str = "atlassian",
    **extra: Any,
) -> httpx.Response:
    body = {"subject": subject, "connector": connector, "tool": tool, **extra}
    return api.post("/v1/authorize", json=body, headers=key)


def test_an_entitled_user_is_allowed_and_the_decision_is_recorded_under_their_name(
    api: TestClient,
    server_key: dict[str, str],  # noqa: F811
) -> None:
    uid = provision(api, "alice@example.com", "eng")
    resp = ask(api, server_key)
    assert resp.status_code == 200
    assert resp.json() == {
        "allowed": True,
        "code": "ok",
        "reason": "ok",
        "policy_version": "delegated-1",
    }
    d = decided()[-1]
    assert (d.subject, d.subject_name, d.roles) == (
        f"user:{uid}",
        "alice@example.com",
        ["engineer"],
    )
    assert (d.action, d.decision, d.data_class) == (
        "tool:atlassian.jira_search",
        "allow",
        "internal",
    )
    assert d.detail["asked_by"].startswith("key:")  # which server asked


@pytest.mark.parametrize(
    ("groups", "question", "code"),
    [
        (("compliance",), {}, "role_not_allowed"),
        (("everyone",), {}, "tier_too_low"),
        (("eng",), {"data_class": "confidential"}, "data_class_not_eligible"),
        (("eng",), {"tool": "jira_delete_everything"}, "tool_not_exposed"),
        (("eng",), {"connector": "payroll"}, "connector_unknown"),
        (("eng",), {"connector": "mock_records"}, "connector_not_external"),
        ((), {}, "no_entitlement"),
    ],
)
def test_everyone_else_is_refused_with_the_reason_and_recorded(
    api: TestClient,  # noqa: F811
    server_key: dict[str, str],
    groups: tuple[str, ...],
    question: dict[str, Any],
    code: str,
) -> None:
    provision(api, "alice@example.com", *groups)
    resp = ask(api, server_key, **question)
    assert resp.status_code == 200  # a decision was made; it is "no"
    assert (resp.json()["allowed"], resp.json()["code"]) == (False, code)
    assert (decided()[-1].decision, decided()[-1].code) == ("deny", code)


def test_unknown_and_deactivated_users_are_refused(
    api: TestClient,
    server_key: dict[str, str],  # noqa: F811
) -> None:
    assert ask(api, server_key, subject="ext-nobody").json()["code"] == "user_not_provisioned"
    uid = provision(api, "alice@example.com", "eng")
    assert ask(api, server_key).json()["allowed"] is True
    off = {"Operations": [{"op": "replace", "value": {"active": False}}]}
    assert api.patch(f"/scim/v2/Users/{uid}", json=off, headers=SCIM).status_code == 200
    # Deprovisioning applies to the tool server at once, like everywhere else.
    assert ask(api, server_key).json() == {
        "allowed": False,
        "code": "user_deactivated",
        "reason": "This user is deactivated.",
        "policy_version": "delegated-1",
    }


def test_only_a_service_in_the_named_group_may_ask(
    api: TestClient,
    idp: Idp,
    server_key: dict[str, str],  # noqa: F811
) -> None:
    provision(api, "alice@example.com", "eng")
    other = ask(api, team_key("eng"))
    assert (other.status_code, other.json()["error"]["code"]) == (403, "not_a_decision_client")
    # A person can't ask about someone else either, whatever groups they are in.
    provision(api, "mallory@example.com", "mcp-servers", "eng")
    person = ask(api, bearer(idp.token(sub="ext-mallory")))
    assert person.json()["error"]["code"] == "not_a_decision_client"
    assert api.post("/v1/authorize", json={}).status_code == 401
    assert [d.code for d in decided() if d.action == "authorize"] == ["not_a_decision_client"] * 2


def test_nobody_may_ask_when_the_policy_names_no_group(
    api: TestClient,
    server_key: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    closed = POLICY.model_copy(
        update={
            "delegated_decisions": POLICY.delegated_decisions.model_copy(
                update={"client_groups": []}
            )
        }
    )
    monkeypatch.setattr(config, "access", closed)
    assert ask(api, server_key).json()["error"]["code"] == "not_a_decision_client"


def test_bad_questions(api: TestClient, server_key: dict[str, str]) -> None:  # noqa: F811
    provision(api, "alice@example.com", "eng")
    assert ask(api, server_key, data_class="nope").json()["error"]["code"] == "invalid_data_class"
    assert ask(api, server_key, subject="").status_code == 400
    assert ask(api, server_key, tool="t" * 65).status_code == 400
    assert ask(api, server_key, extra="x").status_code == 400


def test_the_directory_being_down_is_not_a_yes(
    api: TestClient,
    server_key: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,  # noqa: F811
) -> None:
    async def down(*args: Any) -> None:
        raise ConnectionError("db down")

    monkeypatch.setattr(services.identities, "lookup", down)
    resp = ask(api, server_key)
    assert (resp.status_code, resp.json()["error"]["code"]) == (503, "auth_unavailable")


def test_an_external_connector_is_not_callable_through_the_gateway(
    api: TestClient,
    idp: Idp,  # noqa: F811
) -> None:
    provision(api, "alice@example.com", "eng")
    resp = api.post(
        "/v1/tools/atlassian/jira_search", json={"args": {}}, headers=bearer(idp.token())
    )
    assert (resp.status_code, resp.json()["error"]["code"]) == (403, "tool_not_exposed")


def test_the_policy_check_knows_external_from_built_in() -> None:
    tiers, targets, built_in = {"dev", "standard"}, set[str](), set(connectors.REGISTRY)
    check_references(POLICY, tiers, targets, built_in)  # external needs no code here

    def with_rule(name: str, **rule: Any) -> AccessPolicy:
        base = {"min_tier": "standard", "max_data_class": "internal"}
        return POLICY.model_copy(
            update={
                "connectors": {
                    name: POLICY.connectors["atlassian"].model_validate({**base, **rule})
                }
            }
        )

    for broken, says in (
        (with_rule("mock_records", external=True), "external but also built in"),
        (with_rule("nowhere"), "not a registered connector"),
        (with_rule("mock_records", tools=["x"]), "lists tools but is not external"),
    ):
        with pytest.raises(ValueError, match=says):
            check_references(broken, tiers, targets, built_in)


def test_the_shipped_policy_has_the_external_connector_and_its_clients() -> None:
    from pathlib import Path

    shipped = config.load_access(Path("config"))
    assert shipped.connectors["atlassian"].external is True
    assert shipped.delegated_decisions.client_groups == ["mcp-servers"]
    assert "mcp-servers" in config.limits.teams
