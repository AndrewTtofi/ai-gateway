# AI tools: controls and evidence

*One page for compliance and risk. No technical background needed.*

## What this covers

AI assistants can call **tools**: a lookup in the case-management system, a list of open
changes in the code repository. Each tool is a route out of the AI platform into another
system, so each one is controlled.

## The controls

| # | Control | What it means |
|---|---------|---------------|
| 1 | **Nothing is open by default** | A tool can't be called until it has an entry in the access policy. Deploying the code is not enough. |
| 2 | **Access is by role** | The policy names the roles that may use each tool. Roles come from groups in the company's identity provider, the same groups used for other systems. |
| 3 | **Leavers and movers lose access at once** | Group membership is checked on every call. Removing someone from a group, or deactivating them, takes effect on their next request. |
| 4 | **Data classification is enforced** | Each call declares the class of data involved. Each tool has a ceiling: a tool that sends information to an outside service can't be used with confidential data. |
| 5 | **No standing credentials** | The platform holds no long-lived password or token for the other system. It obtains a credential that expires within an hour, limited to read access, when a call needs one. |
| 6 | **Every decision is recorded** | Each call, allowed or refused, is logged with who, which tool, the data class, the reason and the version of the policy in force. |
| 7 | **Changes are reviewed** | The access policy is a file under version control. A change needs a reviewed pull request and passes automated checks before it applies. |

## What is recorded, and what is not

**Recorded:** time, the person or service, their roles, the tool, the data class, allowed
or refused and why, the policy version, a request id.

**Never recorded:** the question asked, the answer, or the details passed to the tool.
The record shows that a case was looked up and by whom, not which case.

## Evidence you can ask for

| Question | Evidence |
|----------|----------|
| Who could use tool X on a given date? | The access policy at the version stamped on that day's records, plus group membership from the identity provider |
| Who did use it? | The decision log, filtered by tool and date |
| Were there refused attempts? | The same log: refusals are recorded with their reason |
| Who approved a change in access? | The pull request that changed the policy |
| When did a leaver lose access? | The provisioning entry in the audit log, and their refused requests after it |

## How this supports regulatory obligations

Offered as a mapping to discuss with your own counsel, not as legal advice.

- **DORA** (ICT risk management, third-party risk): an inventory of which outside
  services AI tools can reach, least-privilege and time-limited access to them, and logs
  of use.
- **EU AI Act** (record-keeping and human oversight for systems in scope): automatic
  records of each tool use, and a named, reviewed policy that people control.

## Limits to be aware of

- The data class is **declared by the caller**. The platform enforces it but does not
  read content to check it. A default class applies when none is declared.
- A tool acts with the platform's own access to the other system, not the individual's.
  Who asked is in the platform's log; the other system's log shows the platform.
- Decision records stay in the platform's database until someone removes them. There is
  no automatic deletion yet; a retention period should be set from your records policy.
