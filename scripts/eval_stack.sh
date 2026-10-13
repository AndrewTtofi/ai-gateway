#!/usr/bin/env bash
# The gateway, started for an eval run (ADR 0035): the shared eval action calls the two
# models through this gateway's API, so the comparison takes the governed path and every
# call is in the decision log.
#
#   scripts/eval_stack.sh up      # start it, create a key for the run, print the key
#   scripts/eval_stack.sh check N # at least N model calls by that key were decided "allow"
#   scripts/eval_stack.sh down    # stop it and delete its data
#
# Its own compose project and volumes: a dev stack's data is not touched, but the ports
# are the dev stack's (8000, 6379, 5432), so stop that first. Without a .env one is made
# from .env.example with a generated admin key (and removed by `down`); provider keys are
# taken from the environment (ANTHROPIC_API_KEY, OPENAI_API_KEY) if set.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
project="gwevals"
gw="http://127.0.0.1:8000"
marker=".env.created-by-eval-stack"
dc() { docker compose -p "$project" "$@"; }
fail() { echo "eval stack: $*" >&2; exit 1; }
# The admin key goes to curl on stdin (-H @-), never on a command line where `ps` shows it.
admin() { sed -n 's/^GATEWAY_ADMIN_KEY=/Authorization: Bearer /p' .env | curl -sS --fail-with-body --max-time 30 -H @- "$@"; }

case "${1:-}" in
  up)
    if [ ! -f .env ]; then
      {
        grep -vE '^(GATEWAY_ADMIN_KEY|ANTHROPIC_API_KEY|OPENAI_API_KEY)=' .env.example
        echo "GATEWAY_ADMIN_KEY=gw_admin_$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
        echo "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY:-}"
        echo "OPENAI_API_KEY=${OPENAI_API_KEY:-}"
      } > .env
      touch "$marker"
    fi
    dc up -d --build gateway redis postgres > /tmp/eval-stack-up.log 2>&1 \
      || { tail -30 /tmp/eval-stack-up.log >&2; fail "compose up"; }
    for _ in $(seq 1 120); do curl -sf "$gw/healthz" > /dev/null 2>&1 && break; sleep 1; done
    curl -sf "$gw/healthz" > /dev/null || { dc logs --tail 40 gateway >&2; fail "the gateway did not come up"; }
    # One key for the run: any model, because a candidate is by definition not in a tier's
    # list yet, and a small budget of its own, so an eval can't spend more than this.
    budget="${EVAL_BUDGET_USD:-5}"
    admin -X POST "$gw/admin/keys" -H 'content-type: application/json' \
      -d "{\"name\":\"model-evals\",\"tier\":\"standard\",\"allowed_aliases\":[\"*\"],\"monthly_budget_usd\":$budget}" \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["key"])'
    ;;
  check)
    want="${2:?how many calls}"
    sleep 2   # the decision log is written in batches, a moment behind the request
    got="$(admin "$gw/admin/decisions?limit=1000&decision=allow" | python3 -c '
import json, sys
rows = json.load(sys.stdin)["data"]
print(sum(1 for r in rows if r.get("subject_name") == "model-evals" and str(r.get("action", "")).startswith("model:")))')"
    echo "decision log: $got model call(s) by the eval key were allowed (expected at least $want)"
    [ "$got" -ge "$want" ] || fail "the eval's calls are not all in the decision log"
    ;;
  down)
    dc down -v --remove-orphans > /dev/null 2>&1 || true
    if [ -f "$marker" ]; then rm -f .env "$marker"; fi
    ;;
  *) echo "usage: $0 up | check N | down" >&2; exit 64 ;;
esac
