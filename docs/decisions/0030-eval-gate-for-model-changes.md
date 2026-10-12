# 0030 — An eval gate for model changes

- **Status:** accepted
- **Date:** 2026-10-12
- **Phase:** 16

## Context
Changing the model behind an alias is one line of YAML and a reload, and every app moves
at once. Nothing so far says whether the new model still does what those apps rely on:
answers the question, calls the right tool with the right arguments, leaves tools alone
when none is needed, and doesn't obey instructions hidden in a document.

Judge sampling (ADR 0022) scores live traffic after the fact. That finds a regression
when users already have it.

## Options considered

**What to measure**
1. **A benchmark score** — comparable across models, but about the benchmark's tasks, not
   this organisation's.
2. **A few golden tasks owned by the teams that use the gateway**, each with a pass/fail
   check — small, but about real work, and a failure names a task someone cares about.

**What counts as failing the gate**
1. **An absolute pass rate** (say 90%) — a hard task then blocks every model, and people
   delete the task.
2. **Regression against the current model** — the candidate must pass everything the
   current model passes. A task both fail blocks nothing, and still shows in the report.

**How the model is called**
1. **The provider's SDK directly** — simple, but not the path production takes.
2. **Through the gateway's own provider adapters** — the same translation of messages and
   tools that requests get.

**What CI runs**
1. **Real calls on every pull request** — costs money on each push and needs provider
   keys where forks could reach them.
2. **Offline in CI, live on demand** — CI checks the harness and the golden file with
   deterministic stubs; the real comparison is a workflow someone starts for a model change.

## Decision
- `python -m evals.run_evals --current A --candidate B` runs every task in
  `evals/golden.yaml` on both models, through the gateway's adapters, with the connector
  tools offered as tool definitions. It checks the real answer: text, and tool calls with
  their arguments.
- **Exit 1** when the candidate fails a task the current model passes. **Exit 2** when
  the comparison can't be made: an unknown model, a missing key, a provider error. A
  candidate that errors where the current model passes is a regression.
- **Model turn-up has an order.** The candidate must be in `models.yaml` and have a
  data-handling rule in `policy.yaml` (ADR 0028) before it can be evaluated at all.
- **`--runs N`:** a task passes only if every run passes.
- Checks are deliberately crude: substrings and tool calls. They are cheap, readable by
  the team that owns the task, and never need a second model to grade the first.
- The report includes tokens, cost and latency per model, so "better" can be weighed
  against "dearer" and "slower".
- **CI** runs the harness with `--offline`: the baseline stub must pass and a regressed
  stub must fail. The **`evals` workflow** runs the real gate by hand and writes the table
  to the job summary, to attach to the pull request that changes the model.

## Consequences
- A model change comes with evidence a reviewer can read in a minute.
- The gate is only as good as the tasks. Six tasks catch gross regressions (a model that
  stops calling tools, or starts calling them for arithmetic), not subtle ones. Teams have
  to add tasks when something slips through.
- Substring checks are brittle. A correct answer phrased differently can fail, so each
  check should test a fact, not a wording.
- Models aren't deterministic. `--runs` reduces the noise; it doesn't remove it.
- The offline CI step proves nothing about any real model, and says so in its output.
- A live run costs what twelve short calls per run cost on each model.
