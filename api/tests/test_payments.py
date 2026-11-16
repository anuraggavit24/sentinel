"""Module 2a API tests against a real Postgres. Needs DATABASE_URL; skipped otherwise."""
import os
import uuid

import pytest

from .soft_authenticator import SoftAuthenticator

pytestmark = pytest.mark.skipif("DATABASE_URL" not in os.environ, reason="needs a real Postgres (DATABASE_URL)")


@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from app import main, payments, settings

    monkeypatch.setattr(settings, "DEMO_SIGNALS", True)
    monkeypatch.setattr(settings, "SHOW_ANALYST_VIEW", True)
    monkeypatch.setattr(payments, "ist_hour", lambda: 14)  # daytime, so NIGHT_TIME never fires by accident
    with TestClient(main.app) as c:
        user = f"u{uuid.uuid4().hex[:10]}"
        auth = SoftAuthenticator()
        opts = c.post("/auth/register/options", json={"username": user, "displayName": "Payer"}).json()
        r = c.post("/auth/register/verify", json={"challengeId": opts["challengeId"], "credential": auth.create(opts["options"])})
        assert r.status_code == 200, r.text
        yield c


OPERATOR = {"x-demo-operator-token": "test-operator-token"}


def pay(client, amount="500", payee_type="UPI", payee=None, key=None):
    payee = payee or {"upi": "shop@okicici"}
    return client.post(
        "/payments/score",
        json={"payeeType": payee_type, "payee": payee, "amount": amount},
        headers={"Idempotency-Key": key or uuid.uuid4().hex},
    )


def codes(r):
    """Analyst reason codes (the full detail)."""
    return [x["code"] for x in r.json()["analyst"]["reasons"]]


def current_session_id(client):
    return next(s["id"] for s in client.get("/me").json()["sessions"] if s["current"])


def set_signals(client, **signals):
    r = client.put(f"/demo/sessions/{current_session_id(client)}/signals", json=signals, headers=OPERATOR)
    assert r.status_code == 200, r.text
    return r


def test_requires_sign_in():
    from fastapi.testclient import TestClient

    from app import main

    with TestClient(main.app) as anon:
        assert pay(anon).status_code == 401


def test_first_payment_is_new_payee_then_known(client):
    first = pay(client)
    assert first.status_code == 200, first.text
    assert first.json()["band"] == "allow" and codes(first) == ["NEW_PAYEE"]
    second = pay(client)
    # Paid minutes ago is not trusted yet: trust needs 24 hours.
    assert codes(second) == ["NEW_PAYEE"] and "24 hours" in second.json()["analyst"]["reasons"][0]["text"]


def test_amount_far_above_usual_after_history(client):
    for _ in range(5):
        pay(client, amount="400")
    r = pay(client, amount="8000")
    assert "AMOUNT_FAR_ABOVE_USUAL" in codes(r)


def test_fri_mock_on_phone_and_on_mobile_upi(client):
    r = pay(client, payee_type="PHONE", payee={"phone": "+91 99999 00003"})
    assert "FRI_VERY_HIGH" in codes(r) and r.json()["band"] == "step_up"
    u = pay(client, payee={"upi": "9999900002@ybl"})
    assert "FRI_HIGH" in codes(u)


def test_injected_takeover_signals_send_payment_to_sandbox(client):
    set_signals(client, simSwapHoursAgo=2, newDevice=True)
    r = pay(client, amount="45000", payee_type="ACCOUNT", payee={"ifsc": "UTIB0000789", "account": "917020001234"})
    body = r.json()
    # Module 3b: the session sees an ordinary success; the truth is in the analyst view and the database.
    assert body["band"] == "allow" and body["status"] == "completed" and body["reasons"] == []
    assert body["analyst"]["sandbox"] == {"active": True, "why": "takeover signals on a new device", "realBand": "sandbox"}
    assert body["analyst"]["takeoverSignals"]
    assert {"RECENT_SIM_SWAP", "NEW_DEVICE", "NEW_PAYEE"} <= set(codes(r))
    assert body["analyst"]["signals"]["injected"]["simSwapHoursAgo"] == 2


def test_coached_customer_is_step_up_never_sandbox(client):
    set_signals(client, activeCall=True, screenSharing=True)
    r = pay(client, amount="90000", payee={"upi": "refund.desk@okaxis"})
    assert r.json()["band"] == "step_up" and r.json()["analyst"]["coachingSignals"]
    assert "CALL_NEW_PAYEE_LARGE" in codes(r)


def test_signals_are_set_per_session_by_the_operator(client):
    set_signals(client, newDevice=True)
    sid = current_session_id(client)
    assert client.get(f"/demo/sessions/{sid}/signals", headers=OPERATOR).json()["newDevice"] is True


def test_held_payments_do_not_count_as_known_payees(client):
    set_signals(client, simSwapHoursAgo=1, newDevice=True)
    pay(client, amount="50000", payee={"upi": "mule.acct@okhdfc"})
    set_signals(client)
    again = pay(client, amount="100", payee={"upi": "mule.acct@okhdfc"})
    assert "NEW_PAYEE" in codes(again)


@pytest.mark.parametrize(
    "amount,payee_type,payee,detail",
    [
        ("0", "UPI", {"upi": "ab@okicici"}, "more than zero"),
        ("12.345", "UPI", {"upi": "shop@okicici"}, "up to 2 decimals"),
        ("abc", "UPI", {"upi": "shop@okicici"}, "number like"),
        ("20000000", "UPI", {"upi": "shop@okicici"}, "cannot exceed"),
        ("100", "ACCOUNT", {"ifsc": "BAD", "account": "123456789"}, "IFSC"),
        ("100", "PHONE", {"phone": "12345"}, "Mobile"),
    ],
)
def test_validation_errors_are_clear(client, amount, payee_type, payee, detail):
    r = pay(client, amount=amount, payee_type=payee_type, payee=payee)
    assert r.status_code == 422 and detail in r.json()["detail"]


def test_decision_goes_to_audit_outbox_without_payee_identifier(client):
    from app import audit

    r = pay(client, payee={"upi": "secret.person@okhdfc"})
    events = [e for e in audit.pending_events("RISK_DECISION") if e["payload"]["paymentId"] == r.json()["paymentId"]]
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["band"] == "allow" and payload["reasonCodes"] == ["NEW_PAYEE"]
    assert "secret.person" not in str(payload)


def test_raw_payee_is_never_stored(client):
    import psycopg

    r = pay(client, payee={"upi": "never.stored@okhdfc"})
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        row = conn.execute("SELECT payee_hash, payee_display, signals::text, reasons::text FROM payments WHERE id = %s",
                           (r.json()["paymentId"],)).fetchone()
    assert "never.stored" not in " ".join(str(x) for x in row)
    assert len(row[0]) == 64


def test_list_payments(client):
    pay(client)
    listed = client.get("/payments").json()["payments"]
    assert len(listed) == 1 and listed[0]["band"] == "allow" and "okicici" in listed[0]["payee"]
    assert listed[0]["score"] == 15


def test_demo_endpoints_hidden_when_disabled(client, monkeypatch):
    from app import settings

    sid = current_session_id(client)
    monkeypatch.setattr(settings, "DEMO_SIGNALS", False)
    assert client.get(f"/demo/sessions/{sid}/signals", headers=OPERATOR).status_code == 404
    assert client.put(f"/demo/sessions/{sid}/signals", json={"newDevice": True}, headers=OPERATOR).status_code == 404


def test_registering_sets_last_used(client):
    assert client.get("/me").json()["devices"][0]["lastUsedAt"] is not None


def _register_from(peer, headers):
    from fastapi.testclient import TestClient

    from app import main

    with TestClient(main.app, client=(peer, 50000), headers=headers) as c:
        auth, user = SoftAuthenticator(), f"u{uuid.uuid4().hex[:10]}"
        opts = c.post("/auth/register/options", json={"username": user, "displayName": "IP"}).json()
        c.post("/auth/register/verify", json={"challengeId": opts["challengeId"], "credential": auth.create(opts["options"])})
        return c.get("/me").json()["sessions"][0]["ip"]


def test_session_ip_uses_forwarded_header_only_from_a_trusted_proxy():
    # Request arrives from Docker's gateway (a trusted proxy): the forwarded client address is used.
    assert _register_from("172.20.0.1", {"x-forwarded-for": "49.36.10.20"}) == "49.36.10.20"
    # Request arrives straight from the internet: the header is ignored, so it cannot be spoofed.
    assert _register_from("203.0.113.9", {"x-forwarded-for": "1.2.3.4"}) == "203.0.113.9"


# ---------------------------------------------------------------- slice 2b: behaviour

def typing(dwell=110, flight=190, fill=12000, speed=1.0, keystrokes=24):
    return {"keystrokes": keystrokes, "dwellMeanMs": dwell, "dwellStdMs": 18, "flightMeanMs": flight,
            "flightStdMs": 60, "correctionRate": 0.05, "fillTimeMs": fill, "idleBeforeSubmitMs": 900,
            "pointerSpeedPxMs": speed, "pasted": False}


def pay_b(client, amount, upi, behaviour):
    return client.post("/payments/score", json={"payeeType": "UPI", "payee": {"upi": upi}, "amount": amount,
                                                "behaviour": behaviour}, headers={"Idempotency-Key": uuid.uuid4().hex})


def enrol(client, n=6):
    import random

    rnd = random.Random(5)
    for i in range(n):
        r = pay_b(client, "300", f"grocer{i}@okicici",
                  typing(dwell=rnd.gauss(110, 5), flight=rnd.gauss(190, 12), fill=rnd.gauss(12000, 1200), speed=rnd.gauss(1.0, 0.06)))
        assert r.status_code == 200, r.text
    return r


def test_behaviour_learns_then_scores(client):
    first = pay_b(client, "300", "grocer@okicici", typing()).json()
    assert first["analyst"]["signals"]["behaviour"]["model"] == "learning"
    last = enrol(client).json()
    b = last["analyst"]["signals"]["behaviour"]
    assert b["model"] == "robust-z" and b["anomaly"] < 0.3


def test_someone_else_typing_is_flagged(client):
    enrol(client)
    r = pay_b(client, "300", "grocer0@okicici", typing(dwell=210, flight=430, fill=3500, speed=3.2)).json()
    assert {"BEHAVIOUR_ANOMALY_HIGH"} <= {x["code"] for x in r["analyst"]["reasons"]}
    assert {"code": "BEHAVIOUR_UNUSUAL", "text": "The way this form was filled in is unusual for you", "group": "DEVICE"} in r["reasons"]


def test_missing_behaviour_after_enrolment_is_not_a_free_pass(client):
    enrol(client)
    r = pay(client, "300", payee={"upi": "grocer0@okicici"}).json()   # scripted call, no behaviour
    assert "BEHAVIOUR_MISSING" in [x["code"] for x in r["analyst"]["reasons"]]


def test_crafted_behaviour_is_rejected_not_learned(client):
    import psycopg

    enrol(client)
    r = pay_b(client, "300", "grocer0@okicici", typing(dwell=99999)).json()
    b = r["analyst"]["signals"]["behaviour"]
    assert b["note"].startswith("rejected") and "BEHAVIOUR_MISSING" in [x["code"] for x in r["analyst"]["reasons"]]
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        assert conn.execute("SELECT count(*) FROM behaviour_samples WHERE payment_id = %s", (r["paymentId"],)).fetchone()[0] == 0


def test_attacker_typing_during_takeover_never_trains_the_profile(client):
    import psycopg

    enrol(client)
    set_signals(client, simSwapHoursAgo=1, newDevice=True)
    r = pay_b(client, "45000", "mule.x@okaxis", typing(dwell=210, flight=430, fill=3500, speed=3.2)).json()
    assert r["analyst"]["sandbox"]["realBand"] == "sandbox"
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        clean = conn.execute("SELECT clean FROM behaviour_samples WHERE payment_id = %s", (r["paymentId"],)).fetchone()[0]
    assert clean is False


def test_no_key_or_text_is_stored(client):
    import psycopg

    r = pay_b(client, "300", "grocer@okicici", {**typing(), "keys": "secret123", "text": "my pin"}).json()
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        f = conn.execute("SELECT features FROM behaviour_samples WHERE payment_id = %s", (r["paymentId"],)).fetchone()[0]
    assert "keys" not in f and "text" not in f and "secret123" not in str(f)



def test_behaviour_diagnostics_explain_capture_and_learning(client):
    no_data = pay(client, "300", payee={"upi": "diag.one@okicici"}).json()["analyst"]["signals"]["behaviour"]
    assert no_data["captured"] is False and no_data["note"] == "no typing data sent"
    assert no_data["notLearnedBecause"].startswith("no usable typing data")

    few = pay_b(client, "300", "diag.two@okicici", typing(keystrokes=3)).json()["analyst"]["signals"]["behaviour"]
    assert few["captured"] is False and "fewer than 6" in few["note"]

    ok = pay_b(client, "300", "diag.three@okicici", typing()).json()["analyst"]["signals"]["behaviour"]
    assert ok["captured"] is True and ok["learned"] is True and ok["notLearnedBecause"] is None

    set_signals(client, activeCall=True)
    coached = pay_b(client, "300", "diag.four@okicici", typing()).json()["analyst"]["signals"]
    assert coached["behaviour"]["learned"] is False and "coaching signals were on" in coached["behaviour"]["notLearnedBecause"]
    assert coached["operatorSignalsOn"] is True


def test_operator_flag_is_false_when_no_signal_is_on(client):
    r = pay(client, "300", payee={"upi": "quiet.payee@okicici"}).json()
    assert r["analyst"]["signals"]["operatorSignalsOn"] is False



def test_anomalous_typing_is_never_learned_even_if_the_payment_is_allowed(client):
    import psycopg

    enrol(client)
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:  # outside the 10-minute velocity window
        conn.execute("UPDATE payments SET created_at = created_at - interval '1 hour'")
    r = pay_b(client, "300", "grocer0@okicici", typing(dwell=210, flight=430)).json()
    b = r["analyst"]["signals"]["behaviour"]
    assert r["status"] == "completed" and b["learned"] is False and "did not match" in b["notLearnedBecause"]
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        assert conn.execute("SELECT clean FROM behaviour_samples WHERE payment_id = %s", (r["paymentId"],)).fetchone()[0] is False
