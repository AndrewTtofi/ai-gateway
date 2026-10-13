"""Spend exceptions (ADR 0032): more budget for one caller for one month, asked for by the
caller and approved or denied by someone in an approver group named in the policy.

The hard cap stays a hard cap. What changes it is a recorded decision by a second person:
the request, the decision and who made it are rows here, and each step is in the decision
log and the admin audit log.

An approved amount is added to the caller's budget for that month only. The request path
looks for grants only after a reservation was refused, so a caller within budget pays
nothing for this feature.

Approvers are told when a request arrives and when it is decided (ADR 0034), through a
webhook. The message says who, how much and which month. It never carries the requester's
reason or the approver's note: those are free text, and stay in the queue.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import SpendExceptionRow

log = logging.getLogger(__name__)

Status = Literal["pending", "approved", "denied"]


@dataclass(frozen=True)
class SpendException:
    id: str
    subject: str  # "user:<id>" or "key:<id>", as in the decision log
    subject_name: str
    spend_id: str  # where the caller's spend is counted
    period: str  # the month it applies to
    amount_usd: float
    reason: str
    status: Status
    requested_at: datetime
    granted_usd: float | None = None
    decided_by: str | None = None
    decided_by_name: str | None = None
    decided_at: datetime | None = None
    note: str | None = None

    def public(self) -> dict[str, Any]:
        out = asdict(self)
        del out["spend_id"]
        out["requested_at"] = self.requested_at.isoformat()
        out["decided_at"] = self.decided_at.isoformat() if self.decided_at else None
        return out


def new(
    subject: str, subject_name: str, spend_id: str, period: str, amount_usd: float, reason: str
) -> SpendException:
    return SpendException(
        id=uuid.uuid4().hex,
        subject=subject,
        subject_name=subject_name[:100],
        spend_id=spend_id,
        period=period,
        amount_usd=amount_usd,
        reason=reason,
        status="pending",
        requested_at=datetime.now(UTC),
    )


Event = Literal["requested", "approved", "denied"]
NOTIFY_ONCE_SECONDS = 40 * 86400  # longer than the month a request lives in
_tasks: set[asyncio.Task[None]] = set()


def message(entry: SpendException, event: Event) -> dict[str, Any]:
    """What the approvers' channel is sent. No reason and no note: free text written by a
    caller or an approver is not copied to a chat service (ADR 0034)."""
    from app.budget_alerts import usd  # late: budget_alerts loads the config at import

    who, asked = entry.subject_name or entry.subject, usd(entry.amount_usd)
    by = entry.decided_by_name or entry.decided_by or "someone"
    if event == "requested":
        text = (
            f"🙋 AI gateway: {who} asks for {asked} more budget for {entry.period}. "
            "Review: GET /v1/spend/exceptions?status=pending"
        )
    elif event == "approved":
        granted = usd(entry.granted_usd or 0.0)
        text = f"✅ AI gateway: {by} approved {granted} of the {asked} {who} asked for"
        text += f" ({entry.period})"
    else:
        text = f"🚫 AI gateway: {by} denied the {asked} {who} asked for ({entry.period})"
    return {
        "text": text,
        "kind": "spend_exception",
        "event": event,
        "id": entry.id,
        "subject": entry.subject,
        "who": who,
        "amount_usd": entry.amount_usd,
        "granted_usd": entry.granted_usd,
        "period": entry.period,
        "decided_by": entry.decided_by,
    }


def notify(entry: SpendException, event: Event) -> None:
    """Tell the approvers, in the background: a slow or broken webhook must not delay or
    fail the request that was just recorded."""
    task = asyncio.create_task(_notify(entry, event))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _notify(entry: SpendException, event: Event) -> None:
    from app import config, services  # late: services imports most of the app

    log.info("spend exception %s %s (%s)", entry.id, event, entry.subject)
    alerts: Any = services.alerts
    if alerts is None:
        return
    try:
        await alerts.deliver(
            message(entry, event),
            dedupe=f"spend-exception:{{{entry.id}}}:{event}",
            ttl=NOTIFY_ONCE_SECONDS,
            url=config.settings.spend_exceptions_webhook_url or None,
        )
    except Exception as exc:  # the request and the decision are already stored
        log.warning("spend exception notification failed: %s", type(exc).__name__)


class ExceptionStore(Protocol):
    async def create(self, entry: SpendException) -> bool:
        """False when the caller already has a pending request for that month."""

    async def get(self, exception_id: str) -> SpendException | None: ...

    async def decide(
        self,
        exception_id: str,
        status: Status,
        by: str,
        by_name: str,
        granted_usd: float | None,
        note: str | None,
    ) -> SpendException | None:
        """Decide a pending request. None when it isn't pending any more: two approvers
        deciding at once get one decision, not two."""

    async def recent(
        self, subject: str | None, status: str | None, limit: int
    ) -> list[SpendException]: ...

    async def granted(self, spend_id: str, period: str) -> float:
        """Everything approved for this caller in this month."""


class MemoryExceptionStore:
    def __init__(self) -> None:
        self.entries: dict[str, SpendException] = {}

    async def create(self, entry: SpendException) -> bool:
        if any(
            (e.spend_id, e.period, e.status) == (entry.spend_id, entry.period, "pending")
            for e in self.entries.values()
        ):
            return False
        self.entries[entry.id] = entry
        return True

    async def get(self, exception_id: str) -> SpendException | None:
        return self.entries.get(exception_id)

    async def decide(
        self,
        exception_id: str,
        status: Status,
        by: str,
        by_name: str,
        granted_usd: float | None,
        note: str | None,
    ) -> SpendException | None:
        entry = self.entries.get(exception_id)
        if entry is None or entry.status != "pending":
            return None
        decided = replace(
            entry,
            status=status,
            decided_by=by,
            decided_by_name=by_name[:100],
            decided_at=datetime.now(UTC),
            granted_usd=granted_usd,
            note=note,
        )
        self.entries[exception_id] = decided
        return decided

    async def recent(
        self, subject: str | None, status: str | None, limit: int
    ) -> list[SpendException]:
        found = [
            e
            for e in self.entries.values()
            if (subject is None or e.subject == subject) and (status is None or e.status == status)
        ]
        return sorted(found, key=lambda e: e.requested_at, reverse=True)[:limit]

    async def granted(self, spend_id: str, period: str) -> float:
        return sum(
            e.granted_usd or 0.0
            for e in self.entries.values()
            if (e.spend_id, e.period, e.status) == (spend_id, period, "approved")
        )


def _entry(row: SpendExceptionRow) -> SpendException:
    status: Status = "approved" if row.status == "approved" else "pending"
    if row.status == "denied":
        status = "denied"
    return SpendException(
        id=row.id,
        subject=row.subject,
        subject_name=row.subject_name,
        spend_id=row.spend_id,
        period=row.period,
        amount_usd=row.amount_usd,
        reason=row.reason,
        status=status,
        requested_at=row.requested_at,
        granted_usd=row.granted_usd,
        decided_by=row.decided_by,
        decided_by_name=row.decided_by_name,
        decided_at=row.decided_at,
        note=row.note,
    )


class PostgresExceptionStore:
    def __init__(self, sessions: async_sessionmaker[Any]) -> None:
        self.sessions = sessions

    async def create(self, entry: SpendException) -> bool:
        try:
            async with self.sessions() as s, s.begin():
                s.add(SpendExceptionRow(**asdict(entry)))
        except IntegrityError:  # the partial unique index: one pending per caller and month
            return False
        return True

    async def get(self, exception_id: str) -> SpendException | None:
        async with self.sessions() as s:
            row = await s.get(SpendExceptionRow, exception_id)
            return _entry(row) if row else None

    async def decide(
        self,
        exception_id: str,
        status: Status,
        by: str,
        by_name: str,
        granted_usd: float | None,
        note: str | None,
    ) -> SpendException | None:
        # One UPDATE that only matches a pending row: the database picks the winner when
        # two approvers decide at the same moment.
        async with self.sessions() as s, s.begin():
            row = await s.scalar(
                update(SpendExceptionRow)
                .where(SpendExceptionRow.id == exception_id, SpendExceptionRow.status == "pending")
                .values(
                    status=status,
                    decided_by=by,
                    decided_by_name=by_name[:100],
                    decided_at=datetime.now(UTC),
                    granted_usd=granted_usd,
                    note=note,
                )
                .returning(SpendExceptionRow)
            )
            return _entry(row) if row else None

    async def recent(
        self, subject: str | None, status: str | None, limit: int
    ) -> list[SpendException]:
        query = select(SpendExceptionRow).order_by(SpendExceptionRow.requested_at.desc())
        if subject is not None:
            query = query.where(SpendExceptionRow.subject == subject)
        if status is not None:
            query = query.where(SpendExceptionRow.status == status)
        async with self.sessions() as s:
            return [_entry(r) for r in await s.scalars(query.limit(limit))]

    async def granted(self, spend_id: str, period: str) -> float:
        async with self.sessions() as s:
            total = await s.scalar(
                select(func.coalesce(func.sum(SpendExceptionRow.granted_usd), 0.0)).where(
                    SpendExceptionRow.spend_id == spend_id,
                    SpendExceptionRow.period == period,
                    SpendExceptionRow.status == "approved",
                )
            )
            return float(total or 0.0)


class Grants:
    """What approved exceptions add to a caller's budget, for the request path.

    Only asked after a reservation was refused. The answer is remembered for a few
    seconds, so a client retrying in a loop at its cap costs one query per interval, not
    one per request. An approval clears the entry on the replica that handled it; other
    replicas see it within the interval. If the database can't answer, there is no grant:
    the cap holds.
    """

    TTL_SECONDS = 10.0
    MAX_ENTRIES = 10_000

    def __init__(self, store: ExceptionStore, clock: Callable[[], float] = time.monotonic) -> None:
        self.store, self._now = store, clock
        self._seen: dict[tuple[str, str], tuple[float, float]] = {}  # → (amount, at)

    async def total(self, spend_id: str, period: str) -> float:
        key = (spend_id, period)
        hit = self._seen.get(key)
        if hit is not None and self._now() - hit[1] < self.TTL_SECONDS:
            return hit[0]
        try:
            amount = await self.store.granted(spend_id, period)
        except Exception as exc:
            log.error("spend exceptions could not be read: %s", type(exc).__name__)
            return 0.0
        if len(self._seen) >= self.MAX_ENTRIES:
            self._seen.clear()
        self._seen[key] = (amount, self._now())
        return amount

    def forget(self, spend_id: str, period: str) -> None:
        self._seen.pop((spend_id, period), None)
