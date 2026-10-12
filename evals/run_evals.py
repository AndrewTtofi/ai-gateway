"""Compare a candidate model with the current one on the golden tasks (ADR 0030).

    python -m evals.run_evals --current ollama/llama3.2:3b --candidate ollama/qwen2.5:7b
    python -m evals.run_evals --offline --current stub/baseline --candidate stub/baseline

Each task goes to both models through the gateway's own provider adapters, with the
gateway's connector tools offered as tool definitions, and the real answer is checked: its
text, and the tool calls it made. A model can be an alias or a provider/model from
config/models.yaml.

Exit code: 0 no regressions; 1 the candidate fails a task the current model passes;
2 the comparison couldn't be made (unknown model, no API key, a provider error, or a
candidate with no data-handling rule in config/policy.yaml).

`--offline` replaces the models with deterministic stubs, so CI can check the harness and
the golden file without keys. It says nothing about any real model.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

GOLDEN = Path(__file__).with_name("golden.yaml")
MAX_TOKENS = 400


class Check(BaseModel):
    """One check: exactly one of the four kinds."""

    model_config = ConfigDict(extra="forbid")

    contains: list[str] | None = None
    not_contains: list[str] | None = None
    tool_call: str | None = None
    args: dict[str, Any] | None = None  # with tool_call: argument values it must have
    no_tool_call: bool | None = None

    @model_validator(mode="after")
    def _one_kind(self) -> Check:
        kinds = (self.contains, self.not_contains, self.tool_call, self.no_tool_call)
        if sum(k is not None for k in kinds) != 1:
            raise ValueError("a check is one of contains, not_contains, tool_call, no_tool_call")
        if self.args is not None and self.tool_call is None:
            raise ValueError("`args` goes with `tool_call`")
        return self

    def describe(self) -> str:
        if self.contains is not None:
            return f"contains {self.contains}"
        if self.not_contains is not None:
            return f"not_contains {self.not_contains}"
        if self.tool_call is not None:
            return f"tool_call {self.tool_call}" + (f" {self.args}" if self.args else "")
        return "no_tool_call"


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")
    owner: str  # the team that vouches for this task
    prompt: str = Field(min_length=1)
    system: str | None = None
    checks: list[Check] = Field(min_length=1)


class Golden(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tasks: list[Task] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> Golden:
        ids = [t.id for t in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("task ids must be unique")
        return self


def load_golden(path: Path = GOLDEN) -> list[Task]:
    return Golden.model_validate(yaml.safe_load(path.read_text())).tasks


@dataclass
class ToolCall:
    name: str  # connector.tool
    args: dict[str, Any]


@dataclass
class Output:
    """What a model answered, in the terms the checks use."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float | None = None
    error: str | None = None  # the call failed: nothing to check


def failed_checks(task: Task, out: Output) -> list[str]:
    """The checks this answer fails (empty: the task passes)."""
    text = out.text.lower()
    failures = []
    for check in task.checks:
        if check.contains is not None:
            ok = all(s.lower() in text for s in check.contains)
        elif check.not_contains is not None:
            ok = not any(s.lower() in text for s in check.not_contains)
        elif check.tool_call is not None:
            ok = any(
                call.name == check.tool_call
                and all(str(call.args.get(k)) == str(v) for k, v in (check.args or {}).items())
                for call in out.tool_calls
            )
        else:
            ok = not out.tool_calls
        if not ok:
            failures.append(check.describe())
    return failures


Model = Callable[[str, Task], Awaitable[Output]]


def parse_response(response: dict[str, Any]) -> Output:
    """An OpenAI-format chat completion → text and tool calls."""
    from app import connectors

    message = (response.get("choices") or [{}])[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):  # content parts
        content = "".join(p.get("text") or "" for p in content if isinstance(p, dict))
    calls = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        try:
            args = json.loads(function.get("arguments") or "{}")
        except ValueError:
            args = {}
        calls.append(
            ToolCall(
                connectors.tool_id(str(function.get("name") or "")),
                args if isinstance(args, dict) else {},
            )
        )
    usage = response.get("usage") or {}
    return Output(
        text=content if isinstance(content, str) else "",
        tool_calls=calls,
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
    )


async def live(model: str, task: Task) -> Output:
    """One real call, through the adapter the gateway would use for this model."""
    from app import config, connectors, main
    from app.providers import UnsupportedProvider
    from app.providers.base import NotConfigured, ProviderError, UnsupportedRequest
    from app.schemas import ChatCompletionRequest

    try:
        adapter, upstream_model, target = main.resolve_target(model)
    except (LookupError, UnsupportedProvider) as exc:
        return Output(error=f"{type(exc).__name__}: {str(exc)[:200]}")
    messages = [{"role": "system", "content": task.system}] if task.system else []
    messages.append({"role": "user", "content": task.prompt})
    body = ChatCompletionRequest.model_validate(
        {
            "model": model,
            "messages": messages,
            "tools": connectors.tool_definitions(),
            "max_tokens": MAX_TOKENS,
        }
    )
    started = time.perf_counter()
    try:
        response = await adapter.chat(upstream_model, body.upstream_body(upstream_model))
    except (ProviderError, NotConfigured, UnsupportedRequest) as exc:
        # No API key, a provider outage, a request the provider can't express: the task
        # couldn't be run, which is not the same as the model failing it.
        return Output(error=f"{type(exc).__name__}: {str(exc)[:200]}")
    out = parse_response(response)
    out.latency_ms = round((time.perf_counter() - started) * 1000)
    out.cost_usd = config.pricing.cost(target, out.prompt_tokens, out.completion_tokens)
    return out


async def offline(model: str, task: Task) -> Output:
    """Deterministic stand-ins for a model, so the harness can be checked without keys.

    stub/baseline answers every golden task correctly. stub/regressed never calls a tool
    and does what an injected instruction says: what a worse model looks like."""
    wants = [c for c in task.checks if c.tool_call is not None]
    if model == "stub/regressed":
        injected = next((s for c in task.checks for s in c.not_contains or []), "")
        return Output(text=f"{injected} I can't look that up.", cost_usd=0.0)
    if wants:
        calls = [ToolCall(c.tool_call or "", dict(c.args or {})) for c in wants]
        return Output(tool_calls=calls, cost_usd=0.0)
    text = " ".join(s for c in task.checks for s in c.contains or []) or "Done."
    return Output(text=text, cost_usd=0.0)


@dataclass
class TaskResult:
    task: str
    owner: str
    current_failed: list[str]
    candidate_failed: list[str]
    current_error: str | None
    candidate_error: str | None

    @property
    def regression(self) -> bool:
        """The current model passes and the candidate doesn't."""
        current_ok = not self.current_failed and not self.current_error
        return current_ok and bool(self.candidate_failed or self.candidate_error)

    @property
    def inconclusive(self) -> bool:
        return bool(self.current_error or self.candidate_error)


@dataclass
class Report:
    current: str
    candidate: str
    runs: int
    results: list[TaskResult]
    totals: dict[str, dict[str, Any]]  # model → tokens, cost, latency

    @property
    def regressions(self) -> list[TaskResult]:
        return [r for r in self.results if r.regression]

    @property
    def exit_code(self) -> int:
        if self.regressions:
            return 1
        return 2 if any(r.inconclusive for r in self.results) else 0


async def evaluate(
    tasks: list[Task], current: str, candidate: str, model: Model, runs: int
) -> Report:
    """Every task on both models, `runs` times. A task passes only if every run passes:
    a model that gets it right four times in five is not one to gate a rollout on."""
    totals: dict[str, dict[str, Any]] = {
        m: {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cost_usd": 0.0,
            "latency_ms": [],
            "priced": True,
        }
        for m in {current, candidate}
    }

    async def run(name: str, task: Task) -> tuple[list[str], str | None]:
        failed: list[str] = []
        for _ in range(runs):
            out = await model(name, task)
            if out.error:
                return [], out.error
            t = totals[name]
            t["prompt_tokens"] += out.prompt_tokens
            t["completion_tokens"] += out.completion_tokens
            t["latency_ms"].append(out.latency_ms)
            if out.cost_usd is None:
                t["priced"] = False
            else:
                t["cost_usd"] += out.cost_usd
            failed = sorted(set(failed) | set(failed_checks(task, out)))
        return failed, None

    results = []
    for task in tasks:
        cur_failed, cur_error = await run(current, task)
        cand_failed, cand_error = (
            (cur_failed, cur_error) if candidate == current else await run(candidate, task)
        )
        results.append(
            TaskResult(task.id, task.owner, cur_failed, cand_failed, cur_error, cand_error)
        )
    return Report(current, candidate, runs, results, totals)


def _cell(failed: list[str], error: str | None) -> str:
    if error:
        return "ERROR"
    return "FAIL" if failed else "pass"


def render(report: Report) -> str:
    """A table for the terminal and for the pull request that changes the model."""
    width = max(len(r.task) for r in report.results) + 2
    col = max(len(report.current), len(report.candidate), 9) + 2
    lines = [f"{'task':<{width}}{report.current:<{col}}{report.candidate:<{col}}"]
    for r in report.results:
        note = "  <- REGRESSION" if r.regression else ""
        lines.append(
            f"{r.task:<{width}}{_cell(r.current_failed, r.current_error):<{col}}"
            f"{_cell(r.candidate_failed, r.candidate_error):<{col}}{note}"
        )
        for who, failed, error in (
            ("current", r.current_failed, r.current_error),
            ("candidate", r.candidate_failed, r.candidate_error),
        ):
            detail = error or "; ".join(failed)
            if detail:
                lines.append(f"{'':<{width}}  {who}: {detail}")
    lines.append("")
    for name in dict.fromkeys((report.current, report.candidate)):
        t = report.totals[name]
        calls = len(t["latency_ms"])
        avg = round(sum(t["latency_ms"]) / calls) if calls else 0
        cost = f"${t['cost_usd']:.4f}" if t["priced"] else "unpriced"
        lines.append(
            f"{name}: {calls} calls, {t['prompt_tokens']} in / {t['completion_tokens']} out "
            f"tokens, {cost}, {avg} ms average"
        )
    passed = sum(not r.candidate_failed and not r.candidate_error for r in report.results)
    lines.append(
        f"\n{passed}/{len(report.results)} tasks pass on the candidate "
        f"({report.runs} run{'s' if report.runs != 1 else ''} each); "
        f"{len(report.regressions)} regression(s)"
    )
    return "\n".join(lines)


def turn_up_check(candidate: str) -> str | None:
    """A model can't be evaluated for turn-up before someone has said how its data is
    handled: every target behind the candidate needs a rule in config/policy.yaml."""
    from app import config

    try:
        chain = config.registry.resolve(candidate)
    except KeyError:
        return f"'{candidate}' is not a model or alias in config/models.yaml"
    missing = [t for t in chain if config.access.model_rule(t) is None]
    if missing:
        return f"no data-handling rule in config/policy.yaml for {', '.join(missing)}"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--current", required=True, help="the model in use today")
    parser.add_argument("--candidate", required=True, help="the model being considered")
    parser.add_argument("--offline", action="store_true", help="deterministic stubs, no keys")
    parser.add_argument("--runs", type=int, default=1, help="repeats per task (all must pass)")
    parser.add_argument("--golden", type=Path, default=GOLDEN)
    parser.add_argument("--json", type=Path, help="also write the report here as JSON")
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs must be at least 1")

    tasks = load_golden(args.golden)
    if not args.offline:
        for name in dict.fromkeys((args.current, args.candidate)):
            if problem := turn_up_check(name):
                print(f"cannot evaluate: {problem}", file=sys.stderr)
                return 2
    report = asyncio.run(
        evaluate(tasks, args.current, args.candidate, offline if args.offline else live, args.runs)
    )
    if args.offline:
        print("OFFLINE: deterministic stubs. This checks the harness, not a model.\n")
    print(render(report))
    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "current": report.current,
                    "candidate": report.candidate,
                    "runs": report.runs,
                    "offline": args.offline,
                    "regressions": [r.task for r in report.regressions],
                    "results": [asdict(r) for r in report.results],
                    "totals": report.totals,
                },
                indent=2,
            )
        )
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
