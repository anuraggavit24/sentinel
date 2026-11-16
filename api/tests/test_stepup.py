"""Module 3b: the customer's step-up on a held payment, and the sandbox. Needs DATABASE_URL."""
import json
import os
import uuid

import httpx
import psycopg
import pytest

from .soft_authenticator import SoftAuthenticator
from .test_ops import FakeWriter, elevate, register
from .test_payments import pay, set_signals

pytestmark = pytest.mark.skipif("DATABASE_URL" not in os.environ, reason="needs a real Postgres (DATABASE_URL)")
OP = {"x-demo-operator-token": "test-operator-token"}
SAFE = {"onCall": False, "promised": False, "knowsPayee": True}


@pytest.fixture()
def cust(monkeypatch):
    from fastapi.testclient import TestClient

    from app import main, payments, settings

    monkeypatch.setattr(settings, "DEMO_SIGNALS", True)
    monkeypatch.setattr(settings, "SHOW_ANALYST_VIEW", True)
    monkeypatch.setattr(settings, "LEDGER_URL", "")
    monkeypatch.setattr(payments, "ist_hour", lambda: 14)
    with TestClient(main.app) as c:
        with psycopg.connect(os.environ["DATABASE_URL"]) as conn:  # see test_ops.world
            conn.execute("UPDATE payments SET status = 'rejected' WHERE status = 'held'")
        name, auth = register(c, "cust")
        yield c, auth, name


def upi():
    return {"upi": f"p{uuid.uuid4().hex[:8]}@okaxis"}


def held(c, amount="60000"):
    r = pay(c, amount=amount, payee=upi()).json()
    assert r["status"] == "held" and r["band"] == "step_up", r
    return r["paymentId"]


def step_up(c, auth, pid, answers=SAFE, authenticator=None):
    o = c.post(f"/payments/{pid}/step-up/options")
    if o.status_code != 200:
        return o
    o = o.json()
    cred = (authenticator or auth).get(o["options"])
    return c.post(f"/payments/{pid}/step-up", json={"challengeId": o["challengeId"], "credential": cred, "answers": answers})


def db():
    return psycopg.connect(os.environ["DATABASE_URL"])


def row(pid):
    with db() as conn:
        conn.row_factory = psycopg.rows.dict_row
        return conn.execute("SELECT * FROM payments WHERE id = %s", (pid,)).fetchone()


# ---------------------------------------------------------------- step-up
def test_customer_confirms_a_held_payment_with_a_passkey(cust):
    c, auth, _ = cust
    pid = held(c)
    r = step_up(c, auth, pid)
    assert r.status_code == 200 and r.json()["status"] == "completed"
    p = row(pid)
    assert p["status"] == "completed" and p["review_decision"] == "customer_confirmed"
    assert p["step_up"]["answers"] == SAFE and p["response"]["status"] == "completed"
    hist = next(x for x in c.get("/payments").json()["payments"] if x["id"] == pid)
    assert hist["status"] == "completed" and hist["reviewed"] == "customer_confirmed"


@pytest.mark.parametrize("answers", [
    {"onCall": True, "promised": False, "knowsPayee": True},
    {"onCall": False, "promised": True, "knowsPayee": True},
])
def test_scam_answers_keep_it_held_flag_it_and_warn(cust, answers):
    c, auth, _ = cust
    pid = held(c)
    r = step_up(c, auth, pid, answers).json()
    assert r["status"] == "held" and "never ask you to move money" in r["warning"]
    p = row(pid)
    assert p["status"] == "held" and p["step_up_flagged"] is True
    assert p["step_up"]["blockers"] == ["the answers match a known scam pattern"]


def test_one_attempt_only(cust):
    """A scammer on the line must not get a second go at coaching the answers."""
    c, auth, _ = cust
    pid = held(c)
    step_up(c, auth, pid, {"onCall": True, "promised": False, "knowsPayee": True})
    r = step_up(c, auth, pid, SAFE)
    assert r.status_code == 409 and row(pid)["status"] == "held"


def test_answers_never_override_observed_coaching(cust):
    c, auth, _ = cust
    set_signals(c, activeCall=True)
    pid = held(c, amount="45000")
    r = step_up(c, auth, pid, SAFE).json()   # the coached victim says "no call"
    assert r["status"] == "held" and r["warning"] is None
    p = row(pid)
    assert p["step_up"]["blockers"] == ["coaching signals were seen during the payment"] and p["step_up_flagged"] is False


def test_a_confirmed_mule_payee_needs_an_analyst(cust):
    c, auth, _ = cust
    pid = held(c)
    with db() as conn:
        conn.execute("UPDATE payments SET signals = jsonb_set(signals, '{consortium}', %s) WHERE id = %s",
                     (json.dumps({"status": "hit", "hardRule": True}), pid))
    r = step_up(c, auth, pid, SAFE).json()
    assert r["status"] == "held"
    assert row(pid)["step_up"]["blockers"] == ["the payee is a confirmed mule on the consortium ledger"]


def test_someone_elses_passkey_is_refused(cust):
    c, auth, _ = cust
    pid = held(c)
    assert step_up(c, auth, pid, SAFE, authenticator=SoftAuthenticator()).status_code == 400
    assert row(pid)["status"] == "held" and row(pid)["step_up"] is None


def test_only_your_own_held_step_up_payments(cust):
    from fastapi.testclient import TestClient

    from app import main

    c, auth, _ = cust
    pid = held(c)
    other = TestClient(main.app)
    register(other, "other")
    assert other.post(f"/payments/{pid}/step-up/options").status_code == 404
    done = pay(c, amount="100", payee=upi()).json()["paymentId"]
    assert c.post(f"/payments/{done}/step-up/options").status_code == 409


def test_customer_can_cancel_a_held_payment(cust):
    c, _, _ = cust
    pid = held(c)
    r = c.post(f"/payments/{pid}/cancel")
    assert r.json()["status"] == "rejected"
    assert row(pid)["review_decision"] == "customer_cancelled"
    assert c.post(f"/payments/{pid}/cancel").status_code == 409


def test_flagged_step_ups_come_first_in_the_analyst_queue(cust):
    from fastapi.testclient import TestClient

    from app import main

    c, auth, _ = cust
    plain = held(c)
    flagged = held(c)
    step_up(c, auth, flagged, {"onCall": True, "promised": True, "knowsPayee": False})
    analyst = TestClient(main.app)
    name, _ = register(analyst, "anal")
    analyst.post("/ops/analysts", json={"username": name}, headers=OP)
    q = analyst.get("/ops/queue").json()["payments"]
    ids = [x["id"] for x in q]
    assert ids.index(flagged) < ids.index(plain)
    item = next(x for x in q if x["id"] == flagged)
    assert item["scamPattern"] is True and item["stepUp"]["answers"]["onCall"] is True


# ---------------------------------------------------------------- sandbox
def takeover(c, amount="45000", payee=None):
    set_signals(c, simSwapHoursAgo=2, newDevice=True)
    return pay(c, amount=amount, payee=payee or upi()).json()


def second_session(name, auth):
    """The real owner signing in on their own device."""
    from fastapi.testclient import TestClient

    from app import main

    owner = TestClient(main.app)
    o = owner.post("/auth/login/options", json={}).json()
    assert owner.post("/auth/login/verify", json={"challengeId": o["challengeId"], "credential": auth.get(o["options"])}).status_code == 200
    return owner


def test_takeover_session_sees_success_but_nothing_moves(cust):
    c, auth, name = cust
    first = takeover(c)
    assert first["status"] == "completed" and first["band"] == "allow"
    assert row(first["paymentId"])["status"] == "sandboxed"
    # Signals off again: the session stays in the sandbox anyway.
    set_signals(c)
    second = pay(c, amount="20000", payee=upi()).json()
    assert second["status"] == "completed" and second["analyst"]["sandbox"]["why"] == "session already sandboxed"
    assert row(second["paymentId"])["status"] == "sandboxed"
    # The sandboxed session sees the money leave ...
    assert c.get("/account").json()["balance"] == "₹435,000"
    hist = c.get("/payments").json()["payments"]
    assert all(h["status"] == "completed" for h in hist[:2]) and "score" not in hist[0]
    # ... the real owner sees the full balance, and is told what happened.
    owner = second_session(name, auth)
    assert owner.get("/account").json()["balance"] == "₹500,000"
    owner_hist = owner.get("/payments").json()["payments"]
    assert {h["status"] for h in owner_hist[:2]} == {"blocked_takeover"}


def test_sandboxed_session_cannot_step_up_or_cancel_anything_real(cust):
    c, auth, _ = cust
    pid = held(c)          # a real held payment from before the takeover
    takeover(c)
    assert c.post(f"/payments/{pid}/step-up/options").status_code == 404
    assert c.post(f"/payments/{pid}/cancel").status_code == 404


def test_sandboxing_is_audited_once(cust):
    from app import audit

    c, _, _ = cust
    sid = takeover(c)["paymentId"]
    takeover(c)
    ev = [e for e in audit.pending_events("SESSION_SANDBOXED") if e["payload"]["paymentId"] == sid]
    assert len(ev) == 1


def test_sandbox_captures_go_to_the_ledger_as_low_weight_reports(cust, monkeypatch):
    from app import ledger_outbox, settings

    fake = FakeWriter()
    monkeypatch.setattr(settings, "LEDGER_URL", "http://ledger-gateway:8100")
    monkeypatch.setattr(settings, "LEDGER_WRITE_TOKEN", "write-token-0987654321")
    monkeypatch.setattr(ledger_outbox, "_transport", lambda: httpx.MockTransport(fake.handler))
    monkeypatch.setattr("app.ledger._transport", lambda: httpx.MockTransport(fake.handler))
    with db() as conn:
        conn.execute("UPDATE ledger_outbox SET status = 'failed' WHERE status = 'pending'")
    c, _, _ = cust
    pid = takeover(c)["paymentId"]
    with db() as conn:
        conn.row_factory = psycopg.rows.dict_row
        o = conn.execute("SELECT * FROM ledger_outbox WHERE payment_id = %s", (pid,)).fetchone()
    assert o["source"] == "SANDBOX_CAPTURE" and o["severity"] == "LOW" and o["evidence"]["kind"] == "sandbox"
    ledger_outbox.drain_once()
    path, body, _ = fake.writes[-1]
    assert path == "/report-mule" and body["source"] == "SANDBOX_CAPTURE" and body["severity"] == "LOW"


def test_analyst_sees_and_ends_sandboxed_sessions(cust):
    from fastapi.testclient import TestClient

    from app import main

    c, _, _ = cust
    takeover(c)
    takeover(c, amount="30000")
    analyst = TestClient(main.app)
    name, aauth = register(analyst, "anal")
    analyst.post("/ops/analysts", json={"username": name}, headers=OP)
    sessions = analyst.get("/ops/sandbox").json()["sessions"]
    mine = sessions[0]
    assert mine["attempts"] == 2 and mine["attempted"] == "₹75,000" and mine["payees"] == 2 and not mine["revoked"]
    assert analyst.post(f"/ops/sessions/{mine['id']}/revoke").status_code == 403   # needs elevation
    elevate(analyst, aauth)
    assert analyst.post(f"/ops/sessions/{mine['id']}/revoke").json() == {"revoked": True}
    assert c.get("/account").status_code == 401   # the attacker's session is over
