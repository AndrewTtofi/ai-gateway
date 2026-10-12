# Governance: policy, data classes and the decision log

Every request passes **identity → policy → spend → routing → audit**. This page covers the
policy and audit steps. Limits and budgets are in
[Keys, limits and budgets](Keys-Limits-and-Budgets.md);
[ADR 0028](../decisions/0028-access-policy-data-classes-and-decision-log.md) has the reasoning.

## The policy file

`config/policy.yaml` is the one reviewed place for entitlements and data handling. It answers
three questions:

| Question | Section |
|----------|---------|
| Who is this caller and what tier are they? | `group_roles`, `role_tiers`, `tier_rank` |
| Which models may see this request's data? | `data_classes`, `models` |
| Which tools may they call? | `connectors` |

It carries a `version`. Bump it with every change: the version is stamped on every decision,
so you can show which rules were in force at any moment.

Change it in a pull request. CI runs `python -m app.check_policy` (locally: `make policy`),
which fails when:

- the file doesn't parse, or has a key the schema doesn't know (a typo can't drop a rule);
- it names a tier that `limits.yaml` lacks, or a connector that isn't registered;
- **a routable model has no data-handling rule.** A model can't be turned up without
  someone stating what its provider retains.

`make reload` applies it. A broken file is refused and the previous policy stays live.

## Who: principals

Policy and audit never see a credential. They see a **principal**: a subject, its groups,
the roles those groups map to, a tier and a team.

- **A user** authenticates with an OIDC token. Their groups come from the directory the
  identity provider provisions, and the policy turns those groups into roles and a tier
  ([Identity](Identity.md)).
- **A gateway API key** is a *service* principal. Its tier is the one an operator gave it,
  and its team counts as its group, so the keys of team `compliance` get the role that
  group maps to.

## Data classes

A request declares what kind of data it carries:

```bash
curl localhost:8000/v1/chat/completions -H "Authorization: Bearer $GW_KEY" \
  -H 'x-gateway-data-class: confidential' -H 'content-type: application/json' \
  -d '{"model":"fast","messages":[{"role":"user","content":"Summarise this contract: …"}]}'
```

```python
client = OpenAI(base_url="http://localhost:8000/v1", api_key="gw_...",
                default_headers={"x-gateway-data-class": "confidential"})
```

Without the header, `default_data_class` applies. The response header
`x-gateway-data-class` says which class was applied.

Each model has a rule: what its provider retains, and the most sensitive class it may
receive.

```yaml
models:
  "anthropic/*": { retention: 30d,  max_data_class: internal }
  "ollama/*":    { retention: none, max_data_class: restricted }   # self-hosted
```

A rule is looked up by `provider/model`, then `provider/*`, then `"*"` if the policy has one.
A model with no rule gets no data.

### The fallback chain is filtered

An alias is a chain of models with different retention terms. Before routing, the gateway
removes the models that may not hold this request's class:

| Alias `fast` | `internal` request | `confidential` request |
|---|---|---|
| `anthropic/claude-haiku…` (30d) | first choice | removed |
| `openai/…` (30d) | fallback | removed |
| `ollama/llama3.2` (none) | last fallback | the only target |

So a retry or a fallback can never send data to a model that isn't allowed to hold it, and
an outage of the eligible model is an error rather than a leak. `x-gateway-excluded` says
how many targets were removed. When none is left the answer is `403
data_class_not_eligible`. Aliases with a routing policy (`auto`) choose only among eligible
models.

### Features that copy content

The response cache and judge sampling store or forward prompt text. They are skipped for
requests above `secondary_use_max_data_class`.

### What this does not do

The gateway enforces the class the caller declares. It does not read prompts to detect
confidential text labelled `internal`. Choose the default class with that in mind.

## Tools

Connectors expose external systems as tools, behind the same checks:

```bash
curl localhost:8000/v1/tools/mock_records/lookup_case -H "Authorization: Bearer $GW_KEY" \
  -H 'x-gateway-data-class: restricted' -H 'content-type: application/json' \
  -d '{"args":{"case_id":"C-1001"}}'
```

```yaml
connectors:
  mock_records:
    min_tier: standard
    max_data_class: restricted
    allowed_roles: [compliance, trader]   # empty = any role with the tier
```

A connector with no entry here can't be called, even if its code is deployed. Credentials
are minted per call by the connector, never stored in config. `mock_records` is a stand-in
for a system of record.

## The decision log

Every decision is recorded, allow or deny:

| Field | Example |
|-------|---------|
| `at`, `request_id` | joins `usage_log` and the JSON logs |
| `subject`, `subject_kind`, `subject_name` | `key:6f0c…`, `service`, `case-bot` |
| `roles`, `tier`, `team` | `["compliance"]`, `standard`, `compliance` |
| `action` | `model:fast`, `tool:mock_records.lookup_case` |
| `data_class` | `confidential` |
| `decision`, `code`, `reason` | `deny`, `data_class_not_eligible`, the message the caller got |
| `policy_version` | `2026-10-12.1` |
| `detail` | the eligible chain and what was excluded |

Never prompt text, completions or tool arguments.

Recorded codes: `ok`, `model_not_allowed`, `team_unknown`, `data_class_not_eligible`,
`budget`, `team_budget`, `prompt_injection_detected`, and for tools `connector_unknown`,
`tier_too_low`, `tier_unranked`, `role_not_allowed`, `tool_not_exposed`.

Each record is written twice:

- a `gateway.audit` JSON log line, at once;
- a `decision_log` row in Postgres, in background batches.

If Postgres is down the row is dropped and counted
(`gateway_decision_log_dropped_total`), the request is unaffected, and the log line is the
copy that remains. Ship `gateway.audit` to durable storage if the log is your evidence.

```bash
curl -s 'localhost:8081/admin/decisions?decision=deny&limit=50' \
  -H "Authorization: Bearer $GATEWAY_ADMIN_KEY"
```

```sql
-- Evidence for a month: every denial, with the rules that were in force.
SELECT at, subject_name, action, data_class, code, policy_version
FROM decision_log
WHERE decision = 'deny' AND at >= '2026-10-01' AND at < '2026-11-01'
ORDER BY at;
```

`gateway_decisions_total{decision,code}` counts them for dashboards and alerts.
