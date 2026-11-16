"""Volunteer typing study (slice 2b part 2). Needs DATABASE_URL; skipped otherwise."""
import os

import psycopg
import pytest

from .test_payments import typing

pytestmark = pytest.mark.skipif("DATABASE_URL" not in os.environ, reason="needs a real Postgres (DATABASE_URL)")
OP = {"x-demo-operator-token": "test-operator-token"}
PROMPT_KEYS = 24  # all practice prompts are 20-25 characters


@pytest.fixture()
def c(monkeypatch):
    from fastapi.testclient import TestClient

    from app import main, settings

    monkeypatch.setattr(settings, "STUDY_ENABLED", True)
    monkeypatch.setattr(settings, "STUDY_SESSION_GAP_HOURS", 4.0)
    with TestClient(main.app) as client:
        yield client


def invite(c, n=1):
    r = c.post("/research/invites", json={"count": n}, headers=OP)
    assert r.status_code == 200, r.text
    return r.json()["codes"]


def join(c, keyboard="laptop"):
    r = c.post("/research/join", json={"inviteCode": invite(c)[0], "consentVersion": "v1", "keyboard": keyboard})
    assert r.status_code == 200, r.text
    return {"x-participant-code": r.json()["participantCode"]}


def add(c, who, prompt, **kw):
    from app.research import PROMPTS

    keys = len(PROMPTS[prompt]["upi"]) + len(PROMPTS[prompt]["amount"]) + 2
    beh = typing(keystrokes=kw.pop("keystrokes", keys), **kw)
    return c.post("/research/samples", json={"promptNo": prompt, "behaviour": beh}, headers=who)


def session(c, who, n=10, start=0):
    for k in range(start, start + n):
        r = add(c, who, k)
        assert r.status_code == 200, r.text
    return r.json()


def age(who, hours):
    """Move a volunteer's samples back in time (instead of waiting hours in a test)."""
    from app.research import code_hash

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute(
            "UPDATE research_samples SET created_at = created_at - make_interval(secs => %s) WHERE participant_id = "
            "(SELECT id FROM research_participants WHERE code_hash = %s)",
            (hours * 3600, code_hash("participant", who["x-participant-code"])),
        )


def test_study_is_invisible_unless_enabled(c, monkeypatch):
    from app import settings

    monkeypatch.setattr(settings, "STUDY_ENABLED", False)
    assert c.get("/research/protocol").status_code == 404
    assert c.post("/research/invites", json={"count": 1}, headers=OP).status_code == 404


def test_protocol_serves_consent_and_prompts(c):
    p = c.get("/research/protocol").json()
    assert p["consentVersion"] == "v1" and len(p["prompts"]) == p["samplesPerSession"] == 10
    assert any("never sent or stored" in line for line in p["consent"])


def test_only_the_operator_can_issue_invites(c, monkeypatch):
    from app import settings

    monkeypatch.setattr(settings, "DEMO_SIGNALS", False)  # the study does not need the demo injector on
    assert c.post("/research/invites", json={"count": 1}, headers=OP).status_code == 200
    assert c.post("/research/invites", json={"count": 1}).status_code == 401
    assert c.post("/research/invites", json={"count": 1}, headers={"x-demo-operator-token": "guess"}).status_code == 401
    assert c.post("/research/invites", json={"count": 51}, headers=OP).status_code == 422


def test_invite_is_single_use_and_consent_is_required(c):
    code = invite(c)[0]
    body = {"inviteCode": code, "consentVersion": "v0", "keyboard": "laptop"}
    assert c.post("/research/join", json=body).status_code == 400  # old consent text: refused, invite kept
    body["consentVersion"] = "v1"
    first = c.post("/research/join", json=body)
    assert first.status_code == 200
    assert c.post("/research/join", json=body).status_code == 409  # one invite, one person
    assert c.post("/research/join", json={**body, "inviteCode": "AAAA-BBBB-CCCC"}).status_code == 403


def test_participant_code_is_new_and_invite_cannot_be_used_as_one(c):
    code = invite(c)[0]
    r = c.post("/research/join", json={"inviteCode": code, "consentVersion": "v1", "keyboard": "external"}).json()
    assert r["participantCode"] != code
    assert c.get("/research/me", headers={"x-participant-code": code}).status_code == 401
    me = c.get("/research/me", headers={"x-participant-code": r["participantCode"].lower()})  # case-insensitive
    assert me.status_code == 200 and me.json()["keyboard"] == "external"


def test_samples_need_a_participant_code(c):
    assert c.post("/research/samples", json={"promptNo": 0, "behaviour": typing()}).status_code == 401


def test_pasted_too_short_or_implausible_samples_are_rejected(c):
    who = join(c)
    pasted = {**typing(), "pasted": True}
    assert c.post("/research/samples", json={"promptNo": 0, "behaviour": pasted}, headers=who).status_code == 422
    assert add(c, who, 0, keystrokes=10).status_code == 422          # fewer keys than characters
    assert add(c, who, 0, keystrokes=400).status_code == 422         # not this prompt
    assert add(c, who, 0, dwell=5000).status_code == 422             # out of range, not clamped
    assert c.get("/research/me", headers=who).json()["sessions"] == []


def test_prompts_must_be_typed_in_order(c):
    who = join(c)
    r = add(c, who, 3)
    assert r.status_code == 409 and "Expected prompt 1" in r.json()["detail"]


def test_a_session_holds_ten_then_the_next_waits_for_the_gap(c):
    who = join(c)
    st = session(c, who)
    assert st["sessions"] == [{"session": 1, "samples": 10}]
    assert st["next"]["session"] == 2 and st["next"]["allowed"] is False and st["next"]["opensAt"]
    r = add(c, who, 0)
    assert r.status_code == 409 and "opens at" in r.json()["detail"]
    age(who, 5)
    st = session(c, who)
    assert st["sessions"][1] == {"session": 2, "samples": 10} and st["enoughForStudy"] is True


def test_a_long_pause_ends_the_sitting(c):
    """An abandoned half session is kept, and the volunteer continues in a new session after the gap."""
    who = join(c)
    session(c, who, n=6)
    age(who, 1)  # 60 minutes idle: the sitting is over, but the 4-hour gap has not passed
    st = c.get("/research/me", headers=who).json()
    assert st["next"]["session"] == 2 and st["next"]["allowed"] is False
    age(who, 4)
    assert add(c, who, 0).json()["sessions"] == [{"session": 1, "samples": 6}, {"session": 2, "samples": 1}]


def test_no_more_than_three_sessions(c):
    who = join(c)
    for _ in range(3):
        session(c, who)
        age(who, 5)
    st = c.get("/research/me", headers=who).json()
    assert st["next"]["finished"] is True
    assert add(c, who, 0).status_code == 409


def test_study_data_never_touches_bank_profiles(c):
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        before = conn.execute("SELECT count(*) FROM behaviour_samples").fetchone()[0]
    who = join(c)
    session(c, who, n=5)
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        assert conn.execute("SELECT count(*) FROM behaviour_samples").fetchone()[0] == before
        cols = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'research_participants'")}
    assert not cols & {"user_id", "ip", "user_agent", "name", "email"}


def test_withdrawal_deletes_everything_and_the_audit_log_keeps_no_id(c):
    from app import audit
    from app.research import code_hash

    who = join(c)
    session(c, who, n=4)
    r = c.post("/research/withdraw", headers=who)
    assert r.json() == {"withdrawn": True, "samplesDeleted": 4}
    assert c.get("/research/me", headers=who).status_code == 401
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        left = conn.execute(
            "SELECT count(*) FROM research_participants WHERE code_hash = %s",
            (code_hash("participant", who["x-participant-code"]),),
        ).fetchone()[0]
    assert left == 0
    assert audit.pending_events("RESEARCH_WITHDRAWAL")[-1]["payload"] == {"samplesDeleted": 4}


def test_export_is_operator_only_pseudonymous_and_has_no_clock_times(c):
    assert c.post("/research/purge", json={"confirm": "yes"}, headers=OP).status_code == 400
    assert c.post("/research/purge", json={"confirm": "DELETE ALL STUDY DATA"}, headers=OP).status_code == 200
    a, b = join(c), join(c, keyboard="phone")
    session(c, a)
    age(a, 26)
    session(c, a, n=3)
    session(c, b, n=5)
    assert c.get("/research/export").status_code == 401
    data = c.get("/research/export", headers=OP).json()
    assert data["format"] == "sentinel-typing-study/1"
    people = {p["pid"]: p for p in data["participants"]}
    assert set(people) == {"p01", "p02"}
    text = str(data)
    for secret in (a["x-participant-code"], b["x-participant-code"]):
        assert secret not in text
    assert "created_at" not in text and "participant_id" not in text
    two = next(p for p in people.values() if len(p["sessions"]) == 2)
    assert [s["startHours"] for s in two["sessions"]] == [0.0, 26.0]
    assert [len(s["samples"]) for s in two["sessions"]] == [10, 3]
    assert two["sessions"][0]["samples"][0]["promptNo"] == 0 and "dwellMeanMs" in two["sessions"][0]["samples"][0]


def test_purge_deletes_all_study_data(c):
    who = join(c)
    session(c, who, n=2)
    r = c.post("/research/purge", json={"confirm": "DELETE ALL STUDY DATA"}, headers=OP)
    assert r.status_code == 200 and r.json()["participants"] >= 1
    assert c.get("/research/me", headers=who).status_code == 401
    assert c.get("/research/export", headers=OP).json()["participants"] == []


def test_export_feeds_the_evaluation_script(c):
    """End to end: collect, export, evaluate. Uses 5 volunteers with distinct rhythms."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("eval_study", Path(__file__).resolve().parents[1] / "scripts" / "eval_study.py")
    ev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ev)

    c.post("/research/purge", json={"confirm": "DELETE ALL STUDY DATA"}, headers=OP)
    for i in range(5):
        who = join(c)
        for k in range(10):
            assert add(c, who, k, dwell=90 + 25 * i + (k % 3), flight=150 + 40 * i - (k % 2) * 4).status_code == 200
        age(who, 24)
        for k in range(10):
            assert add(c, who, k, dwell=91 + 25 * i + (k % 3), flight=152 + 40 * i).status_code == 200
    r = ev.evaluate(c.get("/research/export", headers=OP).json())
    assert r["participants"] == 5 and r["withLaterSession"] == 5 and r["pilot"] is True
    assert r["rates"]["0.5"]["falseFlagLater"]["value"] == 0.0
