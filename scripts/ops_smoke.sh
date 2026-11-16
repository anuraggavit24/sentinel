#!/usr/bin/env bash
# Module 3a live check: the API's ledger outbox -> ledger-gateway -> Fabric WRITES (two-bank endorsement).
#   new report  ->  already ours (no duplicate)  ->  confirming another bank's report makes it CONFIRMED
# Needs: fablo up and docker compose up -d --build. Usage: cd ~/sentinel && ./scripts/ops_smoke.sh
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1   # fablo needs the project folder; works from anywhere

CHANNEL="fraud-intel"; CC="fraudintel"
B="peer0.bankb.example.com"; C="peer0.bankc.example.com"
HMAC_KEY="consortium-demo-key-v1"
OPERATOR="demo-operator-token-change-me"
RUN=$(date +%s)
PASS=0; FAIL=0
ok()   { PASS=$((PASS + 1)); echo "   ✔ $1"; }
bad()  { FAIL=$((FAIL + 1)); echo "   ✘ $1"; }
step() { echo; echo "── $1"; }
sha()  { printf '%s' "$1" | sha256sum | cut -d' ' -f1; }
args() { jq -cn '{Args: $ARGS.positional}' --args "$@"; }
upi_hash() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | openssl dgst -sha256 -hmac "$HMAC_KEY" | awk '{print $NF}'; }
api_lookup() {
  docker exec sentinel-api python -c "import json,sys; from app import ledger; print(json.dumps(ledger.query_payee([sys.argv[1]]).snapshot()))" "$1"
}
# Queue a filing exactly as an analyst's "reject and report" does (minus the payment), return its id.
queue_filing() {
  docker exec sentinel-api python -c "
import sys
from app import db, ledger_outbox
with db.connect() as conn, conn.transaction():
    print(ledger_outbox.enqueue(conn, payment_id=None, id_hash=sys.argv[1], id_type='UPI', severity='HIGH',
          source='ANALYST', evidence={'smoke': sys.argv[2]}, requested_by=None))" "$1" "$2"
}
# Wait for the API's background drainer to settle a filing; prints "status reportId error".
wait_filing() {
  local out=""
  for _ in $(seq 1 40); do
    out=$(docker exec sentinel-api python -c "
import sys
from app import db
with db.connect() as conn:
    r = conn.execute('SELECT status, report_id, last_error, attempts FROM ledger_outbox WHERE id = %s', (int(sys.argv[1]),)).fetchone()
print(r['status'], r['report_id'] or '-', r['attempts'], (r['last_error'] or '-').replace(' ', '_'))" "$1")
    [[ "$out" != pending* ]] && break
    sleep 1.5
  done
  echo "$out"
}

for tool in fablo jq openssl docker curl; do command -v "$tool" >/dev/null || { echo "Missing tool: $tool"; exit 1; }; done

step "1. Bridge can write"
STATUS=""
for _ in $(seq 1 15); do
  STATUS=$(curl -s localhost:8000/ledger/status -H "x-demo-operator-token: $OPERATOR")
  [[ $(jq -r '.reachable' <<<"$STATUS" 2>/dev/null) == "true" ]] && break
  sleep 1
done
echo "      $STATUS"
jq -e '.reachable == true and .gateway.writes == true' <<<"$STATUS" >/dev/null && ok "gateway reachable with writes on" || bad "gateway not reachable or writes off (LEDGER_WRITE_TOKEN in docker-compose.yml)"

H1=$(upi_hash "ops.${RUN}@okaxis")
step "2. BankA files a new report (endorsed by two banks, committed)"
ID=$(queue_filing "$H1" "new-$RUN"); echo "      outbox row $ID"
T0=$(date +%s%N); R=$(wait_filing "$ID"); T1=$(date +%s%N)
echo "      $R   ($(( (T1 - T0) / 1000000 )) ms incl. drainer interval)"
[[ "$R" == reported* ]] && ok "status reported" || bad "expected reported (docker logs sentinel-ledger-gateway)"
RID=$(awk '{print $2}' <<<"$R")
L=$(api_lookup "$H1"); echo "      $L"
jq -e --arg rid "$RID" '.status == "hit" and .reports[0].reportId == $rid and .reports[0].reporterOrg == "BankAMSP" and .maxWeight == 0.25' <<<"$L" >/dev/null \
  && ok "the ledger has BankA's report (one real bank: weight 0.25)" || bad "report not visible on the ledger"

step "3. Filing again for the same payee writes nothing (already ours)"
ID=$(queue_filing "$H1" "again-$RUN"); R=$(wait_filing "$ID"); echo "      $R"
[[ "$R" == "already_ours $RID"* ]] && ok "already_ours, same report id, no duplicate" || bad "expected already_ours"

H2=$(upi_hash "ops2.${RUN}@okaxis")
step "4. BankB reports a payee first; BankA's filing confirms it -> CONFIRMED"
OUT=$(fablo chaincode invoke "$B,$C" "$CHANNEL" "$CC" "$(args reportMule "$H2" UPI "$(sha "evb-$RUN")" ANALYST MEDIUM)" 2>&1)
grep -q "Chaincode invoke successful" <<<"$OUT" && ok "BankB reportMule committed" || { bad "BankB reportMule failed"; tail -3 <<<"$OUT"; }
sleep 2
ID=$(queue_filing "$H2" "confirm-$RUN"); R=$(wait_filing "$ID"); echo "      $R"
[[ "$R" == confirmed* ]] && ok "status confirmed (BankA confirmed BankB's report)" || bad "expected confirmed"
L=$(api_lookup "$H2"); echo "      $L"
jq -e '.hardRule == true and .maxWeight == 1 and .reports[0].reporterOrg == "BankBMSP"' <<<"$L" >/dev/null \
  && ok "CONFIRMED on the ledger: two banks, not sandbox-only" || bad "expected CONFIRMED"

step "5. Audit trail"
N=$(docker exec sentinel-api python -c "
from app import db
with db.connect() as conn:
    print(conn.execute(\"SELECT count(*) AS n FROM audit_outbox WHERE event_type = 'LEDGER_REPORT_FILED' AND payload->>'evidenceHash' IS NOT NULL\").fetchone()['n'])")
[[ "$N" -ge 3 ]] && ok "LEDGER_REPORT_FILED events recorded ($N so far)" || bad "missing LEDGER_REPORT_FILED events"

echo
echo "Passed $PASS, failed $FAIL."
[[ $FAIL -eq 0 ]]
