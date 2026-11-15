#!/usr/bin/env bash
# Sentinel Phase 1: scenario 4 end to end against the live Fablo network.
# Usage: cd ~/sentinel && ./scripts/scenario4.sh
set -uo pipefail

CHANNEL="fraud-intel"
CC="fraudintel"
A="peer0.banka.example.com"
B="peer0.bankb.example.com"
C="peer0.bankc.example.com"
R="peer0.regulator.example.com"
HMAC_KEY="consortium-demo-key-v1"   # demo only; in production the 3 banks hold this, not the Regulator
RUN=$(date +%s)                       # makes identifiers unique so the script can be re-run

PASS=0
FAIL=0
COMMIT_MS=()
QUERY_MS=()
OUT=""
TXID=""
RESULT=""

# Milliseconds from bash's built-in clock (portable; some newer `date` builds ignore %3N).
ms() { local t=${EPOCHREALTIME//[.,]/}; echo $((t / 1000)); }
sha() { printf '%s' "$1" | sha256sum | cut -d' ' -f1; }

# Off-chain identifier normalisation + keyed hash (spec: IFSC + account, uppercase, no spaces).
idhash_account() {
  local ifsc acct
  ifsc=$(printf '%s' "$1" | tr '[:lower:]' '[:upper:]' | tr -d ' ')
  acct=$(printf '%s' "$2" | tr -d ' ')
  printf '%s' "${ifsc}|${acct}" | openssl dgst -sha256 -hmac "$HMAC_KEY" | awk '{print $NF}'
}

args_json() { jq -cn '{Args: $ARGS.positional}' --args "$@"; }

# invoke <peers_comma_separated> <fn> [args...]  (the first peer's org signs the transaction)
inv() {
  local peers=$1; shift
  local cmd t0 t1
  cmd=$(args_json "$@")
  t0=$(ms)
  OUT=$(fablo chaincode invoke "$peers" "$CHANNEL" "$CC" "$cmd" 2>&1)
  t1=$(ms)
  if grep -q "Chaincode invoke successful" <<<"$OUT"; then
    COMMIT_MS+=($((t1 - t0)))
    TXID=$(grep -oP 'txid \[\K[0-9a-f]+' <<<"$OUT" | head -1)
    return 0
  fi
  return 1
}

# query <peer> <fn> [args...]
q() {
  local peer=$1; shift
  local cmd t0 t1
  cmd=$(args_json "$@")
  t0=$(ms)
  OUT=$(fablo chaincode query "$peer" "$CHANNEL" "$CC" "$cmd" 2>&1)
  t1=$(ms)
  RESULT=$(tail -1 <<<"$OUT")
  if [[ "$RESULT" == \{* ]]; then
    QUERY_MS+=($((t1 - t0)))
    return 0
  fi
  return 1
}

payee() { q "$1" queryPayee "$(jq -cn --arg h "$2" '[$h]')"; }

check() {
  local label=$1 actual=$2 expected=$3
  if [[ "$actual" == "$expected" ]]; then
    PASS=$((PASS + 1)); echo "   ✔ $label"
  else
    FAIL=$((FAIL + 1)); echo "   ✘ $label (expected '$expected', got '$actual')"
  fi
}

must() {
  local label=$1; shift
  if "$@"; then
    PASS=$((PASS + 1)); echo "   ✔ $label"
  else
    FAIL=$((FAIL + 1)); echo "   ✘ $label"; echo "$OUT" | tail -5 | sed 's/^/      /'
  fi
}

expect_reject() {
  local label=$1 pattern=$2; shift 2
  if inv "$@"; then
    FAIL=$((FAIL + 1)); echo "   ✘ $label (transaction was accepted, expected rejection)"
  elif grep -q "$pattern" <<<"$OUT"; then
    PASS=$((PASS + 1)); echo "   ✔ $label"
  else
    FAIL=$((FAIL + 1)); echo "   ✘ $label (rejected, but not with '$pattern')"; echo "$OUT" | tail -3 | sed 's/^/      /'
  fi
}

pct() {
  local p=$1; shift
  [[ $# -eq 0 ]] && { echo "n/a"; return; }
  printf '%s\n' "$@" | sort -n | awk -v p="$p" '{a[NR]=$1} END {i=int((NR-1)*p/100)+1; print a[i]}'
}

step() { echo; echo "── $1"; }

# Fablo's CLI containers only hold their own org's TLS certs. Copy the bank peers' CA certs into
# every org so multi-org invokes work. Wiped by 'fablo down/recreate', so the script re-runs it.
ensure_certs() {
  local base="fablo-target/fabric-config/crypto-config/peerOrganizations"
  [[ -d "$base" ]] || { echo "Run this from ~/sentinel after 'fablo up'"; exit 1; }
  for src in banka bankb bankc; do
    for dst in banka bankb bankc regulator; do
      [[ "$src" == "$dst" ]] && continue
      local target="$base/$dst.example.com/peers/peer0.$src.example.com/tls/ca.crt"
      [[ -f "$target" ]] && continue
      sudo mkdir -p "$(dirname "$target")"
      sudo cp "$base/$src.example.com/peers/peer0.$src.example.com/tls/ca.crt" "$target"
    done
  done
}

for tool in fablo jq openssl docker curl; do
  command -v "$tool" >/dev/null || { echo "Missing tool: $tool"; exit 1; }
done
ensure_certs

MULE=$(idhash_account "HDFC0000123" "5010${RUN}")
INNOCENT=$(idhash_account "SBIN0000456" "3000${RUN}")
CHAOS=$(idhash_account "UTIB0000789" "9170${RUN}")

echo "Sentinel scenario 4 · run $RUN"

step "1. BankA reports a mule account (ANALYST)"
must "reportMule accepted" inv "$A,$B" reportMule "$MULE" ACCOUNT "$(sha "ev-a-$RUN")" ANALYST HIGH
MULE_RID=$TXID
q "$C" getReport "$MULE_RID"
check "status is PENDING" "$(jq -r '.status' <<<"$RESULT")" "PENDING"
check "reporter is BankAMSP" "$(jq -r '.reporterOrg' <<<"$RESULT")" "BankAMSP"

step "2. BankA tries to confirm its own report"
expect_reject "self-confirmation rejected" "own report" "$A,$B" confirmReport "$MULE_RID" "$(sha "conf-a-$RUN")" ANALYST

step "3. BankB confirms with CUSTOMER_COMPLAINT"
must "confirmReport accepted" inv "$B,$A" confirmReport "$MULE_RID" "$(sha "conf-b-$RUN")" CUSTOMER_COMPLAINT
q "$C" getReport "$MULE_RID"
check "status is CONFIRMED" "$(jq -r '.status' <<<"$RESULT")" "CONFIRMED"

step "4. BankC looks up the payee from its own peer"
payee "$C" "$MULE"
check "weight is 1" "$(jq -r '.maxWeight' <<<"$RESULT")" "1"
check "hard rule eligible" "$(jq -r '.hardRuleEligible' <<<"$RESULT")" "true"

step "5. Poisoning: BankA sandbox-captures an innocent account"
must "sandbox report accepted" inv "$A,$B" reportMule "$INNOCENT" ACCOUNT "$(sha "ev-sbx-$RUN")" SANDBOX_CAPTURE HIGH
INNOCENT_RID=$TXID
q "$C" getReport "$INNOCENT_RID"
check "severity capped at LOW" "$(jq -r '.severity' <<<"$RESULT")" "LOW"
payee "$C" "$INNOCENT"
check "weight is 0" "$(jq -r '.maxWeight' <<<"$RESULT")" "0"

step "6. Two-sandbox attack: BankB confirms as SANDBOX_CAPTURE"
must "confirm accepted" inv "$B,$A" confirmReport "$INNOCENT_RID" "$(sha "conf-b-sbx-$RUN")" SANDBOX_CAPTURE
payee "$C" "$INNOCENT"
check "still PENDING" "$(jq -r '.reports[0].status' <<<"$RESULT")" "PENDING"
check "weight still 0" "$(jq -r '.maxWeight' <<<"$RESULT")" "0"

step "6b. One honest bank cannot blacklist: BankC confirms as ANALYST"
must "confirm accepted" inv "$C,$A" confirmReport "$INNOCENT_RID" "$(sha "conf-c-$RUN")" ANALYST
payee "$A" "$INNOCENT"
check "still PENDING" "$(jq -r '.reports[0].status' <<<"$RESULT")" "PENDING"
check "weight 0.25" "$(jq -r '.maxWeight' <<<"$RESULT")" "0.25"
check "hard rule NOT eligible" "$(jq -r '.hardRuleEligible' <<<"$RESULT")" "false"

step "7. Same digits, different bank (IFSC) do not match; messy input normalises"
payee "$C" "$(idhash_account "SBIN0000456" "5010${RUN}")"
check "different IFSC: no reports" "$(jq -r '.reports | length' <<<"$RESULT")" "0"
check "lowercase/spaced input hashes the same" "$(idhash_account " hdfc0000123 " "5010 ${RUN}")" "$MULE"

step "8. Audit log: on-chain anchors expose tampering, even by an insider with the signing key"
AUDIT="http://localhost:8800"
if ! curl -sf "$AUDIT/health" >/dev/null 2>&1; then
  FAIL=$((FAIL + 1))
  echo "   ✘ audit service not reachable. Start it: docker compose up -d --build"
else
  ANCHOR_DATE=$(date -d "+$((RUN % 36500)) days" +%Y-%m-%d)

  audit_append() {  # audit_append <stream> <i> <decision>
    curl -sf -X POST "$AUDIT/events" -H 'content-type: application/json' \
      -d "$(jq -cn --arg s "$1" --argjson i "$2" --arg d "$3" \
            '{stream:$s, eventType:"RISK_DECISION", payload:{session:("s-" + ($i|tostring)), score:($i * 17), decision:$d}}')" >/dev/null
  }
  audit_verify() { curl -sf "$AUDIT/streams/$1/verify?anchor=$2"; }

  # 8a. BankA logs decisions and anchors its chain head on the ledger
  STREAM_A="BankAMSP-run$RUN"
  for i in 1 2 3 4 5; do audit_append "$STREAM_A" "$i" ALLOW; done
  ROOT_A=$(curl -sf "$AUDIT/streams/$STREAM_A/root" | jq -r '.root')
  must "BankA anchors its log root on the ledger" inv "$A,$B" anchorAuditRoot "$ANCHOR_DATE" "$ROOT_A"
  expect_reject "anchor cannot be overwritten" "cannot be overwritten" "$A,$B" anchorAuditRoot "$ANCHOR_DATE" "$(sha "fake-root-$RUN")"
  q "$C" getAnchor BankAMSP "$ANCHOR_DATE"
  ONCHAIN_A=$(jq -r '.rootHash' <<<"$RESULT")
  V=$(audit_verify "$STREAM_A" "$ONCHAIN_A")
  check "untouched log: chain valid" "$(jq -r '.chainValid' <<<"$V")" "true"
  check "untouched log: matches on-chain anchor" "$(jq -r '.anchorMatches' <<<"$V")" "true"

  # 8b. Someone edits a row directly in Postgres (no signing key)
  docker exec sentinel-db psql -U sentinel -d audit -qc \
    "UPDATE audit_log SET payload = replace(payload, 'ALLOW', 'BLOCK') WHERE stream = '$STREAM_A' AND idx = 2" >/dev/null
  echo "   (edited entry 2 directly in Postgres)"
  V=$(audit_verify "$STREAM_A" "$ONCHAIN_A")
  check "DB edit: chain reported broken" "$(jq -r '.chainValid' <<<"$V")" "false"
  check "DB edit: exact entry identified" "$(jq -r '.firstBadIdx' <<<"$V")" "2"
  check "DB edit: no longer matches anchor" "$(jq -r '.anchorMatches' <<<"$V")" "false"

  # 8c. An insider with DB access AND the signing key rewrites history and re-signs it
  STREAM_B="BankBMSP-run$RUN"
  for i in 1 2 3 4 5; do audit_append "$STREAM_B" "$i" ALLOW; done
  ROOT_B=$(curl -sf "$AUDIT/streams/$STREAM_B/root" | jq -r '.root')
  must "BankB anchors its log root on the ledger" inv "$B,$A" anchorAuditRoot "$ANCHOR_DATE" "$ROOT_B"
  q "$C" getAnchor BankBMSP "$ANCHOR_DATE"
  ONCHAIN_B=$(jq -r '.rootHash' <<<"$RESULT")
  curl -sf -X POST "$AUDIT/demo/insider-rewrite" -H 'content-type: application/json' \
    -d "$(jq -cn --arg s "$STREAM_B" '{stream:$s, idx:2, payload:{session:"s-3", score:51, decision:"BLOCK"}}')" >/dev/null
  echo "   (insider rewrote entry 2 and re-signed every later entry)"
  V=$(audit_verify "$STREAM_B" "$ONCHAIN_B")
  check "insider rewrite: internal checks pass (hash chain alone is fooled)" "$(jq -r '.chainValid' <<<"$V")" "true"
  check "insider rewrite: caught by the on-chain anchor" "$(jq -r '.anchorMatches' <<<"$V")" "false"
fi

step "9. Dispute: BankC disputes the confirmed mule report, Regulator overturns"
must "dispute accepted" inv "$C,$A" disputeReport "$MULE_RID" "$(sha "reason-c-$RUN")"
payee "$A" "$MULE"
check "weight halves to 0.5" "$(jq -r '.maxWeight' <<<"$RESULT")" "0.5"
check "hard rule stops firing" "$(jq -r '.hardRuleEligible' <<<"$RESULT")" "false"
must "Regulator ruling accepted" inv "$R,$A,$B" resolveDispute "$MULE_RID" OVERTURNED
payee "$A" "$MULE"
check "weight drops to 0" "$(jq -r '.maxWeight' <<<"$RESULT")" "0"
q "$R" getHistory "$MULE_RID"
check "full history kept" "$(jq -r '[.entries[].value.status] | join(",")' <<<"$RESULT")" "PENDING,CONFIRMED,DISPUTED,OVERTURNED"

step "10. The referee's limits: Regulator cannot report or confirm"
expect_reject "Regulator reportMule rejected" "Only member banks" "$R,$A,$B" reportMule "$(sha "reg-$RUN")" ACCOUNT "$(sha "reg-ev-$RUN")" ANALYST HIGH
expect_reject "Regulator confirmReport rejected" "Only member banks" "$R,$A,$B" confirmReport "$INNOCENT_RID" "$(sha "reg-conf-$RUN")" ANALYST

step "11. Even the referee cannot bypass the 2-bank rule: dispute + UPHELD never promotes"
must "BankB disputes the innocent report" inv "$B,$A" disputeReport "$INNOCENT_RID" "$(sha "reason-b-$RUN")"
must "Regulator upholds it" inv "$R,$A,$B" resolveDispute "$INNOCENT_RID" UPHELD
payee "$C" "$INNOCENT"
check "status restored to PENDING, not CONFIRMED" "$(jq -r '.reports[0].status' <<<"$RESULT")" "PENDING"
check "hard rule NOT eligible" "$(jq -r '.hardRuleEligible' <<<"$RESULT")" "false"

step "Chaos: stop BankC's peer, keep committing with BankA + BankB"
docker stop "$C" >/dev/null && echo "   BankC peer stopped"
must "report commits with BankC down" inv "$A,$B" reportMule "$CHAOS" ACCOUNT "$(sha "ev-chaos-$RUN")" ANALYST MEDIUM
docker start "$C" >/dev/null && echo "   BankC peer restarted, waiting for it to catch up"
CAUGHT_UP="no"
for _ in $(seq 1 30); do
  if payee "$C" "$CHAOS" && [[ "$(jq -r '.reports | length' <<<"$RESULT")" == "1" ]]; then CAUGHT_UP="yes"; break; fi
  sleep 2
done
check "BankC caught up with the missed block" "$CAUGHT_UP" "yes"

echo
echo "══════════════════════════════════════════════"
echo " Result: $PASS passed, $FAIL failed"
echo " Commit latency (CLI, includes docker exec): p50 $(pct 50 "${COMMIT_MS[@]}") ms · p95 $(pct 95 "${COMMIT_MS[@]}") ms · n=${#COMMIT_MS[@]}"
echo " Query latency  (CLI, includes docker exec): p50 $(pct 50 "${QUERY_MS[@]}") ms · p95 $(pct 95 "${QUERY_MS[@]}") ms · n=${#QUERY_MS[@]}"
echo " Note: CLI numbers include container start-up overhead; true peer query latency is measured"
echo "       with the Gateway client in Phase 2. Commit time is dominated by the 2 s orderer BatchTimeout."
echo "══════════════════════════════════════════════"
[[ $FAIL -eq 0 ]]
