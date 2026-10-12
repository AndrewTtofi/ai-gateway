"""Connectors: one external system each, exposed as a small set of tools (ADR 0028).

A connector is reached only through `POST /v1/tools/{connector}/{tool}`, behind the access
policy. `credential()` mints what the call needs just before it, so nothing long-lived
sits in config.

To onboard one: add the class and register it here, give it an entry under `connectors:`
in config/policy.yaml, and add allow and deny tests.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

import httpx
import jwt

log = logging.getLogger(__name__)


class ConnectorError(Exception):
    """The external system failed or refused. The message is safe to show the caller."""

    status = 502


class BadArguments(ConnectorError):
    """The caller's arguments are wrong: nothing was sent to the external system."""

    status = 400


class ConnectorNotConfigured(ConnectorError):
    """The connector is deployed and onboarded, but its credentials aren't set here."""

    status = 503


class Connector(Protocol):
    name: str
    tools: frozenset[str]
    # What each tool is for and the arguments it takes (a JSON Schema): what a model is
    # shown when the tool is offered to it.
    schemas: dict[str, dict[str, Any]]

    async def credential(self) -> str: ...

    async def call(self, tool: str, args: dict[str, Any], credential: str) -> dict[str, Any]: ...


class MockRecords:
    """A stand-in for a system of record (a case-management database), so the policy path
    can be shown and tested without an external account."""

    name = "mock_records"
    tools = frozenset({"lookup_case"})
    schemas = {
        "lookup_case": {
            "description": "Look up a case in the case-management system by its id. "
            "Returns its status and owning team.",
            "parameters": {
                "type": "object",
                "properties": {
                    "case_id": {"type": "string", "description": "The case id, e.g. C-1001"}
                },
                "required": ["case_id"],
            },
        }
    }
    _cases = {"C-1001": {"status": "open", "owner": "compliance"}}

    async def credential(self) -> str:
        return "mock-short-lived-token"

    async def call(self, tool: str, args: dict[str, Any], credential: str) -> dict[str, Any]:
        case = self._cases.get(str(args.get("case_id", "")))
        if case is None:
            raise ConnectorError("case not found")
        return dict(case)


_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")
_BODY_CHARS = 4000  # a pull request description can be 65k characters; a tool result shouldn't be


class GitHub:
    """Pull requests, read-only, through a GitHub App (ADR 0031).

    There is no stored GitHub token. The gateway holds the app's private key and, when a
    call needs one, signs a short JWT with it and exchanges that for an installation token:
    valid for an hour, limited to the repositories the app is installed on, and requested
    with only the permission this connector uses. The token is kept in this process's
    memory until shortly before it expires.
    """

    name = "github"
    tools = frozenset({"list_pull_requests", "get_pull_request"})
    schemas = {
        "list_pull_requests": {
            "description": "List pull requests in a GitHub repository, newest first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "owner/name, e.g. octocat/hello"},
                    "state": {"type": "string", "enum": ["open", "closed", "all"]},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "required": ["repo"],
            },
        },
        "get_pull_request": {
            "description": "Get one pull request in a GitHub repository: its state, author, "
            "branches, size and description.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "owner/name, e.g. octocat/hello"},
                    "number": {"type": "integer", "minimum": 1},
                },
                "required": ["repo", "number"],
            },
        },
    }
    PERMISSIONS = {"pull_requests": "read"}
    JWT_SECONDS = 540  # GitHub refuses an app JWT that lives longer than 10 minutes
    REFRESH_BEFORE_SECONDS = 300  # a token this close to expiry is replaced, not used

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._now = clock
        self._token = ""
        self._expires_at = 0.0
        self._lock = asyncio.Lock()  # one exchange at a time, however many calls are waiting

    def _settings(self) -> tuple[str, str, str, str]:
        from app import config  # late: config imports this module

        s = config.settings
        if not (s.github_app_id and s.github_app_installation_id and s.github_app_private_key_file):
            raise ConnectorNotConfigured("the github connector is not configured")
        return (
            s.github_api_url.rstrip("/"),
            s.github_app_id,
            s.github_app_installation_id,
            s.github_app_private_key_file,
        )

    async def _request(self, method: str, url: str, token: str, **kwargs: Any) -> httpx.Response:
        headers = {
            "authorization": f"Bearer {token}",
            "accept": "application/vnd.github+json",
            "x-github-api-version": "2022-11-28",
        }
        try:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
                return await client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            # Only the exception's type: its text can carry the URL, never worth the risk.
            log.warning("github request failed: %s", type(exc).__name__)
            raise ConnectorError("github could not be reached") from None

    async def credential(self) -> str:
        async with self._lock:
            if self._token and self._now() < self._expires_at - self.REFRESH_BEFORE_SECONDS:
                return self._token
            api, app_id, installation, key_file = self._settings()
            try:
                # Read on every exchange (about once an hour): a rotated key needs no restart.
                private_key = await asyncio.to_thread(Path(key_file).read_text)
                now = int(self._now())
                # Issued a minute ago, so a slightly slow clock here still passes GitHub's check.
                app_jwt = jwt.encode(
                    {"iat": now - 60, "exp": now + self.JWT_SECONDS, "iss": app_id},
                    private_key,
                    algorithm="RS256",
                )
            except (OSError, ValueError, jwt.PyJWTError) as exc:
                log.error("github app key could not be used: %s", type(exc).__name__)
                raise ConnectorNotConfigured("the github connector is not configured") from None
            resp = await self._request(
                "POST",
                f"{api}/app/installations/{installation}/access_tokens",
                app_jwt,
                # Ask for less than the app was granted: this token can only read pull requests.
                json={"permissions": self.PERMISSIONS},
            )
            if resp.status_code != 201:
                log.error("github refused the app credentials: HTTP %d", resp.status_code)
                raise ConnectorError("github refused the gateway's credentials")
            try:
                body = resp.json()
                token = str(body["token"])
                expires_at = datetime.fromisoformat(body["expires_at"]).timestamp()
            except ValueError, KeyError, TypeError:
                raise ConnectorError("github sent an unexpected response") from None
            self._token, self._expires_at = token, expires_at
            return token

    def forget(self, credential: str) -> None:
        """Drop the cached token if it is this one (GitHub said it is no longer valid)."""
        if credential == self._token:
            self._token, self._expires_at = "", 0.0

    async def call(self, tool: str, args: dict[str, Any], credential: str) -> dict[str, Any]:
        repo = args.get("repo")
        # The repository goes into the URL path: only owner/name, nothing that could
        # reach another endpoint with the gateway's token.
        if (
            not isinstance(repo, str)
            or not _REPO.fullmatch(repo)
            or repo.partition("/")[2] in (".", "..")
        ):
            raise BadArguments("repo must be 'owner/name'")
        api = self._settings()[0]
        if tool == "list_pull_requests":
            state, limit = args.get("state", "open"), args.get("limit", 20)
            if state not in ("open", "closed", "all"):
                raise BadArguments("state must be open, closed or all")
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
                raise BadArguments("limit must be between 1 and 50")
            found = await self._get(
                f"{api}/repos/{repo}/pulls", credential, {"state": state, "per_page": limit}
            )
            if not isinstance(found, list):
                raise ConnectorError("github sent an unexpected response")
            pulls = [_summary(p) for p in found if isinstance(p, dict)]
            return {"repo": repo, "pull_requests": pulls}
        number = args.get("number")
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise BadArguments("number must be a positive integer")
        pull = await self._get(f"{api}/repos/{repo}/pulls/{number}", credential)
        if not isinstance(pull, dict):
            raise ConnectorError("github sent an unexpected response")
        raw = pull.get("body")
        body = raw if isinstance(raw, str) else ""
        return {
            "repo": repo,
            **_summary(pull),
            "merged": bool(pull.get("merged")),
            "base": _ref(pull.get("base")),
            "head": _ref(pull.get("head")),
            "changed_files": pull.get("changed_files"),
            "additions": pull.get("additions"),
            "deletions": pull.get("deletions"),
            "body": body[:_BODY_CHARS],
            "body_truncated": len(body) > _BODY_CHARS,
        }

    async def _get(self, url: str, credential: str, params: dict[str, Any] | None = None) -> Any:
        resp = await self._request("GET", url, credential, params=params)
        if resp.status_code == 401:
            self.forget(credential)  # revoked early: the next call exchanges for a new one
            raise ConnectorError("github refused the gateway's credentials")
        if resp.status_code == 404:
            # GitHub answers 404 for both, so a caller can't probe for private repositories.
            raise ConnectorError("not found, or the gateway's app is not installed on that repo")
        if resp.status_code in (403, 429):
            raise ConnectorError("github refused the request (rate limit or permission)")
        if resp.status_code != 200:
            raise ConnectorError(f"github answered HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError:
            raise ConnectorError("github sent an unexpected response") from None


def _ref(value: Any) -> Any:
    return value.get("ref") if isinstance(value, dict) else None


def _summary(pull: dict[str, Any]) -> dict[str, Any]:
    """The few fields a caller needs, not GitHub's whole object (about 300 fields)."""
    user = pull.get("user")
    return {
        "number": pull.get("number"),
        "title": pull.get("title"),
        "state": pull.get("state"),
        "draft": bool(pull.get("draft")),
        "author": user.get("login") if isinstance(user, dict) else None,
        "created_at": pull.get("created_at"),
        "updated_at": pull.get("updated_at"),
        "url": pull.get("html_url"),
    }


REGISTRY: dict[str, Connector] = {c.name: c for c in (MockRecords(), GitHub())}


TOOL_SEPARATOR = "__"  # function names can't contain a dot: mock_records__lookup_case


def tool_definitions() -> list[dict[str, Any]]:
    """Every registered tool as an OpenAI-format function definition, for offering the
    gateway's tools to a model (the eval harness does)."""
    return [
        {
            "type": "function",
            "function": {
                "name": f"{connector.name}{TOOL_SEPARATOR}{tool}",
                "description": connector.schemas[tool]["description"],
                "parameters": connector.schemas[tool]["parameters"],
            },
        }
        for connector in REGISTRY.values()
        for tool in sorted(connector.tools)
    ]


def tool_id(function_name: str) -> str:
    """A model's function name back to `connector.tool`."""
    connector, _, tool = function_name.partition(TOOL_SEPARATOR)
    return f"{connector}.{tool}" if tool else function_name
