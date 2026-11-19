#!/usr/bin/env bash
# Live check of the #/demo bank network against the real Fabric network, through the API.
# Needs: `fablo recreate` with chaincode 0.2.0 (private data collections), then
#        docker compose up -d --build   (API with DEMO_LOGIN=1, gateway with ORGS)
# Runs the story: Bank A reports, Bank B confirms, Bank C blocks; checks private data per bank and signatures.
set -u
cd "$(dirname "$0")/.." || exit 1
API="${API:-http://localhost:8000}"
for tool in curl jq; do command -v "$tool" >/dev/null || { echo "Missing tool: $tool"; exit 1; }; done
pass=0; fail=0
check() { if [ "$1" = "0" ]; then echo "  PASS  $2"; pass=$((pass+1)); else echo "  FAIL  $2"; fail=$((fail+1)); fi; }
jar() { echo "/tmp/sentinel-demo-$1.cookies"; }
call() { local who=$1 method=$2 path=$3 body=${4:-}; curl -s -b "$(jar "$who")" -c "$(jar "$who")" -X "$method" "$API$path" \
  -H 'content-type: application/json' ${body:+-d "$body"}; }

echo "1. Demo is on and the ledger is reachable"
accounts=$(curl -s "$API/demo/accounts")
echo "$accounts" | jq -e '.banks | length == 3' >/dev/null; check $? "three banks (DEMO_LOGIN=1)"
echo "$accounts" | jq -e '.ledgerWrites == true' >/dev/null; check $? "API has the ledger write token"
id_of() { echo "$accounts" | jq -r --arg u "$1" '.banks[].customers[] | select(.upi == $u) | .id'; }
for who in asha bilal chitra; do rm -f "$(jar $who)"; done
call asha POST /demo/login "{\"accountId\": $(id_of asha@banka)}" >/dev/null
call bilal POST /demo/login "{\"accountId\": $(id_of bilal@bankb)}" >/dev/null
call chitra POST /demo/login "{\"accountId\": $(id_of chitra@bankc)}" >/dev/null
scam=$(call asha POST /demo/reset | jq -r .scammer)
echo "   fresh round, scammer: $scam"

wait_recorded() {  # who -> prints the newest transfer once it is on the ledger (up to 30 s)
  for _ in $(seq 1 30); do
    t=$(call "$1" GET /demo/me | jq '.transfers[0]')
    st=$(echo "$t" | jq -r .ledgerStatus)
    [ "$st" = "recorded" ] || [ "$st" = "failed" ] && { echo "$t"; return; }
    sleep 1
  done
  echo "$t"
}

echo "2. Asha (Bank A) pays the scammer, then reports it"
v=$(call asha POST /demo/transfers "{\"toUpi\": \"$scam\", \"amount\": 20000}" | jq -r .result.verdict)
[ "$v" = "ALLOWED" ]; check $? "allowed (nothing on the ledger yet): $v"
t=$(wait_recorded asha)
echo "$t" | jq -e '.ledgerStatus == "recorded"' >/dev/null; check $? "payment recorded on the ledger with private data ($(echo "$t" | jq -r '.ledgerError // ("block " + (.ledgerBlock|tostring))'))"
tid=$(echo "$t" | jq -r .id)
r=$(call asha POST "/demo/transfers/$tid/report")
echo "$r" | jq -e '.result.action == "reported" and .result.statusAfter == "PENDING"' >/dev/null
check $? "Bank A filed a report, signed as Bank A: $(echo "$r" | jq -r '.result.statusAfter // .detail')"

echo "3. Bilal (Bank B) is warned, pays anyway, reports it"
code=$(curl -s -o /dev/null -w '%{http_code}' -b "$(jar bilal)" -X POST "$API/demo/transfers" -H 'content-type: application/json' \
  -d "{\"toUpi\": \"$scam\", \"amount\": 15000}")
[ "$code" = "409" ]; check $? "Bank B warns first (HTTP $code)"
call bilal POST /demo/transfers "{\"toUpi\": \"$scam\", \"amount\": 15000, \"acknowledgeWarning\": true}" >/dev/null
t=$(wait_recorded bilal); tid=$(echo "$t" | jq -r .id)
r=$(call bilal POST "/demo/transfers/$tid/report")
echo "$r" | jq -e '.result.action == "confirmed" and .result.statusAfter == "CONFIRMED"' >/dev/null
check $? "Bank B confirmed Bank A's report with its own key: $(echo "$r" | jq -r '.result.statusAfter // .detail')"

echo "4. Chitra (Bank C) is blocked"
r=$(call chitra POST /demo/transfers "{\"toUpi\": \"$scam\", \"amount\": 5000}")
echo "$r" | jq -e '.result.verdict == "BLOCKED" and .account.balance == 100000' >/dev/null; check $? "blocked, balance unchanged"

echo "5. Private data: each bank's peer answers differently"
pid=$(call asha GET /demo/me | jq -r '.transfers[] | select(.verdict != "BLOCKED") | .id' | head -1)
row() { call asha GET "/demo/ledger?viewer=$1" | jq --arg p "$pid" '.payments[] | select(.paymentId == $p)'; }
row BankAMSP | jq -e '.access == "member" and .details.amountPaise == 2000000 and .bankCopy.matchesChain == true' >/dev/null
check $? "Bank A (sender) reads the details; its database copy matches the chain hash"
row BankCMSP | jq -e '.access == "member" and .details != null' >/dev/null; check $? "Bank C (receiver) reads the details"
row BankBMSP | jq -e '.access == "hash-only" and .details == null and .fabricHashMatches == true' >/dev/null
check $? "Bank B gets only the hash (and Fabric's own hash matches it)"
row RegulatorMSP | jq -e '.access == "hash-only"' >/dev/null; check $? "the Regulator gets only the hash"

echo "6. Signatures in the newest blocks"
chain=$(call asha GET "/demo/chain?count=6")
echo "$chain" | jq -e '[.blocks[].txs[] | select(.type == "endorser")] | length > 0 and all(.signer.signatureValid == true and .signer.issuedByOrgCA == true)' >/dev/null
check $? "every client signature valid, every client certificate issued by its bank's CA"
echo "$chain" | jq -e '[.blocks[].txs[] | select(.type == "endorser") | .endorsements[]] | all(.signatureValid == true and .issuedByOrgCA == true)' >/dev/null
check $? "every endorsement signature valid, every peer certificate issued by its bank's CA"
echo "$chain" | jq -e '[.blocks[].txs[] | select(.fn == "recordPayment") | .privateCollections[]] | any(startswith("pay_"))' >/dev/null
check $? "payment transactions name their private collection"
echo "$chain" | jq -e '[.blocks[] | select(.linkVerified != null) | .linkVerified] | all' >/dev/null; check $? "blocks link by hash"

echo
echo "Passed $pass, failed $fail"
[ "$fail" = "0" ] || { echo "Gateway log: docker logs --tail 40 sentinel-ledger-gateway"; exit 1; }
