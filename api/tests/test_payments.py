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
    monkeypatch.setattr(payments, "ist_hour", lambda: 14)  # daytime, so NIGHT_TIME never fires by accident
    with TestClient(main.app) as c:
        user = f"u{uuid.uuid4().hex[:10]}"
        auth = SoftAuthenticator()
        opts = c.post("/auth/register/options", json={"username": user, "displayName": "Payer"}).json()
        r = c.post("/auth/register/verify", json={"challengeId": opts["challengeId"], "credential": auth.create(opts["options"])})
        assert r.status_code == 200, r.text
        yield c


def pay(client, amount="500", payee_type="UPI", payee=None):
    payee = payee or {"upi": "shop@okicici"}
    return client.post("/payments/score", json={"payeeType": payee_type, "payee": payee, "amount": amount})


def codes(r):
    return [x["code"] for x in r.json()["reasons"]]


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
    assert second.json()["score"] == 0 and codes(second) == []


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
    assert client.put("/demo/signals", json={"simSwapHoursAgo": 2, "newDevice": True}).status_code == 200
    r = pay(client, amount="45000", payee_type="ACCOUNT", payee={"ifsc": "UTIB0000789", "account": "917020001234"})
    body = r.json()
    assert body["band"] == "sandbox" and body["status"] == "held" and body["takeoverSignals"]
    assert {"RECENT_SIM_SWAP", "NEW_DEVICE", "NEW_PAYEE"} <= set(codes(r))
    assert body["signals"]["injected"]["simSwapHoursAgo"] == 2


def test_coached_customer_is_step_up_never_sandbox(client):
    client.put("/demo/signals", json={"activeCall": True, "screenSharing": True})
    r = pay(client, amount="90000", payee={"upi": "refund.desk@okaxis"})
    assert r.json()["band"] == "step_up" and r.json()["coachingSignals"]
    assert "CALL_NEW_PAYEE_LARGE" in codes(r)


def test_signals_belong_to_the_session_not_the_user(client):
    client.put("/demo/signals", json={"newDevice": True})
    assert client.get("/demo/signals").json()["newDevice"] is True


def test_held_payments_do_not_count_as_known_payees(client):
    client.put("/demo/signals", json={"simSwapHoursAgo": 1, "newDevice": True})
    pay(client, amount="50000", payee={"upi": "mule.acct@okhdfc"})
    client.put("/demo/signals", json={})
    again = pay(client, amount="100", payee={"upi": "mule.acct@okhdfc"})
    assert "NEW_PAYEE" in codes(again)


@pytest.mark.parametrize(
    "amount,payee_type,payee,detail",
    [
        ("0", "UPI", {"upi": "a@b"}, "more than zero"),
        ("12.345", "UPI", {"upi": "shop@okicici"}, "2 decimal"),
        ("abc", "UPI", {"upi": "shop@okicici"}, "number"),
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


def test_demo_endpoints_hidden_when_disabled(client, monkeypatch):
    from app import settings

    monkeypatch.setattr(settings, "DEMO_SIGNALS", False)
    assert client.get("/demo/signals").status_code == 404
    assert client.put("/demo/signals", json={"newDevice": True}).status_code == 404


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
