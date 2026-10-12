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


REGISTRY: dict[str, Connector] = {c.name: c for c in (MockRecords(),)}


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
