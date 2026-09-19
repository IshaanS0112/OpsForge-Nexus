#!/usr/bin/env bash
# Walk through OpsForge Nexus end to end against a running backend:
#
#   1. a healthy canary that walks 5% -> 25% -> 50% -> 100% and goes LIVE
#   2. a bad-config canary that its SPRT gate rolls back, opening an incident
#   3. a latency chaos drill on ledger-svc that cascades up the dependency graph
#   4. RCA and business impact for the drill's root incident
#
# Start the stack first, either on the real kind cluster:
#   docker compose up -d --build
# or on the in-process simulated cluster:
#   cd backend && CLUSTER_MODE=simulated uvicorn app.main:app
# then run ./scripts/demo.sh. Each canary stage holds for its gate window
# (45 s by default), so step 1 takes about four minutes. The anomaly layers
# need about ten minutes of traffic to learn a baseline; until then the drill
# is caught by the burn-rate page alone.
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
SERVICE="${SERVICE:-checkout-api}"
DRILL_SERVICE="${DRILL_SERVICE:-ledger-svc}"
RUN="$(date +%H%M%S)"

say() { printf "\n\033[1;36m== %s\033[0m\n" "$1"; }
# json EXPR: print a Python expression over the JSON document on stdin (bound to d).
json() { python3 -c "import sys, json; d = json.load(sys.stdin); print($1)"; }
get() { curl -fsS "$BASE$1"; }
post() {
  local body="${2:-}"
  [ -n "$body" ] || body='{}'
  curl -fsS -X POST "$BASE$1" -H 'content-type: application/json' -d "$body"
}

# Print each stage a deployment enters, with its reason, until it is LIVE or
# ROLLED_BACK.
follow_deployment() {
  local id=$1 last="" line
  for _ in $(seq 1 300); do
    line=$(get "/deployments/$id" | json "d['status'] + '  ' + (d.get('status_reason') or '')")
    if [ "${line%%  *}" != "${last%%  *}" ]; then echo "  $line"; fi
    last=$line
    case "$line" in LIVE*|ROLLED_BACK*) return 0 ;; esac
    sleep 2
  done
  echo "  still running after 10 minutes; see the Deployments screen"
}

say "0) Cluster and control loop"
get /cluster | json "f\"{d['mode']} cluster, context {d['context']}, loop running={d['control_loop']['running']}, traffic flowing={d['load_generator']['serving']}\""
until [ "$(get /cluster | json "d['load_generator']['serving']")" = "True" ]; do
  echo "  waiting for every live colour to be Ready..."; sleep 3
done

say "1) Healthy canary on $SERVICE"
DEP=$(post /deployments "{\"service_name\":\"$SERVICE\",\"version\":\"v1.1.$RUN\"}" | json "d['id']")
follow_deployment "$DEP"

say "2) Bad-config canary on $SERVICE (15% of its requests fail)"
BAD=$(post /deployments "{\"service_name\":\"$SERVICE\",\"version\":\"v1.2.$RUN\",\"simulate_failure\":true}" | json "d['id']")
follow_deployment "$BAD"
get /incidents | json "next((f\"incident {i['id']}: {i['trigger_reason']}\" for i in d if i['related_deployment_id'] == '$BAD'), 'no incident linked to the rollback')"

say "3) Chaos drill: +400 ms latency on $DRILL_SERVICE for 60 s"
EXP=$(post /chaos/inject "{\"service_name\":\"$DRILL_SERVICE\",\"fault_type\":\"LATENCY_INJECTION\",\"magnitude\":400,\"duration_s\":60}" | json "d['id']")
echo "  experiment $EXP"
for _ in $(seq 1 120); do
  [ "$(get "/chaos/experiments/$EXP" | json "d['status']")" = "COMPLETED" ] && break
  sleep 3
done
get "/chaos/experiments/$EXP" | json "'  ' + d['notes']"

ROOT=$(get /incidents | json "next((i['id'] for i in d if i['chaos_experiment_id'] == '$EXP' and not i['is_cascading']), '')")
if [ -z "$ROOT" ]; then echo "  no root incident was opened for the drill"; exit 1; fi
get "/incidents/$ROOT/cascade-tree" | json "'  root ' + d['root']['service_name'] + ' <- ' + (', '.join(c['service_name'] for c in d['children']) or 'no cascade')"

say "4) RCA over the root incident (structured signals, then the model or the rule-based fallback)"
RANKING="'\n'.join(f\"  #{n} [{c['confidence']}] {c['cause']}\" for n, c in enumerate(d['root_cause_candidates'], 1)) + f\"\n  ranked by {d['llm_model_used']}\""
echo "  told about the drill:"
post "/incidents/$ROOT/trigger-rca" | json "$RANKING"
echo "  blind (the drill withheld, evidence only):"
post "/incidents/$ROOT/trigger-rca?blind=true" | json "$RANKING"

say "5) Business impact"
get "/incidents/$ROOT/business-impact?refresh=true" | python3 -m json.tool

say "Done. The Ops Console shows all of this at http://localhost:8080 (compose) or :5173 (vite)."
