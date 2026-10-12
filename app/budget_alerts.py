"""Budget alerts (ADR 0024): tell someone at 50 / 80 / 100% of a monthly budget, before
apps find out from a 429.

The reservation already returns each key's (and team's) new month-to-date total, so a
crossing is spotted with no extra Redis call: a threshold lies between the total before
this request's hold and after it. Holds count like spend, so an alert can come from a
request whose final cost turns out lower. Each threshold alerts once per key or team and
month, across the fleet (a Redis SET NX), through the alert webhook. Without one, it's a
log line and a metric.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app import config
from app.observability import metrics

log = logging.getLogger(__name__)
_tasks: set[asyncio.Task[None]] = set()
DEDUPE_SECONDS = 40 * 86400  # longer than a month: one alert per threshold per month


def usd(amount: float) -> str:
    """$12.34, or more decimals for small amounts ($0.0012, not $0.00)."""
    return f"${amount:,.2f}" if amount >= 1 else f"${amount:.4f}"


def crossed(
    who: str,
    scope_id: str,
    total: float,
    usd: float,
    budget: float,
    period: str,
    soft: float | None = None,
) -> None:
    """Called after a successful hold of `usd` brought spend to `total`. `soft` is the
    caller's soft cap in dollars, if it has one (ADR 0032)."""
    if budget <= 0 or usd <= 0:
        return
    before = total - usd
    marks = [(level * budget, str(round(level * 100))) for level in config.limits.budget_alerts]
    if soft is not None and soft > 0:
        marks.append((soft, "soft"))
    for amount, label in marks:
        if before < amount <= total:
            task = asyncio.create_task(_alert(who, scope_id, label, total, budget, period))
            _tasks.add(task)
            task.add_done_callback(_tasks.discard)


async def _alert(
    who: str, scope_id: str, label: str, total: float, budget: float, period: str
) -> None:
    """`label` is a percentage of the budget ("80") or "soft" for the soft cap."""
    from app import services  # late: services imports most of the app

    reached = "its soft cap" if label == "soft" else f"{label}% of its {period} budget"
    metrics.budget_alerts.labels(label).inc()
    log.warning("%s reached %s (%s of %s)", who, reached, usd(total), usd(budget))
    alerts: Any = services.alerts
    if alerts is None:
        return
    icon = "🛑" if label == "100" else "⚠️"
    payload = {
        "text": f"{icon} AI gateway: {who} has reached {reached} ({usd(total)} of {usd(budget)})",
        "kind": "budget",
        "who": who,
        "level": label if label == "soft" else int(label),
        "spent_usd": round(total, 4),
        "budget_usd": budget,
        "period": period,
    }
    await alerts.deliver(
        payload, dedupe=f"budget-alert:{{{scope_id}}}:{period}:{label}", ttl=DEDUPE_SECONDS
    )
