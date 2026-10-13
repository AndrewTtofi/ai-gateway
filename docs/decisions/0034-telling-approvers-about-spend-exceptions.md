# 0034 — Telling approvers about spend exceptions

- **Status:** accepted
- **Date:** 2026-10-13
- **Phase:** after 20

## Context
A caller at their hard cap asks for more budget, and someone in an approver group decides
(ADR 0032). Until now the request sat in a queue that approvers had to remember to list.
The person asking is blocked, by design, until someone looks: a control that works only
when people poll it turns into a message in a chat thread asking someone to go and look,
and then the reason for the raise lives in that thread and not in the record.

The gateway already sends operators' alerts (breakers, budgets) to a Slack-compatible
webhook, once per event across replicas (ADR 0019, 0024).

## Options considered
**How approvers find out.**
1. **Nothing; document the queue** — what there was.
2. **Email or a direct message to each approver** — the gateway would need each
   approver's address and a mail or chat credential, and group membership would have to
   be resolved into a recipient list on every request.
3. **A webhook to a channel the approvers watch** — one URL, no addresses, and the
   delivery and once-only logic already exist.

**What the message says.**
1. **Everything, including the requester's reason** — an approver could decide from chat.
2. **Who, how much, which month, and where to look** — the reason stays in the queue.

## Decision
A webhook (option 3) carrying no free text (option 2).

- **Two moments:** when a request is made, and when it is approved or denied. The second
  tells the other approvers they can stop looking and names who decided.
- **Where:** `SPEND_EXCEPTIONS_WEBHOOK_URL` if set, so approvers can have a channel of
  their own. Otherwise the operators' alert webhook: a request nobody hears about is
  worse than one heard in the wrong room. With neither set, a log line.
- **No reason and no note.** Both are free text written by a person. The reason can name
  a customer or a project, and it comes from the caller, so it could be written to
  mislead whoever, or whatever, reads the channel. ADR 0032 already keeps it out of the
  decision log and the audit log; a chat service is a third party on top of that. The
  message says where to read it: the queue, which only approvers can list.
- **In the background, after the record is written.** The request and the decision are
  stored and logged first. A slow or failing webhook can't delay or fail them, and it is
  counted (`gateway_alerts_total{result}`).
- **Once per request and step, fleet-wide,** with the same Redis `SET NX` the other
  alerts use, keyed by the request's id.
- **Refused attempts send nothing:** someone outside the group trying to approve, a
  second request while one is pending. They are in the decision log.

## Consequences
- Approvers hear about a request within seconds, without the gateway knowing who they are
  individually.
- Deciding still means going to the API to read the reason. That is one more step, on
  purpose.
- The requester is not told the outcome; they list their own requests or try again. The
  gateway has no way to reach an individual.
- Delivery is best effort: no retry, no reminder for a request that stays pending. A
  missed message leaves the request in the queue, where it was before this change.
- The channel learns who is asking for how much. Anyone in it sees that; keep the
  approvers' channel to approvers if that matters, which is what the separate URL is for.
- Tested with a stand-in for the webhook. It has not been posted to a real Slack
  workspace.
