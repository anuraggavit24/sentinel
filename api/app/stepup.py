"""Module 3b: the customer's own step-up on a held payment.

The customer can confirm a held (step_up) payment themselves with a fresh passkey check and three
questions, or cancel it. What the answers can and cannot do:
- Answers can only make things STRICTER. "Someone on the phone asked me" or "I was promised a refund /
  prize / job, or told my account is at risk" keeps the payment held, flags it to the top of the
  analyst queue, and shows a scam warning.
- Answers can never override what was observed. Coaching signals during the payment (a call, screen
  sharing), or a payee CONFIRMED on the consortium ledger, mean only an analyst can release it: a
  coached victim is told what to answer.
- One attempt. A second try would let a scammer on the line coach a different answer.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from . import audit, db, reauth
from .auth_deps import current_session, now

router = APIRouter()


class Answers(BaseModel):
    onCall: bool          # Is someone on the phone or a video call asking you to make this payment?
    promised: bool        # Were you promised a refund, prize, job or investment return, or told your account is at risk?
    knowsPayee: bool      # Do you know this payee personally, or have you dealt with them before?


class StepUpIn(BaseModel):
    challengeId: uuid.UUID
    credential: dict
    answers: Answers


WARNING = ("Stop. Banks, police and companies never ask you to move money while on a call, and nobody "
           "pays a refund or prize by asking you to send money first. We have held this payment.")


def _own_held(conn, payment_id: uuid.UUID, session: dict, lock: bool = False) -> dict:
    p = conn.execute(
        "SELECT * FROM payments WHERE id = %s AND user_id = %s" + (" FOR UPDATE" if lock else ""),
        (payment_id, session["user_id"]),
    ).fetchone()
    if p is None:
        raise HTTPException(404, "No such payment")
    if session["sandboxed_at"] is not None:
        raise HTTPException(404, "No such payment")  # a sandboxed session gets nothing real to act on
    if p["status"] != "held" or p["band"] != "step_up":
        raise HTTPException(409, f"This payment is {p['status']}, not waiting for your confirmation")
    return p


@router.post("/payments/{payment_id}/step-up/options")
def stepup_options(payment_id: uuid.UUID, session: dict = Depends(current_session)) -> dict:
    with db.connect() as conn:
        p = _own_held(conn, payment_id, session)
        if p["step_up"] is not None:
            raise HTTPException(409, "You have already answered for this payment; our team will review it")
        return reauth.options(conn, session)


@router.post("/payments/{payment_id}/step-up")
def stepup(payment_id: uuid.UUID, body: StepUpIn, session: dict = Depends(current_session)) -> dict:
    a = body.answers
    with db.connect() as conn, conn.transaction():
        p = _own_held(conn, payment_id, session, lock=True)
        if p["step_up"] is not None:
            raise HTTPException(409, "You have already answered for this payment; our team will review it")
        reauth.verify(conn, session, body.challengeId, body.credential, during="payment-step-up")

        scam = a.onCall or a.promised
        blockers = []
        if p["coaching"]:
            blockers.append("coaching signals were seen during the payment")
        if ((p["signals"] or {}).get("consortium") or {}).get("hardRule"):
            blockers.append("the payee is a confirmed mule on the consortium ledger")
        if scam:
            blockers.append("the answers match a known scam pattern")
        released = not blockers
        record = {"answers": a.model_dump(), "released": released, "blockers": blockers, "at": now().isoformat()}
        if released:
            conn.execute(
                "UPDATE payments SET status = 'completed', review_decision = 'customer_confirmed', reviewed_at = now(), "
                "step_up = %s, response = jsonb_set(response, '{status}', '\"completed\"') WHERE id = %s",
                (Jsonb(record), payment_id),
            )
        else:
            conn.execute("UPDATE payments SET step_up = %s, step_up_flagged = %s WHERE id = %s",
                         (Jsonb(record), scam, payment_id))
        audit.enqueue(conn, "CUSTOMER_STEP_UP", {"paymentId": str(payment_id), "userId": str(session["user_id"]),
                                                 "released": released, "scamPattern": scam, "blockers": blockers})
    if released:
        return {"status": "completed", "message": "Confirmed. The payment has been sent."}
    return {
        "status": "held",
        "warning": WARNING if scam else None,
        "message": "This payment needs a check by our fraud team. You do not need to do anything else; "
                   "we will not call you to ask for codes or passwords.",
    }


@router.post("/payments/{payment_id}/cancel")
def cancel(payment_id: uuid.UUID, session: dict = Depends(current_session)) -> dict:
    with db.connect() as conn, conn.transaction():
        p = conn.execute("SELECT status FROM payments WHERE id = %s AND user_id = %s FOR UPDATE",
                         (payment_id, session["user_id"])).fetchone()
        if p is None or session["sandboxed_at"] is not None:
            raise HTTPException(404, "No such payment")
        if p["status"] != "held":
            raise HTTPException(409, f"This payment is {p['status']} and cannot be cancelled")
        conn.execute(
            "UPDATE payments SET status = 'rejected', review_decision = 'customer_cancelled', reviewed_at = now(), "
            "response = jsonb_set(response, '{status}', '\"rejected\"') WHERE id = %s",
            (payment_id,),
        )
        audit.enqueue(conn, "CUSTOMER_CANCELLED", {"paymentId": str(payment_id), "userId": str(session["user_id"])})
    return {"status": "rejected", "message": "Cancelled. No money was sent."}
