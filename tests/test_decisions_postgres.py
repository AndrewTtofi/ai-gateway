"""PostgresDecisionWriter against a real Postgres (migration 0009)."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text

from app.db import make_engine, make_sessions
from app.decisions import DecisionRecord, PostgresDecisionWriter
from tests.test_keys_postgres import URL, store  # noqa: F401 — migrates the test DB


def rec(i: int, decision: str = "allow", **extra: Any) -> DecisionRecord:
    base: dict[str, Any] = dict(
        at=datetime.now(UTC),
        request_id=f"r{i}",
        subject="key:k1",
        subject_kind="service",
        subject_name="case-bot",
        roles=["compliance"],
        tier="standard",
        team="compliance",
        action="model:smart",
        data_class="confidential",
        decision=decision,
        code="ok" if decision == "allow" else "data_class_not_eligible",
        reason="ok",
        policy_version="2026-10-12.1",
        detail={"excluded": ["vendor/big"]},
    )
    return DecisionRecord(**{**base, **extra})


@pytest.fixture
async def writer(store: Any) -> AsyncIterator[PostgresDecisionWriter]:  # noqa: F811
    engine = make_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE decision_log"))
    w = PostgresDecisionWriter(make_sessions(engine))
    w.batch_size, w.flush_seconds = 10, 0.05
    yield w
    await engine.dispose()


async def count(w: PostgresDecisionWriter) -> int:
    async with w.sessions() as s:
        return int(await s.scalar(text("SELECT count(*) FROM decision_log")) or 0)


async def test_decisions_are_written_and_read_back_newest_first(
    writer: PostgresDecisionWriter,
) -> None:
    writer.start()
    for i in range(12):
        writer.submit(rec(i, "deny" if i % 4 == 0 else "allow"))
    for _ in range(50):
        if await count(writer) == 12:
            break
        await asyncio.sleep(0.05)
    await writer.stop()
    assert await count(writer) == 12

    recent = await writer.recent(3)
    assert [r.request_id for r in recent] == ["r11", "r10", "r9"]
    first = recent[0]
    assert (first.subject, first.roles, first.team) == ("key:k1", ["compliance"], "compliance")
    assert (first.data_class, first.policy_version) == ("confidential", "2026-10-12.1")
    assert first.detail == {"excluded": ["vendor/big"]}

    denied = await writer.recent(100, "deny")
    assert [r.request_id for r in denied] == ["r8", "r4", "r0"]
    assert all(r.code == "data_class_not_eligible" for r in denied)


async def test_one_oversized_record_does_not_sink_the_batch(
    writer: PostgresDecisionWriter,
) -> None:
    writer.start()
    writer.submit(rec(1, action="model:" + "x" * 5000, reason="r" * 5000))
    writer.submit(rec(2))
    await writer.stop()  # flushes what is queued
    assert await count(writer) == 2
