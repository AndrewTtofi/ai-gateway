# 0031 — Short-lived connector credentials: a GitHub App

- **Status:** accepted
- **Date:** 2026-10-12
- **Phase:** 17

## Context
Connectors reach external systems on behalf of callers (ADR 0028). The first real one
reads pull requests from GitHub. The usual way to give a service access to GitHub is a
personal access token in an environment variable: it belongs to a person, it has that
person's reach, it lives for months, and anyone who reads the variable has all of it.
A gateway that many callers go through is the worst place to keep a credential like that.

## Options considered
1. **A personal access token** — one variable and done. Tied to a person who may leave,
   broad, long-lived, and the audit trail on GitHub's side names that person.
2. **A fine-grained personal access token** — narrower (chosen repositories, read-only),
   but still tied to a person and still a long-lived secret that works on its own.
3. **Each caller's own GitHub identity (OAuth on behalf of the user)** — the best fit when
   a tool must act as the person asking. It needs a browser consent flow and a token store
   per user, which is a project of its own.
4. **A GitHub App** — an identity that belongs to the organisation, installed on chosen
   repositories with chosen permissions. The gateway holds the app's private key and
   exchanges a signed JWT for an installation token that lives one hour.

## Decision
Option 4. The connector signs a JWT with the app's private key (RS256, valid nine
minutes, issued a minute in the past to tolerate clock drift), posts it to
`/app/installations/{id}/access_tokens`, and gets a token that expires in an hour.

- **The token is asked for with less than the app has:** the request names
  `pull_requests: read`, so if someone later grants the app more, this connector's tokens
  don't grow with it.
- **It is cached in process memory** until five minutes before it expires, and exchanged
  once however many calls are waiting. It is not put in Redis: a shared store would turn a
  credential into data other things can read, and the cost of not sharing is one extra
  exchange per replica per hour.
- **The private key is a file,** named by `GITHUB_APP_PRIVATE_KEY_FILE` and read at each
  exchange, so rotating it needs no restart. It is never a variable, so it can't end up
  in a process listing, an `env` dump or the compose file.
- **A token GitHub rejects is dropped,** so the next call exchanges for a new one.
- **Policy comes first.** The role, tier and data-class checks run before any credential
  is minted; a refused call costs GitHub nothing and is in the decision log.
- **Arguments are validated before they reach a URL:** the repository must be
  `owner/name`, so a caller can't steer the gateway's token to another endpoint.
- **Results are trimmed** to a few fields, and a description to 4,000 characters.
- **Unconfigured is a state, not a crash:** without the three settings the connector
  answers 503 `connector_not_configured` and the rest of the gateway is unaffected.

## Consequences
- No GitHub credential that outlives an hour exists outside GitHub. What does exist is
  the app's private key, and it is now the thing to protect: a file readable only by the
  gateway's user, a secret mount in production, rotated from the app's settings page.
- Calls are made as the app, not as the person asking. GitHub's audit log shows the app;
  who asked is in the gateway's decision log. Every entitled caller sees the same
  repositories: the ones the app is installed on. Per-user access is option 3, and the
  next connector project (an MCP server with OAuth) is where that belongs.
- An installation token also reads public repositories, like any GitHub token. What the
  installation limits is private repositories.
- The tool result can contain text written by anyone who can open a pull request. It is
  third-party content; when it is passed to a model, the injection filter scans it as a
  tool result (ADR 0021).
- One installation is configured. Several organisations would need an installation id
  per repository owner, looked up from the app.
