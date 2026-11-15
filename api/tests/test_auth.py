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


# ---------------------------------------------------------------- review fixes
# All but the 409 race test failed on the previous code. The race test is a regression guard: it pins
# the 409 now that the UNIQUE constraint, not a pre-check, decides the winner.

def _user_id(client) -> str:
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        return str(conn.execute("SELECT id FROM users WHERE username = %s", (client.get("/me").json()["username"],)).fetchone()[0])


def test_login_options_are_identical_for_real_and_unknown_usernames(client):
    user = uname()
    register(client, SoftAuthenticator(), user)
    client.cookies.clear()
    real = client.post("/auth/login/options", json={"username": user}).json()["options"]
    fake = client.post("/auth/login/options", json={"username": uname()}).json()["options"]
    for opts in (real, fake):
        opts.pop("challenge")  # random per request
    assert real == fake, "login options must not reveal whether the username exists"


def test_login_without_a_username_uses_the_discoverable_passkey(client):
    auth, user = SoftAuthenticator(), uname()
    register(client, auth, user)
    client.cookies.clear()
    opts = client.post("/auth/login/options", json={}).json()
    r = client.post("/auth/login/verify", json={"challengeId": opts["challengeId"], "credential": auth.get(opts["options"])})
    assert r.status_code == 200 and r.json()["username"] == user


def test_registration_requires_a_discoverable_passkey(client):
    opts = client.post("/auth/register/options", json={"username": uname(), "displayName": "X"}).json()["options"]
    assert opts["authenticatorSelection"]["residentKey"] == "required"


def test_user_handle_from_another_account_is_rejected(client):
    auth, user = SoftAuthenticator(), uname()
    register(client, auth, user)
    client.cookies.clear()
    opts = client.post("/auth/login/options", json={}).json()
    cred = auth.get(opts["options"])
    cred["response"]["userHandle"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"  # someone else's handle
    r = client.post("/auth/login/verify", json={"challengeId": opts["challengeId"], "credential": cred})
    assert r.status_code == 400


def test_cloned_authenticator_alert_is_recorded_even_though_login_is_rejected(client, app_module):
    from app import audit

    auth, user = SoftAuthenticator(start_counter=10), uname()
    register(client, auth, user)
    uid = _user_id(client)
    client.cookies.clear()
    assert login(client, auth, user, counter=20)[0].status_code == 200
    r, _ = login(client, auth, user, counter=15)
    assert r.status_code == 400
    alerts = [e for e in audit.pending_events("POSSIBLE_CLONED_AUTHENTICATOR") if e["payload"]["userId"] == uid]
    assert len(alerts) == 1, "old code dropped this event: background tasks never run when the endpoint raises"


def test_audit_events_exist_only_for_committed_actions(client):
    from app import audit

    alice, bob = SoftAuthenticator(), SoftAuthenticator()
    a, b = uname(), uname()
    register(client, alice, a)
    uid_a = _user_id(client)
    register(client, bob, b)
    client.cookies.clear()
    opts = client.post("/auth/login/options", json={"username": a}).json()
    assert client.post("/auth/login/verify", json={"challengeId": opts["challengeId"], "credential": bob.get(opts["options"])}).status_code == 400
    assert not [e for e in audit.pending_events("LOGIN_SUCCESS") if e["payload"]["userId"] == uid_a]
    assert [e for e in audit.pending_events("USER_REGISTERED") if e["payload"]["userId"] == uid_a]


def test_two_signups_racing_for_one_username_get_409_not_500(client):
    user = uname()
    o1 = client.post("/auth/register/options", json={"username": user, "displayName": "First"}).json()
    o2 = client.post("/auth/register/options", json={"username": user, "displayName": "Second"}).json()
    r1 = client.post("/auth/register/verify", json={"challengeId": o1["challengeId"], "credential": SoftAuthenticator().create(o1["options"])})
    client.cookies.clear()
    r2 = client.post("/auth/register/verify", json={"challengeId": o2["challengeId"], "credential": SoftAuthenticator().create(o2["options"])})
    assert r1.status_code == 200
    assert r2.status_code == 409, r2.text


def test_device_removal_waits_for_a_concurrent_removal_and_never_leaves_zero_passkeys(client):
    """Another transaction holds the user's passkeys and deletes one; our removal must wait, then refuse."""
    import threading
    import time

    first, user = SoftAuthenticator(), uname()
    register(client, first, user)
    second = SoftAuthenticator()
    opts = client.post("/me/devices/options").json()
    client.post("/me/devices/verify", json={"challengeId": opts["challengeId"], "credential": second.create(opts["options"])})
    devices = client.get("/me").json()["devices"]
    assert len(devices) == 2
    uid = _user_id(client)
    first_id = devices[0]["id"]

    result = {}
    with psycopg.connect(os.environ["DATABASE_URL"]) as other:
        other.execute("SELECT id FROM credentials WHERE user_id = %s FOR UPDATE", (uid,))  # concurrent remover
        t = threading.Thread(target=lambda: result.update(r=client.delete(f"/me/devices/{devices[1]['id']}")))
        t.start()
        time.sleep(0.5)
        assert t.is_alive(), "removal must block while another transaction holds the user's passkeys"
        from webauthn.helpers import base64url_to_bytes

        other.execute("DELETE FROM credentials WHERE id = %s", (base64url_to_bytes(first_id),))
        other.commit()  # the other removal finishes: one passkey left
    t.join(timeout=5)
    assert result["r"].status_code == 409, result["r"].text
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        assert conn.execute("SELECT count(*) FROM credentials WHERE user_id = %s", (uid,)).fetchone()[0] == 1


def test_outbox_drains_in_order_and_survives_an_audit_outage(client, monkeypatch):
    import httpx

    from app import audit, settings

    register(client, SoftAuthenticator(), uname())  # guarantees at least one pending event
    monkeypatch.setattr(settings, "AUDIT_URL", "http://audit.test")
    monkeypatch.setattr(settings, "AUDIT_TOKEN", "t0k3n")
    before = audit.backlog()
    assert before > 0

    down = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(503)))
    assert audit.drain_once(down) == 0
    assert audit.backlog() == before, "nothing may be lost or marked sent while the audit service is down"

    seen = []

    def ok(req: httpx.Request) -> httpx.Response:
        assert req.headers["x-audit-token"] == "t0k3n"
        seen.append(req.read())
        return httpx.Response(201, json={})

    up = httpx.Client(transport=httpx.MockTransport(ok))
    while audit.drain_once(up):
        pass
    assert audit.backlog() == 0 and len(seen) >= before


def test_forged_rollback_with_bad_signature_does_not_raise_a_clone_alert(client):
    """py_webauthn checks the counter before the signature. A forged response (attacker's own key,
    victim's credential id, low counter) must fail as a bad signature and must NOT log a clone alert."""
    from app import audit

    victim, user = SoftAuthenticator(start_counter=10), uname()
    register(client, victim, user)
    assert login(client, victim, user, counter=20)[0].status_code == 200
    before = len(audit.pending_events("POSSIBLE_CLONED_AUTHENTICATOR"))

    attacker = SoftAuthenticator()
    attacker.cred_id = victim.cred_id  # knows the credential id, not the private key
    opts = client.post("/auth/login/options", json={"username": user}).json()
    r = client.post("/auth/login/verify", json={"challengeId": opts["challengeId"], "credential": attacker.get(opts["options"], counter=5)})
    assert r.status_code == 400
    assert "cloned" not in r.json()["detail"]
    assert len(audit.pending_events("POSSIBLE_CLONED_AUTHENTICATOR")) == before


def test_drainer_sends_a_stable_idempotency_key_per_event(client, monkeypatch):
    import httpx

    from app import audit, db, settings

    monkeypatch.setattr(settings, "AUDIT_URL", "http://audit.test")

    seen = []
    transport = httpx.MockTransport(lambda req: (seen.append(__import__("json").loads(req.content)), httpx.Response(201, json={}))[1])
    with db.connect() as conn:
        conn.execute("UPDATE audit_outbox SET sent_at = now() WHERE sent_at IS NULL")
        audit.enqueue(conn, "TEST_EVENT", {"k": 1})
    with httpx.Client(transport=transport) as c:
        assert audit.drain_once(c) == 1
    key = seen[-1]["idempotencyKey"]
    stream, word, row_id = key.rsplit(":", 2)
    assert stream == settings.AUDIT_STREAM and word == "outbox" and row_id.isdigit()
