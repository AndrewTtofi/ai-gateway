# Runbook

How to operate the gateway's governance layer: who gets access, what they may use, what
they may spend, and how to show it afterwards. Written so that someone who didn't build
it can run it.

Alerts and incidents for the routing side (error budgets, circuit breakers, Redis,
backups and restores) are in the [alert runbooks](wiki/Runbooks.md). Deployment is in
[Production deployment](wiki/Production-Deployment.md).

Commands assume the repository root and the dev stack. In production, `/admin/*` is only
on the operator listener (`localhost:8081` on the host).

## Where things are

| What | Where |
|------|-------|
| Who may do what | `config/policy.yaml` (reviewed in pull requests, validated in CI) |
| Limits and budgets | `config/limits.yaml` |
| Models, aliases, fallback chains | `config/models.yaml` |
| Users and groups | The identity provider. The gateway's copy: `GET /scim/v2/Users`, `/Groups` |
| Decisions (allow and deny) | `make decisions`, `GET /admin/decisions`, table `decision_log` |
| Admin and provisioning changes | `GET /admin/audit`, table `admin_audit` |
| Usage and cost | Grafana, table `usage_log` |
| Spend exceptions | `GET /v1/spend/exceptions` (as an approver), table `spend_exceptions` |

## People

### Give someone access
1. In the identity provider, add them to a group that `config/policy.yaml` maps to a role
   (`group_roles`).
2. Nothing to do in the gateway: the provider pushes the user and the membership over SCIM.
3. Check: `curl -s localhost:8000/scim/v2/Users -H "Authorization: Bearer $SCIM_TOKEN"`
   lists them, and their first request appears in `make decisions`.

### Change what someone may do
Move them between groups in the identity provider. It applies on their next request; the
token they already hold keeps working, with the new entitlements.

### Remove someone
Deactivate them in the identity provider, or remove them from every mapped group. Their
next request gets `403 user_deactivated` or `403 no_entitlement`, with a token that is
still valid. Both are in the decision log.

### "I get 403"
Read the `code` in the error, or find the request in `make decisions`:

| Code | Cause | Fix |
|------|-------|-----|
| `user_not_provisioned` | The token is valid but SCIM hasn't delivered the user | Check the provider's SCIM sync and that the user is assigned to the application |
| `user_deactivated` | Deactivated in the identity provider | Intended, or reactivate there |
| `no_entitlement` | None of their groups is in `group_roles` | Add them to a mapped group, or map the group in a pull request |
| `model_not_allowed` | Their tier doesn't allow that alias | A higher tier's group, or add the alias to the tier in `limits.yaml` |
| `data_class_not_eligible` | No model behind the alias may hold that class of data | Use an alias with an eligible model (`local`), or lower the class if the data allows |
| `role_not_allowed`, `tier_too_low` | The tool isn't open to their role or tier | Change `connectors:` in the policy, by pull request |

### The identity provider is down
Tokens already issued keep verifying against cached signing keys. A token signed by a key
the gateway has never seen gets `503 auth_unavailable` until the provider's keys can be
fetched again. API keys are unaffected. Provisioning changes wait in the provider and
arrive when it is back.

## Services

```bash
make key name=case-bot tier=standard team=compliance   # prints the key once
```

The team is the key's group for the policy, so it gets that group's role, and its spend
also counts against the team's budget. The team must exist in `limits.yaml` (`teams:`).
Revoke with `DELETE /admin/keys/{id}`; it takes effect within 30 seconds on every replica.

## Policy changes

1. Edit `config/policy.yaml` and **bump `version`**. The version is stamped on every
   decision record, so an auditor can tell which rules were in force.
2. `make policy` validates it against the other config files. CI runs the same check.
3. Merge by pull request, deploy the file, then `make reload`. A file that fails
   validation is refused and the previous policy stays live.
4. Check `make decisions`: new rows carry the new version.

## Models

### Change the model behind an alias
1. Give the new model a data-handling rule in `policy.yaml` (`models:`), from the
   provider's contract. A model with no rule gets no data and can't be evaluated.
2. Run the eval gate: `make evals current=<provider/model> candidate=<provider/model>`.
   Exit 0: no regression. Exit 1: the report names the tasks the candidate lost.
3. Change the chain in `config/models.yaml`, add the price in `config/pricing.yaml`,
   merge, `make reload`.
4. Watch the per-model panels in Grafana for an hour.

### A provider changed its retention terms
Lower (or raise) that provider's `max_data_class` in `policy.yaml`, bump the version,
reload. Requests above the new ceiling stop reaching it at once; chains that have another
eligible model keep working, the others return `403 data_class_not_eligible`.

## Spend

### Someone is at their cap
They ask: `POST /v1/spend/exceptions` with an amount and a reason. An approver (a member
of `spend_exceptions.approver_groups` in the policy) decides:

```bash
curl -s 'localhost:8000/v1/spend/exceptions?status=pending' -H "Authorization: Bearer $APPROVER"
curl -s -X POST localhost:8000/v1/spend/exceptions/$ID/approve -H "Authorization: Bearer $APPROVER" \
  -H 'content-type: application/json' -d '{"amount_usd": 30, "note": "until the 31st"}'
curl -s -X POST localhost:8000/v1/spend/exceptions/$ID/deny -H "Authorization: Bearer $APPROVER"
```

The grant lasts until the end of the month and applies within ten seconds on every
replica. Nobody can decide their own request. A team at its cap is not covered by this:
raise the team's budget in `limits.yaml` by pull request.

### A budget alert fired
Alerts go out at 50, 80 and 100% of a budget and at a tier's soft cap, once per caller and
month. Find what is spending: Grafana's spend-by-key panel, or

```sql
SELECT alias, count(*), round(sum(cost_usd)::numeric, 2) AS usd
FROM usage_log WHERE key_prefix = '<prefix>' AND created_at > date_trunc('month', now())
GROUP BY alias ORDER BY usd DESC;
```

A loop or a runaway job: revoke the key, or lower its budget with `PATCH /admin/keys/{id}`.

## Connectors

### The GitHub tool returns 503 `connector_not_configured`
One of `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, `GITHUB_APP_PRIVATE_KEY_FILE` is
empty, or the key file is missing, unreadable by the container's user, or not a private key.

### … or 502 "refused the gateway's credentials"
The app id or installation id is wrong, the key was deleted on GitHub, or the host's
clock is more than a minute off.

### Rotate the GitHub App's key
Generate a new key on the app's settings page, replace the file, then delete the old key
on GitHub. The gateway reads the file at its next token exchange, within the hour. No
restart.

### Add a connector
See [Connectors](wiki/Connectors.md#add-a-connector): a class, a policy entry, tests.

## Secrets

| Secret | Rotate by |
|--------|-----------|
| SCIM token | Set the new `SCIM_TOKEN`, restart the gateway, then update the identity provider. Provisioning fails in between and the provider retries |
| Operator keys | `make admin-key name=<operator>`, replace their line in `ADMIN_KEYS_FILE`, restart |
| Provider API keys | Replace the variable, restart. One replica at a time keeps traffic flowing |
| GitHub App key | Above; no restart |
| A gateway API key | Create a new one, move the service, revoke the old one |

## Evidence for an audit

**Who could use the case-lookup tool in March, and who did?**

```sql
SELECT at, subject_name, roles, decision, code, policy_version
FROM decision_log
WHERE action = 'tool:mock_records.lookup_case' AND at >= '2026-03-01' AND at < '2026-04-01'
ORDER BY at;
```

The `policy_version` on each row points at the commit of `config/policy.yaml` that was in
force, which says who *could*.

**Was confidential data ever sent to a model that keeps it?** Refused attempts are rows
with `code = 'data_class_not_eligible'`. For allowed requests, `detail` holds the chain
that was eligible and what was excluded, and `usage_log` (joined on `request_id`) holds
the model that actually served.

**Who approved extra spend, for whom, and when?**

```sql
SELECT requested_at, subject_name, amount_usd, status, granted_usd, decided_by_name, decided_at
FROM spend_exceptions ORDER BY requested_at DESC;
```

**Who changed access?** `GET /admin/audit`: every key change, config reload, spend
decision and SCIM write, with the operator (`scim` for the identity provider).

None of these tables holds prompt text, answers or tool arguments.
