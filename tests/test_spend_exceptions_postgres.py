"""PostgresExceptionStore against a real Postgres (migration 0011)."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text

from app import spend_exceptions
from app.db import make_engine, make_sessions
from app.spend_exceptions import PostgresExceptionStore, SpendException
from tests.test_keys_postgres import URL, store  # noqa: F401 — migrates the test DB


@pytest.fixture
async def exceptions(store: Any) -> AsyncIterator[PostgresExceptionStore]:  # noqa: F811
    engine = make_engine(URL)
    async with engine.begin() as conn:
        await conn.execute(text("TRUNCATE spend_exceptions"))
    yield PostgresExceptionStore(make_sessions(engine))
    await engine.dispose()


def asked(amount: float = 10, spend_id: str = "k1", period: str = "2026-10") -> SpendException:
    return spend_exceptions.new(f"key:{spend_id}", "case-bot", spend_id, period, amount, "why")


async def test_a_request_round_trips_and_is_decided_once(
    exceptions: PostgresExceptionStore,
) -> None:
    entry = asked(30)
    assert await exceptions.create(entry)
    assert await exceptions.get(entry.id) == entry
    assert await exceptions.get("missing") is None
    decided = await exceptions.decide(entry.id, "approved", "user:p", "paula", 25, "until the 31st")
    assert decided is not None
    assert (decided.status, decided.granted_usd, decided.decided_by) == ("approved", 25, "user:p")
    assert decided.decided_at is not None and decided.note == "until the 31st"
    assert await exceptions.decide(entry.id, "denied", "user:q", "quinn", None, None) is None
    assert await exceptions.granted("k1", "2026-10") == 25


async def test_two_approvers_at_once_make_one_decision(
    exceptions: PostgresExceptionStore,
) -> None:
    entry = asked()
    await exceptions.create(entry)
    results = await asyncio.gather(
        *(exceptions.decide(entry.id, "approved", f"user:{i}", "a", 10, None) for i in range(8))
    )
    assert sum(r is not None for r in results) == 1
    assert await exceptions.granted("k1", "2026-10") == 10


async def test_one_pending_request_per_caller_and_month(
    exceptions: PostgresExceptionStore,
) -> None:
    first = asked()
    assert await exceptions.create(first)
    assert not await exceptions.create(asked())  # the partial unique index
    assert await exceptions.create(asked(period="2026-11"))
    assert await exceptions.create(asked(spend_id="k2"))
    await exceptions.decide(first.id, "denied", "user:p", "paula", None, None)
    assert await exceptions.create(asked())  # decided: they may ask again


async def test_grants_sum_what_was_approved_for_that_caller_and_month(
    exceptions: PostgresExceptionStore,
) -> None:
    assert await exceptions.granted("k1", "2026-10") == 0
    for amount, outcome in ((10, "approved"), (5, "approved"), (99, "denied")):
        entry = asked(amount)
        await exceptions.create(entry)
        granted = amount if outcome == "approved" else None
        await exceptions.decide(entry.id, outcome, "user:p", "paula", granted, None)  # type: ignore[arg-type]
    await exceptions.create(asked(50))  # pending: not a grant
    assert await exceptions.granted("k1", "2026-10") == 15
    assert await exceptions.granted("k1", "2026-11") == 0
    assert await exceptions.granted("k2", "2026-10") == 0


async def test_listing_filters_by_subject_and_status(exceptions: PostgresExceptionStore) -> None:
    mine, theirs = asked(spend_id="k1"), asked(spend_id="k2")
    await exceptions.create(mine)
    await exceptions.create(theirs)
    await exceptions.decide(theirs.id, "denied", "user:p", "paula", None, None)
    assert {e.id for e in await exceptions.recent(None, None, 10)} == {mine.id, theirs.id}
    assert [e.id for e in await exceptions.recent("key:k1", None, 10)] == [mine.id]
    assert [e.id for e in await exceptions.recent(None, "denied", 10)] == [theirs.id]
    assert len(await exceptions.recent(None, None, 1)) == 1
