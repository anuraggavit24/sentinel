#!/usr/bin/env bash
# Slice 2c live check: risk API -> ledger-gateway -> Fabric (BankA's peer) -> queryPayee, end to end.
# Needs: fablo up (chaincode deployed) and docker compose up -d --build.
# Usage: cd ~/sentinel && ./scripts/ledger_smoke.sh
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1   # fablo needs the project folder; works from anywhere

CHANNEL="fraud-intel"; CC="fraudintel"
A="peer0.banka.example.com"; B="peer0.bankb.example.com"; C="peer0.bankc.example.com"
HMAC_KEY="consortium-demo-key-v1"   # must match PAYEE_HMAC_KEY in docker-compose.yml
OPERATOR="demo-operator-token-change-me"
RUN=$(date +%s)
UPI="mule.${RUN}@okaxis"            # a fresh payee each run
ORG_DIR="fablo-target/fabric-config/crypto-config/peerOrganizations/banka.example.com"
PASS=0; FAIL=0

ok()   { PASS=$((PASS + 1)); echo "   ✔ $1"; }
bad()  { FAIL=$((FAIL + 1)); echo "   ✘ $1"; }
step() { echo; echo "── $1"; }
sha()  { printf '%s' "$1" | sha256sum | cut -d' ' -f1; }
args() { jq -cn '{Args: $ARGS.positional}' --args "$@"; }
# The same normalisation and keyed hash the API uses for a UPI payee (lowercase VPA).
upi_hash() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | openssl dgst -sha256 -hmac "$HMAC_KEY" | awk '{print $NF}'; }
# Ask the ledger exactly the way a payment does: the API's own code, inside the API container.
api_lookup() {
  docker exec sentinel-api python -c "import json,sys; from app import ledger; print(json.dumps(ledger.query_payee([sys.argv[1]]).snapshot()))" "$1"
}

for tool in fablo jq openssl docker curl; do command -v "$tool" >/dev/null || { echo "Missing tool: $tool"; exit 1; }; done

# Multi-org invokes need the other banks' TLS CA certs in each org's folder (same as scenario4.sh).
base="fablo-target/fabric-config/crypto-config/peerOrganizations"
[[ -d "$base" ]] || { echo "Run this from ~/sentinel after 'fablo up'"; exit 1; }
for src in banka bankb bankc; do
  for dst in banka bankb bankc regulator; do
    [[ "$src" == "$dst" ]] && continue
    target="$base/$dst.example.com/peers/peer0.$src.example.com/tls/ca.crt"
    [[ -f "$target" ]] && continue
    sudo mkdir -p "$(dirname "$target")"
    sudo cp "$base/$src.example.com/peers/peer0.$src.example.com/tls/ca.crt" "$target"
  done
done

step "1. Crypto material the sidecar mounts"
[[ -f "$ORG_DIR/peers/$A/tls/ca.crt" ]] && ok "BankA peer TLS CA found" || bad "missing $ORG_DIR/peers/$A/tls/ca.crt (run fablo up)"
if sudo test -d "$ORG_DIR/users/User1@banka.example.com/msp/keystore"; then
  ok "User1@banka.example.com identity found"
  KEY_UID=$(sudo stat -c %u "$ORG_DIR"/users/User1@banka.example.com/msp/keystore/* | head -1)
  GW_UID=$(grep -A40 '^  ledger-gateway:' docker-compose.yml | grep -oP 'user: "\K[0-9]+' | head -1)
  [[ "$KEY_UID" == "$GW_UID" ]] && ok "key file owner (uid $KEY_UID) matches the gateway's user" \
    || bad "key file is owned by uid $KEY_UID but the gateway runs as uid $GW_UID: set user: \"$KEY_UID:$KEY_UID\" in docker-compose.yml"
else
  bad "no User1 identity; users here are:"; sudo ls "$ORG_DIR/users" 2>/dev/null | sed 's/^/      /'
  echo "      set MSP_DIR in docker-compose.yml to one of them (…/users/<name>/msp)"
fi

step "2. Bridge health (API -> sidecar -> peer ping)"
# Right after 'docker compose up' the gateway may not be listening yet: give it up to 15 s.
for _ in $(seq 1 15); do
  STATUS=$(curl -s localhost:8000/ledger/status -H "x-demo-operator-token: $OPERATOR")
  [[ $(jq -r '.reachable' <<<"$STATUS" 2>/dev/null) == "true" ]] && break
  sleep 1
done
echo "      $STATUS"
[[ $(jq -r '.reachable' <<<"$STATUS") == "true" ]] && ok "sidecar reached BankA's peer" || bad "bridge not reachable (see gateway error above; docker logs sentinel-ledger-gateway)"
[[ $(jq -r '.gateway.msp' <<<"$STATUS") == "BankAMSP" ]] && ok "chaincode sees the caller as BankAMSP" || bad "unexpected MSP"

H=$(upi_hash "$UPI")
step "3. Fresh payee $UPI is clear"
R=$(api_lookup "$H"); echo "      $R"
[[ $(jq -r '.status' <<<"$R") == "clear" ]] && ok "status clear" || bad "expected clear"

step "4. BankA files a report from its sandbox only -> still weighs nothing (and is capped at LOW severity)"
OUT=$(fablo chaincode invoke "$A,$B" "$CHANNEL" "$CC" "$(args reportMule "$H" UPI "$(sha "ev-$RUN")" SANDBOX_CAPTURE HIGH)" 2>&1)
grep -q "Chaincode invoke successful" <<<"$OUT" && ok "reportMule committed" || { bad "reportMule failed"; tail -3 <<<"$OUT"; }
RID=$(grep -oP 'txid \[\K[0-9a-f]+' <<<"$OUT" | head -1)
sleep 2
R=$(api_lookup "$H"); echo "      $R"
jq -e '.status == "hit" and .maxWeight == 0' <<<"$R" >/dev/null && ok "hit with weight 0 (honeypot alone cannot flag a payee)" || bad "expected hit, weight 0"

step "5. BankB confirms from a customer complaint -> one real bank: weight 0.25"
OUT=$(fablo chaincode invoke "$B,$C" "$CHANNEL" "$CC" "$(args confirmReport "$RID" "$(sha "conf-b-$RUN")" CUSTOMER_COMPLAINT)" 2>&1)
grep -q "Chaincode invoke successful" <<<"$OUT" && ok "confirmReport (BankB) committed" || { bad "confirmReport failed"; tail -3 <<<"$OUT"; }
sleep 2
R=$(api_lookup "$H"); echo "      $R"
jq -e '.maxWeight == 0.25 and .hardRule == false' <<<"$R" >/dev/null && ok "PENDING, weight 0.25 (+10 points)" || bad "expected weight 0.25"

step "6. BankC confirms from its own analyst -> two real banks: CONFIRMED"
OUT=$(fablo chaincode invoke "$C,$A" "$CHANNEL" "$CC" "$(args confirmReport "$RID" "$(sha "conf-c-$RUN")" ANALYST)" 2>&1)
grep -q "Chaincode invoke successful" <<<"$OUT" && ok "confirmReport (BankC) committed" || { bad "confirmReport failed"; tail -3 <<<"$OUT"; }
sleep 2
R=$(api_lookup "$H"); echo "      $R"
jq -e '.hardRule == true and .maxWeight == 1' <<<"$R" >/dev/null && ok "CONFIRMED, weight 1: payments to it are held" || bad "expected hardRule true"
LAT=$(jq -r '.latencyMs' <<<"$R"); echo "      lookup latency: ${LAT} ms"

echo
echo "Passed $PASS, failed $FAIL."
echo "In the app, pay UPI ID  $UPI  -> held, 'Banks have received fraud reports about this payee'."
[[ $FAIL -eq 0 ]]
