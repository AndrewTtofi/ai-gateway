# Production deployment

ADRs 0016 and 0023. Two things ship for production: a **release image** and a **production compose
file** that runs on one host. For Kubernetes or a cloud platform, the
[operations page](Operations-and-Deployment.md) lists what to set up; the compose file shows
every piece working together.

## Release image

Pushing a `v*` tag runs `.github/workflows/release.yml`:
- it builds the gateway for **amd64 and arm64**;
- it pushes the image to `ghcr.io/andrewttofi/ai-gateway` with tags such as `2.0.0`, `2.0` and
  the commit SHA;
- it attaches an **SBOM** and **build provenance**, and signs an attestation with GitHub's
  OIDC identity.

Before pushing, the workflow scans the amd64 build with Trivy (HIGH and CRITICAL, fixable).
If the scan fails, nothing is published. The workflow runs only GitHub-owned actions, because the repository allows no third-party
ones. Docker steps use the `docker` CLI and `buildx` on the runner. Releases before 2.0.0
belong to the routing-only project and are published as `ghcr.io/andrewttofi/llm-gateway`.

Verify an image before running it:

```bash
gh attestation verify oci://ghcr.io/andrewttofi/ai-gateway:2.0.0 --owner AndrewTtofi
```

## `docker-compose.prod.yml`

```
internet ──443──► Caddy (TLS) ──► gateway ×2 ──► Redis · Redis (cache) · Postgres
                    │
operators ─ssh─► 127.0.0.1:8081 (admin, metrics, readiness)
```

```bash
cp .env.prod.example .env.prod        # fill in; generate secrets with: openssl rand -hex 32
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d
docker compose -f docker-compose.prod.yml --env-file .env.prod --profile monitoring up -d   # + Prometheus, Grafana
```

| Service | What it does |
|---------|--------------|
| `caddy` | **Front door:** automatic TLS for `GATEWAY_DOMAIN`, with HSTS. **Public site:** serves only the API; `/admin`, `/metrics`, `/readyz` and the API docs (`/docs`, `/redoc`, `/openapi.json`) are a 404 there. **Operator listener:** a second listener on `127.0.0.1:8081`, reached over an SSH tunnel (`ssh -L 8081:localhost:8081 host`) or a VPN. **Upstreams:** discovers every gateway replica (per-replica health checks and least-connections balancing), flushes streams immediately, caps request bodies, and allows 30-minute requests. **Slow clients:** 10 s to send headers, 2 min for a body, 2 min idle |
| `migrate` | Runs once before the replicas start: `alembic upgrade head`, then creates the read-only database role for Grafana. Gets only the database URL and that role's password |
| `gateway` ×2 | The release image. Read-only filesystem, all capabilities dropped, `no-new-privileges`. Graceful shutdown waits 30 s for open streams, inside a 45 s stop grace period. API docs off (`DOCS_ENABLED=false`): the schema would list the admin API |
| `redis` | Rate limits, budgets, breakers. Password from a config file (not visible in `ps`), append-only persistence |
| `redis-cache` | The response cache only: capped at `CACHE_MAXMEMORY` (512 MB) with LRU eviction, no persistence. Clients decide how fast a cache grows, so it's kept away from limits and budgets (ADR 0023) |
| `postgres` | Keys, the directory (users and groups), the decision log, usage log, admin audit log, spend exceptions, judge scores |
| `prune-decisions` | Deletes `decision_log` rows older than `DECISION_RETENTION_DAYS` every day. Does nothing while that is empty (the default): the decision log is the audit record, so the age is yours to choose. Gets only the database URL |
| `prune-usage` | Deletes `usage_log` rows older than `USAGE_RETENTION_DAYS` (default 90) every day. Gets only the database URL |
| `backup` | Daily `pg_dump` (custom format) into the `pg-backups` volume, kept `BACKUP_KEEP_DAYS` (14). Gets only the database password. Restore and restore drill: [Runbooks](Runbooks.md#backups) |
| `alertmanager` *(monitoring)* | Sends Prometheus alerts to `ALERTMANAGER_SLACK_URL` (Slack-compatible webhook), each with a link to its [runbook](Runbooks.md). Without a URL, alerts are only on its own page, `127.0.0.1:9093` |
| `prometheus` *(monitoring)* | Scrapes every replica's `:9100` by DNS discovery; loads the SLO and alert rules |
| `grafana` *(monitoring)* | On `127.0.0.1:3000` only, sign-up off. It reads Postgres as `grafana_ro`, which can read `usage_log`, `judge_scores` and every `api_keys` column **except `key_hash`** |

### Identity and connectors

Set these in `.env.prod` ([Identity](Identity.md), [Connectors](Connectors.md)); all are
optional, and empty means the feature is off:

| Variable | For |
|----------|-----|
| `OIDC_ISSUER`, `OIDC_AUDIENCE` (and optionally `OIDC_JWKS_URL`, `OIDC_USER_CLAIM`, `OIDC_USER_MATCH`) | Accepting access tokens from your identity provider |
| `SCIM_TOKEN` | The bearer token your identity provider presents to `https://<domain>/scim/v2` |
| `GITHUB_APP_ID`, `GITHUB_APP_INSTALLATION_ID`, `GITHUB_APP_PRIVATE_KEY_FILE` | The GitHub connector. The key file is a path on the host, mounted read-only; it must be readable by uid 1000 |

`/scim/v2` is on the public site, because the identity provider calls it from outside. It
accepts only its own token. If your provider publishes its egress addresses, allow only
those at the firewall or in the Caddyfile.

**Secrets:** each container gets only the variables it needs. The gateway never sees
Grafana's passwords; `migrate` and `prune-usage` never see provider keys or the admin key.
Use hex secrets, because they go into connection URLs as-is. The read-only role's password
must be ASCII.

**Networks:** three networks:
- `edge`: Caddy and the gateway.
- `data`: Postgres, both Redis instances and the jobs that use them. It's `internal`, with
  no route out.
- `metrics`: Prometheus, Alertmanager and Grafana.

Only the gateway shares a network with Caddy, so no other container can reach the
operator listener.

**Images:** third-party images are pinned by tag *and* digest (`redis:8.10.2-alpine@sha256:…`),
so a re-pushed tag can't change what runs. Dependabot updates both, a week after a release.

Verified locally by building the image and running the whole stack, with these checks:
- TLS and health checks;
- both replicas receiving traffic;
- operator endpoints unreachable from the public site;
- the read-only role refused `key_hash`;
- retention running.

## Maintenance

```bash
docker compose -f docker-compose.prod.yml exec gateway python -m app.maintenance prune --days 90
docker compose -f docker-compose.prod.yml exec gateway python -m app.maintenance grant-readonly --role grafana_ro
```

- **Decision log retention:** `prune-decisions --days N` (`make prune-decisions DAYS=365`)
  deletes the same way and has no default age. `--dry-run` (`DRY=1`) counts what would go.
- **Retention:** `prune` deletes in batches, with short transactions. Export spend history
  you need for finance before it's pruned.
- **Read-only role:** `grant-readonly` creates or updates the role. The password
  (`READONLY_PASSWORD`) is sent as a SCRAM-SHA-256 verifier computed locally, so it never
  appears in plaintext in Postgres logs.

## Upgrading

1. Read the CHANGELOG for the new version, and check whether there are migrations.
2. Set `GATEWAY_VERSION` in `.env.prod`.
3. Run `docker compose … up -d`. `migrate` runs first, then the replicas are replaced, and
   in-flight streams finish during the grace period.

Config changes don't need a release: edit `config/` and run
`docker compose -f docker-compose.prod.yml --env-file .env.prod restart gateway`. That restarts
the replicas one by one, and each reloads the config. `POST /admin/reload` through the
operator listener reaches only the one replica Caddy picks.
