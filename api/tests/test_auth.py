"""Full WebAuthn ceremonies against a real Postgres. Needs DATABASE_URL; skipped otherwise."""
import os
import uuid

import psycopg
import pytest

from .soft_authenticator import SoftAuthenticator

pytestmark = pytest.mark.skipif("DATABASE_URL" not in os.environ, reason="needs a real Postgres (DATABASE_URL)")


@pytest.fixture(scope="module")
def app_module():
    from app import main

    return main


@pytest.fixture()
def client(app_module):
    from fastapi.testclient import TestClient

    with TestClient(app_module.app) as c:
        yield c


def uname() -> str:
    return f"u{uuid.uuid4().hex[:10]}"


def register(client, auth: SoftAuthenticator, username: str):
    opts = client.post("/auth/register/options", json={"username": username, "displayName": "Test User"}).json()
    body = {"challengeId": opts["challengeId"], "credential": auth.create(opts["options"]), "label": "Laptop"}
    return client.post("/auth/register/verify", json=body), body


def login(client, auth: SoftAuthenticator, username: str, **get_kwargs):
    opts = client.post("/auth/login/options", json={"username": username}).json()
    body = {"challengeId": opts["challengeId"], "credential": auth.get(opts["options"], **get_kwargs)}
    return client.post("/auth/login/verify", json=body), body


def test_register_signs_in_and_lists_the_device(client):
    auth, user = SoftAuthenticator(), uname()
    r, _ = register(client, auth, user)
    assert r.status_code == 200, r.text
    assert "sentinel_session" in client.cookies
    me = client.get("/me").json()
    assert me["username"] == user and len(me["devices"]) == 1 and me["devices"][0]["label"] == "Laptop"


def test_login_with_passkey_and_counter_advances(client):
    auth, user = SoftAuthenticator(), uname()
    register(client, auth, user)
    client.cookies.clear()
    r, _ = login(client, auth, user)
    assert r.status_code == 200, r.text
    assert client.get("/me").status_code == 200


def test_replayed_registration_is_rejected(client):
    auth, user = SoftAuthenticator(), uname()
    first, body = register(client, auth, user)
    assert first.status_code == 200
    again = client.post("/auth/register/verify", json=body)
    assert again.status_code == 400 and "already used" in again.json()["detail"]


def test_replayed_login_is_rejected(client):
    auth, user = SoftAuthenticator(), uname()
    register(client, auth, user)
    ok, body = login(client, auth, user)
    assert ok.status_code == 200
    assert client.post("/auth/login/verify", json=body).status_code == 400


def test_expired_challenge_is_rejected(client):
    auth, user = SoftAuthenticator(), uname()
    opts = client.post("/auth/register/options", json={"username": user, "displayName": "X"}).json()
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute("UPDATE challenges SET expires_at = now() - interval '1 second' WHERE id = %s", (opts["challengeId"],))
    r = client.post("/auth/register/verify", json={"challengeId": opts["challengeId"], "credential": auth.create(opts["options"])})
    assert r.status_code == 400


def test_counter_rollback_flags_a_cloned_authenticator(client):
    auth, user = SoftAuthenticator(start_counter=10), uname()
    register(client, auth, user)
    assert login(client, auth, user, counter=20)[0].status_code == 200
    r, _ = login(client, auth, user, counter=15)  # a clone still at an older counter
    assert r.status_code == 400 and "cloned" in r.json()["detail"]


def test_wrong_origin_is_rejected(client):
    auth, user = SoftAuthenticator(), uname()
    opts = client.post("/auth/register/options", json={"username": user, "displayName": "X"}).json()
    cred = auth.create(opts["options"], origin="https://evil.example.com")
    r = client.post("/auth/register/verify", json={"challengeId": opts["challengeId"], "credential": cred})
    assert r.status_code == 400


def test_taken_username_is_rejected(client):
    auth, user = SoftAuthenticator(), uname()
    register(client, auth, user)
    r = client.post("/auth/register/options", json={"username": user, "displayName": "Someone else"})
    assert r.status_code == 409


def test_another_users_passkey_cannot_sign_in_as_you(client):
    alice, bob = SoftAuthenticator(), SoftAuthenticator()
    a, b = uname(), uname()
    register(client, alice, a)
    register(client, bob, b)
    client.cookies.clear()
    opts = client.post("/auth/login/options", json={"username": a}).json()
    r = client.post("/auth/login/verify", json={"challengeId": opts["challengeId"], "credential": bob.get(opts["options"])})
    assert r.status_code == 400


def test_unknown_username_looks_the_same_but_cannot_sign_in(client):
    auth = SoftAuthenticator()
    r = client.post("/auth/login/options", json={"username": "nobody-here"})
    assert r.status_code == 200 and r.json()["options"].get("allowCredentials", []) == []
    v = client.post("/auth/login/verify", json={"challengeId": r.json()["challengeId"], "credential": auth.get(r.json()["options"])})
    assert v.status_code == 400


def test_cannot_remove_only_passkey_but_can_after_adding_another(client):
    first, user = SoftAuthenticator(), uname()
    register(client, first, user)
    only = client.get("/me").json()["devices"][0]["id"]
    assert client.delete(f"/me/devices/{only}").status_code == 409

    second = SoftAuthenticator()
    opts = client.post("/me/devices/options").json()
    added = client.post("/me/devices/verify", json={"challengeId": opts["challengeId"], "credential": second.create(opts["options"]), "label": "Phone"})
    assert added.status_code == 200, added.text
    assert len(client.get("/me").json()["devices"]) == 2

    # Signed in with the first passkey, so removing it also ends this session.
    assert client.delete(f"/me/devices/{only}").status_code == 200
    assert client.get("/me").status_code == 401


def test_revoked_session_and_logout(client):
    auth, user = SoftAuthenticator(), uname()
    register(client, auth, user)
    sid = client.get("/me").json()["sessions"][0]["id"]
    assert client.delete(f"/me/sessions/{sid}").status_code == 200
    assert client.get("/me").status_code == 401

    login(client, auth, user)
    assert client.post("/auth/logout").status_code == 200
    assert client.get("/me").status_code == 401


def test_bad_username_format_is_rejected(client):
    assert client.post("/auth/register/options", json={"username": "Bad Name!", "displayName": "x"}).status_code == 422
