"""Module 3a: fraud operations and ledger filings. Needs DATABASE_URL; skipped otherwise.
The ledger-gateway is faked at the HTTP layer (its real behaviour is tested in ledger-gateway/test)."""
import json
import os
import uuid

import httpx
import psycopg
import pytest

from .soft_authenticator import SoftAuthenticator
from .test_payments import pay

pytestmark = pytest.mark.skipif("DATABASE_URL" not in os.environ, reason="needs a real Postgres (DATABASE_URL)")
OP = {"x-demo-operator-token": "test-operator-token"}
BIG = "60000"  # first payment of ₹60,000 to a new payee: 42 points, held for review


def register(c, prefix):
    auth, name = SoftAuthenticator(), f"{prefix}{uuid.uuid4().hex[:10]}"
    opts = c.post("/auth/register/options", json={"username": name, "displayName": prefix.title()}).json()
    r = c.post("/auth/register/verify", json={"challengeId": opts["challengeId"], "credential": auth.create(opts["options"])})
    assert r.status_code == 200, r.text
    return name, auth


@pytest.fixture()
def world(monkeypatch):
    """A customer and an analyst, each with their own browser (cookie jar)."""
    from fastapi.testclient import TestClient

    from app import main, payments, settings

    monkeypatch.setattr(settings, "SHOW_ANALYST_VIEW", True)
    monkeypatch.setattr(settings, "LEDGER_URL", "")
    monkeypatch.setattr(payments, "ist_hour", lambda: 14)
    with TestClient(main.app) as customer:
        analyst = TestClient(main.app)
        register(customer, "cust")
        name, auth = register(analyst, "anal")
        assert analyst.post("/ops/analysts", json={"username": name}, headers=OP).json()["role"] == "analyst"
        yield {"customer": customer, "analyst": analyst, "auth": auth, "name": name}


def elevate(c, auth):
    opts = c.post("/ops/elevate/options").json()
    return c.post("/ops/elevate", json={"challengeId": opts["challengeId"], "credential": auth.get(opts["options"])})


def held(customer, upi=None):
    r = pay(customer, amount=BIG, payee={"upi": upi or f"mule{uuid.uuid4().hex[:6]}@okaxis"})
    assert r.json()["status"] == "held", r.json()
    return r.json()["paymentId"]


def decide(c, pid, **body):
    return c.post(f"/ops/payments/{pid}/decision", json={"decision": "reject", **body})


def db():
    return psycopg.connect(os.environ["DATABASE_URL"])


# ---------------------------------------------------------------- access
def test_customers_cannot_see_the_queue(world):
    assert world["customer"].get("/ops/queue").status_code == 403


def test_only_the_operator_grants_the_role(world):
    assert world["customer"].post("/ops/analysts", json={"username": world["name"]}).status_code == 401
    assert world["customer"].post("/ops/analysts", json={"username": "nobody-here"}, headers=OP).status_code == 404


def test_analyst_sees_held_payments_but_not_completed_ones(world):
    pid = held(world["customer"])
    small = pay(world["customer"], amount="100").json()["paymentId"]
    ids = [p["id"] for p in world["analyst"].get("/ops/queue").json()["payments"]]
    assert pid in ids and small not in ids
    item = next(p for p in world["analyst"].get("/ops/queue").json()["payments"] if p["id"] == pid)
    assert item["customer"].endswith("•••") and item["score"] >= 40 and item["reasons"]
    assert "@" in item["payee"] and len(item["payeeRef"]) == 12


def test_same_payee_held_for_other_customers_is_shown(world):
    from fastapi.testclient import TestClient

    from app import main

    upi = f"shared{uuid.uuid4().hex[:6]}@okaxis"
    held(world["customer"], upi)
    other = TestClient(main.app)
    register(other, "cust")
    pid = held(other, upi)
    item = next(p for p in world["analyst"].get("/ops/queue").json()["payments"] if p["id"] == pid)
    assert item["otherCustomers7d"] == 1


# ---------------------------------------------------------------- elevation
def test_deciding_needs_a_fresh_passkey_check(world):
    pid = held(world["customer"])
    r = decide(world["analyst"], pid)
    assert r.status_code == 403 and "passkey" in r.json()["detail"]
    assert elevate(world["analyst"], world["auth"]).status_code == 200
    assert decide(world["analyst"], pid).status_code == 200


def test_elevation_expires(world):
    assert elevate(world["analyst"], world["auth"]).status_code == 200
    with db() as conn:
        conn.execute("UPDATE sessions SET ops_elevated_until = now() - interval '1 second' "
                     "WHERE user_id = (SELECT id FROM users WHERE username = %s)", (world["name"],))
    assert decide(world["analyst"], held(world["customer"])).status_code == 403


def test_elevation_is_per_session(world):
    """A second session of the same analyst (another device, or a stolen cookie) is not elevated."""
    from fastapi.testclient import TestClient

    from app import main

    assert elevate(world["analyst"], world["auth"]).status_code == 200
    second = TestClient(main.app)
    opts = second.post("/auth/login/options", json={}).json()
    r = second.post("/auth/login/verify", json={"challengeId": opts["challengeId"], "credential": world["auth"].get(opts["options"])})
    assert r.status_code == 200, r.text
    assert decide(second, held(world["customer"])).status_code == 403


def test_another_users_passkey_cannot_elevate(world):
    opts = world["analyst"].post("/ops/elevate/options").json()
    stranger = SoftAuthenticator()
    r = world["analyst"].post("/ops/elevate", json={"challengeId": opts["challengeId"], "credential": stranger.get(opts["options"])})
    assert r.status_code == 400


def test_removing_the_role_ends_elevation(world):
    elevate(world["analyst"], world["auth"])
    world["analyst"].post("/ops/analysts", json={"username": world["name"], "analyst": False}, headers=OP)
    assert world["analyst"].get("/ops/queue").status_code == 403


# ---------------------------------------------------------------- decisions
def test_release_completes_the_payment_and_the_customer_sees_it(world):
    elevate(world["analyst"], world["auth"])
    pid = held(world["customer"])
    r = decide(world["analyst"], pid, decision="release", note="customer confirmed by phone")
    assert r.json()["status"] == "completed"
    row = next(p for p in world["customer"].get("/payments").json()["payments"] if p["id"] == pid)
    assert row["status"] == "completed" and row["reviewed"] == "release"
    assert pid not in [p["id"] for p in world["analyst"].get("/ops/queue").json()["payments"]]


def test_released_money_counts_as_sent_but_does_not_create_trust(world):
    elevate(world["analyst"], world["auth"])
    upi = f"rel{uuid.uuid4().hex[:6]}@okaxis"
    pid = held(world["customer"], upi)
    decide(world["analyst"], pid, decision="release")
    with db() as conn:
        trust = conn.execute("SELECT trust_eligible FROM payments WHERE id = %s", (pid,)).fetchone()
    assert trust == (False,)
    nxt = pay(world["customer"], amount="100", payee={"upi": upi}).json()["analyst"]["signals"]
    assert nxt["payeeTotal24h"] == "₹60,000" and nxt["payeeTrusted"] is False


def test_reject_and_replay_shows_the_outcome(world):
    elevate(world["analyst"], world["auth"])
    key, upi = uuid.uuid4().hex, f"rep{uuid.uuid4().hex[:6]}@okaxis"
    pid = pay(world["customer"], amount=BIG, payee={"upi": upi}, key=key).json()["paymentId"]
    decide(world["analyst"], pid)
    again = pay(world["customer"], amount=BIG, payee={"upi": upi}, key=key).json()
    # The customer's app retrying the same request must learn the outcome, not see "held" again.
    assert again["replayed"] is True and again["paymentId"] == pid
    assert again["status"] == "rejected" and again["review"]["decision"] == "reject"


def test_a_payment_is_decided_once(world):
    elevate(world["analyst"], world["auth"])
    pid = held(world["customer"])
    assert decide(world["analyst"], pid).status_code == 200
    r = decide(world["analyst"], pid, decision="release")
    assert r.status_code == 409 and "rejected" in r.json()["detail"]


def test_nobody_reviews_their_own_payment(world):
    elevate(world["analyst"], world["auth"])
    own = pay(world["analyst"], amount=BIG, payee={"upi": f"own{uuid.uuid4().hex[:6]}@okaxis"}).json()["paymentId"]
    assert own not in [p["id"] for p in world["analyst"].get("/ops/queue").json()["payments"]]
    r = decide(world["analyst"], own)
    assert r.status_code == 403 and "own payment" in r.json()["detail"]


def test_decision_is_audited(world):
    from app import audit

    elevate(world["analyst"], world["auth"])
    pid = held(world["customer"])
    decide(world["analyst"], pid, decision="release")
    ev = [e for e in audit.pending_events("ANALYST_DECISION") if e["payload"]["paymentId"] == pid]
    assert ev and ev[0]["payload"]["decision"] == "release" and ev[0]["payload"]["reported"] is False


def test_only_a_rejection_can_be_reported(world):
    elevate(world["analyst"], world["auth"])
    assert decide(world["analyst"], held(world["customer"]), decision="release", report=True).status_code == 400


def test_reporting_needs_ledger_writes_configured(world):
    elevate(world["analyst"], world["auth"])
    r = decide(world["analyst"], held(world["customer"]), report=True)
    assert r.status_code == 503


# ---------------------------------------------------------------- ledger filings
class FakeWriter:
    """Pretends to be the ledger-gateway: answers queryPayee from `reports`, records writes."""

    def __init__(self):
        self.reports = []
        self.writes = []
        self.fail = None   # None | "502" | "refuse" | "race" | "already-confirmed"

    def handler(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if req.url.path == "/query-payee":
            return httpx.Response(200, json={"result": {"idHashes": body["idHashes"], "maxWeight": 0,
                                                         "hardRuleEligible": False, "reports": self.reports}})
        self.writes.append((req.url.path, body, req.headers.get("x-ledger-token")))
        if self.fail == "502":
            return httpx.Response(502, json={"error": "peer unreachable", "kind": "unavailable"})
        if self.fail == "refuse":
            return httpx.Response(422, json={"error": "idHash must be a 64-character lowercase hex", "kind": "chaincode"})
        if self.fail == "race":
            return httpx.Response(422, json={"error": "Active report x already exists for this identifier", "kind": "chaincode"})
        if self.fail == "already-confirmed":
            return httpx.Response(422, json={"error": "BankAMSP has already confirmed report r", "kind": "chaincode"})
        rid = "f" * 64
        return httpx.Response(200, json={"result": {"reportId": rid, "status": "PENDING"}})


@pytest.fixture()
def writer(world, monkeypatch):
    from app import ledger_outbox, settings

    fake = FakeWriter()
    monkeypatch.setattr(settings, "LEDGER_URL", "http://ledger-gateway:8100")
    monkeypatch.setattr(settings, "LEDGER_TOKEN", "read-token-1234567890")
    monkeypatch.setattr(settings, "LEDGER_WRITE_TOKEN", "write-token-0987654321")
    monkeypatch.setattr(ledger_outbox, "_transport", lambda: httpx.MockTransport(fake.handler))
    with db() as conn:  # earlier tests may have left due rows; this fixture owns the outbox
        conn.execute("UPDATE ledger_outbox SET status = 'failed' WHERE status = 'pending'")
    elevate(world["analyst"], world["auth"])
    return fake


def report(world, severity="HIGH", note="mule account, 3 victims"):
    pid = held(world["customer"])
    r = decide(world["analyst"], pid, report=True, severity=severity, note=note)
    assert r.status_code == 200, r.text
    return pid, r.json()["outboxId"]


def outbox(oid):
    with db() as conn:
        conn.row_factory = psycopg.rows.dict_row
        return conn.execute("SELECT * FROM ledger_outbox WHERE id = %s", (oid,)).fetchone()


def drain():
    from app import ledger_outbox

    return ledger_outbox.drain_once()


def test_reject_and_report_files_a_new_report_with_the_payees_hash(world, writer):
    from app.ledger_outbox import evidence_hash

    pid, oid = report(world)
    assert outbox(oid)["status"] == "pending"
    assert drain() == 1
    path, body, token = writer.writes[-1]
    row = outbox(oid)
    with db() as conn:
        payee_hash = conn.execute("SELECT payee_hash FROM payments WHERE id = %s", (pid,)).fetchone()[0]
    assert path == "/report-mule" and token == "write-token-0987654321"
    assert body == {"idHash": payee_hash, "idType": "UPI", "evidenceHash": row["evidence_hash"],
                    "source": "ANALYST", "severity": "HIGH"}
    assert row["evidence_hash"] == evidence_hash(row["evidence"])   # the hash is of the stored case record
    assert row["evidence"]["note"] == "mule account, 3 victims"
    # The case record names the payment and the reasons, never the payee identifier or its hash.
    assert set(row["evidence"]) == {"paymentId", "decision", "note", "reasonCodes", "score", "analystId",
                                    "decidedAt", "payeeType", "bank"}
    assert payee_hash not in json.dumps(row["evidence"])
    assert row["status"] == "reported" and row["report_id"] == "f" * 64


def test_another_banks_report_is_confirmed_not_duplicated(world, writer):
    writer.reports = [{"reportId": "e" * 64, "status": "PENDING", "reporterOrg": "BankBMSP", "idHash": "x",
                       "weight": 0.25, "nonSandboxBanks": 1}]
    _, oid = report(world)
    drain()
    path, body, _ = writer.writes[-1]
    assert path == "/confirm-report" and body["reportId"] == "e" * 64 and body["source"] == "ANALYST"
    assert outbox(oid)["status"] == "confirmed"


def test_our_own_active_report_means_nothing_to_write(world, writer):
    writer.reports = [{"reportId": "d" * 64, "status": "CONFIRMED", "reporterOrg": "BankAMSP", "idHash": "x",
                       "weight": 1, "nonSandboxBanks": 2}]
    _, oid = report(world)
    drain()
    assert writer.writes == [] and outbox(oid)["status"] == "already_ours"


def test_already_confirmed_by_us_counts_as_done(world, writer):
    writer.reports = [{"reportId": "e" * 64, "status": "CONFIRMED", "reporterOrg": "BankCMSP", "idHash": "x",
                       "weight": 1, "nonSandboxBanks": 2}]
    writer.fail = "already-confirmed"
    _, oid = report(world)
    drain()
    assert outbox(oid)["status"] == "already_ours"


def test_a_ledger_outage_is_retried_with_backoff_then_failed_and_audited(world, writer):
    from app import audit, ledger_outbox

    writer.fail = "502"
    pid, oid = report(world)
    drain()
    row = outbox(oid)
    assert row["status"] == "pending" and row["attempts"] == 1 and "unreachable" in row["last_error"]
    assert drain() == 0  # not due yet: backoff
    for _ in range(ledger_outbox.MAX_ATTEMPTS - 1):
        with db() as conn:
            conn.execute("UPDATE ledger_outbox SET next_attempt_at = now() WHERE id = %s", (oid,))
        drain()
    assert outbox(oid)["status"] == "failed" and outbox(oid)["attempts"] == ledger_outbox.MAX_ATTEMPTS
    assert any(e["payload"]["paymentId"] == pid for e in audit.pending_events("LEDGER_WRITE_FAILED"))


def test_a_definite_refusal_fails_at_once(world, writer):
    writer.fail = "refuse"
    _, oid = report(world)
    drain()
    assert outbox(oid)["status"] == "failed" and outbox(oid)["attempts"] == 1


def test_a_race_with_another_bank_is_retried_and_then_confirms(world, writer):
    writer.fail = "race"
    _, oid = report(world)
    drain()
    assert outbox(oid)["status"] == "pending"   # someone filed first: look again next round
    writer.fail = None
    writer.reports = [{"reportId": "e" * 64, "status": "PENDING", "reporterOrg": "BankCMSP", "idHash": "x",
                       "weight": 0.25, "nonSandboxBanks": 1}]
    with db() as conn:
        conn.execute("UPDATE ledger_outbox SET next_attempt_at = now() WHERE id = %s", (oid,))
    drain()
    assert outbox(oid)["status"] == "confirmed" and writer.writes[-1][0] == "/confirm-report"


def test_a_claimed_row_is_not_sent_twice(world, writer):
    """Two drainers at once: the lease and SKIP LOCKED mean each row is sent by one of them."""
    import threading

    _, oid = report(world)
    results = []
    threads = [threading.Thread(target=lambda: results.append(drain())) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [0, 1] and len([w for w in writer.writes if w[0] == "/report-mule"]) == 1


def test_filings_are_listed_for_analysts(world, writer):
    _, oid = report(world)
    drain()
    f = next(x for x in world["analyst"].get("/ops/filings").json()["filings"] if x["id"] == oid)
    assert f["status"] == "reported" and f["reportId"] == "f" * 64 and f["amount"] == "₹60,000"
    assert world["customer"].get("/ops/filings").status_code == 403


def test_one_filing_per_payment(world, writer):
    from app import ledger_outbox

    pid, _ = report(world)
    with db() as conn, pytest.raises(psycopg.errors.UniqueViolation):
        conn.row_factory = psycopg.rows.dict_row
        ledger_outbox.enqueue(conn, payment_id=pid, id_hash="a" * 64, id_type="UPI", severity="LOW",
                              source="ANALYST", evidence={}, requested_by=None)
