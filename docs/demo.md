# Demo

The governed request path in about a minute, on your machine, with no accounts and no
provider API keys. Every command here was run against the dev stack.

Each step shows one stage of **identity → policy → spend → routing → audit**.

## Setup

```bash
cp .env.example .env      # set GATEWAY_ADMIN_KEY (openssl rand -hex 32)
make up                   # gateway, Redis, Postgres, Prometheus, Grafana

# a helper: create a key and keep only the secret
newkey() { make -s key "$@" | sed -E 's/.*"key": ?"([^"]+)".*/\1/'; }

CASE_BOT=$(newkey name=case-bot tier=standard team=compliance)
BUILD_BOT=$(newkey name=build-bot tier=standard team=eng-backend)
BATCH=$(newkey name=batch-job tier=chaos budget=0.03)
ONCALL=$(newkey name=platform-oncall tier=standard team=platform-admin)
```

A key's team is its group. `config/policy.yaml` maps the group to a role, and the role
decides what the key may do. Users signed in through an identity provider get their
groups from the directory instead ([step 6](#6-real-identity-optional-about-two-minutes)).

## 1. Policy: the same tool, two roles

```bash
curl -s localhost:8000/v1/tools/mock_records/lookup_case \
  -H "Authorization: Bearer $CASE_BOT" -H 'x-gateway-data-class: restricted' \
  -H 'content-type: application/json' -d '{"args":{"case_id":"C-1001"}}'
```
```json
{"connector":"mock_records","tool":"lookup_case","result":{"status":"open","owner":"compliance"}}
```

```bash
curl -s localhost:8000/v1/tools/mock_records/lookup_case \
  -H "Authorization: Bearer $BUILD_BOT" \
  -H 'content-type: application/json' -d '{"args":{"case_id":"C-1001"}}'
```
```json
{"error":{"message":"mock_records needs a role it allows","type":"permission_error","param":null,"code":"role_not_allowed"}}
```

Same tier, same endpoint. The policy allows the case-lookup tool for the `compliance` and
`trader` roles, and `build-bot` is an `engineer`.

## 2. Data handling: confidential data never reaches a model that keeps it

```bash
curl -s localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $BUILD_BOT" -H 'x-gateway-data-class: confidential' \
  -H 'content-type: application/json' \
  -d '{"model":"smart","messages":[{"role":"user","content":"Summarise this contract"}]}'
```
```json
{"error":{"message":"No model behind 'smart' is eligible for 'confidential' data.","type":"permission_error","param":"x-gateway-data-class","code":"data_class_not_eligible"}}
```

Every model behind `smart` is a hosted model that the example policy allows up to
`internal` data. The request is refused before anything is sent anywhere.

With [Ollama](https://ollama.com) running (`ollama pull llama3.2:3b`), ask the `fast`
alias instead. Its chain is two hosted models and then a local one:

```bash
curl -s -D - -o /dev/null localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $BUILD_BOT" -H 'x-gateway-data-class: confidential' \
  -H 'content-type: application/json' \
  -d '{"model":"fast","messages":[{"role":"user","content":"Say hi"}]}' | grep -i x-gateway
```
```
x-gateway-attempts: 1
x-gateway-provider: ollama/llama3.2:3b
x-gateway-fallback: false
x-gateway-data-class: confidential
x-gateway-excluded: 2
```

The two hosted models were removed from the chain for this request, so no retry or
fallback could send the data to them. The local model answered.

## 3. Spend: a hard cap

`batch-job` has a monthly budget of three cents on a test model that is priced on purpose.

```bash
for i in $(seq 1 12); do
  curl -s -o /dev/null -w '%{http_code} ' localhost:8000/v1/chat/completions \
    -H "Authorization: Bearer $BATCH" -H 'content-type: application/json' \
    -d '{"model":"fake/metered","messages":[{"role":"user","content":"hello"}]}'
done; echo
```
```
200 200 200 200 200 200 200 200 429 429 429 429
```

The 429 says what to do next: `Monthly budget for this key is exhausted. Ask for more
with POST /v1/spend/exceptions.`

## 4. Spend: an exception a second person approves

```bash
EXC=$(curl -s localhost:8000/v1/spend/exceptions -H "Authorization: Bearer $BATCH" \
  -H 'content-type: application/json' \
  -d '{"amount_usd":1,"reason":"month-end batch"}' | sed -E 's/.*"id":"([^"]+)".*/\1/')

# the requester can't approve their own request
curl -s -X POST localhost:8000/v1/spend/exceptions/$EXC/approve -H "Authorization: Bearer $BATCH"
```
```json
{"error":{"message":"Only an approver group may decide spend exceptions.","type":"permission_error","param":null,"code":"not_an_approver"}}
```

```bash
# someone in the platform-admin group can
curl -s -X POST localhost:8000/v1/spend/exceptions/$EXC/approve -H "Authorization: Bearer $ONCALL"

curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $BATCH" -H 'content-type: application/json' \
  -d '{"model":"fake/metered","messages":[{"role":"user","content":"hello"}]}'
```
```
200
```

The extra dollar applies to this caller for this month only.

## 5. Audit: every decision, allowed or denied

```bash
make decisions n=9      # rows are written in batches: give the last request a second
```
```
19:55:44 allow ok                       model:fake/metered                 batch-job        internal
19:55:44 allow ok                       spend_exception:approve            platform-oncall  none
19:55:44 deny  not_an_approver          spend_exception:approve            batch-job        none
19:55:44 allow ok                       spend_exception:request            batch-job        none
19:55:44 deny  budget                   model:fake/metered                 batch-job        internal
…
```

Each row also holds the request id, the caller's roles and tier, and the version of the
policy that was in force. It never holds prompts, answers or tool arguments. The full
records are at `GET /admin/decisions` and in the `decision_log` table.

## 6. Real identity (optional, about two minutes)

```bash
make idp-e2e
```

Starts a throwaway stack with [authentik](https://goauthentik.io) as the identity
provider and plays a joiner, a mover and a leaver: a user is created and added to a group
in the identity provider, arrives in the gateway over SCIM, calls a tool with an OIDC
token, changes group, and is deactivated. The script checks each answer and the audit
trail. [Identity](wiki/Identity.md) explains what happens.

## 7. A model change, gated (optional)

```bash
python -m evals.run_evals --offline --current stub/baseline --candidate stub/baseline   # exit 0
python -m evals.run_evals --offline --current stub/baseline --candidate stub/regressed  # exit 1
```

The same harness runs the golden tasks against two real models
(`make evals current=… candidate=…`) and fails when the candidate does worse than the
model it would replace. [Model evals](wiki/Model-Evals.md) has the details.

## 8. A real connector (optional, needs a GitHub App)

With a GitHub App configured ([Connectors](wiki/Connectors.md#create-the-github-app)):

```bash
curl -s localhost:8000/v1/tools/github/list_pull_requests \
  -H "Authorization: Bearer $BUILD_BOT" -H 'content-type: application/json' \
  -d '{"args":{"repo":"your-org/your-repo","state":"all","limit":3}}'
```

The gateway holds no GitHub token: it signs a short JWT with the app's key and trades it
for a one-hour token that can only read pull requests.

## Clean up

```bash
make down
```
