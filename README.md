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

23 chaincode tests, including attack tests:
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
| Insider with DB access **and** the signing key rewrites and re-signs | **not caught** | caught, **for entries at or before the last anchor** |
| Same insider rewrites entries added **after** the last anchor | not caught | **not caught** until the next anchor (anchor often) |

Writes to the audit service need the `x-audit-token` header (`AUDIT_TOKEN`). The service signs whatever
it is given, so without the token anyone who can reach the port could append forged entries that verify.
Both services listen on `127.0.0.1` only.

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
- Passkeys are discoverable, so sign-in options never list credential IDs. The options are identical
  for every caller, and sign-in works with or without typing a username.
- A passkey from another account cannot sign in to yours (credential ID, typed username and user handle must all agree).
- A sign counter that goes backwards is rejected as a possibly cloned authenticator, and the alert is
  recorded even though the login fails. Synced passkeys (Google, Apple, Windows Hello) usually report a
  counter of 0, which turns this check off; it mainly protects hardware security keys.
- You cannot remove your only passkey, even with two removals racing (the user's passkeys are locked first).
  Removing a passkey ends the sessions opened with it.
- Session tokens are random, stored only as SHA-256 hashes, and sent in an httpOnly cookie.

Known limit: **registration** still reveals whether a username is taken (it has to say "taken").
Hiding that needs email or phone verification before sign-up.

### Audit events: transactional outbox

Every security event (registered, signed in, cloned authenticator, device added or removed, session revoked,
logout) is written to `audit_outbox` **in the same database transaction** as the action. A background drainer
forwards events to the audit-log service in order and retries while it is down. Events about rejected requests
(cloned authenticator) are written on a separate connection so they survive the rollback.
`GET /health` reports `auditBacklog`, the number of events not yet delivered.

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

24 API tests (ceremonies, replay, phishing origin, enumeration, clone alert, forged clone alert, removal race, outbox outage, idempotent delivery)
and 13 audit-service tests.

The tests use a software authenticator (`api/tests/soft_authenticator.py`) that produces real
P-256 keys, CBOR and signatures, so every ceremony is verified exactly as a browser's would be.
`web/e2e/passkey.e2e.mjs` drives the real UI in Chrome with a virtual authenticator.

## Security review log

Bugs found in review after Module 1 shipped, each with the test that proves the fix.

| # | Bug | Impact | Fix | Test |
|---|---|---|---|---|
| 1 | Clone alert queued as a FastAPI background task, then the endpoint raised | Background tasks only run on a normal return, so the most important alert was **never recorded** | Transactional outbox; rejected-request events written on their own connection | `test_counter_rollback_flags_a_cloned_authenticator` (checks the event, not just the 400) |
| 2 | Login options listed a real user's credential IDs and an unknown user's as empty | Anyone could tell which usernames exist from one request; the README claimed the opposite | Discoverable passkeys; login options identical for everyone, database not queried | enumeration test compares a real and an unknown user |
| 3 | Audit service signed any write and was reachable on the network | Anyone on the same Wi-Fi could append forged, validly signed entries | `x-audit-token` (constant-time compare); ports bound to 127.0.0.1 | `test_writes_require_the_token_when_configured` |
| 4 | Two concurrent passkey removals | Account left with zero passkeys (locked out) | Lock the user's passkeys `FOR UPDATE` first | removal race test (second connection holds the lock) |
| 5 | py_webauthn checks the sign counter **before** the signature | A forged response (attacker's key, known credential ID, low counter) logged a "cloned authenticator" alert against an innocent user; in Module 2 that alert raises their risk score | Verify the signature first (counter check off), then compare counters ourselves | `test_forged_rollback_with_bad_signature_does_not_raise_a_clone_alert` |
| 6 | Audit service with no `AUDIT_TOKEN` accepted unauthenticated writes | A forgotten setting silently opened the log | Fail closed: 503 unless `AUDIT_ALLOW_UNAUTHENTICATED=1` (tests only) | `test_writes_are_refused_when_no_token_is_configured` |
| 7 | Outbox delivery was at-least-once with no deduplication | A crash after the audit service stored an event but before it was marked sent wrote the event **twice** into the hash chain | Stable `idempotencyKey` per outbox row; the audit service stores a repeat once | `test_retried_delivery_with_same_idempotency_key_is_stored_once`, `test_drainer_sends_a_stable_idempotency_key_per_event` |

Still open: no connection pool (psycopg_pool), no cleanup job for expired challenges and sessions,
strict audit ordering only with one API instance, the drainer holds a transaction while it calls the
audit service, a failed sign-in does not burn its challenge, and registration still reveals taken usernames.

## Phase 2 · Module 2a: explainable risk scoring for payments

Every payment gets a score from 0 to 100, a band, and the reasons behind each point.

| Band | Score | Meaning (Module 3 implements the action) |
|---|---|---|
| allow | 0–39 | Goes through |
| step_up | 40+ | Passkey re-authentication, a warning, possibly a cooling-off delay |
| sandbox | 70+ **and** a takeover signal (SIM swap or failed integrity) **on a new device** | Fake balance, no real money moves, fraud ops alerted |

A coached customer (on a call, screen shared) can score 90 and still only get **step_up**: they are the
real customer on their own device, so showing them a fake balance would be wrong.

| Signal group | Signals | Source in this web demo |
|---|---|---|
| Transaction | new payee, amount vs your usual (median of completed payments), very large amount, night time (IST), rapid payments | real, from your payment history |
| Telecom | payee mobile number's FRI category (also for mobile-number UPI IDs), recent SIM swap | FRI **mocked**; SIM swap **injected** |
| Device | new device, failed integrity check, synced passkey on a new device | synced flag real (from the passkey); others **injected** |
| Coaching | active call, screen sharing, accessibility service | **injected** (a browser cannot read these) |
| Consortium | payee reported on the ledger | slice 2c |

Engine `rules-v1`: weighted rules, not ML. Slice 2b adds behaviour models; the rules stay as the
explainable safety net. Injected signals are recorded in the audit log (`DEMO_SIGNALS_SET`) and every
decision is recorded as `RISK_DECISION` (score, band, reason codes; never the payee identifier).

Privacy: payee identifiers are normalised (IFSC + account, lowercase UPI, +91 mobile) and stored only as
an HMAC with the consortium key, the same hash the ledger uses, plus a masked display string.

Client IP: `X-Forwarded-For` is honoured only when the request comes from a trusted proxy
(`TRUSTED_PROXIES`: loopback and Docker's private range); otherwise it is ignored, so it cannot be spoofed.
On a single laptop the "client" is still localhost; the fix matters once the app is deployed.

Tests: 27 unit tests (normalisation, every rule and band, client IP), 20 API tests on Postgres, and
`web/e2e/payments.e2e.mjs` (takeover → sandbox, coached → step-up, FRI, validation) in real Chrome.
