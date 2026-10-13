# CLAUDE.md

Instructions for Claude Code working in this repo.

## Project
Governed AI gateway for an enterprise AI platform: every request passes
identity → policy → spend → routing → audit (ADR 0027). Both halves are built: routing
(multi-provider routing, fallback, rate limiting, cost tracking, observability) and the
governance layer in front of it (identity, policy, spend exceptions, connectors, the
decision log, the eval gate). Clients call it in the OpenAI chat-completions or the
Anthropic Messages format; that is the wire format, not what the project is.
Full plan in `PLAN.md`. Current state in `SESSION.md`.

The owner is a platform/DevOps engineer learning AI engineering. When you write
AI-specific code (streaming, provider formats, token accounting), explain the
non-obvious parts briefly in the PR/summary so he learns from it.

## Every session
1. Read `SESSION.md` first. Work on the "Next up" item unless told otherwise.
2. Before ending: update `SESSION.md` and add entries to `CHANGELOG.md` under `[Unreleased]`.
   (Or run the `end-session` skill.)

## Stack
Python 3.14 · FastAPI · Pydantic v2 · httpx (async) · Redis 8 · Postgres 18 (SQLAlchemy 2 async + Alembic)
· Prometheus · Grafana · pytest + pytest-asyncio + respx · ruff + mypy · Docker Compose.

## Layout
```
app/
  main.py            FastAPI app, routes, the request pipeline
  config.py          settings (env) + YAML loaders for config/
  access.py          the access policy: roles, tiers, data classes, connectors
  oidc.py scim.py directory.py   identity: token checks, SCIM server, users and groups
  decisions.py       the decision log
  connectors.py      tools behind the policy (GitHub App, mock system of record)
  spend_exceptions.py  requests for more budget, approvals, grants
  schemas.py         OpenAI-format request/response models
  providers/         one adapter per provider; base.py defines the interface
  routing/           alias resolution, fallback chain, circuit breaker
  ratelimit/         token bucket and spend (Redis + Lua)
  observability/     metrics, logging, usage records
config/
  policy.yaml        who may do what; which model may hold which data  ← bump `version`
  models.yaml        aliases, models, fallback chains  ← ONLY place models are named
  pricing.yaml       $/1M tokens per model
  limits.yaml        tiers, soft and hard caps, teams
evals/               golden tasks and the eval gate for model changes
tests/               unit, integration, load/, e2e/
docs/                demo, architecture, runbook, enablement/, wiki/
docs/decisions/      ADRs (NNNN-title.md)
```

## Rules
- **Never hard-code a model name, provider URL, or price in `app/`.** Read from `config/`.
  Tests may use the `fake` provider or the `local` alias.
- All provider adapters implement `app/providers/base.py::ProviderAdapter`.
- Internal format is OpenAI chat-completions. Adapters translate in and out.
- Async everywhere on the request path. No blocking calls.
- Secrets only via env vars. Never log API keys, prompts in full, or auth headers.
  Log prompt length / hash, not content.
- New behaviour needs a test. Mock providers with `respx`; never call paid APIs in unit tests.
  Integration tests against real providers are marked `@pytest.mark.live` and skipped by default.
- Errors returned to clients use OpenAI's error shape: `{"error": {"message", "type", "code"}}`.
- Decision and audit records never hold content (prompts, answers, tool arguments, tokens).
- A policy change bumps `version` in `config/policy.yaml` and passes `make policy`.
- Non-obvious decisions → ADR in `docs/decisions/` using `0000-template.md`.

## Commands
```
make up          # start the stack
make down
make test        # unit + integration (no live calls)
make test-live   # includes live provider tests (costs money)
make lint        # ruff + mypy
make logs
make reload      # hot-reload config/*.yaml
make policy      # validate the access policy against the rest of the config
make decisions   # the latest governance decisions
make idp         # the stack plus an identity provider (authentik)
make evals current=A candidate=B   # the eval gate (costs money)
```

## Agents
Use the agents in `.claude/agents/` for their areas: `architect` to plan a phase,
`provider-adapter`, `test-writer`, `reliability-engineer`, `observability` to build,
`code-reviewer` before merging, `docs-keeper` to update docs.

## Commits
Conventional commits: `feat(router): add circuit breaker`, `fix(stream): …`, `docs: …`.
One phase per branch: `phase-N-short-name`.
