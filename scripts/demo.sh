#!/usr/bin/env bash
# End-to-end demo of the OpsForge pipeline against a running backend.
#   Terminal 1:  cd backend && HEALTH_CHECK_FAST=1 uvicorn app.main:app
#   Terminal 2:  ./scripts/demo.sh
#
# HEALTH_CHECK_FAST=1 keeps the health gate instant for a snappy demo; drop it
# to watch the gate poll over the real 60s window.
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
jq_get() { python3 -c "import sys,json;print(json.load(sys.stdin)$1)"; }

say() { printf "\n\033[1;36m== %s\033[0m\n" "$1"; }

say "1) Healthy deploy -> should go LIVE"
DEP=$(curl -s -X POST "$BASE/deployments" -H 'content-type: application/json' \
  -d '{"service_name":"checkout","version":"v1.0.0"}' | jq_get "['id']")
echo "deployment: $DEP"
for _ in $(seq 1 20); do
  ST=$(curl -s "$BASE/deployments/$DEP" | jq_get "['status']")
  echo "  status: $ST"; [ "$ST" = "LIVE" ] && break; sleep 0.5
done

say "2) Bad deploy -> health gate fails -> auto-rollback -> incident"
BAD=$(curl -s -X POST "$BASE/deployments" -H 'content-type: application/json' \
  -d '{"service_name":"checkout","version":"v2.0.0","simulate_failure":true}' | jq_get "['id']")
for _ in $(seq 1 20); do
  ST=$(curl -s "$BASE/deployments/$BAD" | jq_get "['status']")
  echo "  status: $ST"; [ "$ST" = "ROLLED_BACK" ] && break; sleep 0.5
done

INC=$(curl -s "$BASE/incidents" | jq_get "[0]['id']")
echo "incident: $INC"
curl -s "$BASE/incidents" | jq_get "[0]['trigger_reason']"

say "3) Trigger RCA (structured signals -> LLM or rule-based fallback)"
curl -s -X POST "$BASE/incidents/$INC/trigger-rca" | python3 -m json.tool

say "4) Business impact (auditable calculation_basis)"
curl -s "$BASE/incidents/$INC/business-impact" | python3 -m json.tool

say "Done."
