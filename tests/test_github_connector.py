"""The GitHub connector (ADR 0031): GitHub App credentials and read-only pull requests.
GitHub is mocked with respx; the app key is generated per test."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app import config, connectors
from app.connectors import BadArguments, ConnectorError, ConnectorNotConfigured, GitHub
from tests.test_governance import STRICT, decided, team_key, tool

API = "https://github.test"
TOKENS = f"{API}/app/installations/42/access_tokens"
NOW = 1_800_000_000.0  # the connector's clock in these tests
PULL = {
    "number": 7,
    "title": "Add retries",
    "state": "open",
    "draft": False,
    "user": {"login": "octocat", "id": 1},
    "created_at": "2026-10-01T10:00:00Z",
    "updated_at": "2026-10-02T10:00:00Z",
    "html_url": "https://github.com/acme/api/pull/7",
    "node_id": "left out of the result",
}


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


@pytest.fixture(scope="module")
def app_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )


def put(path: Path, content: bytes | None) -> None:
    """Replace the key file (None: remove it)."""
    if content is None:
        path.unlink()
    else:
        path.write_bytes(content)


@pytest.fixture
def key_file(tmp_path: Path, app_key: rsa.RSAPrivateKey, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The app's settings, with its private key in a file."""
    path = tmp_path / "app.pem"
    put(path, pem(app_key))
    monkeypatch.setattr(config.settings, "github_api_url", API)
    monkeypatch.setattr(config.settings, "github_app_id", "1234")
    monkeypatch.setattr(config.settings, "github_app_installation_id", "42")
    monkeypatch.setattr(config.settings, "github_app_private_key_file", str(path))
    return path


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def github(key_file: Path, clock: Clock) -> GitHub:
    return GitHub(clock=clock)


def issued(token: str = "ghs_first", lifetime: float = 3600) -> httpx.Response:  # noqa: S107
    expires = datetime.fromtimestamp(NOW + lifetime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return httpx.Response(201, json={"token": token, "expires_at": expires})


# --- the credential --------------------------------------------------------------------


@respx.mock
async def test_the_app_jwt_is_short_lived_and_the_token_is_scoped_down(
    github: GitHub, app_key: rsa.RSAPrivateKey
) -> None:
    exchange = respx.post(TOKENS).mock(return_value=issued())
    assert await github.credential() == "ghs_first"
    sent = exchange.calls.last.request
    claims = jwt.decode(
        sent.headers["authorization"].removeprefix("Bearer "),
        app_key.public_key(),
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )
    assert claims["iss"] == "1234"
    assert claims["exp"] - claims["iat"] <= 600  # GitHub's maximum
    assert claims["iat"] < NOW < claims["exp"]
    # Less than the app was granted: only what this connector reads.
    assert json.loads(sent.content) == {"permissions": {"pull_requests": "read"}}


@respx.mock
async def test_the_token_is_reused_until_shortly_before_it_expires(
    github: GitHub, clock: Clock
) -> None:
    exchange = respx.post(TOKENS).mock(side_effect=[issued("ghs_first"), issued("ghs_second")])
    assert await github.credential() == "ghs_first"
    clock.now = NOW + 3600 - 301  # still more than five minutes left
    assert await github.credential() == "ghs_first"
    assert exchange.call_count == 1
    clock.now = NOW + 3600 - 299  # too close to expiry to hand out
    assert await github.credential() == "ghs_second"
    assert exchange.call_count == 2


@respx.mock
async def test_concurrent_calls_share_one_exchange(github: GitHub) -> None:
    exchange = respx.post(TOKENS).mock(return_value=issued())
    tokens = await asyncio.gather(*(github.credential() for _ in range(10)))
    assert set(tokens) == {"ghs_first"} and exchange.call_count == 1


@respx.mock
async def test_a_rotated_key_is_picked_up_at_the_next_exchange(
    github: GitHub, clock: Clock, key_file: Path
) -> None:
    exchange = respx.post(TOKENS).mock(side_effect=[issued("ghs_first"), issued("ghs_second")])
    await github.credential()
    rotated = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    put(key_file, pem(rotated))
    clock.now = NOW + 3600
    await github.credential()
    signed = exchange.calls.last.request.headers["authorization"].removeprefix("Bearer ")
    jwt.decode(
        signed,
        rotated.public_key(),
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )


@pytest.mark.parametrize(
    "setting", ["github_app_id", "github_app_installation_id", "github_app_private_key_file"]
)
@respx.mock
async def test_a_missing_setting_means_not_configured(
    github: GitHub, monkeypatch: pytest.MonkeyPatch, setting: str
) -> None:
    monkeypatch.setattr(config.settings, setting, "")
    with pytest.raises(ConnectorNotConfigured):
        await github.credential()
    assert not respx.calls  # nothing was sent anywhere


@pytest.mark.parametrize("content", [None, "", "not a key"])
@respx.mock
async def test_a_missing_or_unusable_key_file_means_not_configured(
    github: GitHub, key_file: Path, content: str | None
) -> None:
    put(key_file, None if content is None else content.encode())
    with pytest.raises(ConnectorNotConfigured) as err:
        await github.credential()
    assert str(key_file) not in str(err.value) and not respx.calls


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(401, json={"message": "A JSON web token could not be decoded"}),
        httpx.Response(404, json={"message": "Not Found"}),
        httpx.Response(201, json={"nothing": "useful"}),
        httpx.Response(201, text="<html>"),
        httpx.ConnectError("boom"),
    ],
)
@respx.mock
async def test_a_failed_exchange_is_a_connector_error_and_nothing_is_cached(
    github: GitHub, answer: Any
) -> None:
    respx.post(TOKENS).mock(side_effect=[answer, issued()])
    with pytest.raises(ConnectorError) as err:
        await github.credential()
    assert "token" not in str(err.value).lower()
    assert await github.credential() == "ghs_first"  # the next call tries again


# --- the tools -------------------------------------------------------------------------


@respx.mock
async def test_listing_pull_requests_returns_a_small_summary(github: GitHub) -> None:
    listing = respx.get(f"{API}/repos/acme/api/pulls").mock(
        return_value=httpx.Response(200, json=[PULL])
    )
    out = await github.call("list_pull_requests", {"repo": "acme/api", "limit": 5}, "ghs_first")
    assert out == {
        "repo": "acme/api",
        "pull_requests": [
            {
                "number": 7,
                "title": "Add retries",
                "state": "open",
                "draft": False,
                "author": "octocat",
                "created_at": "2026-10-01T10:00:00Z",
                "updated_at": "2026-10-02T10:00:00Z",
                "url": "https://github.com/acme/api/pull/7",
            }
        ],
    }
    sent = listing.calls.last.request
    assert sent.headers["authorization"] == "Bearer ghs_first"
    assert dict(sent.url.params) == {"state": "open", "per_page": "5"}


@respx.mock
async def test_one_pull_request_with_a_bounded_description(github: GitHub) -> None:
    full = {
        **PULL,
        "merged": True,
        "base": {"ref": "main", "sha": "a"},
        "head": {"ref": "retries", "sha": "b"},
        "changed_files": 3,
        "additions": 40,
        "deletions": 2,
        "body": "x" * 5000,
    }
    respx.get(f"{API}/repos/acme/api/pulls/7").mock(return_value=httpx.Response(200, json=full))
    out = await github.call("get_pull_request", {"repo": "acme/api", "number": 7}, "t")
    assert (out["merged"], out["base"], out["head"]) == (True, "main", "retries")
    assert (out["changed_files"], out["additions"], out["deletions"]) == (3, 40, 2)
    assert len(out["body"]) == 4000 and out["body_truncated"] is True


@pytest.mark.parametrize(
    ("tool_name", "args"),
    [
        ("list_pull_requests", {}),
        ("list_pull_requests", {"repo": 5}),
        ("list_pull_requests", {"repo": "acme"}),
        ("list_pull_requests", {"repo": "acme/api/../../user"}),
        ("list_pull_requests", {"repo": "acme/.."}),
        ("list_pull_requests", {"repo": "acme/api?x=1"}),
        ("list_pull_requests", {"repo": "../orgs/acme"}),
        ("list_pull_requests", {"repo": "acme/api", "state": "merged"}),
        ("list_pull_requests", {"repo": "acme/api", "limit": 500}),
        ("list_pull_requests", {"repo": "acme/api", "limit": True}),
        ("get_pull_request", {"repo": "acme/api"}),
        ("get_pull_request", {"repo": "acme/api", "number": "7/files"}),
        ("get_pull_request", {"repo": "acme/api", "number": 0}),
    ],
)
@respx.mock
async def test_bad_arguments_never_reach_github(
    github: GitHub, tool_name: str, args: dict[str, Any]
) -> None:
    with pytest.raises(BadArguments):
        await github.call(tool_name, args, "ghs_first")
    assert not respx.calls


@pytest.mark.parametrize(
    ("answer", "says"),
    [
        (httpx.Response(404, json={"message": "Not Found"}), "not found"),
        (httpx.Response(403, json={"message": "rate limit"}), "refused the request"),
        (httpx.Response(500, text="oops"), "HTTP 500"),
        (httpx.Response(200, text="<html>"), "unexpected"),
        (httpx.Response(200, json={"not": "a list"}), "unexpected"),
        (httpx.ReadTimeout("slow"), "could not be reached"),
    ],
)
@respx.mock
async def test_github_failures_are_connector_errors_without_the_token(
    github: GitHub, answer: Any, says: str
) -> None:
    respx.get(f"{API}/repos/acme/api/pulls").mock(side_effect=[answer])
    with pytest.raises(ConnectorError) as err:
        await github.call("list_pull_requests", {"repo": "acme/api"}, "ghs_secret_value")
    assert says in str(err.value) and "ghs_secret_value" not in str(err.value)


@respx.mock
async def test_a_token_github_no_longer_accepts_is_dropped(github: GitHub) -> None:
    exchange = respx.post(TOKENS).mock(side_effect=[issued("ghs_first"), issued("ghs_second")])
    respx.get(f"{API}/repos/acme/api/pulls").mock(return_value=httpx.Response(401))
    token = await github.credential()
    with pytest.raises(ConnectorError):
        await github.call("list_pull_requests", {"repo": "acme/api"}, token)
    assert await github.credential() == "ghs_second"  # not the revoked one
    assert exchange.call_count == 2


# --- through the gateway ---------------------------------------------------------------

GITHUB_RULE = {"min_tier": "standard", "max_data_class": "internal", "allowed_roles": ["engineer"]}


@pytest.fixture
def onboarded(key_file: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    policy = STRICT.model_validate({**STRICT.model_dump(), "connectors": {"github": GITHUB_RULE}})
    monkeypatch.setattr(config, "access", policy)
    monkeypatch.setitem(connectors.REGISTRY, "github", GitHub(clock=clock))


@respx.mock
def test_an_engineer_reads_pull_requests_through_the_gateway(
    client: TestClient, onboarded: None
) -> None:
    respx.post(TOKENS).mock(return_value=issued())
    respx.get(f"{API}/repos/acme/api/pulls").mock(return_value=httpx.Response(200, json=[PULL]))
    resp = tool(client, team_key("eng"), "github", "list_pull_requests", repo="acme/api")
    assert resp.status_code == 200
    assert resp.json()["result"]["pull_requests"][0]["number"] == 7
    assert "ghs_first" not in resp.text
    (d,) = decided()
    assert (d.decision, d.action) == ("allow", "tool:github.list_pull_requests")
    assert "acme" not in str(d.row()) and "ghs_first" not in str(d.row())


@respx.mock
def test_other_roles_and_more_sensitive_data_are_refused_before_any_credential_is_minted(
    client: TestClient, onboarded: None
) -> None:
    wrong_role = tool(client, team_key("compliance"), "github", "list_pull_requests", repo="a/b")
    assert wrong_role.json()["error"]["code"] == "role_not_allowed"
    too_sensitive = tool(
        client, team_key("eng"), "github", "list_pull_requests", "confidential", repo="a/b"
    )
    assert too_sensitive.status_code == 403
    assert [d.decision for d in decided()] == ["deny", "deny"]
    assert not respx.calls


@respx.mock
def test_an_unconfigured_connector_is_a_503(
    client: TestClient, onboarded: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config.settings, "github_app_id", "")
    resp = tool(client, team_key("eng"), "github", "list_pull_requests", repo="acme/api")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "connector_not_configured"


@respx.mock
def test_bad_arguments_are_the_callers_error(client: TestClient, onboarded: None) -> None:
    respx.post(TOKENS).mock(return_value=issued())
    resp = tool(client, team_key("eng"), "github", "list_pull_requests", repo="../user")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_tool_arguments"


def test_the_shipped_policy_onboards_github_for_engineers_only() -> None:
    rule = config.load_access(Path("config")).connectors["github"]
    assert rule.allowed_roles == ["engineer"] and rule.max_data_class == "internal"
