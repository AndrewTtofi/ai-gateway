# Roadmap

Where the gateway goes after v2.2. The routing gateway (phases 0–12) and the governance
layer (phases 13–20) are done; see `PLAN.md` and the
[CHANGELOG](https://github.com/AndrewTtofi/ai-gateway/blob/main/CHANGELOG.md). This page is
a plan, not a promise. Each item says **why** it matters, so the order can change.

## Next: make the governance claims stronger

| Item | Why |
|------|-----|
| **Run the eval gate between two hosted models** | It has only compared local models so far. The workflow is ready (the shared action, through this gateway, ADR 0035); it needs provider keys and approved spend |
| **Verify SCIM and OIDC against a hosted identity provider** (Okta, Entra) | Their request forms are covered from documentation, not a live tenant |
| **Run this gateway and the MCP server together** against a real Atlassian site | Done for sign-in, account linking, a Confluence read and a Jira search (2026-10-13). Still to confirm live: writes |
| ~~**Notify approvers** of a pending spend exception~~ | Done: a webhook on request and on decision (ADR 0034). Not yet: telling the requester, reminders for a request left pending |
| ~~**A retention job for the decision log**~~ | Done: `prune-decisions`, off until `DECISION_RETENTION_DAYS` is set |
| **Put the injection classifier under the data-class ceiling** | It is the one feature that can still copy prompt content to a model without that check |
| **Budget grants for teams,** not only individual callers | A team at its cap is still raised by editing config |
| **Token exchange (RFC 8693) for external tool servers** | The gateway would verify the user itself instead of taking a named service's word (ADR 0033) |
| **Detect sensitive data** instead of trusting the declared class | Enforcement is exact; classification is the caller's job |

## Run it for real

The gateway is built, tested and hardened, but it hasn't served real traffic yet.

| Item | Why |
|------|-----|
| **Pick a deploy target and deploy**: a VM with `docker-compose.prod.yml`, Cloud Run or ECS | Everything so far is verified locally. Real traffic shows what the benchmarks can't |
| **Live tests for every provider in the chains** (`OPENAI_API_KEY` and the rest) | The OpenAI fallbacks and the Responses API path have only been tested against mocks |
| ~~**Budget alerts at 50 / 80 / 100%**~~ | Done (ADR 0024) |
| **Dashboards from real usage**: tune guardrail weights, cache thresholds, `concurrent_requests` and `output_tokens_per_second` | All of these defaults are educated guesses until real traffic tunes them |
| ~~**Backups and a restore drill**~~ | Done: daily `pg_dump`, restore and `make restore-drill` (ADR 0024) |

## Wider API coverage

| Item | Why |
|------|-----|
| **`/v1/responses` inbound** (the OpenAI Responses API for clients) | OpenAI's newer SDK features and agent tools speak it. The gateway only uses it outbound today |
| **`/v1/embeddings`** through aliases, with limits and cost tracking | Apps doing retrieval call embeddings constantly; today they bypass the gateway |
| **Anthropic server tools and document blocks** on `/v1/messages` (rejected with 400 now) | Web search, code execution and PDF documents are common in Claude apps |
| **Batch APIs** (Anthropic and OpenAI, ~50% cheaper) for jobs that can wait | The cheapest tokens are the ones billed at half price |
| **Exact token counts** from the providers' count endpoints, for `count_tokens` and the estimate | The character estimate is rough. Billing is reconciled anyway, but rate limits and context fit use the estimate |

## Operating it at scale

| Item | Why |
|------|-----|
| **Kubernetes**: a Helm chart, HPA on in-flight requests, PodDisruptionBudgets | The compose file shows the pieces; most production platforms are Kubernetes |
| **OpenTelemetry tracing** (gateway → provider spans, trace ids into the usage log) | Metrics say *that* p95 rose; traces say *where* |
| **Fleet-wide concurrency limits** (in Redis), as an option | The per-key limit is per replica today (ADR 0023) |
| **Redis Cluster / Sentinel, and multi-region** | Limits fail open when Redis is down; high availability makes that rare |
| **An admin UI** for keys, the exception queue and the decision log | Today those are `make` targets and the API. People already sign in through the identity provider |
| **Per-team routing defaults** (policy, allowed models, guardrail level per team) | Teams have budgets; they should be able to have their own rules too |
| **Judge cost per team** (a judge budget, or charging samples to the alias's owners) | Judge calls are recorded (`_judge`) but are the operator's cost |

## Ideas: quality and safety

| Item | Why |
|------|-----|
| **PII detection and redaction** before prompts leave the gateway | The gateway sees every prompt; it's the natural place to keep emails, card numbers and IDs from providers |
| **Output scanning** (secrets, PII, unsafe links in answers) | The injection filter only looks at what goes in |
| **Prompt templates and versions** managed in the gateway | A/B tests already change prompts per arm; versioned templates make that reviewable |

## Not planned

- **Routing through consumer subscriptions** (Claude Pro/Max, ChatGPT Plus, Copilot). Their
  terms forbid serving them to other users or apps
  ([Subscriptions and terms](Subscriptions-and-Terms.md)).
- **Hard-coding models or prices in code.** Config stays the only place models are named.

## How items get done

Same as every phase so far:
1. Plan the work, and write an ADR for any non-obvious choice.
2. Do it on its own branch, with tests (mocked providers; live tests marked and opt-in).
3. Run a review.
4. Update the docs in the same PR. The wiki syncs itself after the merge.

To propose an item, open an issue or a PR against this page.
