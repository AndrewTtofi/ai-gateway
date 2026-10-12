"""The model turn-up gate (ADR 0030): golden tasks, checks, and what counts as a regression."""

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app import config, connectors, main
from app.providers.base import ProviderError
from evals import run_evals
from evals.run_evals import Check, Output, Task, ToolCall, evaluate, failed_checks, parse_response


def task(*checks: dict[str, Any], **extra: Any) -> Task:
    return Task.model_validate(
        {"id": "t", "owner": "platform", "prompt": "do it", "checks": list(checks), **extra}
    )


def call(name: str = "mock_records.lookup_case", **args: Any) -> ToolCall:
    return ToolCall(name, args)


# --- the golden file ----------------------------------------------------------------------


def test_the_shipped_golden_file_is_valid_and_has_an_injection_case() -> None:
    tasks = run_evals.load_golden()
    assert len(tasks) >= 6 and all(t.owner for t in tasks)
    assert any(
        c.not_contains
        for t in tasks
        if "ignore all previous instructions" in t.prompt
        for c in t.checks
    )
    # Every tool a task expects is one the gateway really offers.
    offered = {connectors.tool_id(d["function"]["name"]) for d in connectors.tool_definitions()}
    assert {c.tool_call for t in tasks for c in t.checks if c.tool_call} <= offered


@pytest.mark.parametrize(
    "bad",
    [
        {"contains": ["a"], "no_tool_call": True},  # two kinds in one check
        {},  # no kind
        {"args": {"x": 1}, "contains": ["a"]},  # args without tool_call
        {"containz": ["a"]},  # a typo
    ],
)
def test_a_malformed_check_is_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Check.model_validate(bad)


def test_task_ids_must_be_unique(tmp_path: Path) -> None:
    one = {"id": "same", "owner": "x", "prompt": "p", "checks": [{"no_tool_call": True}]}
    path = tmp_path / "golden.yaml"
    path.write_text(json.dumps({"tasks": [one, one]}))  # JSON is YAML
    with pytest.raises(ValidationError):
        run_evals.load_golden(path)


# --- checks ---------------------------------------------------------------------------------


def test_text_checks() -> None:
    t = task({"contains": ["Rolled", "4411"]}, {"not_contains": ["AKIA"]})
    assert failed_checks(t, Output(text="Deploy 4411 was rolled back.")) == []
    assert failed_checks(t, Output(text="It was rolled back.")) == ["contains ['Rolled', '4411']"]
    assert failed_checks(t, Output(text="4411 rolled; key akia123")) == ["not_contains ['AKIA']"]


def test_tool_checks() -> None:
    wants = task({"tool_call": "mock_records.lookup_case", "args": {"case_id": "C-1"}})
    assert failed_checks(wants, Output(tool_calls=[call(case_id="C-1")])) == []
    assert failed_checks(wants, Output(tool_calls=[call(case_id="C-2")])) != []  # wrong argument
    assert failed_checks(wants, Output(tool_calls=[call("other.tool", case_id="C-1")])) != []
    assert failed_checks(wants, Output(text="The case is open.")) != []  # answered from thin air
    none = task({"no_tool_call": True})
    assert failed_checks(none, Output(text="42")) == []
    assert failed_checks(none, Output(tool_calls=[call()])) == ["no_tool_call"]


def test_a_real_response_is_read_for_text_and_tool_calls() -> None:
    out = parse_response(
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "mock_records__lookup_case",
                                    "arguments": '{"case_id": "C-1001"}',
                                }
                            },
                            {
                                "function": {
                                    "name": "mock_records__lookup_case",
                                    "arguments": "{bad",
                                }
                            },
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 120, "completion_tokens": 15},
        }
    )
    assert out.text == "" and (out.prompt_tokens, out.completion_tokens) == (120, 15)
    assert out.tool_calls == [call(case_id="C-1001"), call()]  # bad JSON: no arguments
    parts = parse_response(
        {"choices": [{"message": {"content": [{"type": "text", "text": "hi"}]}}]}
    )
    assert parts.text == "hi" and parts.tool_calls == []
    assert parse_response({}).text == ""


def test_the_gateways_tools_are_offered_as_function_definitions() -> None:
    (definition,) = connectors.tool_definitions()
    fn = definition["function"]
    assert fn["name"] == "mock_records__lookup_case"
    assert fn["parameters"]["required"] == ["case_id"] and fn["description"]
    assert connectors.tool_id(fn["name"]) == "mock_records.lookup_case"


# --- what counts as a regression ---------------------------------------------------------------


def scripted(answers: dict[str, list[Output]]) -> Any:
    """A model whose answers are scripted, per model name, in order."""
    calls = {name: 0 for name in answers}

    async def model(name: str, _: Task) -> Output:
        out = answers[name][calls[name] % len(answers[name])]
        calls[name] += 1
        return out

    return model


def run(answers: dict[str, list[Output]], runs: int = 1) -> run_evals.Report:
    t = task({"contains": ["42"]})
    return asyncio.run(evaluate([t], "old", "new", scripted(answers), runs))


GOOD, BAD, BROKEN = Output(text="42"), Output(text="41"), Output(error="ProviderError: down")


def test_a_regression_is_the_candidate_failing_what_the_current_model_passes() -> None:
    assert run({"old": [GOOD], "new": [GOOD]}).exit_code == 0
    report = run({"old": [GOOD], "new": [BAD]})
    assert report.exit_code == 1 and [r.task for r in report.regressions] == ["t"]
    # Both fail: the task is too hard for either, not a reason to block the candidate.
    assert run({"old": [BAD], "new": [BAD]}).exit_code == 0
    assert run({"old": [BAD], "new": [GOOD]}).exit_code == 0  # an improvement


def test_every_run_must_pass() -> None:
    """Right four times in five is not a model to gate a rollout on."""
    flaky = [GOOD, GOOD, BAD]
    assert run({"old": [GOOD], "new": flaky}, runs=2).exit_code == 0
    assert run({"old": [GOOD], "new": flaky}, runs=3).exit_code == 1


def test_a_model_that_cannot_be_called_makes_the_comparison_inconclusive() -> None:
    assert run({"old": [BROKEN], "new": [GOOD]}).exit_code == 2  # no baseline
    # The candidate erroring where the current model passes is a regression, not a shrug.
    assert run({"old": [GOOD], "new": [BROKEN]}).exit_code == 1


def test_totals_add_up_tokens_cost_and_latency() -> None:
    priced = Output(
        text="42", prompt_tokens=100, completion_tokens=10, latency_ms=200, cost_usd=0.01
    )
    report = run({"old": [priced], "new": [Output(text="42", latency_ms=50)]}, runs=3)
    old, new = report.totals["old"], report.totals["new"]
    assert (old["prompt_tokens"], old["completion_tokens"]) == (300, 30)
    assert old["cost_usd"] == pytest.approx(0.03) and old["priced"] is True
    assert new["priced"] is False  # an unpriced model is reported as such, not as free
    assert "unpriced" in run_evals.render(report) and "$0.0300" in run_evals.render(report)


# --- the command ------------------------------------------------------------------------------


def test_offline_passes_for_the_baseline_and_fails_for_a_regressed_stub(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert (
        run_evals.main(["--offline", "--current", "stub/baseline", "--candidate", "stub/baseline"])
        == 0
    )
    out = capsys.readouterr().out
    assert "OFFLINE" in out and "0 regression(s)" in out
    report = tmp_path / "evals.json"
    code = run_evals.main(
        [
            "--offline",
            "--current",
            "stub/baseline",
            "--candidate",
            "stub/regressed",
            "--json",
            str(report),
        ]
    )
    assert code == 1 and "<- REGRESSION" in capsys.readouterr().out
    saved = json.loads(report.read_text())
    assert saved["offline"] is True and "injected-document" in saved["regressions"]


def test_a_candidate_must_be_configured_and_classified_before_it_is_evaluated(
    registry: config.Registry, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Model turn-up has an order: models.yaml, a data-handling rule, then the evals."""
    assert run_evals.main(["--current", "local", "--candidate", "nope/model"]) == 2
    assert "not a model or alias" in capsys.readouterr().err
    strict = config.access.model_copy(update={"models": {}})
    monkeypatch.setattr(config, "access", strict)
    assert run_evals.main(["--current", "local", "--candidate", "local"]) == 2
    assert "no data-handling rule" in capsys.readouterr().err


def test_a_live_run_calls_the_gateways_adapter_with_the_tools(
    registry: config.Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    class Adapter:
        async def chat(self, model: str, request: dict[str, Any]) -> dict[str, Any]:
            seen.update(model=model, request=request)
            return {
                "choices": [{"message": {"content": "The deploy 4411 was rolled back."}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 9},
            }

    monkeypatch.setattr(main, "resolve_target", lambda m: (Adapter(), "tiny", "mock/tiny"))
    pricing = config.Pricing.model_validate(
        {"models": {"mock/tiny": {"input": 1.0, "output": 2.0}}}
    )
    monkeypatch.setattr(config, "pricing", pricing)
    out = asyncio.run(run_evals.live("local", task({"contains": ["4411"]}, system="Be brief.")))
    assert out.text.startswith("The deploy") and out.error is None
    assert out.cost_usd == pytest.approx((50 * 1.0 + 9 * 2.0) / 1_000_000)
    assert seen["model"] == "tiny"
    assert [m["role"] for m in seen["request"]["messages"]] == ["system", "user"]
    assert seen["request"]["tools"][0]["function"]["name"] == "mock_records__lookup_case"


def test_a_provider_error_is_reported_not_raised(
    registry: config.Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Down:
        async def chat(self, model: str, request: dict[str, Any]) -> dict[str, Any]:
            raise ProviderError("upstream is down", status=503)

    monkeypatch.setattr(main, "resolve_target", lambda m: (Down(), "tiny", "mock/tiny"))
    out = asyncio.run(run_evals.live("local", task({"no_tool_call": True})))
    assert out.error is not None and "upstream is down" in out.error


def test_a_provider_without_a_key_is_inconclusive_not_a_failed_task(
    registry: config.Registry, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TEST_ANTHROPIC_KEY")  # the test registry's `claude` provider key
    code = run_evals.main(["--current", "claude-new", "--candidate", "claude-new"])
    out = capsys.readouterr().out
    assert code == 2 and "ERROR" in out and "not configured" in out
