"""SCIM 2.0 server: the identity provider provisions users and groups here (ADR 0029).

The subset identity providers actually call (RFC 7643/7644): Users and Groups with
create, read, list with an `eq` filter, replace, PATCH and delete. Three PATCH dialects
are accepted, because providers differ:

    {"op": "remove", "path": "members[value eq \\"<id>\\"]"}            Okta
    {"op": "remove", "path": "members", "value": [{"value": "<id>"}]}    authentik, Entra
    {"op": "replace", "value": {"active": false}}                        no path

The identity provider authenticates with one bearer token (`SCIM_TOKEN`). Errors use the
SCIM error schema, so the provider shows a useful message instead of "unknown error".
"""

from __future__ import annotations

import hmac
import re
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app import admin_auth, config, services
from app.directory import Conflict, Group, User, UserAttr

router = APIRouter(prefix="/scim/v2", include_in_schema=False)

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
MEDIA = "application/scim+json"
MAX_PAGE = 200
OPERATOR = "scim"  # who made the change, in the admin audit log

_FILTER = re.compile(r'^\s*(\w+)\s+eq\s+"((?:[^"\\]|\\.)*)"\s*$', re.IGNORECASE)
_MEMBER_PATH = re.compile(r'^members\[\s*value\s+eq\s+"([^"]+)"\s*\]$', re.IGNORECASE)
_USER_FILTERS: dict[str, UserAttr] = {"username": "user_name", "externalid": "external_id"}


class ScimError(Exception):
    def __init__(self, status: int, detail: str, scim_type: str | None = None) -> None:
        self.status, self.detail, self.scim_type = status, detail, scim_type


def error(status: int, detail: str, scim_type: str | None = None) -> JSONResponse:
    body: dict[str, Any] = {"schemas": [ERROR_SCHEMA], "status": str(status), "detail": detail}
    if scim_type:
        body["scimType"] = scim_type
    return JSONResponse(body, status_code=status, media_type=MEDIA)


def _ok(body: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status, media_type=MEDIA)


def _authorize(request: Request) -> None:
    token = config.settings.scim_token
    scheme, _, presented = request.headers.get("authorization", "").partition(" ")
    if not token:
        raise ScimError(401, "SCIM provisioning is not enabled on this gateway")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        presented.strip().encode(), token.encode()
    ):
        raise ScimError(401, "invalid SCIM token")


async def _body(request: Request) -> dict[str, Any]:
    try:
        raw = await request.json()
    except ValueError, RecursionError:
        raise ScimError(400, "the body is not valid JSON", "invalidSyntax") from None
    if not isinstance(raw, dict):
        raise ScimError(400, "the body must be a JSON object", "invalidSyntax")
    return raw


def _stamp(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")


def _location(request: Request, kind: str, rid: str) -> str:
    return f"{str(request.base_url).rstrip('/')}/scim/v2/{kind}/{rid}"


def user_resource(user: User, request: Request) -> dict[str, Any]:
    out: dict[str, Any] = {
        "schemas": [USER_SCHEMA],
        "id": user.id,
        "userName": user.user_name,
        "active": user.active,
        "meta": {
            "resourceType": "User",
            "created": _stamp(user.created_at),
            "lastModified": _stamp(user.updated_at),
            "location": _location(request, "Users", user.id),
        },
    }
    if user.external_id is not None:
        out["externalId"] = user.external_id
    if user.display_name is not None:
        out["displayName"] = user.display_name
    if user.email is not None:
        out["emails"] = [{"value": user.email, "primary": True}]
    return out


def group_resource(group: Group, request: Request) -> dict[str, Any]:
    out: dict[str, Any] = {
        "schemas": [GROUP_SCHEMA],
        "id": group.id,
        "displayName": group.display_name,
        "members": [{"value": m} for m in group.members],
        "meta": {
            "resourceType": "Group",
            "created": _stamp(group.created_at),
            "lastModified": _stamp(group.updated_at),
            "location": _location(request, "Groups", group.id),
        },
    }
    if group.external_id is not None:
        out["externalId"] = group.external_id
    return out


def _listing(total: int, start: int, resources: list[dict[str, Any]]) -> JSONResponse:
    return _ok(
        {
            "schemas": [LIST_SCHEMA],
            "totalResults": total,
            "startIndex": start,
            "itemsPerPage": len(resources),
            "Resources": resources,
        }
    )


def _paging(request: Request) -> tuple[int, int]:
    try:
        start = max(int(request.query_params.get("startIndex", 1)), 1)
        count = min(max(int(request.query_params.get("count", 100)), 0), MAX_PAGE)
    except ValueError:
        raise ScimError(400, "startIndex and count must be integers", "invalidValue") from None
    return start, count


def _eq_filter(request: Request) -> tuple[str, str] | None:
    """`attribute eq "value"`, the one filter providers send (to find a resource before
    creating it). Anything else is refused rather than silently ignored: ignoring a
    filter would answer "everyone matches"."""
    raw = request.query_params.get("filter")
    if not raw:
        return None
    m = _FILTER.match(raw)
    if not m:
        raise ScimError(
            400, 'only filters of the form `attribute eq "value"` are supported', "invalidFilter"
        )
    return m.group(1).lower(), m.group(2).replace('\\"', '"')


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):  # some providers send strings
        return value.lower() == "true"
    raise ScimError(400, "`active` must be true or false", "invalidValue")


def _text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ScimError(
            400,
            f"`{name}` must be a non-empty string of at most {limit} characters",
            "invalidValue",
        )
    return value.strip()


def _email(body: dict[str, Any]) -> str | None:
    emails = body.get("emails")
    if not isinstance(emails, list) or not emails:
        return None
    chosen = next((e for e in emails if isinstance(e, dict) and e.get("primary")), emails[0])
    value = chosen.get("value") if isinstance(chosen, dict) else None
    return value[:320] if isinstance(value, str) and value else None


def _display(body: dict[str, Any]) -> str | None:
    name = body.get("displayName")
    if not name and isinstance(body.get("name"), dict):
        n = body["name"]
        name = n.get("formatted") or " ".join(
            str(p) for p in (n.get("givenName"), n.get("familyName")) if p
        )
    return str(name)[:255] if name else None


def _user_fields(body: dict[str, Any]) -> dict[str, Any]:
    external = body.get("externalId")
    return {
        "user_name": _text(body.get("userName"), "userName", 320),
        "external_id": str(external)[:255] if external else None,
        "active": _bool(body.get("active", True)),
        "display_name": _display(body),
        "email": _email(body),
    }


def _member_ids(value: Any) -> list[str]:
    """Member ids from `[{"value": id}, …]`, one `{"value": id}`, or a bare id."""
    items = value if isinstance(value, list) else [value]
    ids = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("value"), str):
            ids.append(item["value"])
        elif isinstance(item, str):
            ids.append(item)
    return ids


def _operations(body: dict[str, Any]) -> list[tuple[str, str, Any]]:
    ops = body.get("Operations")
    if not isinstance(ops, list) or not ops:
        raise ScimError(400, "a PATCH needs `Operations`", "invalidSyntax")
    out = []
    for op in ops:
        if not isinstance(op, dict) or str(op.get("op", "")).lower() not in (
            "add",
            "remove",
            "replace",
        ):
            raise ScimError(
                400, "each operation needs `op`: add, remove or replace", "invalidSyntax"
            )
        out.append((str(op["op"]).lower(), str(op.get("path") or ""), op.get("value")))
    return out


async def _changed(action: str, target: str, **detail: Any) -> None:
    """Every provisioning change is an access change: drop cached identities everywhere,
    and record it in the admin audit log."""
    services.identities.invalidate()
    await services.announce_key_change()
    await admin_auth.record(OPERATOR, action, target, **detail)


# --- discovery ----------------------------------------------------------------------------


@router.get("/ServiceProviderConfig")
async def service_provider_config(request: Request) -> JSONResponse:
    _authorize(request)
    off = {"supported": False}
    return _ok(
        {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
            "patch": {"supported": True},
            "bulk": {**off, "maxOperations": 0, "maxPayloadSize": 0},
            "filter": {"supported": True, "maxResults": MAX_PAGE},
            "changePassword": off,
            "sort": off,
            "etag": off,
            "authenticationSchemes": [
                {
                    "type": "oauthbearertoken",
                    "name": "Bearer token",
                    "description": "A static bearer token set on the gateway (SCIM_TOKEN).",
                }
            ],
        }
    )


@router.get("/ResourceTypes")
async def resource_types(request: Request) -> JSONResponse:
    _authorize(request)
    kinds = [("User", "/Users", USER_SCHEMA), ("Group", "/Groups", GROUP_SCHEMA)]
    return _listing(
        len(kinds),
        1,
        [
            {
                "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
                "id": name,
                "name": name,
                "endpoint": endpoint,
                "schema": schema,
            }
            for name, endpoint, schema in kinds
        ],
    )


# --- users --------------------------------------------------------------------------------


@router.post("/Users")
async def create_user(request: Request) -> JSONResponse:
    _authorize(request)
    fields = _user_fields(await _body(request))
    try:
        user = await services.directory.create_user(User(id=str(uuid.uuid4()), **fields))
    except Conflict:
        raise ScimError(
            409, f"userName {fields['user_name']!r} already exists", "uniqueness"
        ) from None
    await _changed("scim.user.create", user.id, user_name=user.user_name, active=user.active)
    return _ok(user_resource(user, request), 201)


@router.get("/Users")
async def list_users(request: Request) -> JSONResponse:
    _authorize(request)
    start, count = _paging(request)
    attr, value = None, None
    if (found := _eq_filter(request)) is not None:
        if found[0] not in _USER_FILTERS:
            raise ScimError(400, "users can be filtered by userName or externalId", "invalidFilter")
        attr, value = _USER_FILTERS[found[0]], found[1]
    total, users = await services.directory.list_users(attr, value, start, count)
    return _listing(total, start, [user_resource(u, request) for u in users])


@router.get("/Users/{user_id}")
async def get_user(user_id: str, request: Request) -> JSONResponse:
    _authorize(request)
    user = await services.directory.get_user(user_id)
    if user is None:
        raise ScimError(404, f"User {user_id[:64]} not found")
    return _ok(user_resource(user, request))


@router.put("/Users/{user_id}")
async def replace_user(user_id: str, request: Request) -> JSONResponse:
    _authorize(request)
    return await _update_user(user_id, _user_fields(await _body(request)), request, "replace")


@router.patch("/Users/{user_id}")
async def patch_user(user_id: str, request: Request) -> JSONResponse:
    _authorize(request)
    changes: dict[str, Any] = {}
    for op, path, value in _operations(await _body(request)):
        # With no path the value is an object of attributes; with one, it is that attribute.
        attrs = value if not path and isinstance(value, dict) else {path: value}
        for name, v in attrs.items():
            key = str(name).lower()
            if key == "active":
                changes["active"] = False if op == "remove" else _bool(v)
            elif key == "username":
                changes["user_name"] = _text(v, "userName", 320)
            elif key == "externalid":
                changes["external_id"] = str(v)[:255] if v else None
            elif key == "displayname" or key == "name.formatted":
                changes["display_name"] = str(v)[:255] if v else None
            elif key == "emails" or key.startswith("emails["):
                changes["email"] = _email({"emails": v if isinstance(v, list) else [{"value": v}]})
            # Other attributes (name parts, locale, …) aren't stored: accepted and ignored.
    return await _update_user(user_id, changes, request, "patch")


async def _update_user(
    user_id: str, changes: dict[str, Any], request: Request, how: str
) -> JSONResponse:
    before = await services.directory.get_user(user_id)
    if before is None:
        raise ScimError(404, f"User {user_id[:64]} not found")
    try:
        user = await services.directory.update_user(user_id, changes) if changes else before
    except Conflict:
        raise ScimError(409, "that userName already exists", "uniqueness") from None
    if user is None:
        raise ScimError(404, f"User {user_id[:64]} not found")
    action = "scim.user.update"
    if before.active != user.active:
        action = "scim.user.activate" if user.active else "scim.user.deactivate"
    await _changed(action, user.id, user_name=user.user_name, active=user.active, how=how)
    return _ok(user_resource(user, request))


@router.delete("/Users/{user_id}")
async def delete_user(user_id: str, request: Request) -> Response:
    _authorize(request)
    if not await services.directory.delete_user(user_id):
        raise ScimError(404, f"User {user_id[:64]} not found")
    await _changed("scim.user.delete", user_id[:64])
    return Response(status_code=204)


# --- groups -------------------------------------------------------------------------------


@router.post("/Groups")
async def create_group(request: Request) -> JSONResponse:
    _authorize(request)
    body = await _body(request)
    name = _text(body.get("displayName"), "displayName", 255)
    external = body.get("externalId")
    try:
        group = await services.directory.create_group(
            Group(
                id=str(uuid.uuid4()),
                display_name=name,
                external_id=str(external)[:255] if external else None,
                members=tuple(_member_ids(body.get("members") or [])),
            )
        )
    except Conflict:
        raise ScimError(409, f"a group named {name!r} already exists", "uniqueness") from None
    await _changed("scim.group.create", group.id, name=name, members=len(group.members))
    return _ok(group_resource(group, request), 201)


@router.get("/Groups")
async def list_groups(request: Request) -> JSONResponse:
    _authorize(request)
    start, count = _paging(request)
    name = None
    if (found := _eq_filter(request)) is not None:
        if found[0] != "displayname":
            raise ScimError(400, "groups can be filtered by displayName", "invalidFilter")
        name = found[1]
    total, groups = await services.directory.list_groups(name, start, count)
    return _listing(total, start, [group_resource(g, request) for g in groups])


@router.get("/Groups/{group_id}")
async def get_group(group_id: str, request: Request) -> JSONResponse:
    _authorize(request)
    group = await services.directory.get_group(group_id)
    if group is None:
        raise ScimError(404, f"Group {group_id[:64]} not found")
    return _ok(group_resource(group, request))


@router.put("/Groups/{group_id}")
async def replace_group(group_id: str, request: Request) -> JSONResponse:
    _authorize(request)
    body = await _body(request)
    external = body.get("externalId")
    return await _update_group(
        group_id,
        request,
        display_name=_text(body.get("displayName"), "displayName", 255),
        external_id=str(external)[:255] if external else None,
        members=_member_ids(body.get("members") or []),
    )


@router.patch("/Groups/{group_id}")
async def patch_group(group_id: str, request: Request) -> JSONResponse:
    _authorize(request)
    name: str | None = None
    members: list[str] | None = None
    add: list[str] = []
    remove: list[str] = []
    for op, path, value in _operations(await _body(request)):
        if m := _MEMBER_PATH.match(path):  # members[value eq "<id>"]: one member, by filter
            (remove if op == "remove" else add).append(m.group(1))
            continue
        attrs = value if not path and isinstance(value, dict) else {path: value}
        for attr, v in attrs.items():
            key = str(attr).lower()
            if key == "displayname" and op != "remove":
                name = _text(v, "displayName", 255)
            elif key == "members":
                if op == "add":
                    add += _member_ids(v)
                elif op == "replace":
                    members, add, remove = _member_ids(v), [], []
                elif v is None:  # remove with no value: everyone
                    members, add, remove = [], [], []
                else:
                    remove += _member_ids(v)
    return await _update_group(
        group_id, request, display_name=name, members=members, add=add, remove=remove
    )


async def _update_group(group_id: str, request: Request, **changes: Any) -> JSONResponse:
    try:
        group = await services.directory.update_group(group_id, **changes)
    except Conflict:
        raise ScimError(409, "a group with that displayName already exists", "uniqueness") from None
    if group is None:
        raise ScimError(404, f"Group {group_id[:64]} not found")
    await _changed(
        "scim.group.update",
        group.id,
        name=group.display_name,
        members=len(group.members),
        added=len(changes.get("add") or []),
        removed=len(changes.get("remove") or []),
    )
    return _ok(group_resource(group, request))


@router.delete("/Groups/{group_id}")
async def delete_group(group_id: str, request: Request) -> Response:
    _authorize(request)
    if not await services.directory.delete_group(group_id):
        raise ScimError(404, f"Group {group_id[:64]} not found")
    await _changed("scim.group.delete", group_id[:64])
    return Response(status_code=204)
