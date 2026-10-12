"""The SCIM 2.0 server (ADR 0029): what an identity provider sends, and what it gets back."""

import asyncio
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, main, services
from app.admin_auth import MemoryAuditStore
from app.config import Registry

TOKEN = "scim-test-token-0123456789abcdef"  # noqa: S105 (a throwaway test token)
SCIM = {"Authorization": f"Bearer {TOKEN}"}
ERROR = "urn:ietf:params:scim:api:messages:2.0:Error"
PATCH = "urn:ietf:params:scim:api:messages:2.0:PatchOp"


@pytest.fixture
def idp(registry: Registry, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A client that is the identity provider: it holds the SCIM token and no gateway key."""
    monkeypatch.setattr(config.settings, "scim_token", TOKEN)
    monkeypatch.setattr(services, "audit", MemoryAuditStore())
    return TestClient(main.app, headers=SCIM)


def user(idp: TestClient, name: str = "alice@example.com", **extra: Any) -> dict[str, Any]:
    resp = idp.post("/scim/v2/Users", json={"userName": name, "externalId": f"ext-{name}", **extra})
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def group(idp: TestClient, name: str, *members: str) -> dict[str, Any]:
    resp = idp.post(
        "/scim/v2/Groups", json={"displayName": name, "members": [{"value": m} for m in members]}
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def members(idp: TestClient, gid: str) -> set[str]:
    return {m["value"] for m in idp.get(f"/scim/v2/Groups/{gid}").json()["members"]}


def patch(idp: TestClient, path: str, *ops: dict[str, Any]) -> httpx.Response:
    return idp.patch(path, json={"schemas": [PATCH], "Operations": list(ops)})


def audited() -> list[tuple[str, str | None]]:
    store = services.audit
    assert isinstance(store, MemoryAuditStore)
    return [(e.action, e.target) for e in store.entries]


# --- the token ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}]
)
def test_a_missing_or_wrong_token_is_a_401_in_the_scim_error_schema(
    idp: TestClient, header: dict[str, str]
) -> None:
    anon = TestClient(main.app, headers=header)
    for method, path in (("GET", "/scim/v2/Users"), ("POST", "/scim/v2/Groups")):
        resp = anon.request(method, path, json={})
        assert resp.status_code == 401
        assert resp.json()["schemas"] == [ERROR] and resp.json()["status"] == "401"
        assert resp.headers["content-type"].startswith("application/scim+json")


def test_scim_is_off_until_a_token_is_configured(
    idp: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config.settings, "scim_token", "")
    assert idp.get("/scim/v2/Users").status_code == 401
    assert (
        TestClient(main.app, headers={"Authorization": "Bearer "}).get("/scim/v2/Users").status_code
        == 401
    )  # an empty token never matches an empty setting


def test_a_gateway_key_is_not_a_scim_token(idp: TestClient, auth: dict[str, str]) -> None:
    assert TestClient(main.app, headers=auth).get("/scim/v2/Users").status_code == 401


# --- users ----------------------------------------------------------------------------------


def test_create_then_read_a_user(idp: TestClient) -> None:
    created = user(
        idp,
        displayName="Alice A",
        emails=[{"value": "other@example.com"}, {"value": "alice@example.com", "primary": True}],
    )
    assert created["schemas"] == ["urn:ietf:params:scim:schemas:core:2.0:User"]
    assert created["userName"] == "alice@example.com" and created["active"] is True
    assert created["externalId"] == "ext-alice@example.com"
    assert created["emails"] == [{"value": "alice@example.com", "primary": True}]
    assert created["meta"]["resourceType"] == "User"
    assert created["meta"]["location"].endswith(f"/scim/v2/Users/{created['id']}")
    assert idp.get(f"/scim/v2/Users/{created['id']}").json() == created
    assert audited() == [("scim.user.create", created["id"])]


def test_a_duplicate_username_is_a_409_uniqueness(idp: TestClient) -> None:
    user(idp)
    resp = idp.post("/scim/v2/Users", json={"userName": "alice@example.com"})
    assert resp.status_code == 409
    assert resp.json()["scimType"] == "uniqueness" and resp.json()["schemas"] == [ERROR]


def test_providers_look_a_user_up_by_filter_before_creating(idp: TestClient) -> None:
    alice, _bob = user(idp), user(idp, "bob@example.com")
    found = idp.get('/scim/v2/Users?filter=userName eq "alice@example.com"').json()
    assert found["totalResults"] == 1 and found["Resources"][0]["id"] == alice["id"]
    assert found["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:ListResponse"]
    by_ext = idp.get('/scim/v2/Users?filter=externalId eq "ext-bob@example.com"').json()
    assert [u["userName"] for u in by_ext["Resources"]] == ["bob@example.com"]
    none = idp.get('/scim/v2/Users?filter=userName eq "nobody@example.com"').json()
    assert none["totalResults"] == 0 and none["Resources"] == []


def test_an_unsupported_filter_is_refused_not_ignored(idp: TestClient) -> None:
    """Ignoring a filter would answer "everyone matches"."""
    user(idp)
    for bad in ('userName co "ali"', 'title eq "x"', "nonsense"):
        resp = idp.get("/scim/v2/Users", params={"filter": bad})
        assert resp.status_code == 400 and resp.json()["scimType"] == "invalidFilter"


def test_lists_are_paged(idp: TestClient) -> None:
    for i in range(5):
        user(idp, f"user{i}@example.com")
    page = idp.get("/scim/v2/Users?startIndex=2&count=2").json()
    assert (page["totalResults"], page["startIndex"], page["itemsPerPage"]) == (5, 2, 2)
    assert [u["userName"] for u in page["Resources"]] == ["user1@example.com", "user2@example.com"]
    assert idp.get("/scim/v2/Users?count=abc").status_code == 400


@pytest.mark.parametrize(
    "operation",
    [
        {"op": "replace", "value": {"active": False}},  # Okta
        {"op": "Replace", "path": "active", "value": False},  # Entra: capitalised op
        {"op": "replace", "path": "active", "value": "False"},  # …and a string
        {"op": "remove", "path": "active"},
    ],
)
def test_every_dialect_of_deactivation_deactivates(
    idp: TestClient, operation: dict[str, Any]
) -> None:
    alice = user(idp)
    resp = patch(idp, f"/scim/v2/Users/{alice['id']}", operation)
    assert resp.status_code == 200 and resp.json()["active"] is False
    assert idp.get(f"/scim/v2/Users/{alice['id']}").json()["active"] is False
    assert audited()[-1] == ("scim.user.deactivate", alice["id"])


def test_put_replaces_a_user_and_can_reactivate(idp: TestClient) -> None:
    alice = user(idp, active=False)
    assert alice["active"] is False
    resp = idp.put(
        f"/scim/v2/Users/{alice['id']}",
        json={"userName": "alice.a@example.com", "active": True, "name": {"formatted": "Alice A"}},
    )
    body = resp.json()
    assert (body["userName"], body["active"], body["displayName"]) == (
        "alice.a@example.com",
        True,
        "Alice A",
    )
    assert audited()[-1][0] == "scim.user.activate"


def test_patch_can_rename_and_ignores_attributes_it_does_not_store(idp: TestClient) -> None:
    alice = user(idp)
    resp = patch(
        idp,
        f"/scim/v2/Users/{alice['id']}",
        {"op": "replace", "value": {"userName": "al@example.com", "locale": "en-GB"}},
        {"op": "add", "path": "name.givenName", "value": "Alice"},
    )
    assert resp.status_code == 200 and resp.json()["userName"] == "al@example.com"


def test_delete_removes_the_user_and_their_memberships(idp: TestClient) -> None:
    alice, bob = user(idp), user(idp, "bob@example.com")
    g = group(idp, "compliance", alice["id"], bob["id"])
    assert idp.delete(f"/scim/v2/Users/{alice['id']}").status_code == 204
    assert idp.get(f"/scim/v2/Users/{alice['id']}").status_code == 404
    assert members(idp, g["id"]) == {bob["id"]}
    assert idp.delete(f"/scim/v2/Users/{alice['id']}").status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"userName": ""},
        {"userName": 7},
        {"userName": "a", "active": "maybe"},
        ["not", "object"],
    ],
)
def test_a_bad_user_is_a_400_in_the_scim_error_schema(idp: TestClient, body: Any) -> None:
    resp = idp.post("/scim/v2/Users", json=body)
    assert resp.status_code == 400 and resp.json()["schemas"] == [ERROR]
    assert idp.post("/scim/v2/Users", content=b"{not json").status_code == 400


def test_unknown_ids_are_404s(idp: TestClient) -> None:
    for path in ("/scim/v2/Users/nope", "/scim/v2/Groups/00000000-0000-0000-0000-000000000000"):
        resp = idp.get(path)
        assert resp.status_code == 404 and resp.json()["status"] == "404"
    assert patch(idp, "/scim/v2/Users/nope", {"op": "replace", "value": {}}).status_code == 404


# --- groups ---------------------------------------------------------------------------------


def test_create_a_group_with_members(idp: TestClient) -> None:
    alice = user(idp)
    g = group(idp, "compliance", alice["id"], "an-id-the-directory-does-not-know")
    assert g["displayName"] == "compliance" and g["meta"]["resourceType"] == "Group"
    assert members(idp, g["id"]) == {alice["id"]}  # unknown ids grant nothing
    found = idp.get('/scim/v2/Groups?filter=displayName eq "compliance"').json()
    assert found["totalResults"] == 1 and found["Resources"][0]["id"] == g["id"]
    assert idp.post("/scim/v2/Groups", json={"displayName": "compliance"}).status_code == 409


def test_members_are_added_and_removed_in_every_dialect(idp: TestClient) -> None:
    a, b, c = (user(idp, f"{n}@example.com")["id"] for n in "abc")
    g = group(idp, "eng-backend")["id"]
    path = f"/scim/v2/Groups/{g}"

    patch(idp, path, {"op": "add", "path": "members", "value": [{"value": a}, {"value": b}]})
    assert members(idp, g) == {a, b}
    patch(idp, path, {"op": "add", "value": {"members": [{"value": c}]}})  # no path
    assert members(idp, g) == {a, b, c}

    # Okta: the member to remove is in the path filter, with no value.
    patch(idp, path, {"op": "remove", "path": f'members[value eq "{a}"]'})
    assert members(idp, g) == {b, c}
    # authentik and Entra: path `members`, the ids in the value.
    patch(idp, path, {"op": "remove", "path": "members", "value": [{"value": b}]})
    assert members(idp, g) == {c}

    patch(idp, path, {"op": "replace", "path": "members", "value": [{"value": a}]})
    assert members(idp, g) == {a}
    patch(idp, path, {"op": "remove", "path": "members"})  # no value: everyone
    assert members(idp, g) == set()


def test_adding_a_member_twice_keeps_one(idp: TestClient) -> None:
    a = user(idp)["id"]
    g = group(idp, "eng-backend", a)["id"]
    patch(idp, f"/scim/v2/Groups/{g}", {"op": "add", "path": "members", "value": [{"value": a}]})
    assert idp.get(f"/scim/v2/Groups/{g}").json()["members"] == [{"value": a}]


def test_a_group_can_be_renamed_replaced_and_deleted(idp: TestClient) -> None:
    a, b = user(idp)["id"], user(idp, "bob@example.com")["id"]
    g = group(idp, "desk", a)["id"]
    renamed = patch(
        idp, f"/scim/v2/Groups/{g}", {"op": "replace", "value": {"displayName": "trading-desk"}}
    )
    assert renamed.json()["displayName"] == "trading-desk" and members(idp, g) == {a}
    put = idp.put(
        f"/scim/v2/Groups/{g}", json={"displayName": "trading-desk", "members": [{"value": b}]}
    )
    assert put.status_code == 200 and members(idp, g) == {b}
    assert idp.delete(f"/scim/v2/Groups/{g}").status_code == 204
    assert idp.get(f"/scim/v2/Groups/{g}").status_code == 404
    assert [a for a, _ in audited()][-3:] == [
        "scim.group.update",
        "scim.group.update",
        "scim.group.delete",
    ]


def test_a_patch_without_operations_is_a_400(idp: TestClient) -> None:
    g = group(idp, "desk")["id"]
    assert idp.patch(f"/scim/v2/Groups/{g}", json={}).status_code == 400
    assert patch(idp, f"/scim/v2/Groups/{g}", {"op": "explode"}).status_code == 400


def test_discovery_endpoints(idp: TestClient) -> None:
    cfg = idp.get("/scim/v2/ServiceProviderConfig").json()
    assert cfg["patch"]["supported"] is True and cfg["bulk"]["supported"] is False
    kinds = idp.get("/scim/v2/ResourceTypes").json()
    assert [r["name"] for r in kinds["Resources"]] == ["User", "Group"]


def test_a_change_drops_cached_identities(idp: TestClient) -> None:
    """Deprovisioning must not wait for a cache to expire."""
    alice = user(idp)
    g = group(idp, "compliance", alice["id"])

    def groups() -> tuple[str, ...]:
        identity = asyncio.run(services.identities.lookup("user_name", "alice@example.com"))
        assert identity is not None
        return identity.groups

    assert groups() == ("compliance",)
    patch(
        idp,
        f"/scim/v2/Groups/{g['id']}",
        {"op": "remove", "path": f'members[value eq "{alice["id"]}"]'},
    )
    assert groups() == ()
