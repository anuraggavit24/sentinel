"""Red-team tests: real scam and abuse patterns against the payments API. Each one failed on the
first 2a release (see README "Security review log"). Needs DATABASE_URL; skipped otherwise."""
import concurrent.futures as cf
import os
import uuid

import psycopg
import pytest

from .soft_authenticator import SoftAuthenticator
from .test_payments import OPERATOR, codes, current_session_id, pay, set_signals

pytestmark = pytest.mark.skipif("DATABASE_URL" not in os.environ, reason="needs a real Postgres (DATABASE_URL)")


@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from app import main, payments, settings

    monkeypatch.setattr(settings, "DEMO_SIGNALS", True)
    monkeypatch.setattr(settings, "SHOW_ANALYST_VIEW", True)
    monkeypatch.setattr(payments, "ist_hour", lambda: 14)
    with TestClient(main.app) as c:
        auth, user = SoftAuthenticator(), f"r{uuid.uuid4().hex[:10]}"
        opts = c.post("/auth/register/options", json={"username": user, "displayName": "Victim"}).json()
        c.post("/auth/register/verify", json={"challengeId": opts["challengeId"], "credential": auth.create(opts["options"])})
        yield c


def age_payments(hours: int) -> None:
    """Move every payment back in time, to test the 24-hour trust rule without waiting a day."""
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute("UPDATE payments SET created_at = created_at - make_interval(hours => %s)", (hours,))


# ---------------------------------------------------------------- coached-customer patterns

def test_one_rupee_test_payment_does_not_unlock_a_large_coached_payment(client):
    set_signals(client, activeCall=True)
    assert pay(client, "1", payee={"upi": "verify.kyc@okaxis"}).json()["band"] == "allow"
    big = pay(client, "90000", payee={"upi": "verify.kyc@okaxis"}).json()
    assert big["band"] == "step_up", big["analyst"]
    assert {"NEW_PAYEE", "CALL_NEW_PAYEE_LARGE"} <= {r["code"] for r in big["analyst"]["reasons"]}


def test_test_payment_even_a_day_later_does_not_create_trust(client):
    set_signals(client, activeCall=True)
    pay(client, "1", payee={"upi": "verify.kyc@okaxis"})
    age_payments(48)
    big = pay(client, "90000", payee={"upi": "verify.kyc@okaxis"}).json()
    assert big["band"] == "step_up"  # ₹1 is below the trust minimum and was made on a call


def test_payment_made_during_a_call_never_creates_trust(client):
    set_signals(client, activeCall=True)
    pay(client, "5000", payee={"upi": "helpdesk@okaxis"})
    age_payments(48)
    assert "NEW_PAYEE" in codes(pay(client, "500", payee={"upi": "helpdesk@okaxis"}))


def test_genuine_payee_becomes_trusted_after_24_hours(client):
    pay(client, "2000", payee={"upi": "landlord@okhdfc"})
    age_payments(25)
    assert codes(pay(client, "2000", payee={"upi": "landlord@okhdfc"})) == []


def test_splitting_a_large_amount_on_a_call_is_caught(client):
    set_signals(client, activeCall=True)
    bands = [pay(client, "9999", payee={"upi": "split.mule@okaxis"}).json()["band"] for _ in range(3)]
    assert bands[0] == "allow" and bands[1] == "step_up" and bands[2] == "step_up"


def test_spreading_money_over_many_new_payees_adds_up(client):
    for i in range(5):
        r = pay(client, "11000", payee={"upi": f"mule{i}@okaxis"})
    assert "UNTRUSTED_PAYEES_TOTAL" in codes(r)


# ---------------------------------------------------------------- information leaks

def test_customer_cannot_read_fri_category(client, monkeypatch):
    from app import settings

    monkeypatch.setattr(settings, "SHOW_ANALYST_VIEW", False)
    body = pay(client, "500", payee_type="PHONE", payee={"phone": "9999900002"}).json()
    assert "analyst" not in body and "score" not in body
    assert {"code": "TELECOM_FLAG", "text": "This payee's mobile number has been flagged by telecom fraud-risk signals",
            "group": "TELECOM"} in body["reasons"]
    assert "HIGH" not in str(body) and "FRI" not in str(body)
    assert "score" not in client.get("/payments").json()["payments"][0]


def test_session_cannot_change_its_own_signals(client):
    sid = current_session_id(client)
    assert client.put(f"/demo/sessions/{sid}/signals", json={}).status_code == 401
    assert client.put(f"/demo/sessions/{sid}/signals", json={}, headers={"x-demo-operator-token": "guess"}).status_code == 401
    # The old session-owned endpoints are gone.
    assert client.put("/demo/signals", json={}).status_code in (404, 405)


def test_operator_endpoints_fail_closed_without_a_configured_token(client, monkeypatch):
    from app import settings

    monkeypatch.setattr(settings, "DEMO_OPERATOR_TOKEN", "")
    sid = current_session_id(client)
    assert client.put(f"/demo/sessions/{sid}/signals", json={}, headers=OPERATOR).status_code == 503


# ---------------------------------------------------------------- retries and concurrency

def test_retry_with_same_idempotency_key_does_not_pay_twice(client):
    key = uuid.uuid4().hex
    first = pay(client, "500", key=key).json()
    again = pay(client, "500", key=key).json()
    assert again["paymentId"] == first["paymentId"] and again["replayed"] is True and first["replayed"] is False
    assert len(client.get("/payments").json()["payments"]) == 1


def test_reusing_a_key_for_a_different_payment_is_rejected(client):
    key = uuid.uuid4().hex
    pay(client, "500", key=key)
    assert pay(client, "900", key=key).status_code == 409


def test_missing_idempotency_key_is_rejected(client):
    r = client.post("/payments/score", json={"payeeType": "UPI", "payee": {"upi": "shop@okicici"}, "amount": "500"})
    assert r.status_code == 400 and "Idempotency-Key" in r.json()["detail"]


def test_parallel_burst_is_fully_counted_by_the_velocity_rule(client):
    from fastapi.testclient import TestClient

    from app import main

    cookie = client.cookies.get("sentinel_session")

    def one(i):
        with TestClient(main.app) as k:
            k.cookies.set("sentinel_session", cookie)
            return pay(k, "100", payee={"upi": "burst.shop@okicici"}).json()

    with cf.ThreadPoolExecutor(8) as ex:
        results = list(ex.map(one, range(8)))
    flagged = sum("RAPID_PAYMENTS" in [r["code"] for r in x["analyst"]["reasons"]] for x in results)
    assert flagged == 5, f"payments 4 to 8 must be flagged, got {flagged}"


def test_parallel_duplicate_submits_create_one_payment(client):
    from fastapi.testclient import TestClient

    from app import main

    cookie, key = client.cookies.get("sentinel_session"), uuid.uuid4().hex

    def one(_):
        with TestClient(main.app) as k:
            k.cookies.set("sentinel_session", cookie)
            return pay(k, "750", key=key).json()["paymentId"]

    with cf.ThreadPoolExecutor(6) as ex:
        ids = set(ex.map(one, range(6)))
    assert len(ids) == 1


# ---------------------------------------------------------------- hostile input and configuration

@pytest.mark.parametrize("amount", ["Infinity", "NaN", "-Infinity", "1e5", "١٠٠", "१००", "0x10", " ", "1.", ".5", "+100"])
def test_hostile_amounts_are_rejected_cleanly(client, amount):
    r = pay(client, amount)
    assert r.status_code == 422, (amount, r.status_code, r.text)


def test_indian_digit_grouping_is_accepted(client):
    assert pay(client, "1,00,000").json()["amount"] == "₹100,000"


def test_payments_refused_without_hmac_key(client, monkeypatch):
    from app import settings

    monkeypatch.setattr(settings, "PAYEE_HMAC_KEY", "")
    assert pay(client).status_code == 503
