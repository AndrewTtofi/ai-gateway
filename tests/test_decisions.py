"""The decision log (ADR 0028): what a record holds, and how it's stored."""

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from app import config, decisions, services
from app.access import Principal
from app.decisions import DecisionRecord, MemoryDecisionSink, PostgresDecisionWriter
from app.observability import logging as obs_log
from app.observability import metrics

ALICE = Principal("key:k1", "service", "case-bot", ("compliance",), ("compliance",), "standard")


@pytest.fixture
def sink(monkeypatch: pytest.MonkeyPatch) -> MemoryDecisionSink:
    s = MemoryDecisionSink()
    monkeypatch.setattr(services, "decisions", s)
    return s


def test_a_record_says_who_what_which_data_the_outcome_and_the_policy(
    sink: MemoryDecisionSink,
) -> None:
    token = obs_log.request_id.set("req-1")
    try:
        decisions.record(ALICE, "model:smart", "confidential", "deny", "tier_too_low", "no")
    finally:
        obs_log.request_id.reset(token)
    (r,) = sink.records
    assert (r.subject, r.subject_kind, r.subject_name) == ("key:k1", "service", "case-bot")
    assert (r.roles, r.tier) == (["compliance"], "standard")
    assert (r.action, r.data_class) == ("model:smart", "confidential")
    assert (r.decision, r.code, r.reason) == ("deny", "tier_too_low", "no")
    assert r.request_id == "req-1"  # joins the usage row and the logs
    assert r.policy_version == config.access.version  # which rules decided


def test_decisions_are_counted_by_outcome_and_code(sink: MemoryDecisionSink) -> None:
    def count() -> float:
        return (
            metrics.registry.get_sample_value(
                "gateway_decisions_total", {"decision": "allow", "code": "ok"}
            )
            or 0
        )

    before = count()
    decisions.record(ALICE, "model:smart", "internal", "allow", excluded=["a/b"])
    assert count() == before + 1
    assert sink.records[0].detail == {"excluded": ["a/b"]}


def test_recent_is_newest_first_and_filters_by_outcome(sink: MemoryDecisionSink) -> None:
    for i, outcome in enumerate(("allow", "deny", "allow")):
        decisions.record(ALICE, f"model:m{i}", "internal", outcome)  # type: ignore[arg-type]
    recent = asyncio.run(sink.recent(2))
    assert [r.action for r in recent] == ["model:m2", "model:m1"]
    assert [r.action for r in asyncio.run(sink.recent(10, "deny"))] == ["model:m1"]


def test_oversized_values_are_cut_to_their_columns() -> None:
    r = DecisionRecord(
        at=datetime.now(UTC),
        request_id="r" * 500,
        subject="s" * 500,
        subject_kind="service",
        subject_name="n" * 500,
        roles=[],
        tier="standard",
        team=None,
        action="model:" + "x" * 5000,
        data_class="internal",
        decision="deny",
        code="c" * 500,
        reason="because " * 500,
        policy_version="v1",
    )
    row = r.row()
    assert len(row["action"]) == 200 and len(row["reason"]) == 300 and len(row["subject"]) == 100
    assert len(row["request_id"]) == 64 and len(row["code"]) == 64 and row["team"] is None


def test_the_postgres_writer_drops_and_counts_when_its_queue_is_full() -> None:
    """A slow or dead database must never slow or fail a request: the row is dropped and
    counted, and the audit log line remains."""

    async def scenario() -> float:
        sessions: Any = None  # never reached: the writer isn't started
        writer = PostgresDecisionWriter(sessions)
        writer.queue = asyncio.Queue(maxsize=1)
        before = metrics.registry.get_sample_value("gateway_decision_log_dropped_total") or 0
        made = [
            DecisionRecord(
                at=datetime.now(UTC),
                request_id="r",
                subject="key:k1",
                subject_kind="service",
                subject_name="n",
                roles=[],
                tier="standard",
                team=None,
                action="model:m",
                data_class="internal",
                decision="allow",
                code="ok",
                reason="ok",
                policy_version="v1",
            )
            for _ in range(3)
        ]
        for d in made:
            writer.submit(d)
        after = metrics.registry.get_sample_value("gateway_decision_log_dropped_total") or 0
        return after - before

    assert asyncio.run(scenario()) == 2
