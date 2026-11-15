"""Guided behaviour enrolment. Needs DATABASE_URL; skipped otherwise."""
import os
import uuid

import psycopg
import pytest

from .soft_authenticator import SoftAuthenticator
from .test_payments import pay_b, typing

pytestmark = pytest.mark.skipif("DATABASE_URL" not in os.environ, reason="needs a real Postgres (DATABASE_URL)")


@pytest.fixture()
def user(monkeypatch):
    from fastapi.testclient import TestClient

    from app import main, payments, settings

    monkeypatch.setattr(settings, "DEMO_SIGNALS", True)
    monkeypatch.setattr(settings, "SHOW_ANALYST_VIEW", True)
    monkeypatch.setattr(payments, "ist_hour", lambda: 14)
    with TestClient(main.app) as c:
        auth, name = SoftAuthenticator(), f"e{uuid.uuid4().hex[:10]}"
        opts = c.post("/auth/register/options", json={"username": name, "displayName": "Enrol"}).json()
        c.post("/auth/register/verify", json={"challengeId": opts["challengeId"], "credential": auth.create(opts["options"])})
        yield c, auth


def start(c, auth, reset=False, **get_kwargs):
    opts = c.post("/behaviour/enrol/options").json()
    return c.post("/behaviour/enrol/start", json={"challengeId": opts["challengeId"],
                                                   "credential": auth.get(opts["options"], **get_kwargs), "reset": reset})


def sample(c, **kw):
    return c.post("/behaviour/enrol/sample", json={"behaviour": typing(**kw)})


def test_samples_need_a_fresh_passkey_check(user):
    c, _ = user
    r = sample(c)
    assert r.status_code == 403 and "passkey" in r.json()["detail"]


def test_enrol_five_samples_then_profile_scores(user):
    c, auth = user
    assert start(c, auth).status_code == 200
    for i in range(5):
        r = sample(c, dwell=110 + i, flight=190 - i)
        assert r.status_code == 200, r.text
    assert r.json()["done"] is True
    prof = c.get("/behaviour/profile").json()
    assert prof["enrolled"] and prof["samples"] == 5 and prof["enrolment"] is None
    own = pay_b(c, "300", "first.shop@okicici", typing()).json()["analyst"]["signals"]["behaviour"]
    assert own["model"] == "robust-z" and own["anomaly"] < 0.3
    other = pay_b(c, "300", "first.shop@okicici", typing(dwell=210, flight=430, fill=3500, speed=3.2)).json()
    assert "BEHAVIOUR_ANOMALY_HIGH" in [x["code"] for x in other["analyst"]["reasons"]]


def test_window_closes_after_five(user):
    c, auth = user
    start(c, auth)
    for _ in range(5):
        sample(c)
    assert sample(c).status_code == 403


def test_window_expires(user):
    c, auth = user
    start(c, auth)
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute("UPDATE behaviour_enrolment SET expires_at = now() - interval '1 second'")
    assert sample(c).status_code == 403


def test_window_belongs_to_the_session_that_passed_the_check(user):
    from fastapi.testclient import TestClient

    from app import main

    c, auth = user
    start(c, auth)
    # The same account signed in on another session (e.g. an attacker with a second stolen cookie).
    with TestClient(main.app) as other:
        opts = other.post("/auth/login/options", json={}).json()
        other.post("/auth/login/verify", json={"challengeId": opts["challengeId"], "credential": auth.get(opts["options"])})
        assert other.post("/behaviour/enrol/sample", json={"behaviour": typing()}).status_code == 403


def test_another_accounts_passkey_cannot_open_enrolment(user):
    c, _ = user
    stranger = SoftAuthenticator()
    opts = c.post("/behaviour/enrol/options").json()
    r = c.post("/behaviour/enrol/start", json={"challengeId": opts["challengeId"], "credential": stranger.get(opts["options"])})
    assert r.status_code == 400


def test_existing_profile_needs_explicit_reset(user):
    c, auth = user
    start(c, auth)
    for _ in range(5):
        sample(c)
    assert start(c, auth).status_code == 409
    assert start(c, auth, reset=True).status_code == 200
    assert c.get("/behaviour/profile").json()["samples"] == 0


def test_pasted_or_crafted_samples_are_refused(user):
    c, auth = user
    start(c, auth)
    assert sample(c, keystrokes=2).status_code == 422
    assert sample(c, dwell=99999).status_code == 422
    assert c.get("/behaviour/profile").json()["enrolment"]["added"] == 0


def test_enrolment_is_audited(user):
    from app import audit

    c, auth = user
    before = len(audit.pending_events("BEHAVIOUR_ENROLLED"))
    start(c, auth)
    for _ in range(5):
        sample(c)
    assert len(audit.pending_events("BEHAVIOUR_ENROLLED")) == before + 1

