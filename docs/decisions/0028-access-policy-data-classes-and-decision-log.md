# 0028 — Access policy, data classes and the decision log

- **Status:** accepted
- **Date:** 2026-10-12
- **Phase:** 14

## Context
The routing gateway knew one thing about a caller: an API key with a tier. An enterprise
platform has to answer more: who is this (which groups and role), what are they entitled
to, what kind of data is in the request, which models are allowed to hold that data, and
afterwards, what was decided and under which rules.

Three constraints shaped the design:

- Different models come with different retention terms, and a fallback chain mixes them.
- Features added earlier (response cache, judge sampling) copy prompt content to other
  stores and other models.
- The request path must not gain a hard dependency or noticeable latency.

## Options considered

**Where the rules live**
1. **A YAML file in the repo** — reviewed in pull requests, versioned, diffable, validated
   in CI. Needs a deploy or reload to change.
2. **A database with an admin UI** — changes without a reload, but a rule change is then
   not reviewed, and the history is whatever the UI logs.
3. **A policy engine (OPA/Rego)** — more expressive, but another service and another
   language for rules that are, here, lookups in three tables.

**When data class is checked**
1. **Check the first model in the chain** — simple, but a fallback then sends the data to
   a model that was never checked.
2. **Filter the whole chain before routing** — retries and fallback can only reach
   eligible models.

**Who says what class the data is**
1. **The caller declares it** (a header), with a default.
2. **The gateway classifies the content** — it would have to read every prompt, and a
   classifier is another model the data is sent to.

**How decisions are recorded**
1. **A synchronous database write per request** — nothing is lost, but Postgres becomes a
   hard dependency of every request and adds its latency to each one.
2. **Background batches, plus a log line written at once** — never slows a request; a row
   can be lost during a database outage, and the log line is then the remaining copy.

## Decision
- **Policy as a file.** `config/policy.yaml` holds group → role → tier, the data classes,
  a data-handling rule per model, and connector entitlements. It has a `version` that is
  stamped on every decision. A Pydantic schema rejects unknown keys, so a typo can't
  silently remove a rule. It reloads with the rest of the config, all or nothing.
- **Cross-checked at load.** Every tier it ranks must exist in `limits.yaml`, every
  connector it names must be registered, and **every routable model must have a
  data-handling rule**. A model can't be turned up without someone stating its retention.
  `python -m app.check_policy` runs this in CI.
- **Fail closed.** A model with no rule gets no data. A connector with no entry can't be
  called. A catch-all `"*"` rule exists for those who want the opposite, and it has to be
  written down.
- **One principal.** Policy and audit see a `Principal` (subject, kind, groups, roles,
  tier, team), never a credential. Today an API key becomes a service principal: its tier
  is the one an operator gave it and its team is its group. Users from the identity
  provider (phase 15) become principals too, and the rules don't change.
- **Tier entitlement stays where it was.** Which aliases a tier may call is still
  `allowed_aliases` in `limits.yaml`. The policy adds what was missing (roles, data
  classes, connectors) instead of a second way to say the same thing.
- **The caller declares the data class** with `x-gateway-data-class`; without it the
  policy's default applies. The response says which class was applied.
- **The chain is filtered, not the first target.** Ineligible models are removed before
  routing. With none left, the answer is 403 `data_class_not_eligible`. Policy-routed
  aliases (`auto`) choose only among eligible candidates.
- **Secondary use has its own ceiling.** The response cache and judge sampling are
  skipped above `secondary_use_max_data_class`.
- **Tools go through the same door.** `POST /v1/tools/{connector}/{tool}` checks that the
  connector is onboarded, the caller's tier and role, and the data class.
- **Decision log.** One record per decision, allow or deny: subject, roles, tier, team,
  action, data class, outcome, a fixed reason code, the policy version and the request id.
  Never content or tool arguments. It is written as a `gateway.audit` log line at once and
  as a `decision_log` row in background batches, through the usage log's writer.

## Consequences
- An access change is a reviewed diff, and an auditor can be shown the policy version
  behind any decision.
- **The data class is only as good as the caller's label.** The gateway enforces what is
  declared; it does not detect confidential text sent as `internal`. The default class and
  the per-tier injection filter are the backstops, and content inspection (PII detection)
  stays on the roadmap.
- A chain can shrink to one model, or to none, for sensitive data. That is the point, but
  operators need at least one eligible model per alias for each class they expect.
- With the shipped example policy, a default (`internal`) request no longer falls back to
  providers classified `public`-only.
- A decision row can be lost if Postgres is down while the queue is full. The log line is
  the second copy; shipping `gateway.audit` to durable storage is an operator's job.
- The optional guardrail classifier also sends prompt text to a model and is not yet
  covered by the secondary-use ceiling.
- Groups come from a key's team until phase 15 brings real ones from the identity provider.
