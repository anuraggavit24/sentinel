# Sentinel: consortium fraud-intelligence ledger

> **Before `fablo up`:** `cd chaincode/fraudintel && npm install && npm run build`
> `dist/` is git-ignored, so a fresh clone must build first.
> Run everything inside WSL2, not PowerShell.

Hyperledger Fabric chaincode (TypeScript) for sharing mule-account reports between banks,
with a 2-bank non-sandbox confirmation rule and a Regulator that can only rule on disputes.

## Tests

```bash
cd chaincode/fraudintel
npm test
```

23 tests, including attack tests:
- two-sandbox poisoning
- one bank plus a sandbox capture
- a dispute escalated through the Regulator
- partial writes from failed transactions

The test stub mirrors Fabric's commit model: reads never see uncommitted writes,
and a failed transaction leaves no writes behind.

## Scenario 4 on the live network

```bash
cd ~/sentinel
fablo up                      # first time, or after fablo down
./scripts/scenario4.sh        # report, confirm, poisoning, disputes, referee limits, chaos test
```

Uses Fabric 2.5.16 (2.5.15+ is required for Docker Engine 29).

## Audit-log service (step 8)

Each bank keeps an append-only log of its decisions in Postgres. Every entry stores the previous
entry's hash and an Ed25519 signature. The bank anchors the chain head on the ledger with
`anchorAuditRoot`, and `/verify` checks both the chain and the on-chain anchor.

| Attack | Hash chain alone | With on-chain anchor |
|---|---|---|
| Edit a row directly in Postgres | caught (hash mismatch at that entry) | caught |
| Insider with DB access **and** the signing key rewrites and re-signs | **not caught** | caught |

```bash
docker compose up -d --build   # from the repo root: Postgres, audit API (:8800), Sentinel API (:8000)
```

Tests (unit tests always run; API tests need a Postgres via `DATABASE_URL`):

```bash
cd audit
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

`DEMO_INSIDER=1` enables `/demo/insider-rewrite` for the demo only. The signing seed in
`docker-compose.yml` is a demo value; a real bank would keep the key in an HSM.

## Phase 2 · Module 1: passwordless login (passkeys)

No passwords and no SMS OTPs. Users register and sign in with a passkey (Windows Hello, Touch ID,
an Android phone or a security key). The private key never leaves the device; the server stores
only the public key.

| Part | Where | What it does |
|---|---|---|
| `api/` | FastAPI + py_webauthn + Postgres | Registration, sign-in, devices, sessions; audit events to the audit log |
| `web/` | React + Vite + @simplewebauthn/browser | Sign-up, sign-in, manage passkeys and sessions |

Security rules enforced (each one has a test):
- Challenges are single use and expire after 5 minutes (replay is rejected).
- The browser origin and relying-party ID must match (a phishing site's response is rejected).
- User verification is required (fingerprint, face or PIN, not just a tap).
- A sign counter that goes backwards is treated as a possibly cloned authenticator.
- A passkey from another account cannot sign in to yours.
- Unknown usernames get the same response as real ones, so usernames cannot be enumerated.
- You cannot remove your only passkey; removing a passkey ends the sessions opened with it.
- Session tokens are random, stored only as SHA-256 hashes, and sent in an httpOnly cookie.

### Run it

```bash
docker compose up -d --build          # repo root
cd web && npm install && npm run dev  # then open http://localhost:5173 in Chrome or Edge on Windows
```

### Tests

```bash
cd api && python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
DATABASE_URL=postgresql://sentinel:sentinel@localhost:5432/sentinel_test pytest -q   # needs Postgres
```

The tests use a software authenticator (`api/tests/soft_authenticator.py`) that produces real
P-256 keys, CBOR and signatures, so every ceremony is verified exactly as a browser's would be.
`web/e2e/passkey.e2e.mjs` drives the real UI in Chrome with a virtual authenticator.
