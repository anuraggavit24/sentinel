"""Slice 2b: guided behaviour enrolment.

Why a passkey check first: with an empty profile, anyone holding a stolen session cookie could enrol
THEIR OWN typing rhythm as the victim's "normal". Enrolment therefore opens only after a fresh passkey
assertion by the account owner, for the session that made it, for 10 minutes, for 5 samples.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from . import audit, db, reauth
from .auth_deps import current_session, now
from .risk import behaviour as bh

router = APIRouter()
WINDOW_MINUTES = 10


class StartIn(BaseModel):
    challengeId: uuid.UUID
    credential: dict[str, Any]
    reset: bool = False


class SampleIn(BaseModel):
    behaviour: Optional[dict] = None


def _clean_count(conn, user_id) -> int:
    return conn.execute(
        "SELECT count(*) AS n FROM behaviour_samples WHERE user_id = %s AND clean", (user_id,)
    ).fetchone()["n"]


def _window(conn, user_id, session_id, lock: bool = False) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM behaviour_enrolment WHERE user_id = %s" + (" FOR UPDATE" if lock else ""), (user_id,)
    ).fetchone()
    if row is None or row["expires_at"] <= now() or row["session_id"] != session_id:
        return None
    return row


@router.get("/behaviour/profile")
def profile(session: dict = Depends(current_session)) -> dict:
    with db.connect() as conn:
        n = _clean_count(conn, session["user_id"])
        w = _window(conn, session["user_id"], session["session_id"])
    return {
        "samples": n,
        "enrolMin": bh.ENROL_MIN,
        "enrolled": n >= bh.ENROL_MIN,
        "enrolment": None if w is None else {"added": w["added"], "needed": bh.ENROL_MIN, "expiresAt": w["expires_at"].isoformat()},
    }


@router.post("/behaviour/enrol/options")
def enrol_options(session: dict = Depends(current_session)) -> dict:
    with db.connect() as conn:
        return reauth.options(conn, session)


@router.post("/behaviour/enrol/start")
def enrol_start(body: StartIn, session: dict = Depends(current_session)) -> dict:
    with db.connect() as conn, conn.transaction():
        reauth.verify(conn, session, body.challengeId, body.credential, during="enrolment")

        existing = _clean_count(conn, session["user_id"])
        if existing >= bh.ENROL_MIN and not body.reset:
            raise HTTPException(409, "You already have a typing profile. Choose 'reset' to build a new one.")
        if body.reset:
            conn.execute("DELETE FROM behaviour_samples WHERE user_id = %s", (session["user_id"],))
        conn.execute(
            "INSERT INTO behaviour_enrolment (user_id, session_id, added, expires_at) VALUES (%s, %s, 0, %s) "
            "ON CONFLICT (user_id) DO UPDATE SET session_id = EXCLUDED.session_id, added = 0, "
            "opened_at = now(), expires_at = EXCLUDED.expires_at",
            (session["user_id"], session["session_id"], now() + timedelta(minutes=WINDOW_MINUTES)),
        )
        audit.enqueue(conn, "BEHAVIOUR_ENROLMENT_STARTED", {"userId": str(session["user_id"]), "reset": body.reset})
    return {"open": True, "needed": bh.ENROL_MIN, "minutes": WINDOW_MINUTES}


@router.post("/behaviour/enrol/sample")
def enrol_sample(body: SampleIn, session: dict = Depends(current_session)) -> dict:
    try:
        sample = bh.validate(body.behaviour)
    except bh.BehaviourInvalid as exc:
        raise HTTPException(422, f"Typing data rejected: {exc}") from exc
    if sample is None:
        raise HTTPException(422, f"Type the sample by hand: at least {bh.MIN_KEYSTROKES} keystrokes, no pasting")
    with db.connect() as conn, conn.transaction():
        w = _window(conn, session["user_id"], session["session_id"], lock=True)
        if w is None:
            raise HTTPException(403, "Enrolment is not open for this session. Confirm with your passkey first.")
        conn.execute(
            "INSERT INTO behaviour_samples (user_id, payment_id, features, clean, source) VALUES (%s, NULL, %s, true, 'enrolment')",
            (session["user_id"], Jsonb(sample)),
        )
        added = w["added"] + 1
        if added >= bh.ENROL_MIN:
            conn.execute("DELETE FROM behaviour_enrolment WHERE user_id = %s", (session["user_id"],))
            audit.enqueue(conn, "BEHAVIOUR_ENROLLED", {"userId": str(session["user_id"]), "samples": added})
        else:
            conn.execute("UPDATE behaviour_enrolment SET added = %s WHERE user_id = %s", (added, session["user_id"]))
    return {"added": added, "needed": bh.ENROL_MIN, "done": added >= bh.ENROL_MIN}
