# Security policy

## Reporting a vulnerability

Please **don't open a public issue** for security problems. Report them privately via
[GitHub's private vulnerability reporting](https://github.com/AndrewTtofi/ai-gateway/security/advisories/new).

Include what you found, how to reproduce it, and the impact you expect. You'll get an
acknowledgement within a few days. This is a personal project, so there's no bounty.

## Scope

In scope: the gateway code in `app/`, its configuration handling, and the Docker setup.
For example:

- leaking provider API keys, connector credentials or prompt content;
- bypassing authentication, rate limits or budgets;
- acting as another user, or keeping access after being deprovisioned;
- sending data to a model or a tool the access policy does not allow for it;
- approving your own spend exception, or writing to the audit logs;
- making the gateway call arbitrary URLs.

The threat model and what mitigates each risk are in
[docs/wiki/Security.md](docs/wiki/Security.md).

## Handling secrets

- Provider keys live only in `.env` (git-ignored) and are read from environment variables.
- The gateway never returns provider error text that could contain key fragments or
  account IDs to clients (see `docs/decisions/0002-streaming-errors-and-disconnects.md`).
- Connector credentials are minted per call and live an hour; the GitHub App's key is a
  mounted file. Decision and audit records never hold prompts, answers, tool arguments
  or tokens.
- CI uses no secrets.
