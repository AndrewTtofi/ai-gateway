# 0032 — Soft caps and a spend exception queue

- **Status:** accepted
- **Date:** 2026-10-12
- **Phase:** 18

## Context
Every caller has a monthly budget, enforced atomically before routing (ADR 0007, 0023).
It is one number and one behaviour: at the cap, requests are refused until next month or
until an operator edits a config file or a key. Two things are missing:

- **Warning before refusal.** Alerts go to operators at 50, 80 and 100% (ADR 0024); the
  caller finds out from a 429.
- **A controlled way past the cap.** Work that has to finish this month needs someone to
  raise the limit. Done by editing config, the raise is permanent unless someone
  remembers to undo it, anyone with repository access can do it, and nothing records why.

## Options considered
1. **Raise budgets generously** — no process, and no control: the cap stops meaning anything.
2. **An operator edits the key or the tier** — works today, but the change is permanent,
   needs the admin key, and the reason lives in a chat thread.
3. **A soft cap only** — warn at a lower amount and never refuse. Good for trusted teams,
   but then nothing stops a runaway loop.
4. **Both caps, plus an exception a second person approves** — the hard cap stays hard;
   the way past it is a request, a decision by someone in a named group, and a grant that
   expires with the month.

## Decision
Option 4.

**Soft cap.** A tier may set `soft_budget_usd` below `monthly_budget_usd`. Past it
requests still run; the response carries `x-gateway-budget: soft-cap-exceeded`, the
decision record notes it, and one alert goes out per caller and month. The header is
judged on spend before the request's own hold, so an estimate can't trigger it early.

**Exception queue.**
- Any caller may ask: `POST /v1/spend/exceptions` with an amount and a reason, up to the
  policy's `max_usd`, one pending request per caller and month.
- Only a member of a group in the policy's `spend_exceptions.approver_groups` may approve
  or deny, and never their own request. Membership is read from the directory on each
  call, like every other entitlement. With no group named, nobody can approve.
- An approver may grant less than was asked, not more.
- An approved amount is added to that caller's budget **for that month only**. Next month
  starts from the tier's budget again; nothing has to be undone.
- A decision is one `UPDATE … WHERE status = 'pending'`, so two approvers acting at the
  same moment produce one decision.

**On the request path.** Grants are looked up only after a reservation was refused. A
caller within budget pays nothing for this feature: no extra read, no cache to keep
warm. A refused caller costs one query, remembered for ten seconds so a client retrying
in a loop can't turn its 429s into database load. The replica that handled an approval
forgets its entry at once; others see the grant within ten seconds. If the database
can't answer, there is no grant and the cap holds.

**Audit.** The request, each refused attempt to decide, and the decision are decision
records (who, what, when, policy version). Decisions are also in the admin audit log,
since they change what someone may spend. The requester's reason is stored with the
request, where the approver needs it, and never copied into either log.

## Consequences
- The hard cap can be set where it should be, because hitting it is no longer a dead end.
- A grant applies to the caller's own budget, not to a team's. A team at its cap is still
  raised in `limits.yaml`, by review.
- A service key in an approver team can approve, because a key's team is its group
  everywhere in the policy. Keep approver groups to people unless a service should decide.
- The request and approval go through the API. There is no notification to approvers
  beyond the pending list; a chat or ticket integration would sit on top of these endpoints.
- A request made at the end of a month and approved in the next applies to the month it
  was made in, and so does nothing. The requester asks again.
