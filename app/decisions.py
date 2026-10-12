"""The decision log: one record per governance decision, allow or deny (ADR 0028).

Who asked, for what, with which class of data, what was decided, why, and which policy
version decided it. Never prompt or tool content.

Each record goes two ways: a structured log line (`gateway.audit`), written at once, and
a row in Postgres, written in batches in the background. If the database is down the row
is dropped and counted, and the log line is the copy that remains.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import config
from app.access import Principal
from app.db import DecisionRow
from app.observability import logging as obs_log
from app.observability import metrics
from app.observability.usage import PostgresUsageWriter

Outcome = Literal["allow", "deny"]


@dataclass
class DecisionRecord:
    at: datetime
    request_id: str
    subject: str
    subject_kind: str
    subject_name: str
    roles: list[str]
    tier: str
    team: str | None
    action: str  # "model:<alias>" or "tool:<connector>.<tool>"
    data_class: str
    decision: Outcome
    code: str  # a fixed name: ok, model_not_allowed, data_class_not_eligible, …
    reason: str
    policy_version: str
    detail: dict[str, Any] = field(default_factory=dict)

    def row(self) -> dict[str, Any]:
        out = asdict(self)
        # Columns are bounded; one oversized value must never sink a whole batch.
        for col, limit in (
            ("request_id", 64),
            ("subject", 100),
            ("subject_kind", 16),
            ("subject_name", 100),
            ("tier", 50),
            ("team", 100),
            ("action", 200),
            ("data_class", 64),
            ("code", 64),
            ("reason", 300),
            ("policy_version", 32),
        ):
            if isinstance(out[col], str):
                out[col] = out[col][:limit]
        return out

    def public(self) -> dict[str, Any]:
        return {**self.row(), "at": self.at.isoformat()}


class DecisionSink(Protocol):
    def submit(self, record: DecisionRecord) -> None: ...
    async def recent(self, limit: int, decision: str | None = None) -> list[DecisionRecord]: ...


class MemoryDecisionSink:
    """Keeps records in a list: tests and `GATEWAY_STORES=memory`."""

    def __init__(self) -> None:
        self.records: list[DecisionRecord] = []

    def submit(self, record: DecisionRecord) -> None:
        self.records.append(record)

    async def recent(self, limit: int, decision: str | None = None) -> list[DecisionRecord]:
        rows = [r for r in self.records if decision is None or r.decision == decision]
        return list(reversed(rows[-limit:]))


class PostgresDecisionWriter(PostgresUsageWriter):
    """The usage log's writer (bounded queue, batched inserts, never blocks a request) on
    the decision_log table."""

    def __init__(self, sessions: async_sessionmaker[Any]) -> None:
        super().__init__(sessions, table=DecisionRow, dropped=metrics.decisions_dropped)

    async def recent(self, limit: int, decision: str | None = None) -> list[DecisionRecord]:
        query = select(DecisionRow).order_by(DecisionRow.id.desc()).limit(limit)
        if decision is not None:
            query = query.where(DecisionRow.decision == decision)
        async with self.sessions() as s:
            rows = await s.scalars(query)
            return [
                DecisionRecord(
                    at=r.at,
                    request_id=r.request_id,
                    subject=r.subject,
                    subject_kind=r.subject_kind,
                    subject_name=r.subject_name,
                    roles=list(r.roles or []),
                    tier=r.tier,
                    team=r.team,
                    action=r.action,
                    data_class=r.data_class,
                    decision="allow" if r.decision == "allow" else "deny",
                    code=r.code,
                    reason=r.reason,
                    policy_version=r.policy_version,
                    detail=dict(r.detail or {}),
                )
                for r in rows
            ]


def record(
    principal: Principal,
    action: str,
    data_class: str,
    decision: Outcome,
    code: str = "ok",
    reason: str = "ok",
    **detail: Any,
) -> None:
    """Log and queue one decision. Never raises and never waits: recording a decision
    must not change it."""
    from app import services

    entry = DecisionRecord(
        at=datetime.now(UTC),
        request_id=obs_log.request_id.get(),
        subject=principal.subject,
        subject_kind=principal.kind,
        subject_name=principal.name,
        roles=list(principal.roles),
        tier=principal.tier,
        team=principal.team,
        action=action,
        data_class=data_class,
        decision=decision,
        code=code,
        reason=reason,
        policy_version=config.access.version,
        detail=detail,
    )
    metrics.decisions.labels(decision, code).inc()
    obs_log.audit.info(
        "decision",
        subject=entry.subject,
        tier=entry.tier,
        action=entry.action[:200],
        data_class=entry.data_class,
        decision=decision,
        code=code,
        policy_version=entry.policy_version,
    )
    services.decisions.submit(entry)
