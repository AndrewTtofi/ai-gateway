# SESSION.md

Running log of where the work is. Updated at the end of every session
(by you, or by Claude via `/end-session`). Newest session on top.
Keep "Current state" short and always true.

## Current state
- **Phase:** 17, short-lived connector credentials, done (ADR 0031). Phases 13–16 are merged to `main`. This repo is `ai-gateway`, seeded from llm-gateway; llm-gateway itself is left untouched.
- **Branch:** `phase-17-connectors`. Remote `github.com/AndrewTtofi/ai-gateway` (private until phase 19). Phase pull requests are rebase-merged once CI is green.
- **Status:**
  - `make test`: 781 pass and 42 skip without Redis and Postgres; lint clean.
  - CI runs: tests with fuzzing, the policy check, the offline eval check, pip-audit, a Trivy image scan, an end-to-end job on the production compose stack, and an identity job against authentik.
  - Verified live on 2026-10-12: the `github` connector against a real GitHub App, on the host, inside the dev container (key mounted read-only) and through `POST /v1/tools/github/...` (engineer allowed; another role and `confidential` data refused).
  - The local dev database is at migration 0010.
- **Released:** nothing from this repo. llm-gateway's last release was v1.3.1.
- **Next up:**
  - Phase 18, spend exceptions and hardening (`PLAN.md`): soft and hard caps per principal, the exception queue.
  - A live eval between two hosted models needs provider keys and permission to spend.
- **Blockers:** none. Owner: confirm the Anthropic API key was rotated; `OPENAI_API_KEY` is still empty.
- **Open questions:**
  - No image has been released from this repo; production files still name the llm-gateway image until v2.0.0.
  - `docker-compose.prod.yml` passes neither the identity settings (OIDC, SCIM) nor the GitHub App's to the gateway yet.
  - The wiki workflow needs the wiki's first page created once in the web UI.
  - The optional guardrail classifier isn't covered by the secondary-use ceiling yet (ADR 0028).
  - Okta's and Entra's SCIM forms are covered by unit tests from their docs, not by a live tenant.
  - A key's team only gives it a role if the team is also in `limits.yaml` (`teams:`), and the shipped file has none: service keys can't reach role-limited tools out of the box. The live check of the GitHub tool added two teams temporarily.
  - The GitHub connector uses one installation; several organisations would need one per repository owner.
  - This machine has a stale `ghcr.io` login in Docker, which makes public pulls from ghcr fail; `docker logout ghcr.io` clears it.

---

## Session log

### 2026-10-12 — Session 20 (GitHub connector)
**Did**
- A GitHub App (read-only pull requests), created through GitHub's manifest flow.
- The `github` connector: app JWT → one-hour installation token, scoped down and cached in memory; two read-only tools; arguments validated before they reach a URL.
- Onboarded in the policy for engineers, up to `internal` data. ADR 0031 and a Connectors page with the walkthrough.
- Checked live against GitHub, in the container, and through the gateway.

**Learned**
- A GitHub App has three credentials with three lifetimes: the private key (until rotated), the app JWT (minutes, only good for asking for tokens) and the installation token (an hour, the only one that reads data). Only the first is stored.
- An installation token reads public repositories too. The installation limits private ones.
- The live call through the gateway returned 502 for a bad repository name. A caller's mistake is a 400; the mocked tests had only checked that an error was raised.

### 2026-10-12 — Session 19 (local verification, evals)
**Did**
- Ran everything locally in Docker for the first time in this repo: the full suite against real Redis and Postgres, both end-to-end scripts, and the dev stack with Ollama.
- The fuzz test found a 500 on content parts with a null or non-string `text`. Fixed at validation (400), with regression tests.
- The eval gate: harness, six golden tasks, tool schemas on connectors, offline CI check, manual live workflow.
- A live run between two local models caught two real regressions (a smaller model calling a tool to do arithmetic).

**Learned**
- A fuzz test that passes in CI has only not failed yet. Running it a few more times locally found a whole class of input the handlers assumed away; the fix belonged at the edge, not in each of the ten places that read the field.
- Gate on regression against the current model, not on an absolute score: a task both models fail is information, not a blocker.
- The first version of a golden check tested a wording ("rolled back") and failed a correct answer ("a rollback"). Check facts.

### 2026-10-12 — Session 18 (identity)
**Did**
- SCIM 2.0 server (`/scim/v2`) and a directory in Postgres (migration 0010); every change audited as operator `scim`.
- OIDC access tokens verified against the provider's JWKS; a user travels the request path in the same shape as an API key, so limits, budget and usage are per user.
- Groups are read from the directory on every request, not from token claims.
- authentik in the dev stack from a blueprint (`make idp`), and `scripts/identity_e2e.sh` (join, move, leave) as a CI job.
- Chose authentik over a hosted tenant: no account, no tunnel, testable in CI.

**Learned**
- Reading the provider's source beat guessing: authentik sends the same hashed user id as the token's `sub` and as the SCIM `externalId`, removes group members with the path-filter form, and issues no client-credentials token unless the grant type is listed on the provider.
- "Groups in the token" and "groups in a directory" differ by exactly the token's lifetime. That gap is the whole reason for SCIM.

### 2026-10-12 — Session 17 (governance core)
**Did**
- `config/policy.yaml` with a Pydantic schema, cross-checked against the rest of the config; CI validates it.
- `Principal`: policy and audit see a subject with groups, roles, tier and team. API keys resolve to one.
- Data classes: declared per request, applied to the fallback chain before routing, and to the cache and judge.
- `POST /v1/tools/{connector}/{tool}` with a mock connector.
- Decision log: `decision_log` (migration 0009), the audit log line, `/admin/decisions`.
- 61 new tests.

**Learned**
- Checking only the first model of a chain is not enough: the request that matters is the one that falls back. Filtering the chain makes the eligible model's outage an error instead of a leak.
- Requiring a data-handling rule for every routable model turns "what does this vendor retain?" into a question CI asks at model turn-up.

### 2026-10-12 — Session 16 (governance baseline)
**Did**
- Created `ai-gateway` from llm-gateway's full history (branch `main` only, no tags, so no release ran) and renamed the project.
- Checked every commit, tag and pull request of llm-gateway for credentials before reusing the history: none found.
- ADR 0027: why one gateway, why this codebase is the base, and why not a submodule.
- Planned phases 13–19 in `PLAN.md`.

**Learned**
- Governance can't wrap a router from outside. Data-class rules have to filter the fallback chain itself, or a fallback can send data to a model that isn't allowed to hold it.

### 2026-10-06 — Session 15 (pre-deploy hardening)
**Did**
- **Performance:** profiled with py-spy under load and removed a per-chunk timeout context and avoidable Redis calls.
  - −9% CPU and −0.8 ms p50 per request, −12% CPU per stream.
  - Benchmark overhead: +2.79 ms p50, and +54 ms p95 TTFT at 200 streams (was +533).
- **Operations (ADR 0024):**
  - Alertmanager, and a runbook per alert;
  - daily pg_dump, restore.sh and `make restore-drill`;
  - budget alerts at 50/80/100%.
  - All verified on a local production stack.
- **Security (ADR 0025):**
  - pip-audit and Trivy in CI and in the release workflow. Trivy found 5 HIGH issues (pip's bundled packages, Debian `libpcre2`), fixed by removing pip and applying Debian updates.
  - Per-operator admin keys, the `admin_audit` log (migration 0008), failed-login limits, and network segmentation.
- **Test depth (ADR 0026):**
  - Hypothesis fuzzing found 2 bugs that gave 500s, now fixed.
  - Golden provider fixtures (from the documented formats) and a recorder for real ones.
  - `scripts/e2e_prod.sh` as a CI job.
- **Soak:** 60 minutes, 259 511 requests, 100% success, memory flat.

**Learned**
- A timeout context around every streamed chunk was ~10% of streaming CPU. One watchdog per stream does the same job.
- The vulnerabilities in a Python image are often in pip's vendored packages, which the app never uses. Remove pip after install.
- Fuzzing the HTTP entry points is cheap, and it caught crashes that hand-written tests never would have.

### 2026-10-06 — Session 14
**Did**
- Full security audit (background agent, PoCs). Nothing Critical. The confirmed findings:
  - High: unbilled reasoning, hang-ups and timeouts; one tenant opening shared breakers through pool exhaustion or long requests.
  - Medium: estimate gaps; usage rows dropped through int4 overflow; billing parameters passed through; CPU on unauthenticated large bodies; tail-only guardrail scan; shared-cache poisoning; spend lost during Redis outages.
  - Ten Lows.
- All fixed in one commit (ADR 0023), with migration 0007 and 40 regression tests (after a code review round). The audit PoCs re-run:
  - the breaker stays closed;
  - a bad key with a 32 MB body costs 0.01 s (was 0.25 s);
  - a 950k-message body is rejected in 0.38 s (was 4.55 s).
- Docs: Security (audit summary), Keys/limits, Routing, Cache, Quality and safety, Production, Configuration, Providers, Observability, API reference, FAQ, Glossary, README.
- Merged #10 → #14 in order. Each squash merge made the next PR conflict. Merging `main` into the branch with `-X ours` re-added definitions the branch had moved (duplicates in `app/config.py`, `app/main.py`), so each branch was restored to its reviewed tree before merging. `main` equals the final phase 10 tree.
- Published the wiki to the Wiki tab; added the `wiki` sync workflow.
- Released v1.3.0 (1.2.0 skipped: its work shipped in 1.3.0). Added the roadmap.

**Learned**
- A shared, fleet-wide circuit breaker must only count failures the provider caused. Pool waits and request-length timeouts are caused by clients.
- A request with no usage must be billed on time as well as on bytes relayed: providers bill reasoning they never stream.
- Squash-merging the bottom of a stacked PR chain makes the next PR conflict. `merge -X ours` can still duplicate moved code: always diff the result against the branch's reviewed tree.
- The GitHub Wiki is a separate git repo, created only when the first page is made in the web UI.

### 2026-10-06 — Session 13 (continued)
**Did**
- Phase 10 (`phase-10-extensions`), ADRs 0017–0022:
  - policy routing (`auto`);
  - response cache, exact and semantic;
  - self-healing: probes, quarantine, alerts;
  - A/B routing;
  - prompt-injection filter;
  - LLM-as-judge.
- Live: `auto` picked Haiku for cost and Opus 5.5 for quality; an injection was logged by rule name; all 25 dashboard queries ran.
- Code review fixes (18 items + nits):
  - **Quarantine:** only 401 and quota now.
  - **Cache:**
    - keys use a denylist and include routing hints;
    - the semantic index is partitioned, expires and evicts;
    - the threshold is a true cosine similarity;
    - embeddings time out after 2 s;
    - multimodal requests are exact-only, and `n > 1` isn't cached.
  - **Policy:** `allow_pin` and `allowed_hints`; concurrent breaker reads; policy candidates are probed.
  - **Self-healing:** probe timeouts count as failures; half-open isn't alerted; webhooks are async.
  - **Guardrails:** bounded scan; NFKD and confusables; the block response hides rule names.
  - **Load-time checks** for judge, classifier and embedding references.
- Wiki and README for phases 9–10 (5 new pages). `make test` 539.

**Learned**
- Cache keys built from an allowlist of fields silently collide when the request model allows extras: use a denylist
- Redis VSIM scores are (1 + cosine) / 2, not cosine
- Quarantining on a single 403/404 lets one request take a model away from every tenant

### 2026-10-06 — Session 13
**Did**
- Phase 9 (`phase-9-gaps`), ADRs 0013–0016:
  - prompt caching and thinking through `/v1/messages`;
  - OpenAI Responses API adapter (Astra and Sol call tools again);
  - long-context tiers, cache-write prices and off-peak pricing;
  - `PATCH /admin/keys`, teams with budgets, body limit;
  - release workflow (GHCR, SBOM, provenance), `docker-compose.prod.yml`, retention job, read-only Grafana role.
- The production stack was brought up locally from a local image, twice (before and after review):
  - TLS, 2 replicas, per-replica balancing;
  - operator listener, read-only role, retention;
  - secrets scoped per container.
- Code review fixes:
  - **Caddy:** dynamic upstreams, plus an operator listener in place of `remote_ip` matching.
  - **Leaks:** usage extension fields leaking to OpenAI clients; OpenAI clients could turn thinking on.
  - **Requests:** system prompts arriving as part lists at non-Anthropic providers; truncated chunked bodies on disconnect.
  - **Database role:** the password is sent as a SCRAM verifier, through driver-level DDL.
  - **Validation:** off-peak windows checked and billed at request start; batch size 0 rejected.

**Learned**
- SQLAlchemy `text()` treats `:word` inside string literals as bind parameters; run DDL with `exec_driver_sql`
- Source-IP allowlists are unreliable behind proxies and Docker's userland proxy: put operator endpoints on a separate listener
- redis-py's VSIM raises AttributeError when the vector set doesn't exist yet

### 2026-10-05 — Session 12
**Did**
- Researched subscription terms across providers (wiki: Subscriptions-and-Terms). No credential reuse. Pass-through is documented for Claude Code and Codex but not built
- Wiki in `docs/wiki/` (17 pages incl. a multi-app use case), `scripts/publish_wiki.sh`
- Model catalog (`config/catalog.yaml`), `GET /v1/catalog` (prices, capabilities, quality, breaker, live stats), `make prices` sync with review, weekly drift workflow. ADR 0011

**Learned**
- No provider exposes prices via API; LiteLLM (provider IDs, capabilities) + OpenRouter (cross-check) agree on current prices
- The sync immediately found missing OpenAI cached-input prices (cache reads were billed at full input price)

### 2026-10-05 — Session 11
**Did**
- v1.0.0 merged, tagged, GitHub release. Commits from now on authored only by the owner (no co-author trailer)
- `/v1/messages` (Anthropic Messages API inbound, translated at the edge; ADR 0010), `count_tokens`, `x-api-key` auth,
  pluggable stream wire format. 21 tests incl. the real Anthropic SDK (streaming tool calls, typed errors, fallback Claude → OpenAI-compatible)
- Ran real Claude Code against the dev stack (`fake/ok`): found it sends `role: "system"` inside `messages` → now supported

**Learned**
- Anthropic streams numbered content blocks with open/delta/stop events; OpenAI streams flat deltas → open a new block when the output kind changes
- Usage comes last in OpenAI streams, so Anthropic's message_delta must wait for the end
- Dropping `cache_control` in translation means no prompt caching: matters a lot for agent workloads

### 2026-10-05 — Session 10
**Did**
- Phase 7: README rewrite (leads with "OpenAI-compatible = the client API; Claude is the
  default primary"), mermaid architecture, deploy notes; `/readyz`; Grafana image renderer
  (profile `screenshots`) → `docs/img/dashboard.png`; `docs/WRITEUP.md`; v1.0.0

**Learned**
- Grafana 13 refuses to start with a renderer URL and the default renderer token: set a shared one
- Readiness must not fail on shared deps (Redis/Postgres) or one blip removes every replica
- Demo traffic must use realistic ratios: priced calls at 100% just show budgets exhausted

### 2026-10-05 — Session 9
**Did**
- Phase 6: bench rig (mock LLM replaying measured Claude timing, 2nd replica, Toxiproxy),
  own load generator (per-chunk timing, open/closed loop), 9 scenarios, SLO burn-rate alerts,
  docs/RESULTS.md + README numbers. ADR 0009

**Results (laptop, after the review's methodology fixes)**
- Overhead +2.6 ms p50 / +3.8 ms p95 (paired), +4.2 ms to realistic TTFT (0.8%)
- One replica within 10% of direct up to 200 streams, core saturates at 400; 1→2 replicas 445→614 req/s
- Rate limits -0.06% across 2 replicas; budget overshoot +3.4%; breaker: 5 requests paid, 0 client errors
- 100% served through provider/Redis/Postgres failures; 90/90 streams survived SIGTERM; fast-burn alert fired

- code-reviewer found the *method* flawed in places: budget overshoot and token-estimate
  numbers were mock artifacts (mock ignored max_tokens), the +8 ms TTFT was within noise,
  capacity clients ran in lockstep, breaker recovery was set by the scenario's timing,
  stale-if-error was never exercised, the TTFT SLO measured the serving attempt only,
  bench keys could spend real money. All fixed; whole suite re-run; report derives every
  claim from the data

**Learned**
- Pair comparisons (same seed → same mock delays) to remove the mock's own noise
- Prometheus keeps data in an anonymous volume: `--force-recreate` needs `-V` for a clean slate
- Measure the rig before the system: direct-to-mock baselines showed the load generator, not
  the gateway, saturating first; alternating runs exposed a cold-start "2.7× scaling"
- Report latency over *all* responses, and align fault injection by wall clock
- A stalled provider costs attempts × first_token before fallback (retries on timeouts)
- Silent no-op string edits: always assert a replacement matched


### 2026-10-05 — Session 8
**Did**
- Phase 5: usage_log (Postgres, background batch writer), Prometheus metrics (bounded labels),
  Grafana dashboard as code + provisioned datasources, JSON logs with request ids,
  cached-token pricing. ADR 0008. Demo keys `team-a`, `team-b` left in the dev DB so the
  dashboard has data

- code-reviewer pass: 2 blockers (unknown models recorded as 200 with the client's model
  string as a metric label → unbounded series; one oversized value sank a 100-row batch)
  + 11 should-fix (shutdown lost the in-progress batch, failures flagged as fallbacks,
  mid-stream disconnects as clean 200s, fallback latency blamed on the healthy provider,
  /metrics on the public port, …). All fixed with tests; usage_log.id → BIGINT
- Re-verified: all 17 panels return data after 400 mixed requests

**Decided**
- Spend per key from Postgres (exact, unbounded keys), not Prometheus labels (cardinality)
- Usage writes never block requests: queue + batch, drop and count when Postgres is slow/down
- Pure ASGI middleware for request ids (BaseHTTPMiddleware would break disconnect detection)

**Learned**
- Grafana's /api/ds/query runs a panel's real queries: a good DoD check that a dashboard
  shows data, not just that it loads


### 2026-10-05 — Session 7
**Did**
- Phase 4: API keys (hashed, Postgres via Alembic, cached lookup, admin API), token-aware rate
  limits (two Redis token buckets per key, one Lua script, estimate → reconcile), 429s with
  OpenAI-style headers, monthly USD budgets, metered streams. Split main.py into modules.
  ADR 0006 (keys), 0007 (limits/budgets)

**Decided**
- SHA-256 for keys (random, not passwords); 30 s cache → revocation within 30 s
- No tiktoken: char estimate + reconciliation with real usage
- Budget/limits fail open when Redis is down; auth fails closed when Postgres is down (cached keys keep working)

- code-reviewer pass: 2 control bypasses (token debt forgiven by a 120 s Redis TTL → ~10× the
  limit with huge prompts; hanging up before the answer cost $0) + 10 should-fix (budget
  overshoot by everything in flight, key-flood cache eviction, no Postgres timeouts, refusal
  iterations unbilled, …). All fixed with tests; the three key fixes mutation-checked

**Learned**
- `x += await f()` loses updates across coroutines — Python reads `x` before suspending
- A load test must saturate the limiter, or it measures the client


### 2026-10-05 — Session 6
**Did**
- Phase 3: router with retries/backoff/jitter + fallback chains, circuit breaker per target in
  Redis (Lua) with memory store, chaos provider + aliases, stream_total deadline, pool limits,
  retiring old adapters after a grace period. ADR 0004 (retries/fallback/breaker), 0005 (mid-stream)

**Decided**
- Breakers per `provider/model`, not per provider (Anthropic overload is per model)
- Fail classification: client fault returns, gateway fault falls back without retry, transient retries then falls back
- No mid-stream fallback: error in-band, breaker learns (ADR 0005)

- code-reviewer pass: 1 blocker (committed-stream wrapper never closed the upstream if the client
  left during the first chunk — a Phase 1 regression), probe slot leaks, stragglers closing open
  breakers, Redis latency on every request, error ranking, unbounded breaker state via direct
  model names, fake provider reachable in prod, CROSSSLOT keys, grace period too short. All fixed
  with tests; blocker + probe release mutation-checked
- Live demo on chaos-blip: open → half-open probe → closed, 137/137 requests succeeded

**Learned**
- Unit tests silently used the real local Redis through the app lifespan — test config must pin `store: memory`
- `aclose()` on an async generator that never started doesn't run its `finally`; a wrapper that
  must clean up has to be a class, not a generator
- A breaker must only let the probe close it, or in-flight stragglers make it flap


### 2026-10-05 — Session 5
**Did**
- Phase 2 on `phase-2-providers`: Anthropic adapter via official SDK, translation module with unit tests,
  capability flags per model in config, refusal fallback (beta), `/v1/models` + SIGHUP + safe reload
- code-reviewer pass found 3 blockers + 9 should-fix, all fixed with tests (mutation-checked):
  unmapped stream transport errors, assistant-first/system-only requests, malformed input → 500,
  temperature+top_p, `""` args for no-arg tools, declined model's tool calls leaking past a
  refusal fallback, usage iterations, schema transform, user hashing, truncated streams,
  first_token vs stream_idle, image media types
- 115 tests; live tests ready but skipped (no key)

**Decided**
- Official `anthropic` SDK over raw httpx (ADR 0003); mocks via `httpx2.MockTransport`
- Repo goes public. History rewritten so commits use the GitHub noreply email; recreated as a fresh
  public repo (PR numbers restarted — `#N` references in older notes point at the archive)
- Never commit with the personal email; provider keys only in `.env`, never in chat or the repo

**Learned**
- Model differences (sampling params, forced tool choice, effort) belong in config, not `if model ==`
- SDK raises a mid-stream `error` event as `APIStatusError` with status 200


### 2026-10-05 — Session 4
**Did**
- Phase 1 on `phase-1-proxy`: `/v1/chat/completions` pass-through, SSE streaming, disconnect cancellation, OpenAI error mapping
- 47 unit/integration tests + 2 e2e; DoD met: `openai` SDK streams from Ollama through the running gateway
- ADR 0002: streaming error semantics, disconnects, what provider error text clients may see
- code-reviewer pass: fixed disconnect during first-token wait, upstream left open on slow-client
  disconnect (`SSEResponse`), provider error text leaking key fragments/org IDs, raw 500s on
  malformed upstream bodies, swallowed outside cancellation, 404/insufficient_quota mapping

**Learned**
- In SSE the HTTP status is final once headers go out — so pull the first chunk before committing to 200
- Streamed responses carry deltas, not the full message; usage only arrives (as a final chunk with empty `choices`) when `stream_options.include_usage` is set

**Deferred to Phase 3** (from review)
- Per-provider connection-pool limits + short pool timeout; overall stream deadline;
  close reload-retired adapters after a grace period; read retry statuses from config

**Next**
- Phase 2: Anthropic adapter (translation notes in the Claude discussion: thinking always-on, no forced tool_choice, refusal stop reason)


### 2026-10-05 — Session 3
**Did**
- Dependency audit: installed versions were all latest, but nothing was pinned (only `>=` floors)
- Added pip-tools locks with hashes, raised floors, pinned Docker images, Dependabot (branch `chore/pin-deps`)

**Decided**
- Kept Redis 7 / Postgres 16 / Python 3.12 majors; Postgres 18 needs a pgdata migration — separate decision
- Standard pip-tools layout (`.in` → `.txt`) so Dependabot can regenerate locks

- Merged PR #1. Then major upgrades on `chore/major-upgrades`: Python 3.14.8, Redis 8.10.2 (8.8.3 in #5, then Dependabot #2), Postgres 18.6
- Postgres 18 image stores data under `/var/lib/postgresql/18/docker`; mount moved to `/var/lib/postgresql`.
  Old PG16 volume had 0 tables — dumped (`pg_dumpall`) then removed and recreated
- Local `.venv` rebuilt on Python 3.14.8 (via `uv python install`, no system changes)

**Learned**
- Upgrading Postgres majors in Docker is never just a tag bump: data dirs are version-specific (pg_upgrade or dump/restore)

**Next**
- Merge `chore/major-upgrades`, then Phase 1


### 2026-10-05 — Session 2
**Did**
- Created `.venv`, installed dev deps; `make test` / `make lint` run
- Fixed ruff E501 failures in `app/main.py` (CI would have been red)
- Admin key check now uses `secrets.compare_digest` (timing-safe)
- HTTP errors returned in OpenAI error shape
- Added `tests/test_api.py` (healthz, /v1/models, /admin/reload auth)
- CI lint step now runs mypy too, matching `make lint`

- Created `.env` from example with a generated `GATEWAY_ADMIN_KEY` (API keys still empty)
- `make up`: all 5 services healthy; `/admin/reload` works with the admin key
- Pulled `ollama/llama3.2:3b`; verified the container reaches Ollama via `host.docker.internal`
- Filled OpenAI models: fast → `gpt-6-luna`, balanced → `gpt-6.1-sol`, smart → `gpt-6-astra`
- Filled all prices in `pricing.yaml` (checked 2026-10-05); test that every chain model is priced
- `git init`, repo-local identity, pushed to private GitHub repo; first CI run green

**Decided**
- Repo is private for now; flip to public at Phase 7

**Next**
- Owner: add provider API keys to `.env`
- Phase 1 — pass-through proxy


### 2026-10-05 — Session 1
**Did**
- Chose project: LLM gateway (#11) as first AI engineering project
- Generated repo scaffold: PLAN, CLAUDE.md, agents, skills, Docker stack, config files

**Decided**
- Python + FastAPI (AI ecosystem language) over Go
- OpenAI-compatible API surface
- All models/prices in `config/` so models can be swapped without code changes

**Next**
- Phase 0 tasks in PLAN.md

---

<!-- Template for new entries (copy above the previous session)

### YYYY-MM-DD — Session N
**Did**
-
**Decided**
-
**Learned**
-
**Next**
-
**Blockers**
-
-->
