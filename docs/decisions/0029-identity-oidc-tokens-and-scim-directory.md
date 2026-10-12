# 0029 — Identity: OIDC tokens and a SCIM-provisioned directory

- **Status:** accepted
- **Date:** 2026-10-12
- **Phase:** 15

## Context
Until now every caller was an API key that an operator created by hand. People should
call the gateway as themselves, with the account they already have, and lose access the
moment the organisation says so: when they leave, or when they move team.

That needs two things from an identity provider (IdP), both standards:

- **OIDC**: an access token the gateway can verify, saying who is calling.
- **SCIM 2.0**: the IdP pushes users, groups and memberships to the gateway as they change.

## Options considered

**Where a caller's groups come from**
1. **From claims in the token** — no directory to keep, but the groups are as old as the
   token. Someone removed from a group keeps the entitlement until the token expires, and
   a deactivated user keeps working.
2. **From a directory the IdP provisions over SCIM, read on every request** — access
   changes take effect on the next request. Costs a lookup per request and a SCIM server.

**Which identity provider to develop against**
1. **A hosted tenant (Okta, Entra)** — the names enterprises use, but it needs an account
   and secrets, can't reach a laptop without a tunnel, and can't run in CI.
2. **authentik, in the compose stack** — open source, speaks both OIDC and SCIM, needs no
   account, and the whole flow can be tested end to end on every pull request.
3. **Keycloak** — no built-in SCIM client.

**How a token is matched to a provisioned user**
1. **By email or username** — readable, but both can change, and a reused address would
   inherit someone else's entitlements.
2. **By the IdP's immutable user id** — the token's `sub` against the SCIM `externalId`.

**How a user travels through the request path**
1. **A second code path for users** — limits, budgets, metering, cache scoping and usage
   would each need to learn about users.
2. **The same shape as an API key** — one path; the user's directory id plays the key id.

## Decision
- **The token says who; the directory says what.** A bearer token that isn't a gateway
  key and is shaped like a JWT is verified against the IdP's JWKS: asymmetric algorithms
  only, issuer, audience, expiry. Group claims in it are ignored.
- **Read on every request:** is this user provisioned and active, and which groups are
  they in. The policy (ADR 0028) turns the groups into roles and a tier. A token that is
  still valid stops working when its user is deactivated or leaves a group.
- **SCIM 2.0 server** at `/scim/v2`, stored in Postgres: Users and Groups with create,
  read, list with an `eq` filter, replace, PATCH and delete, and the SCIM error schema.
  PATCH accepts the dialects providers differ on. An unsupported filter is refused, since
  ignoring it would answer "everyone matches". The IdP authenticates with one bearer token.
- **Join on the immutable id:** the token's `sub` against `externalId` by default. Both
  the claim and the attribute are configurable, because providers differ.
- **Identities are cached for 30 seconds per replica.** A SCIM write clears the cache on
  every replica at once (the announcement API keys already use); the 30 seconds only bound
  a lost announcement.
- **One request path.** An authenticated user is carried in the same shape as an API key,
  with their directory id as the id. Rate limits, the monthly budget and the usage log are
  therefore per user, across all of their tokens.
- **Denials are evidence.** Not provisioned, deactivated, or no group that grants access
  is a 403 and a decision record. A bad token is a 401. Provider keys unavailable is a 503.
- **Provisioning is audited:** every SCIM change goes to the admin audit log as operator
  `scim`.
- **API keys stay** for services and machines. Both kinds of caller are principals to the
  policy.
- **authentik is the development IdP**, configured from a blueprint in the repo.
  `scripts/identity_e2e.sh` runs join, move, leave against it in CI. Nothing in the
  gateway is specific to it: another provider is the `OIDC_*` variables and `SCIM_TOKEN`.

## Consequences
- Offboarding is one action in the IdP. The gateway needs no separate revocation.
- The directory is one more thing the request path reads. It is cached, and when it can't
  be read the answer is a 503, never a guess.
- Access tokens are not checked for revocation at the IdP (no introspection call per
  request). Deactivating the user covers the case that matters; a stolen token for an
  active user is good until it expires, so token lifetimes should be short.
- A stream already running when its user is deactivated finishes; the next request is refused.
- The SCIM token is a long-lived shared secret. It can only write the directory, every
  write is audited, and rotating it is a redeploy. OAuth for the SCIM client would remove it.
- `/scim/v2` has to be reachable by the IdP, so it is on the public listener, behind its token.
- Verified against authentik in CI. Okta's and Entra's PATCH forms are covered by unit
  tests written from their documentation, not by a live tenant.
- An identity whose groups map to no role cannot use the gateway at all. A base level of
  access for everyone is a group everyone is in.
