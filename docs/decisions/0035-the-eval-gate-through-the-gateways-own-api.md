# 0035 — The eval gate runs through the gateway's own API

- **Status:** accepted
- **Date:** 2026-10-13
- **Phase:** after 20

## Context
The eval gate (ADR 0030) compares a candidate model with the current one on golden
tasks. Its harness calls the provider adapters in-process, which is right for a laptop
and wrong in two ways for the run that decides a model change:

- It needs the provider keys in the job that runs it, and its calls pass none of the
  gateway's controls: no budget, no data class, no decision record. The one workload that
  is allowed to call a model nobody has approved yet was the one that left no trace.
- It measures the adapter, not what an application gets from the gateway.

Since then the harness was extracted into a reusable GitHub Action,
[model-eval-action](https://github.com/AndrewTtofi/model-eval-action), which calls any
OpenAI-format API over HTTP and reports quality, tool use, cost and latency. The gateway
is such an API. The two had never been used together.

## Options considered
1. **Keep the in-process harness in the workflow** — nothing to build; the gaps above stay,
   and the action is something this project publishes and doesn't use.
2. **Delete the in-process harness, use only the action** — one code path, but a local
   comparison would then need a running stack, and the turn-up check (is the candidate
   configured and classified?) is this gateway's business, not a generic action's.
3. **The workflow uses the action against a gateway it starts; the in-process harness
   stays for local runs and the offline check.**

## Decision
Option 3.

- **The `evals` workflow starts the gateway, creates a key, and points the action at
  `http://127.0.0.1:8000/v1`.** The eval is a client like any other.
- **One source of truth for the tasks.** `python -m evals.export` writes what the action
  reads from this repository's own files: the golden tasks (with tool names as a model
  is offered them, `connector__tool`), the connector tools as function definitions, and
  the two models' prices from `pricing.yaml`. Nothing is kept twice.
- **The turn-up check runs first** and writes nothing if it fails: the candidate must be
  in `models.yaml` and have a data-handling rule in `policy.yaml`.
- **A key for the run:** any model, because a candidate is by definition not on a tier's
  list yet, with a budget of its own (five dollars by default). The gateway enforces the
  ceiling on what an eval can spend; the workflow doesn't have to.
- **Declared data class `public`,** because the golden tasks hold nothing sensitive. A
  candidate whose rule allows only public data can then be evaluated at all.
- **The response cache is bypassed.** An alias may have one, and a repeat (`runs`) must be
  the model's answer, not a stored one.
- **Name a `provider/model`, not an alias,** when the question is about one model. An
  alias is a fallback chain: if its primary is down, the eval would grade the fallback.
- **Every call is checked against the decision log** at the end of the run: as many
  `allow` records for the run's key as the action says it made calls.
- **On a pull request that touches the harness, the golden tasks or the model config,**
  the same path runs with the fake provider's models. No keys, no spend. It shows the
  wiring holds and says nothing about a model, so it posts no comment.
- **The action is pinned to a commit.** It is the only action in this repository that
  isn't GitHub's, and it is this project's own.

## Consequences
- An eval leaves the same evidence as any other use of a model: who (the run's key), which
  model, which data class, which policy version.
- The run measures the gateway's path: translation, the injection filter, limits. That is
  what applications get. It is not a benchmark of the provider alone, and the latency
  includes the gateway's few milliseconds.
- Two harnesses check the same golden file with the same rules, one in each repository.
  A new kind of check has to be added to both. `evals.export` validates the file with
  the local one before handing it to the other, which catches a file only one of them
  understands, not a difference in how they judge.
- The workflow needs Docker and a minute to start the stack.
- The injection filter's mode comes from the key's tier. A tier that blocks would turn
  the injected-document task into an error, not an answer.
- **Run for real so far:** locally on 2026-10-13, through the gateway in Docker, with the
  fake models and with a local model served by Ollama (the `local` alias against its own
  primary: the same three of six tasks passed on both sides, twelve calls, twelve
  decision records). On pull requests in CI with the fake models. **Not yet between two
  hosted models:** that needs provider keys and someone to approve the spend.
