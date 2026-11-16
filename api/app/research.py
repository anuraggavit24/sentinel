"""Slice 2b part 2: volunteer typing study, to measure the behaviour model on REAL people.

Why this exists: every behaviour number so far comes from synthetic users. This collects timing-only
samples from consenting volunteers, under a fixed protocol, so `scripts/eval_study.py` can report real
false-flag and miss rates with honest confidence intervals.

Protocol
- Everyone types the same 10 practice prompts (a UPI ID and an amount, like the payment form), so other
  volunteers' samples are fair "impostor" attempts on identical text.
- A session is up to 10 samples. A volunteer does 2-3 sessions at least STUDY_SESSION_GAP_HOURS apart:
  the first 5 samples build the profile, later sessions are the realistic genuine test (a different day,
  a different mood), and the rest of session 1 is the optimistic same-sitting test.

Privacy
- Not linked to bank accounts. No name, IP address or user agent is stored; only timing statistics.
- Invites are single use (stops one person posing as many). The participant code is created at join, so
  whoever handed out the invite cannot link it to the data. Only hashes of both codes are stored.
- Withdrawal deletes every sample. The audit log records operator actions and counts, never participant
  ids, so a deletion leaves nothing behind in the append-only log.
- The export drops ids and clock times: participants are shuffled pseudonyms, sessions carry only
  "hours after the first session".
"""
from __future__ import annotations

import hashlib
import random
import secrets
from datetime import timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from . import audit, db, settings
from .auth_deps import now
from .payments import check_operator_token
from .risk import behaviour as bh

CONSENT_VERSION = "v1"
CONSENT = [
    "This is a college project (Blockchain Technology class) testing whether typing rhythm can help spot fraud.",
    "Only timing is recorded: how long keys are held, the pause between keys and how often you correct. "
    "The text you type is never sent or stored.",
    "Your name, email, IP address and browser are not recorded. You get a random participant code; "
    "nobody else, including the organiser, can link it to you unless you tell them.",
    "The data is used only to measure how often the model wrongly flags a person and how often it misses "
    "someone else. Results are reported as group totals.",
    "You can withdraw at any time with your participant code. All your samples are deleted immediately.",
    "All study data is deleted when the project ends.",
]
PROMPTS = [
    {"upi": "practice.one@okhdfc", "amount": "1500"},
    {"upi": "sample.payee@okicici", "amount": "2750"},
    {"upi": "typing.test@okaxis", "amount": "980"},
    {"upi": "my.rhythm@oksbi", "amount": "12400"},
    {"upi": "last.sample@ybl", "amount": "640"},
    {"upi": "green.leaf@okhdfc", "amount": "3320"},
    {"upi": "river.stone@okaxis", "amount": "715"},
    {"upi": "blue.kite@oksbi", "amount": "8060"},
    {"upi": "quiet.road@ybl", "amount": "2490"},
    {"upi": "north.star@okicici", "amount": "560"},
]
SAMPLES_PER_SESSION = len(PROMPTS)
MIN_SESSIONS = 2
MAX_SESSIONS = 3
SESSION_IDLE_MINUTES = 30   # a pause longer than this ends the sitting
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32: no I, L, O, U
CONFIRM_PURGE = "DELETE ALL STUDY DATA"


def require_operator(x_demo_operator_token: Optional[str] = Header(default=None)) -> None:
    check_operator_token(x_demo_operator_token)  # works with demo signals off


def study_enabled() -> None:
    if not settings.STUDY_ENABLED:
        raise HTTPException(404, "Not Found")


router = APIRouter(prefix="/research", dependencies=[Depends(study_enabled)])


# ---------------------------------------------------------------- codes
def new_code() -> str:
    raw = "".join(secrets.choice(_ALPHABET) for _ in range(12))  # 60 bits
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def _normalise(code: str) -> str:
    c = code.upper().replace("-", "").replace(" ", "")
    return c.replace("O", "0").replace("I", "1").replace("L", "1")


def code_hash(kind: str, code: str) -> bytes:
    """Domain-separated, so an invite code can never be used as a participant code."""
    return hashlib.sha256(f"{kind}:{_normalise(code)}".encode()).digest()


# ---------------------------------------------------------------- session bookkeeping
def _sessions(conn, pid) -> list[dict]:
    return conn.execute(
        "SELECT session_no, count(*) AS n, min(created_at) AS first, max(created_at) AS last "
        "FROM research_samples WHERE participant_id = %s GROUP BY session_no ORDER BY session_no",
        (pid,),
    ).fetchall()


def _next(sessions: list[dict]) -> dict:
    """Where the next sample goes: same sitting, a new session, not yet, or never (finished)."""
    t = now()
    if not sessions:
        return {"session": 1, "promptNo": 0, "allowed": True, "opensAt": None, "finished": False}
    last = sessions[-1]
    if last["n"] < SAMPLES_PER_SESSION and t - last["last"] < timedelta(minutes=SESSION_IDLE_MINUTES):
        return {"session": last["session_no"], "promptNo": last["n"], "allowed": True, "opensAt": None, "finished": False}
    if last["session_no"] >= MAX_SESSIONS:
        return {"session": None, "promptNo": None, "allowed": False, "opensAt": None, "finished": True}
    opens = last["first"] + timedelta(hours=settings.STUDY_SESSION_GAP_HOURS)
    return {
        "session": last["session_no"] + 1,
        "promptNo": 0,
        "allowed": t >= opens,
        "opensAt": None if t >= opens else opens.isoformat(),
        "finished": False,
    }


def _status(conn, pid) -> dict:
    sessions = _sessions(conn, pid)
    full = sum(1 for s in sessions if s["n"] >= SAMPLES_PER_SESSION)
    return {
        "sessions": [{"session": s["session_no"], "samples": s["n"]} for s in sessions],
        "next": _next(sessions),
        "samplesPerSession": SAMPLES_PER_SESSION,
        "minSessions": MIN_SESSIONS,
        "maxSessions": MAX_SESSIONS,
        "enoughForStudy": full >= MIN_SESSIONS,
    }


def participant(x_participant_code: Optional[str] = Header(default=None)) -> dict:
    if not x_participant_code:
        raise HTTPException(401, "Participant code required")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, keyboard FROM research_participants WHERE code_hash = %s",
            (code_hash("participant", x_participant_code),),
        ).fetchone()
    if row is None:
        raise HTTPException(401, "Participant code not recognised (or you have withdrawn)")
    return row


# ---------------------------------------------------------------- volunteer endpoints
@router.get("/protocol")
def protocol() -> dict:
    return {
        "consentVersion": CONSENT_VERSION,
        "consent": CONSENT,
        "prompts": PROMPTS,
        "samplesPerSession": SAMPLES_PER_SESSION,
        "minSessions": MIN_SESSIONS,
        "maxSessions": MAX_SESSIONS,
        "sessionGapHours": settings.STUDY_SESSION_GAP_HOURS,
    }


class JoinIn(BaseModel):
    inviteCode: str = Field(min_length=4, max_length=40)
    consentVersion: str
    keyboard: Literal["laptop", "external", "phone"]


@router.post("/join")
def join(body: JoinIn) -> dict:
    if body.consentVersion != CONSENT_VERSION:
        raise HTTPException(400, "Please read and accept the current consent text")
    code = new_code()
    with db.connect() as conn, conn.transaction():
        inv = conn.execute(
            "SELECT used_at FROM research_invites WHERE code_hash = %s FOR UPDATE",
            (code_hash("invite", body.inviteCode),),
        ).fetchone()
        if inv is None:
            raise HTTPException(403, "Invite code not recognised")
        if inv["used_at"] is not None:
            raise HTTPException(409, "This invite code has already been used")
        conn.execute("UPDATE research_invites SET used_at = now() WHERE code_hash = %s",
                     (code_hash("invite", body.inviteCode),))
        conn.execute(
            "INSERT INTO research_participants (code_hash, consent_version, keyboard) VALUES (%s, %s, %s)",
            (code_hash("participant", code), CONSENT_VERSION, body.keyboard),
        )
    return {"participantCode": code, "samplesPerSession": SAMPLES_PER_SESSION, "minSessions": MIN_SESSIONS}


@router.get("/me")
def me(p: dict = Depends(participant)) -> dict:
    with db.connect() as conn:
        return {"keyboard": p["keyboard"], **_status(conn, p["id"])}


class SampleIn(BaseModel):
    promptNo: int = Field(ge=0, lt=len(PROMPTS))
    behaviour: Optional[dict] = None


@router.post("/samples")
def add_sample(body: SampleIn, p: dict = Depends(participant)) -> dict:
    try:
        sample = bh.validate(body.behaviour)
    except bh.BehaviourInvalid as exc:
        raise HTTPException(422, f"Typing data rejected: {exc}") from exc
    if sample is None or sample["pasted"]:
        raise HTTPException(422, "Type the sample by hand, without pasting")
    # The server never sees the text, but it knows how long the prompt is: fewer key presses than
    # characters means it was not typed; far more means it is not a sample of this prompt.
    text_len = len(PROMPTS[body.promptNo]["upi"]) + len(PROMPTS[body.promptNo]["amount"])
    keys = body.behaviour["keystrokes"]
    if not text_len <= keys <= 4 * text_len + 10:
        raise HTTPException(422, f"{keys} key presses does not fit a {text_len}-character prompt")
    with db.connect() as conn, conn.transaction():
        conn.execute("SELECT 1 FROM research_participants WHERE id = %s FOR UPDATE", (p["id"],))
        nxt = _next(_sessions(conn, p["id"]))
        if nxt["finished"]:
            raise HTTPException(409, "You have finished every session. Thank you!")
        if not nxt["allowed"]:
            raise HTTPException(409, f"Your next session opens at {nxt['opensAt']}")
        if body.promptNo != nxt["promptNo"]:
            raise HTTPException(409, f"Expected prompt {nxt['promptNo'] + 1}, got {body.promptNo + 1}")
        conn.execute(
            "INSERT INTO research_samples (participant_id, session_no, prompt_no, features) VALUES (%s, %s, %s, %s)",
            (p["id"], nxt["session"], body.promptNo, Jsonb(sample)),
        )
        return _status(conn, p["id"])


@router.post("/withdraw")
def withdraw(p: dict = Depends(participant)) -> dict:
    with db.connect() as conn, conn.transaction():
        n = conn.execute("SELECT count(*) AS n FROM research_samples WHERE participant_id = %s", (p["id"],)).fetchone()["n"]
        conn.execute("DELETE FROM research_participants WHERE id = %s", (p["id"],))
        audit.enqueue(conn, "RESEARCH_WITHDRAWAL", {"samplesDeleted": n})  # a count, never an id
    return {"withdrawn": True, "samplesDeleted": n}


# ---------------------------------------------------------------- operator endpoints
class InvitesIn(BaseModel):
    count: int = Field(ge=1, le=50)


@router.post("/invites", dependencies=[Depends(require_operator)])
def invites(body: InvitesIn) -> dict:
    codes = [new_code() for _ in range(body.count)]
    with db.connect() as conn, conn.transaction():
        for c in codes:
            conn.execute("INSERT INTO research_invites (code_hash) VALUES (%s)", (code_hash("invite", c),))
        audit.enqueue(conn, "RESEARCH_INVITES_ISSUED", {"count": body.count})
    return {"codes": codes, "note": "Shown once. One code per volunteer."}


@router.get("/export", dependencies=[Depends(require_operator)])
def export() -> dict:
    with db.connect() as conn, conn.transaction():
        people = conn.execute("SELECT id, keyboard, consent_version FROM research_participants").fetchall()
        rows = conn.execute(
            "SELECT participant_id, session_no, prompt_no, features, created_at FROM research_samples ORDER BY id"
        ).fetchall()
        by_pid: dict = {}
        for r in rows:
            by_pid.setdefault(r["participant_id"], []).append(r)
        out = []
        for person in people:
            samples = by_pid.get(person["id"], [])
            if not samples:
                continue
            first = min(s["created_at"] for s in samples)
            sessions: dict[int, dict] = {}
            for s in samples:
                sess = sessions.setdefault(s["session_no"], {"session": s["session_no"], "startHours": None, "samples": []})
                if sess["startHours"] is None:
                    sess["startHours"] = round((s["created_at"] - first).total_seconds() / 3600, 1)
                sess["samples"].append({"promptNo": s["prompt_no"], **s["features"]})
            out.append({"keyboard": person["keyboard"], "consentVersion": person["consent_version"],
                        "sessions": [sessions[k] for k in sorted(sessions)]})
        random.SystemRandom().shuffle(out)  # join order could hint at who is who
        for i, item in enumerate(out, 1):
            item["pid"] = f"p{i:02d}"
        total = sum(len(s["samples"]) for item in out for s in item["sessions"])
        audit.enqueue(conn, "RESEARCH_EXPORTED", {"participants": len(out), "samples": total})
    return {
        "format": "sentinel-typing-study/1",
        "exportedOn": now().date().isoformat(),
        "protocol": {"samplesPerSession": SAMPLES_PER_SESSION, "minSessions": MIN_SESSIONS,
                     "sessionGapHours": settings.STUDY_SESSION_GAP_HOURS, "prompts": len(PROMPTS)},
        "participants": out,
    }


class PurgeIn(BaseModel):
    confirm: str


@router.post("/purge", dependencies=[Depends(require_operator)])
def purge(body: PurgeIn) -> dict:
    if body.confirm != CONFIRM_PURGE:
        raise HTTPException(400, f'Send {{"confirm": "{CONFIRM_PURGE}"}} to delete all study data')
    with db.connect() as conn, conn.transaction():
        n = conn.execute("SELECT count(*) AS n FROM research_participants").fetchone()["n"]
        conn.execute("DELETE FROM research_participants")
        conn.execute("DELETE FROM research_invites")
        audit.enqueue(conn, "RESEARCH_PURGED", {"participants": n})
    return {"purged": True, "participants": n}
