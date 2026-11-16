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

| 8 | A payee became "known" after any completed payment, of any size, at any time | The ₹1 "verification" trick: coached ₹90,000 payment went from step_up (65) to allow (35) | Trust needs ≥ ₹500, > 24 h old, no coaching signals | `test_one_rupee_test_payment_does_not_unlock_a_large_coached_payment` and 3 more |
| 9 | No rule on the total sent to one payee | 3 × ₹9,999 on a call all allowed | Running 24 h total per untrusted payee and across untrusted payees | `test_splitting_a_large_amount_on_a_call_is_caught`, `test_spreading_money_over_many_new_payees_adds_up` |
| 10 | Customer-facing reason named the FRI category | Any customer could look up a number's FRI rating | Customer sees a generic telecom warning; category, points and score only in the analyst view | `test_customer_cannot_read_fri_category` |
| 11 | The session being scored controlled its own demo signals | An attacker in the session could clear the SIM-swap signal | Operator token, per-session endpoint; old endpoint removed | `test_session_cannot_change_its_own_signals` |
| 12 | Scoring had no idempotency | Double-click or network retry paid twice | Required `Idempotency-Key`, stored response, unique index | `test_retry_with_same_idempotency_key_does_not_pay_twice`, `test_parallel_duplicate_submits_create_one_payment` |
| 13 | Parallel requests counted uncommitted rows | 8 parallel payments: only 2 flagged as rapid | Per-user advisory lock around scoring | `test_parallel_burst_is_fully_counted_by_the_velocity_rule` |
| 14 | Amount parsed with `Decimal` directly | `Infinity` → 500, `1e5` and Arabic-Indic digits accepted | ASCII pattern checked before conversion | `test_hostile_amounts_are_rejected_cleanly` (11 inputs) |
| 15 | Vite proxy appended to `X-Forwarded-For` | The browser could choose the IP the API records | The proxy overwrites the header with the socket address | verified with an echo server: `6.6.6.6` sent, `127.0.0.1` received |
| 16 | `PAYEE_HMAC_KEY` had a default in code | A demo key could silently reach a deployment | No default; payments return 503 until it is set | `test_payments_refused_without_hmac_key` |

| 17 | "Sent" totals counted held payments | Found in a live demo: one held ₹1.85 lakh attempt pushed every new-payee payment (even ₹250) to step-up for 24 hours, and the customer was told money was "sent" | Totals count completed payments only; held attempts still count for velocity | `test_held_payments_do_not_count_as_money_sent` (fails without the fix) |
| 18 | Behaviour model scored form-dependent features | Owner's own typing scored 0.98 after enrolling on the practice form | Model uses rhythm features only | `test_form_context_does_not_count_as_identity`; browser test: owner 0.05 |
| 19 | An allowed payment with anomalous typing was learned | An attacker whose payment slips through would teach the profile their rhythm | Samples with anomaly ≥ 0.3 are never learned | `test_anomalous_typing_is_never_learned_even_if_the_payment_is_allowed` |
| 20 | Schema DDL ran on every start-up | `ALTER TABLE ... IF NOT EXISTS` takes an exclusive lock even when nothing changes: concurrent start-ups deadlocked in-flight payments (flaky test, 2 of 6 runs); in production, a rolling restart | DDL only when the schema version is older, serialised with an advisory lock; same fix in the audit service | `test_startup_does_not_block_on_a_busy_payments_table` (fails without the fix); parallel tests 10/10 |
| 21 | "Signals were set by the demo operator" shown on every demo payment | The analyst view claimed injected signals when none were on | Shown only when a signal is actually on | `test_operator_flag_is_false_when_no_signal_is_on` |
| 22 | One person could pose as many volunteers | Fake "people" would make the model look better at telling people apart | Single-use invites issued by the operator | `test_invite_is_single_use_and_consent_is_required` |
| 23 | Study runs on a shared laptop | A participant code kept in the browser would hand the next volunteer someone else's session | Code held in memory only; "Finish" wipes it; browser test checks storage is empty | `study.e2e.mjs`: no participant code left in browser storage |
| 24 | Withdrawal vs an append-only audit log | A participant id in the hash-chained log could never be deleted | Audit events carry counts, never participant ids | `test_withdrawal_deletes_everything_and_the_audit_log_keeps_no_id` |
| 25 | Export could re-identify volunteers | Join order and clock times match "who sat at the laptop when" | Shuffled pseudonyms; sessions carry hours since the first session, not timestamps | `test_export_is_operator_only_pseudonymous_and_has_no_clock_times` |
| 26 | The server cannot see what was typed | A script could submit 6 keystrokes for a 24-character prompt | Key-press count must fit the prompt length (at least the length, at most 4× + 10); prompts in order | `test_pasted_too_short_or_implausible_samples_are_rejected`, `test_prompts_must_be_typed_in_order` |

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
| Transaction | untrusted payee, running total to one untrusted payee (splitting), total to untrusted payees in 24 h, amount vs your usual, very large amount, night time (IST), rapid payments | real, from your payment history |
| Telecom | payee mobile number's FRI category (also for mobile-number UPI IDs), recent SIM swap | FRI **mocked**; SIM swap **injected** |
| Device | new device, failed integrity check, synced passkey on a new device | synced flag real (from the passkey); others **injected** |
| Coaching | active call (with the running total to an untrusted payee), screen sharing, accessibility service | **injected** by the operator (a browser cannot read these) |
| Behaviour | typing and mouse rhythm compared with your own clean history; missing data after enrolment | real, from the browser (slice 2b) |
| Consortium | payee reported on the ledger | slice 2c |

**Payee trust.** A payee counts as trusted only after a completed payment of at least ₹500, made
**more than 24 hours ago** and **without coaching signals**. A ₹1 "verification" payment, or anything
sent while on a call, never makes a payee trusted.

**What the customer sees.** Band and plain-language reasons only: no score, no points, and no FRI
category (only "flagged by telecom fraud-risk signals"). Otherwise the payment form becomes a tool for
checking which mule numbers are flagged. The score, points and category appear only with
`SHOW_ANALYST_VIEW=1` (demo / fraud ops).

**Demo signals** are set by an operator (`/demo/sessions/{id}/signals`, `x-demo-operator-token`), never
by the session being scored.

Every payment needs an `Idempotency-Key` header: a retry with the same key returns the original decision
instead of paying twice; the same key with a different payment gets 409. Scoring is serialised per user
(Postgres advisory lock), so a parallel burst cannot slip past the velocity and running-total rules. Injected signals are recorded in the audit log (`DEMO_SIGNALS_SET`) and every
decision is recorded as `RISK_DECISION` (score, band, reason codes; never the payee identifier).

Privacy: payee identifiers are normalised (IFSC + account, lowercase UPI, +91 mobile) and stored only as
an HMAC with the consortium key, the same hash the ledger uses, plus a masked display string.

Client IP: `X-Forwarded-For` is honoured only when the request comes from a trusted proxy
(`TRUSTED_PROXIES`: loopback and Docker's private range); otherwise it is ignored, so it cannot be spoofed.
On a single laptop the "client" is still localhost; the fix matters once the app is deployed.

Tests: unit tests for normalisation, every rule and band, and client IP; API tests on Postgres; 21 red-team
tests (`api/tests/test_redteam.py`); and `web/e2e/payments.e2e.mjs` in real Chrome (takeover → sandbox,
coached → step-up, ₹1 test payment, FRI hidden from the customer, double-click, validation).

## Phase 2 · Module 2b: behaviour model (part 1)

The payment form records **timing only**: how long keys are held, the pause between keys, corrections,
paste, time to fill the form, the pause before pressing send, and mouse speed. Key identities are used
in the browser only to pair key-down with key-up; no key, character or text is sent or stored.

- **Profile:** the user's last 50 **clean** samples. A sample is clean only if the payment was allowed,
  had no coaching or takeover signals, **and its typing already matched the profile** (anomaly < 0.3).
  So an attacker's typing never trains the profile, even when one of their payments slips through.
- **Guided enrolment:** "Set up typing profile" asks for a **fresh passkey check** (Windows Hello), then
  5 practice samples typed by hand, within 10 minutes, in that session only. Without the passkey check,
  someone holding only a stolen session cookie could enrol their own rhythm as the victim's.
- **Model features: rhythm only** (key hold time and its variation, pause between keys and its
  variation, correction rate). Form time, the pause before submit and mouse speed are recorded but not
  scored: they depend on which form is filled, not on who fills it. With them included, the owner's own
  typing scored 0.98 on the payment form after enrolling on the practice form.
- **Model:** robust z-score per feature (median and MAD), from 5 clean samples.
- **Rule:** anomaly ≥ 0.3 adds 10 points, ≥ 0.5 adds 20. Behaviour never blocks a payment on its own.
- **Diagnostics:** the analyst view says, per payment, whether typing was captured and why it was or was
  not added to the profile.
- **Missing data is not a free pass:** once a profile exists, a payment with no usable typing data
  (all pasted, scripted, or a rejected payload) adds 10 points. Cost: users who paste everything pay it.
- **Crafted payloads:** out-of-range or non-numeric values are rejected, not clamped, and never learned.

### Evaluation: synthetic users only

`python api/scripts/eval_behaviour.py 60` (reproducible). Thresholds were chosen on seed 11; this report uses seed 7.

```
Synthetic evaluation: 60 users, 20 genuine + 20 impostor attempts each, seed 7

model              history    EER    at        anomaly >= 0.3              anomaly >= 0.5      
                                              genuine      impostor       genuine      impostor
robust-z                 5   4.9%  0.21          0.9%         84.5%          0.0%         53.8%
robust-z                10   5.3%  0.19          0.2%         82.4%          0.0%         52.5%
robust-z                19   4.7%  0.18          0.0%         81.9%          0.0%         52.0%
robust-z                40   3.8%  0.18          0.0%         83.3%          0.0%         51.4%
isolation-forest        20  11.5%  0.90         84.8%        100.0%         66.3%         99.5%
isolation-forest        40   8.5%  0.95         81.9%         99.8%         60.5%         99.8%

Thresholds 0.3 / 0.5 were chosen on a different seed (11) than this report (7).
SYNTHETIC DATA ONLY. Real-world error rates need the volunteer study.
```

How to read it:
- **The robust z-score is used, the Isolation Forest is not.** On this data the forest had about twice
  the equal error rate and was badly calibrated (it flagged about half of genuine attempts at 0.6).
  The synthetic users have independent, bell-shaped features, which favours the z-score; the forest's
  advantage (combinations of features) can only show up on real data, so it is kept for re-evaluation.
- **At the chosen thresholds**, under 1% of a user's own attempts are flagged at 0.3 (about 82–85% of
  other people's are), and none at 0.5 (about 51–54% of other people's are).
- **These are not real-world numbers.** Real typing is messier (fatigue, a new keyboard, a phone vs a
  laptop). Real error rates need real people: see the volunteer study below.

### Limits (stated, not solved)
- Client-side signals can be forged by an attacker who reads this code and scripts the browser. Banks
  harden collection SDKs; this demo does not.
- Five samples is a thin profile; early decisions are less reliable.
- The profile is per user across devices; typing on a phone and on a laptop will differ.

## Phase 2 · Module 2b part 2: volunteer typing study (real error rates)

Every behaviour number above comes from synthetic users. This part collects **real** typing from
consenting volunteers under a fixed protocol, and `api/scripts/eval_study.py` turns it into error rates
with honest confidence intervals.

**Protocol (fixed before any data is seen)**
- Everyone types the same 10 practice prompts (a UPI ID and an amount, like the payment form), so other
  volunteers' samples are fair impostor attempts on identical text.
- A session is 10 samples. Each volunteer does 2 sessions (3 at most), at least 4 hours apart.
- **Profile:** the first 5 samples of session 1 (the same as guided enrolment).
- **Same-sitting test:** the rest of session 1. Optimistic: same hour, same warm-up.
- **Later-session test:** sessions 2–3. This is the number that matters.
- **Impostors:** every sample of every other volunteer, scored against each profile.
- Rates are averaged **per person**, and the 95% intervals resample **people**, not attempts: one
  person's attempts are correlated, so resampling attempts would claim false precision.
- Thresholds stay at 0.3 / 0.5 (chosen on synthetic data). They are **not** re-tuned on the study data:
  tuning and reporting on the same data flatters the result.
- Fewer than 5 volunteers: no numbers. Fewer than 20: marked as a pilot.

**Privacy**
- Not linked to bank accounts. No name, email, IP address or browser is stored; only timing statistics.
- Invites are single use. The participant code is created at join, so whoever handed out the invite
  cannot link it to the data. Only hashes of both codes are stored.
- Withdrawal deletes every sample immediately. The audit log records operator actions and counts only.
- The export has shuffled pseudonyms (`p01`…) and "hours after the first session" instead of clock times.
- At the end of the project, everything is deleted with one operator call (below).
- Volunteers read the consent text and tick "I agree" before anything is recorded. For a class project
  this is informed consent, not an ethics-board approval; if the college has a process, use it.

### Running the study

The API port is bound to this machine only, so volunteers type on your laptop (which also keeps the
keyboard the same for everyone: differences come from the person, not the hardware).

```bash
TOKEN=demo-operator-token-change-me

# 1. Issue invite codes (one per volunteer; shown once)
curl -s -X POST localhost:8000/research/invites -H "x-demo-operator-token: $TOKEN" \
     -H 'content-type: application/json' -d '{"count": 10}'

# 2. Each volunteer opens http://localhost:5173/#/study, enters an invite, reads the consent,
#    writes down their participant code and types 10 samples. "Finish" before the next person.
#    4+ hours later (ideally the next day), they come back with their code for session 2.

# 3. Export and evaluate
curl -s localhost:8000/research/export -H "x-demo-operator-token: $TOKEN" > study.json
python3 api/scripts/eval_study.py study.json

# 4. When the project ends
curl -s -X POST localhost:8000/research/purge -H "x-demo-operator-token: $TOKEN" \
     -H 'content-type: application/json' -d '{"confirm": "DELETE ALL STUDY DATA"}'
```

The script reports, at each threshold, how often a volunteer's **own** attempts are flagged (same
sitting and later session), how often **other people** are missed, the equal error rate, and which
volunteers the model fails (more than 25% of their later attempts flagged). `study.json` is volunteer
data: do not commit it.

Tests: `api/tests/test_study.py` (16, API and database), `api/tests/test_study_eval.py` (9, the
evaluation maths, including a check that the fast EER matches a plain loop), and `web/e2e/study.e2e.mjs`
(12 checks in real Chrome: join, consent gate, a full session typed with real key events, the session gap,
resume, empty browser storage after "Finish", withdrawal).

### Results

No volunteer data yet. Until there is, the only behaviour numbers are the synthetic ones above, and they
must not be presented as real-world accuracy.

### Limits of the study
- **Zero-effort impostors only:** other volunteers type normally. Nobody watches the victim and tries
  to copy their rhythm, which a real attacker could; that would be missed more often.
- **One laptop:** real customers type on their own phones. Phone typing is a different study.
- **Small numbers:** a class can give 10–30 volunteers. Report the intervals, not just the percentages.
- **Same prompts:** short, practised text. Free typing in a real payment varies more.

### Resetting demo data

Payments, typing samples and operator signals can be cleared without deleting accounts or passkeys:

```bash
docker exec sentinel-db psql -U sentinel -d sentinel -c "TRUNCATE payments, behaviour_samples, behaviour_enrolment, session_signals CASCADE;"
```

This does not touch the volunteer study; study data is removed only by withdrawal or the purge call.
