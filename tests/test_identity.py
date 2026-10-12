"""Users from the identity provider (ADR 0029): an OIDC token says who, the directory and
the policy say what they may do, on every request."""

import asyncio
import base64
import hashlib
import hmac
import json
import time
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app import config, main, services
from app.access import AccessPolicy
from app.config import Registry
from app.decisions import DecisionRecord, MemoryDecisionSink
from app.oidc import KeysUnavailable, TokenError, Verifier

ISSUER = "https://idp.test/application/o/gateway/"
AUDIENCE = "gateway-client-id"
SCIM_TOKEN = "scim-test-token-0123456789abcdef"  # noqa: S105 (a throwaway test token)
SCIM = {"Authorization": f"Bearer {SCIM_TOKEN}"}
MSGS = [{"role": "user", "content": "hi"}]

POLICY = AccessPolicy.model_validate(
    {
        "version": "identity-1",
        "data_classes": ["public", "internal", "confidential", "restricted"],
        "default_data_class": "internal",
        "group_roles": {"compliance": "compliance", "eng": "engineer", "everyone": "staff"},
        "role_tiers": {"compliance": "standard", "engineer": "standard", "staff": "dev"},
        "tier_rank": ["dev", "standard"],
        "models": {"*": {"retention": "none", "max_data_class": "restricted"}},
        "secondary_use_max_data_class": "internal",
        "connectors": {
            "mock_records": {
                "min_tier": "standard",
                "max_data_class": "restricted",
                "allowed_roles": ["compliance"],
            }
        },
    }
)


class Idp:
    """A stand-in identity provider: signing keys, a JWKS endpoint, and tokens."""

    def __init__(self) -> None:
        self.keys: dict[str, rsa.RSAPrivateKey] = {}
        self.fetches: list[str] = []
        self.down = False
        self.rotate("k1")

    def rotate(self, kid: str) -> None:
        self.keys = {kid: rsa.generate_private_key(public_exponent=65537, key_size=2048)}

    @property
    def kid(self) -> str:
        return next(iter(self.keys))

    def jwks(self) -> dict[str, Any]:
        out = []
        for kid, key in self.keys.items():
            jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
            out.append({**jwk, "kid": kid, "use": "sig", "alg": "RS256"})
        return {"keys": out}

    async def fetch(self, url: str) -> dict[str, Any]:
        self.fetches.append(url)
        if self.down:
            raise httpx.ConnectError("idp down")
        if url.endswith("/.well-known/openid-configuration"):
            return {"issuer": ISSUER, "jwks_uri": f"{ISSUER}jwks/"}
        return self.jwks()

    def token(self, sub: str = "ext-alice", **claims: Any) -> str:
        now = int(time.time())
        payload = {"iss": ISSUER, "aud": AUDIENCE, "sub": sub, "iat": now, "exp": now + 300}
        payload.update(claims)
        headers = {"kid": payload.pop("kid", self.kid)}
        key = payload.pop("signing_key", self.keys[self.kid])
        return jwt.encode(
            {k: v for k, v in payload.items() if v is not None}, key, "RS256", headers
        )


@pytest.fixture
def idp(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> Idp:
    provider = Idp()
    verifier = Verifier(ISSUER, AUDIENCE, fetch=provider.fetch)
    monkeypatch.setattr(services, "oidc", lambda: verifier)
    monkeypatch.setattr(config.settings, "scim_token", SCIM_TOKEN)
    monkeypatch.setattr(config, "access", POLICY)
    return provider


@pytest.fixture
def api(idp: Idp) -> TestClient:
    return TestClient(main.app)  # no default credentials


def provision(api: TestClient, name: str = "alice@example.com", *groups: str) -> str:
    """What the identity provider does over SCIM. → the user's directory id."""
    ext = f"ext-{name.split('@')[0]}"
    resp = api.post("/scim/v2/Users", json={"userName": name, "externalId": ext}, headers=SCIM)
    assert resp.status_code == 201, resp.text
    uid = str(resp.json()["id"])
    for g in groups:
        found = api.get(f'/scim/v2/Groups?filter=displayName eq "{g}"', headers=SCIM).json()
        if found["totalResults"]:
            gid = found["Resources"][0]["id"]
            body = {"Operations": [{"op": "add", "path": "members", "value": [{"value": uid}]}]}
            assert api.patch(f"/scim/v2/Groups/{gid}", json=body, headers=SCIM).status_code == 200
        else:
            body = {"displayName": g, "members": [{"value": uid}]}
            assert api.post("/scim/v2/Groups", json=body, headers=SCIM).status_code == 201
    return uid


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def chat(api: TestClient, token: str, model: str = "local") -> httpx.Response:
    return api.post(
        "/v1/chat/completions", json={"model": model, "messages": MSGS}, headers=bearer(token)
    )


def lookup_case(api: TestClient, token: str) -> httpx.Response:
    return api.post(
        "/v1/tools/mock_records/lookup_case",
        json={"args": {"case_id": "C-1001"}},
        headers=bearer(token),
    )


def decided() -> list[DecisionRecord]:
    sink = services.decisions
    assert isinstance(sink, MemoryDecisionSink)
    return sink.records


def code(resp: httpx.Response) -> Any:
    return resp.json()["error"]["code"]


# --- a provisioned user ------------------------------------------------------------------


def test_a_provisioned_user_calls_the_gateway_with_their_token(api: TestClient, idp: Idp) -> None:
    uid = provision(api, "alice@example.com", "compliance", "everyone")
    resp = lookup_case(api, idp.token())
    assert resp.status_code == 200 and resp.json()["result"]["status"] == "open"
    (d,) = decided()
    assert (d.subject, d.subject_kind, d.subject_name) == (
        f"user:{uid}",
        "user",
        "alice@example.com",
    )
    assert (d.roles, d.tier) == (["compliance", "staff"], "standard")  # the highest tier wins
    assert d.action == "tool:mock_records.lookup_case" and d.decision == "allow"


def test_usage_limits_and_spend_are_per_user(
    api: TestClient, idp: Idp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config.limits.tiers["standard"], "allowed_aliases", ["*"])
    uid = provision(api, "alice@example.com", "eng")
    assert chat(api, idp.token(), "chaos/ok").status_code == 200
    usage = services.usage.records[-1]  # type: ignore[attr-defined]
    assert (usage.key_id, usage.key_prefix) == (uid, "user")  # every token of hers adds up here


def test_the_token_can_arrive_as_x_api_key(api: TestClient, idp: Idp) -> None:
    """Anthropic SDKs send their credential in x-api-key."""
    provision(api, "alice@example.com", "compliance")
    resp = api.post(
        "/v1/tools/mock_records/lookup_case",
        json={"args": {"case_id": "C-1001"}},
        headers={"x-api-key": idp.token()},
    )
    assert resp.status_code == 200


def test_the_user_can_be_matched_on_another_claim(
    api: TestClient, idp: Idp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config.settings, "oidc_user_claim", "preferred_username")
    monkeypatch.setattr(config.settings, "oidc_user_match", "user_name")
    provision(api, "alice@example.com", "compliance")
    token = idp.token(sub="something-opaque", preferred_username="alice@example.com")
    assert lookup_case(api, token).status_code == 200
    assert code(lookup_case(api, idp.token(sub="something-opaque"))) == "invalid_token"  # no claim


def test_a_users_team_is_the_first_group_with_a_team_budget(
    api: TestClient, idp: Idp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(config.limits.teams, "compliance", config.Team(monthly_budget_usd=50))
    provision(api, "alice@example.com", "compliance", "everyone")
    lookup_case(api, idp.token())
    assert decided()[-1].team == "compliance"


# --- groups come from the directory, on every request ---------------------------------------


def test_entitlements_follow_group_membership_not_the_token(api: TestClient, idp: Idp) -> None:
    """The same token, before and after the identity provider moves the user."""
    uid = provision(api, "alice@example.com", "eng")
    token = idp.token(groups=["compliance"])  # a claim the gateway doesn't trust
    assert code(lookup_case(api, token)) == "role_not_allowed"

    provision(api, "bob@example.com", "compliance")  # creates the group
    gid = api.get('/scim/v2/Groups?filter=displayName eq "compliance"', headers=SCIM).json()[
        "Resources"
    ][0]["id"]
    add = {"Operations": [{"op": "add", "path": "members", "value": [{"value": uid}]}]}
    api.patch(f"/scim/v2/Groups/{gid}", json=add, headers=SCIM)
    assert lookup_case(api, token).status_code == 200

    remove = {"Operations": [{"op": "remove", "path": f'members[value eq "{uid}"]'}]}
    api.patch(f"/scim/v2/Groups/{gid}", json=remove, headers=SCIM)
    assert code(lookup_case(api, token)) == "role_not_allowed"


def test_a_deactivated_user_gets_403_with_a_token_that_is_still_valid(
    api: TestClient, idp: Idp
) -> None:
    uid = provision(api, "alice@example.com", "compliance")
    token = idp.token()
    assert lookup_case(api, token).status_code == 200

    off = {"Operations": [{"op": "replace", "value": {"active": False}}]}
    assert api.patch(f"/scim/v2/Users/{uid}", json=off, headers=SCIM).status_code == 200
    resp = lookup_case(api, token)
    assert resp.status_code == 403 and code(resp) == "user_deactivated"
    assert chat(api, token).status_code == 403  # every endpoint
    d = decided()[-1]
    assert (d.decision, d.code, d.action, d.subject) == (
        "deny",
        "user_deactivated",
        "authenticate",
        f"user:{uid}",
    )

    on = {"Operations": [{"op": "replace", "value": {"active": True}}]}
    api.patch(f"/scim/v2/Users/{uid}", json=on, headers=SCIM)
    assert lookup_case(api, token).status_code == 200


def test_a_deleted_user_is_no_longer_provisioned(api: TestClient, idp: Idp) -> None:
    uid = provision(api, "alice@example.com", "compliance")
    token = idp.token()
    assert api.delete(f"/scim/v2/Users/{uid}", headers=SCIM).status_code == 204
    assert code(lookup_case(api, token)) == "user_not_provisioned"


def test_a_valid_token_for_someone_never_provisioned_is_403_and_recorded(
    api: TestClient, idp: Idp
) -> None:
    resp = lookup_case(api, idp.token(sub="ext-mallory"))
    assert resp.status_code == 403 and code(resp) == "user_not_provisioned"
    (d,) = decided()
    assert (d.decision, d.subject, d.action) == ("deny", "token:ext-mallory", "authenticate")


def test_a_user_whose_groups_map_to_no_role_has_no_entitlement(api: TestClient, idp: Idp) -> None:
    provision(api, "alice@example.com", "book-club")
    resp = chat(api, idp.token())
    assert resp.status_code == 403 and code(resp) == "no_entitlement"
    assert decided()[-1].code == "no_entitlement"


# --- tokens that must not get in -------------------------------------------------------------


def forged(idp: Idp, how: str) -> str:
    now = int(time.time())
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    if how == "expired":
        return idp.token(exp=now - 3600)
    if how == "wrong audience":
        return idp.token(aud="some-other-app")
    if how == "wrong issuer":
        return idp.token(iss="https://evil.test/")
    if how == "another key, same kid":
        return idp.token(signing_key=other)
    if how == "unknown kid":
        return idp.token(kid="k-unknown", signing_key=other)
    if how == "no expiry":
        return idp.token(exp=None)
    payload = {"iss": ISSUER, "aud": AUDIENCE, "sub": "ext-alice", "exp": now + 300}
    if how == "hs256 signed with the public key":
        # Algorithm confusion: if the server used its public key as an HMAC secret, anyone
        # could mint tokens. Built by hand, since PyJWT refuses to sign this way.
        public = (
            idp.keys[idp.kid]
            .public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        )

        def b64(raw: bytes) -> str:
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

        head = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": idp.kid}).encode())
        body = b64(json.dumps(payload).encode())
        sig = hmac.new(public, f"{head}.{body}".encode(), hashlib.sha256).digest()
        return f"{head}.{body}.{b64(sig)}"
    if how == "alg none":
        return jwt.encode(payload, None, algorithm="none", headers={"kid": idp.kid})  # type: ignore[arg-type]
    return "not.a.jwt"


@pytest.mark.parametrize(
    "how",
    [
        "expired",
        "wrong audience",
        "wrong issuer",
        "another key, same kid",
        "unknown kid",
        "no expiry",
        "hs256 signed with the public key",
        "alg none",
        "garbage",
    ],
)
def test_a_token_that_fails_verification_is_a_401(api: TestClient, idp: Idp, how: str) -> None:
    provision(api, "alice@example.com", "compliance")
    assert lookup_case(api, idp.token()).status_code == 200  # the real one works
    resp = lookup_case(api, forged(idp, how))
    assert resp.status_code == 401, how
    assert code(resp) == "invalid_token"
    assert resp.headers["www-authenticate"] == 'Bearer error="invalid_token"'


def test_gateway_keys_still_work_beside_oidc(
    api: TestClient, idp: Idp, auth: dict[str, str]
) -> None:
    resp = api.post(
        "/v1/chat/completions", json={"model": "chaos/ok", "messages": MSGS}, headers=auth
    )
    assert resp.status_code == 200
    assert code(chat(api, "gw_" + "x" * 43)) == "invalid_api_key"


def test_without_oidc_a_jwt_is_just_a_bad_key(
    api: TestClient, idp: Idp, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(services, "oidc", lambda: None)
    resp = chat(api, idp.token())
    assert resp.status_code == 401 and code(resp) == "invalid_api_key"


def test_the_verifier_is_built_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(services, "_verifier", None)
    assert services.oidc() is None  # nothing configured
    monkeypatch.setattr(config.settings, "oidc_issuer", ISSUER)
    monkeypatch.setattr(config.settings, "oidc_audience", AUDIENCE)
    first = services.oidc()
    assert first is not None and first is services.oidc()  # one instance: it caches keys
    monkeypatch.setattr(config.settings, "oidc_audience", "another")
    assert services.oidc() is not first


# --- the provider's keys ----------------------------------------------------------------------


def test_keys_come_from_discovery_and_are_fetched_once(api: TestClient, idp: Idp) -> None:
    provision(api, "alice@example.com", "compliance")
    for _ in range(3):
        assert lookup_case(api, idp.token()).status_code == 200
    assert idp.fetches == [f"{ISSUER}.well-known/openid-configuration", f"{ISSUER}jwks/"]


def test_a_rotated_key_is_picked_up_and_unknown_kids_cannot_hammer_the_provider() -> None:
    idp, now = Idp(), [1000.0]
    verifier = Verifier(ISSUER, AUDIENCE, f"{ISSUER}jwks/", fetch=idp.fetch, clock=lambda: now[0])

    async def scenario() -> None:
        assert (await verifier.verify(idp.token()))["sub"] == "ext-alice"
        assert len(idp.fetches) == 1
        for _ in range(5):  # random key ids inside the minute: no fetches
            with pytest.raises(TokenError):
                await verifier.verify(idp.token(kid="nope"))
        assert len(idp.fetches) == 1

        idp.rotate("k2")
        with pytest.raises(TokenError):  # still inside the minute: the new key isn't known yet
            await verifier.verify(idp.token())
        now[0] += 61
        assert (await verifier.verify(idp.token()))["sub"] == "ext-alice"
        assert len(idp.fetches) == 2

    asyncio.run(scenario())


def test_when_the_provider_is_down_cached_keys_keep_working(api: TestClient, idp: Idp) -> None:
    provision(api, "alice@example.com", "compliance")
    assert lookup_case(api, idp.token()).status_code == 200
    idp.down = True
    assert lookup_case(api, idp.token()).status_code == 200  # no fetch needed


def test_with_no_keys_at_all_the_answer_is_503_not_401() -> None:
    idp = Idp()
    idp.down = True
    verifier = Verifier(ISSUER, AUDIENCE, fetch=idp.fetch)
    with pytest.raises(KeysUnavailable):
        asyncio.run(verifier.verify(idp.token()))


def test_keys_unavailable_is_a_503_at_the_api(api: TestClient, idp: Idp) -> None:
    idp.down = True
    resp = chat(api, idp.token())
    assert resp.status_code == 503 and code(resp) == "auth_unavailable"
