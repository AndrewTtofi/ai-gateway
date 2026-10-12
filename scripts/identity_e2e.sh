#!/usr/bin/env bash
# End-to-end identity test against a real identity provider (authentik), ADR 0029:
#
#   join    a user is created in the identity provider and put in a group
#           → SCIM provisions them → their OIDC token works on the gateway
#   move    they leave the group → the same token loses the entitlement
#   leave   they are deactivated → the same token gets 403
#
#   scripts/identity_e2e.sh          # builds, runs the checks, tears everything down
#   KEEP=1 scripts/identity_e2e.sh   # leave the stack up afterwards
#
# Needs Docker and ports 8000 and 9000 free. Uses the fake provider: no keys, no spend.
# CI runs it on every pull request.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
project="gwidp$$"
work="$(mktemp -d)"
env_file="$work/env"
rand() { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
admin_token="$(rand)"
admin_key="$(rand)"   # the gateway's operator key, for reading the logs at the end
cat > "$env_file" <<ENV
SCIM_TOKEN=$(rand)
GATEWAY_OIDC_CLIENT_SECRET=$(rand)
AUTHENTIK_SECRET_KEY=$(rand)
AUTHENTIK_BOOTSTRAP_PASSWORD=$(rand)
AUTHENTIK_BOOTSTRAP_TOKEN=$admin_token
GATEWAY_ADMIN_KEY=$admin_key
ENV
created_env=""
if [ ! -f .env ]; then cp .env.example .env; created_env=1; fi   # the gateway's env_file

dc() { docker compose -p "$project" --env-file "$env_file" -f docker-compose.yml -f docker-compose.idp.yml "$@"; }
cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then
    echo "--- identity e2e FAILED; recent logs:" >&2
    dc ps -a >&2 || true
    dc logs --tail 40 gateway >&2 || true
    # authentik's crash reports are mostly stack frames: keep the lines that say what happened.
    # authentik logs every request and long stack frames: keep what says what went wrong.
    dc logs --no-color authentik-worker authentik-server 2>&1 \
      | grep -E '"level": ?"(warning|error|critical)"|Traceback|ai-gateway\.yaml|AI Gateway' \
      | grep -vE '/health/|"logger": ?"authentik.asgi"|Task (enqueued|started)' | cut -c1-900 | tail -40 >&2 || true
  fi
  if [ -z "${KEEP:-}" ]; then dc down -v --remove-orphans > /dev/null 2>&1 || true; fi
  [ -n "$created_env" ] && rm -f .env
  rm -rf "$work"
  exit "$status"
}
trap cleanup EXIT
ok() { echo "  ok   $*"; }
fail() { echo "  FAIL $*" >&2; exit 1; }
json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }

gw="http://127.0.0.1:8000"
idp="http://localhost:9000"   # localhost, not 127.0.0.1: it becomes the tokens' issuer
api() {  # api METHOD PATH [JSON] → body; fails on an HTTP error
  local method="$1" path="$2" body="${3:-}"
  curl -sS --fail-with-body --max-time 30 -X "$method" "$idp/api/v3$path" \
    -H "Authorization: Bearer $admin_token" -H 'content-type: application/json' \
    ${body:+-d "$body"}
}
wait_for() {  # wait_for SECONDS DESCRIPTION COMMAND…
  local seconds="$1" what="$2"; shift 2
  for _ in $(seq 1 "$seconds"); do "$@" > /dev/null 2>&1 && return 0; sleep 1; done
  fail "timed out after ${seconds}s waiting for: $what"
}

echo "start (authentik takes a minute or two to migrate on first boot)"
# `up` returns once the authentik server is healthy (the worker waits for that).
dc up -d --build gateway redis postgres authentik-db authentik-server authentik-worker > "$work/up.log" 2>&1 \
  || { tail -30 "$work/up.log" >&2; fail "compose up"; }
wait_for 120 "the gateway" curl -sf "$gw/healthz"
ok "gateway is up"
wait_for 300 "authentik" curl -sf "$idp/-/health/ready/"
wait_for 240 "the admin API token" api GET "/core/users/?username=akadmin"
blueprint_status() {  # → successful | error | warning | unknown | (empty: not discovered yet)
  api GET "/managed/blueprints/?search=ai-gateway.yaml" \
    | json '(d["results"] or [{}])[0].get("status", "")'
}
for _ in $(seq 1 180); do
  state="$(blueprint_status 2>/dev/null || true)"
  case "$state" in
    successful) break ;;
    error) api GET "/managed/blueprints/?search=ai-gateway.yaml" >&2; fail "authentik rejected the blueprint" ;;
  esac
  sleep 2
done
[ "$state" = successful ] || fail "the blueprint was not applied (status: '${state:-not discovered}')"
wait_for 60 "the gateway's OIDC application" \
  curl -sf "$idp/application/o/ai-gateway/.well-known/openid-configuration"
ok "authentik is up, the blueprint is applied"
issuer="$(curl -sf "$idp/application/o/ai-gateway/.well-known/openid-configuration" | json 'd["issuer"]')"
[ "$issuer" = "$idp/application/o/ai-gateway/" ] || fail "unexpected issuer: $issuer"

# --- join ---------------------------------------------------------------------------------
group="$(api POST /core/groups/ '{"name":"compliance"}' | json 'd["pk"]')"
user="$(api POST /core/users/ "{\"username\":\"alice\",\"name\":\"Alice Example\",\"email\":\"alice@example.com\",\"is_active\":true,\"groups\":[\"$group\"]}" | json 'd["pk"]')"
api POST /core/tokens/ "{\"identifier\":\"alice-e2e\",\"intent\":\"app_password\",\"user\":$user,\"expiring\":false}" > /dev/null
password="$(api GET /core/tokens/alice-e2e/view_key/ | json 'd["key"]')"
ok "identity provider: user alice created in group compliance"

token() {  # an access token for alice, the way a script or CLI gets one
  curl -sS --fail-with-body --max-time 30 "$idp/application/o/token/" \
    -d grant_type=client_credentials -d client_id=ai-gateway \
    -d username=alice --data-urlencode "password=$password" -d 'scope=openid profile' \
    | json 'd["access_token"]'
}
access="$(token)" || fail "no access token for alice"
python3 - "$access" <<'PY' || fail "the access token is not an RS256 JWT for the gateway"
import base64, json, sys
head, body, _ = sys.argv[1].split(".")
pad = lambda s: s + "=" * (-len(s) % 4)
h, c = (json.loads(base64.urlsafe_b64decode(pad(p))) for p in (head, body))
assert h["alg"] == "RS256", h
assert c["aud"] == "ai-gateway" and c["iss"].endswith("/application/o/ai-gateway/"), c
PY
ok "alice has an RS256 access token for audience ai-gateway"

tool() {  # → the HTTP status and error code of a tool call with alice's token
  curl -s --max-time 30 -o "$work/out" -w '%{http_code}' "$gw/v1/tools/mock_records/lookup_case" \
    -H "Authorization: Bearer $access" -H 'content-type: application/json' \
    -H 'x-gateway-data-class: restricted' -d '{"args":{"case_id":"C-1001"}}'
  python3 -c "import json;d=json.load(open('$work/out'));print(' ' + str((d.get('error') or {}).get('code') or 'ok'))" 2>/dev/null || echo " ?"
}
expect() {  # expect "STATUS CODE" DESCRIPTION: polls, since provisioning is asynchronous
  local want="$1" what="$2" got=""
  for _ in $(seq 1 90); do got="$(tool)"; [ "$got" = "$want" ] && { ok "$what ($got)"; return 0; }; sleep 1; done
  fail "$what: wanted '$want', last got '$got'"
}

[ "$(curl -s -o /dev/null -w '%{http_code}' "$gw/healthz" -H "Authorization: Bearer $access")" = 200 ] || fail "/healthz"
expect "200 ok" "provisioned over SCIM: alice's token calls a compliance-only tool"

# --- move ---------------------------------------------------------------------------------
everyone="$(api POST /core/groups/ '{"name":"everyone"}' | json 'd["pk"]')"
api POST "/core/groups/$everyone/add_user/" "{\"pk\":$user}" > /dev/null
api POST "/core/groups/$group/remove_user/" "{\"pk\":$user}" > /dev/null
expect "403 tier_too_low" "moved out of compliance: the same token loses the tool"

# --- leave --------------------------------------------------------------------------------
api PATCH "/core/users/$user/" '{"is_active":false}' > /dev/null
expect "403 user_deactivated" "deactivated in the identity provider: the same token gets 403"

# --- the evidence ---------------------------------------------------------------------------
# Decision rows are written in background batches: give the last one a moment to land.
recorded() { curl -sf "$gw/admin/decisions?limit=200" -H "Authorization: Bearer $admin_key" | grep -q "\"code\":\"$1\""; }
for code in ok tier_too_low user_deactivated; do
  wait_for 20 "a '$code' decision in the decision log" recorded "$code"
done
audit="$(curl -sf "$gw/admin/audit?limit=200" -H "Authorization: Bearer $admin_key")"
for action in scim.user.create scim.group.create scim.user.deactivate; do
  grep -q "\"$action\"" <<<"$audit" || fail "no '$action' in the admin audit log"
done
ok "decision log and provisioning audit trail are complete"
echo "identity e2e: all checks passed"
