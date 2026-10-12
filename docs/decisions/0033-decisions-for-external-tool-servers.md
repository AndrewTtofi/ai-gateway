# 0033 — Decisions for external tool servers

- **Status:** accepted
- **Date:** 2026-10-13
- **Phase:** 20

## Context
The gateway's own connectors run inside it and act as an app (ADR 0031). Some tools are
better run elsewhere: an MCP server that works in Jira and Confluence *as each user*, with
that user's own grant, holds per-user credentials and speaks a protocol the gateway
doesn't. But who may use which tool, with which class of data, should still be decided in
one place and recorded in one log.

The obvious integration, the tool server forwarding the user's token to the gateway, is
wrong. The token was issued for the tool server; a resource server must not pass a token
on, and the gateway must not accept one issued for someone else.

## Options considered
1. **Each tool server keeps its own policy** — no dependency, and entitlements drift
   apart: two files, two audit logs, two answers to "who can use this?".
2. **The gateway proxies the tool server** — one path, but the gateway would need the
   per-user credentials or a token exchange with the identity provider (RFC 8693), which
   the bundled provider doesn't offer.
3. **The tool server asks the gateway for a decision** — it authenticates the user
   itself, then asks, with its own credential: "may this user use this tool, with this
   class of data?"

## Decision
Option 3: `POST /v1/authorize`.

```json
{"subject": "<the user's id from their token>", "connector": "atlassian",
 "tool": "jira_add_comment", "data_class": "internal"}
```
```json
{"allowed": false, "code": "role_not_allowed", "reason": "…", "policy_version": "2026-10-13.1"}
```

- **Who may ask** is in the policy: `delegated_decisions.client_groups`. Only a service
  (a gateway API key whose team is one of those groups) may ask. A person can't ask about
  another person, whatever groups they are in. With no group named, nobody may ask.
- **The user is resolved the way the gateway resolves its own callers:** the subject is
  looked up in the SCIM-provisioned directory, so a deactivated or unprovisioned user is
  refused here too, at once.
- **The decision is the connector check that already exists** (tier, role, data class),
  against a policy entry marked `external: true`, with an optional list of the tools it
  may ask about. An external entry needs no code in the gateway; a built-in connector
  can't be asked about, and an external one can't be called through the gateway.
- **The answer is always 200 with `allowed`.** A refusal is a decision, not an error. The
  errors are the asker's: 403 if it may not ask, 400 for a malformed question, 503 if the
  directory can't be read (no answer is not a yes).
- **It is recorded under the user's name,** with the asking server in the detail, in the
  same decision log as everything else.

## Consequences
- One policy file and one decision log cover tools the gateway doesn't run.
- The tool server depends on the gateway for every call. It must fail closed when the
  gateway doesn't answer, and it pays one request per call unless it caches allows
  (which trades immediacy of deprovisioning for latency).
- The asking server is trusted to name the user truthfully. That trust is scoped by the
  client group and visible in the log (`asked_by`), and the server is expected to have
  verified the user's token itself.
- Both services must see the same user identifier: the token's `sub` at the tool server
  has to be what the gateway matches in its directory (`OIDC_USER_MATCH`).
- Token exchange (option 2) remains the better design where the identity provider
  supports it: the gateway could then verify the user itself instead of taking a
  service's word.
