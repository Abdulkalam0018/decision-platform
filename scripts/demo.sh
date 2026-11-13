#!/usr/bin/env bash
# End-to-end demo against a running server:  dp serve  (or: make serve)
# Walks through: approve, reject, duplicate replay, invalid input, dependency
# outage + retry, manual review, rule change with impact analysis, audit verify.
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8000}"
EX="$(cd "$(dirname "$0")/../examples/requests" && pwd)"
RUN="$(date +%s)"
H='Content-Type: application/json'
say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
post() { curl -s -X POST "$BASE/v1/workflows/$1/requests" -H "$H" -H "Idempotency-Key: $2" --data @"$3"; }
field() { python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('$1'))"; }
# Copy an example with a per-run loan amount so re-running the demo is not caught by duplicate detection.
vary() { python3 -c "import sys,json; d=json.load(open(sys.argv[1])); d['loan']['amount'] += int(sys.argv[2]) % 997; d['applicant']['id'] = sys.argv[3] or d['applicant']['id']; print(json.dumps(d))" "$1" "$RUN" "${2:-}" > "$TMP/$(basename "$1")"; echo "$TMP/$(basename "$1")"; }
TMP="$(mktemp -d)"

say "0. Start from loan_application v1 (rolls back if an earlier run activated v2)"
curl -s -X POST "$BASE/v1/workflows/loan_application/versions/1/activate?actor=demo&reason=demo%20reset" | field active_version

say "1. Happy path: loan approved"
APPROVE=$(vary "$EX/loan_approve.json")
R1=$(post loan_application "demo-$RUN-1" "$APPROVE")
echo "$R1" | python3 -c "import sys,json; d=json.load(sys.stdin); print(json.dumps({k: d[k] for k in ('request_id','workflow_version','status')}, indent=2)); print('path:', ' -> '.join(d['decision']['path']))"
ID1=$(echo "$R1" | field request_id)

say "2. Same Idempotency-Key again: replayed, no new request"
curl -s -i -X POST "$BASE/v1/workflows/loan_application/requests" -H "$H" -H "Idempotency-Key: demo-$RUN-1" \
  --data @"$APPROVE" | grep -iE "^HTTP|idempotent-replayed" || true

say "3. Rejection with every failing rule explained"
R3=$(post loan_application "demo-$RUN-3" "$EX/loan_reject_eligibility.json")
curl -s "$BASE/v1/requests/$(echo "$R3" | field request_id)/explanation?format=text"; echo

say "4. Invalid input -> 422 problem+json"
curl -s -X POST "$BASE/v1/workflows/loan_application/requests" -H "$H" -H "Idempotency-Key: demo-$RUN-4" \
  --data @"$EX/loan_invalid.json" | python3 -m json.tool

say "5. Credit bureau outage: request parks in RETRY_PENDING, then manual retry resumes it"
curl -s -X PUT "$BASE/v1/admin/dependencies/credit_bureau/faults" -H "$H" -d '{"fail_next":["transient","timeout","transient"]}' >/dev/null
RETRY=$(vary "$EX/loan_approve.json" APP-1003)
R5=$(post loan_application "demo-$RUN-5" "$RETRY"); ID5=$(echo "$R5" | field request_id)
echo "status after submit: $(echo "$R5" | field status)"
echo "status after retry:  $(curl -s -X POST "$BASE/v1/requests/$ID5/retry" | field status)"
curl -s "$BASE/v1/requests/$ID5/history" | python3 -c "import sys,json; [print(' ', t['from_status'], '->', t['to_status'], '|', t['reason']) for t in json.load(sys.stdin)['transitions']]"

say "6. Manual review then reviewer decision"
R6=$(post loan_application "demo-$RUN-6" "$(vary "$EX/loan_manual_review.json")"); ID6=$(echo "$R6" | field request_id)
echo "engine: $(echo "$R6" | field status)"
curl -s -X POST "$BASE/v1/requests/$ID6/review" -H "$H" \
  -d '{"decision":"approve","reviewer":"underwriter.kim","comment":"Salary slips verified"}' | field status

say "7. Rule change: publish v2, check impact, activate"
curl -s -X POST "$BASE/v1/workflows?actor=risk-team" --data-binary @"$(dirname "$0")/../config/candidates/loan_application.v2.yaml" | python3 -m json.tool || true
curl -s "$BASE/v1/workflows/loan_application/impact?from_version=1&to_version=2" | python3 -c "import sys,json; d=json.load(sys.stdin); print({k: d[k] for k in ('replayed','changed','by_transition')})"
curl -s -X POST "$BASE/v1/workflows/loan_application/versions/2/activate?actor=risk-team&reason=demo" | python3 -m json.tool

say "8. Audit chain verification"
curl -s "$BASE/v1/admin/audit/verify" | python3 -m json.tool
echo; echo "Swagger UI: $BASE/docs"
