# AI Gateway wiki

AI Gateway is a self-hosted, governed gateway for an enterprise AI platform. Every
request, from a person or a service, to a model or a tool, passes five stages:

**identity → policy → spend → routing → audit**

- **Identity:** people sign in through the company's identity provider (OIDC); users and
  groups are provisioned over SCIM. Services use gateway API keys.
- **Policy:** a reviewed file maps group → role → tier and says which model may hold
  which class of data, and which role may call which tool.
- **Spend:** rate limits, a soft cap that warns, a hard cap that refuses, and an
  exception queue with second-person approval.
- **Routing:** an alias maps to a chain of models, filtered to the ones allowed to hold
  the request's data, with retries, fallback and circuit breakers.
- **Audit:** every decision, allowed or denied, is recorded. Never the content.

```
people, services ──► identity → policy → spend → routing → audit ──► models, tools
```

Clients call it in the OpenAI chat-completions format (`/v1/chat/completions`) or the
Anthropic Messages format (`/v1/messages`). The formats are a compatibility detail, not
the product.

The README is the short version. This wiki explains how each part works and why it was
built that way, including the AI-specific parts (tokens, streaming, provider formats) for
readers who are new to them.

## Where to start

| If you want to… | Read |
|-----------------|------|
| Run it locally in five minutes | [Getting started](Getting-Started.md) |
| See the governed path in a minute | [The demo](https://github.com/AndrewTtofi/ai-gateway/blob/main/docs/demo.md) |
| Onboard people and control what they may do | [Identity](Identity.md), then [Governance](Governance.md) |
| Decide which data may reach which model | [Governance → data classes](Governance.md#data-classes) |
| Let assistants reach other systems safely | [Connectors](Connectors.md) |
| Set soft and hard spend caps, and approve exceptions | [Keys, limits and budgets](Keys-Limits-and-Budgets.md#soft-cap-and-hard-cap) |
| Check a model change before making it | [Model evals](Model-Evals.md) |
| Show an auditor who did what | [Governance → the decision log](Governance.md#the-decision-log) and the [runbook](https://github.com/AndrewTtofi/ai-gateway/blob/main/docs/runbook.md#evidence-for-an-audit) |
| Operate it day to day | [The runbook](https://github.com/AndrewTtofi/ai-gateway/blob/main/docs/runbook.md) |
| See it serve many apps with separate keys and budgets | [Use case: one gateway for many apps](Use-Case-Multi-App.md) |
| Understand the vocabulary (aliases, targets, tokens, TTFT, SSE) | [Core concepts](Core-Concepts.md) and [Glossary](Glossary.md) |
| See how a request flows through the system | [Architecture](Architecture.md) |
| Know which providers work and what gets translated | [Providers and translation](Providers-and-Translation.md) |
| Understand failover and circuit breakers | [Routing and reliability](Routing-and-Reliability.md) |
| Issue keys and control spend | [Keys, limits and budgets](Keys-Limits-and-Budgets.md) |
| Monitor it | [Observability](Observability.md) |
| Let apps pick models by price, capability, quality and live speed; keep prices current | [Choosing models](Choosing-Models.md) |
| Route by policy (`model: auto`) or run A/B tests | [Smart routing](Smart-Routing.md) |
| Cache answers (exact or semantic) | [Response cache](Response-Cache.md) |
| Filter prompt injection; score answer quality | [Quality and safety](Quality-and-Safety.md) |
| Recover providers automatically; get alerts | [Self-healing](Self-Healing.md) |
| Deploy with TLS, replicas and monitoring | [Production deployment](Production-Deployment.md) |
| Change models, prices or limits | [Configuration reference](Configuration-Reference.md) |
| Integrate a client | [API reference](API-Reference.md) |
| Run it in production | [Operations and deployment](Operations-and-Deployment.md) |
| Check the performance claims | [Testing and benchmarks](Testing-and-Benchmarks.md) |
| Review the security model | [Security](Security.md) |
| Ask whether a subscription (Claude Pro/Max, ChatGPT/Codex) works with it | [Subscriptions and provider terms](Subscriptions-and-Terms.md) |
| Find a quick answer | [FAQ](FAQ.md) |
| See what's next | [Roadmap](Roadmap.md) |

## Design records

Every non-obvious decision has an ADR in
[`docs/decisions/`](https://github.com/AndrewTtofi/ai-gateway/tree/main/docs/decisions).
Each wiki page links to the ADRs behind it.

## Keeping the wiki in sync

The wiki's source lives in the main repo under
[`docs/wiki/`](https://github.com/AndrewTtofi/ai-gateway/tree/main/docs/wiki), so it is
reviewed in the same pull requests as the code it describes. After every merge to `main`
that changes it, the `wiki` workflow publishes it to the GitHub Wiki tab
(`scripts/publish_wiki.sh`). Edit the files in `docs/wiki/`, not the Wiki tab: edits made
there are overwritten by the next sync.
