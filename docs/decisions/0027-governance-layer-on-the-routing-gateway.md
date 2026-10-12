# 0027 — One gateway: a governance layer on the routing gateway

- **Status:** accepted
- **Date:** 2026-10-12
- **Phase:** 13

## Context
Two codebases covered two halves of the same product:

- **llm-gateway (v1.3.1):** provider adapters, streaming, fallback chains, circuit
  breakers, token-aware rate limits, atomic budgets, a usage log and operations. Its
  callers are API keys with a tier and a team. It has no users, no identity provider and no
  data-handling rules.
- **A governance skeleton (about 600 lines):** the request path an enterprise platform
  needs, identity → policy → spend → audit. Groups map to roles and tiers, each request
  declares a data class, a model is eligible only for the classes its retention terms
  allow, hard caps have an exception queue, and every decision is logged with the policy
  version. Its model call was a stub, its directory lived in memory and its ledger was a
  SQLite file with a check-then-write race.

The goal is one gateway where every request passes identity → policy → spend →
routing/fallback → audit. Governance cannot be bolted on from outside: a `confidential`
request must never fall back to a model that retains data for 30 days, so eligibility has
to be applied to the fallback chain inside the router. The response cache and the judge
also copy prompt content, so they need the same rule.

## Options considered
1. **Build on the skeleton and port routing into it** — keeps the small codebase, but
   means rewriting streaming, provider translation, metering and the breaker, which are
   the parts that took the longest to get right and have the tests.
2. **A new repo with llm-gateway as a git submodule** — keeps the two repos separate, but
   llm-gateway has no plugin interface, so most changes would still land in it. Every
   phase would need two coordinated changes and the new repo would hold little code.
3. **A subtree copy** — self-contained, but leaves two copies of the gateway to diverge.
4. **Rename llm-gateway in place** — simplest, but the routing-only project stops existing
   as its own thing.
5. **A new repo seeded from llm-gateway's full history** — llm-gateway stays untouched at
   v1.3.1, and this repo continues from the same commits.

## Decision
Option 5. This repo starts from llm-gateway's history and gains the skeleton's governance
model as a layer in front of the router; the skeleton's concepts are ported, its
implementation is not.

What that means for each part:

| Part | Kept from llm-gateway | Ported from the skeleton |
|------|----------------------|--------------------------|
| Identity | API keys, as credentials for services | Users and groups from the identity provider (OIDC tokens, SCIM provisioning); every caller resolves to one principal |
| Policy | Tiers and allowed aliases | Group → role → tier, data classes, model eligibility by retention, a versioned policy file validated in CI |
| Spend | Atomic reservation in Redis, reconcile to real usage, alerts once per level per month | Soft and hard caps per principal, an exception queue with audited approval |
| Audit | Usage log, admin audit log, request ids | One decision record per request, allow or deny, with the reason and policy version |
| Tools | — | Connectors behind the same policy check, with short-lived credentials |
| Evals | Judge sampling stays for live traffic | A golden-task harness that gates model changes in CI |

## Consequences
- The hard parts are reused with their tests, and each governance phase is a change to
  working software rather than a rebuild.
- Storage stays Postgres and Redis. The skeleton's SQLite was right for a single-file
  reference build; with two replicas, a shared ledger and directory need a real database.
- The stack stays on Python 3.14, the version the code and the hashed lockfiles target.
- The repo is larger than a governance-only project would be. The README has to make the
  governance path easy to find among the routing features.
- Two repos share their early history. llm-gateway is not changed by this work.
