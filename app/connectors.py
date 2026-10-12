"""Connectors: one external system each, exposed as a small set of tools (ADR 0028).

A connector is reached only through `POST /v1/tools/{connector}/{tool}`, behind the access
policy. `credential()` mints what the call needs just before it, so nothing long-lived
sits in config.

To onboard one: add the class and register it here, give it an entry under `connectors:`
in config/policy.yaml, and add allow and deny tests.
"""

from __future__ import annotations

from typing import Any, Protocol


class ConnectorError(Exception):
    """The external system failed or refused. The message is safe to show the caller."""


class Connector(Protocol):
    name: str
    tools: frozenset[str]

    async def credential(self) -> str: ...

    async def call(self, tool: str, args: dict[str, Any], credential: str) -> dict[str, Any]: ...


class MockRecords:
    """A stand-in for a system of record (a case-management database), so the policy path
    can be shown and tested without an external account."""

    name = "mock_records"
    tools = frozenset({"lookup_case"})
    _cases = {"C-1001": {"status": "open", "owner": "compliance"}}

    async def credential(self) -> str:
        return "mock-short-lived-token"

    async def call(self, tool: str, args: dict[str, Any], credential: str) -> dict[str, Any]:
        case = self._cases.get(str(args.get("case_id", "")))
        if case is None:
            raise ConnectorError("case not found")
        return dict(case)


REGISTRY: dict[str, Connector] = {c.name: c for c in (MockRecords(),)}
