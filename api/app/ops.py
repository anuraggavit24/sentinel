"""Module 3a: fraud operations. Analysts review held payments, release or reject them, and file what
they find to the consortium ledger.

Rules
- Analysts are passkey users with role 'analyst', granted by the operator (operator token).
- Viewing the queue needs the role. Deciding needs the role AND a fresh passkey check on this session
  in the last OPS_ELEVATION_MINUTES ("elevation"), so a stolen analyst cookie cannot release money.
- Nobody reviews their own payment.
- A decision is final: the payment row is locked, and a second decision gets 409.
- Reporting to the ledger is only possible together with a rejection, and goes through the outbox.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from . import audit, db, ledger_outbox, reauth, settings
from .auth_deps import current_session, now
from .payments import check_operator_token
from .risk.rules import rupees

router = APIRouter(prefix="/ops")


def analyst(session: dict = Depends(current_session)) -> dict:
    if session["role"] != "analyst":
        raise HTTPException(403, "Fraud operations are for analysts only")
    return session


def elevated(session: dict = Depends(analyst)) -> dict:
    until = session["ops_elevated_until"]
    if until is None or until <= now():
        raise HTTPException(403, "Confirm with your passkey first (analyst actions need a fresh check)")
    return session


def _mask(name: str) -> str:
    return (name[:1] + "•••") if name else "•••"


# ---------------------------------------------------------------- operator: who is an analyst
class AnalystIn(BaseModel):
    username: str
    analyst: bool = True


@router.post("/analysts")
def set_analyst(body: AnalystIn, x_demo_operator_token: Optional[str] = Header(default=None)) -> dict:
    check_operator_token(x_demo_operator_token)
    role = "analyst" if body.analyst else "customer"
    with db.connect() as conn, conn.transaction():
        row = conn.execute("UPDATE users SET role = %s WHERE username = %s RETURNING id", (role, body.username)).fetchone()
        if row is None:
            raise HTTPException(404, "No such user")
        # Losing the role also ends any elevation, everywhere.
        conn.execute("UPDATE sessions SET ops_elevated_until = NULL WHERE user_id = %s", (row["id"],))
        audit.enqueue(conn, "ANALYST_ROLE_CHANGED", {"userId": str(row["id"]), "role": role})
    return {"username": body.username, "role": role}


# ---------------------------------------------------------------- elevation
class ElevateIn(BaseModel):
    challengeId: uuid.UUID
    credential: dict


@router.post("/elevate/options")
def elevate_options(session: dict = Depends(analyst)) -> dict:
    with db.connect() as conn:
        return reauth.options(conn, session)


@router.post("/elevate")
def elevate(body: ElevateIn, session: dict = Depends(analyst)) -> dict:
    until = now() + timedelta(minutes=settings.OPS_ELEVATION_MINUTES)
    with db.connect() as conn, conn.transaction():
        reauth.verify(conn, session, body.challengeId, body.credential, during="ops-elevation")
        conn.execute("UPDATE sessions SET ops_elevated_until = %s WHERE id = %s", (until, session["session_id"]))
        audit.enqueue(conn, "OPS_ELEVATED", {"userId": str(session["user_id"]), "sessionId": str(session["session_id"]),
                                             "minutes": settings.OPS_ELEVATION_MINUTES})
    return {"elevatedUntil": until.isoformat(), "minutes": settings.OPS_ELEVATION_MINUTES}


@router.get("/status")
def ops_status(session: dict = Depends(analyst)) -> dict:
    until = session["ops_elevated_until"]
    return {
        "elevatedUntil": until.isoformat() if until and until > now() else None,
        "ledgerWrites": bool(settings.LEDGER_URL and settings.LEDGER_WRITE_TOKEN),
        "bank": settings.LEDGER_MSP,
    }


# ---------------------------------------------------------------- queue
@router.get("/queue")
def queue(session: dict = Depends(analyst)) -> dict:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT p.id, p.created_at, p.amount_paise, p.payee_type, p.payee_display, p.payee_hash, p.score, p.band, "
            "       p.reasons, p.signals, p.coaching, u.display_name, "
            # The same payee held for other customers recently is itself a mule signal.
            "       (SELECT count(DISTINCT q.user_id) FROM payments q WHERE q.payee_hash = p.payee_hash "
            "          AND q.user_id <> p.user_id AND q.created_at > now() - interval '7 days') AS other_customers "
            "FROM payments p JOIN users u ON u.id = p.user_id "
            "WHERE p.status = 'held' AND p.user_id <> %s ORDER BY p.created_at LIMIT 100",
            (session["user_id"],),
        ).fetchall()
    return {"payments": [
        {
            "id": str(r["id"]),
            "createdAt": r["created_at"].isoformat(),
            "amount": rupees(r["amount_paise"]),
            "payeeType": r["payee_type"],
            "payee": r["payee_display"],
            "payeeRef": r["payee_hash"][:12],  # lets analysts spot the same payee without the identifier
            "customer": _mask(r["display_name"]),
            "score": r["score"],
            "band": r["band"],
            "reasons": r["reasons"],
            "coaching": r["coaching"],
            "consortium": (r["signals"] or {}).get("consortium"),
            "otherCustomers7d": r["other_customers"],
        }
        for r in rows
    ]}


# ---------------------------------------------------------------- decisions
class DecisionIn(BaseModel):
    decision: Literal["release", "reject"]
    note: str = Field(default="", max_length=500)
    report: bool = False
    severity: Literal["LOW", "MEDIUM", "HIGH"] = "MEDIUM"


@router.post("/payments/{payment_id}/decision")
def decide(payment_id: uuid.UUID, body: DecisionIn, session: dict = Depends(elevated)) -> dict:
    if body.report and body.decision != "reject":
        raise HTTPException(400, "Only a rejected payment can be reported to the consortium")
    if body.report and not (settings.LEDGER_URL and settings.LEDGER_WRITE_TOKEN):
        raise HTTPException(503, "Ledger writes are not configured on this server")
    decided_at = now()
    with db.connect() as conn, conn.transaction():
        p = conn.execute("SELECT * FROM payments WHERE id = %s FOR UPDATE", (payment_id,)).fetchone()
        if p is None:
            raise HTTPException(404, "No such payment")
        if p["user_id"] == session["user_id"]:
            raise HTTPException(403, "You cannot review your own payment")
        if p["status"] != "held":
            raise HTTPException(409, f"Already decided: this payment is {p['status']}")
        new_status = "completed" if body.decision == "release" else "rejected"
        review = {"decision": body.decision, "at": decided_at.isoformat()}
        conn.execute(
            "UPDATE payments SET status = %s, reviewed_by = %s, reviewed_at = %s, review_decision = %s, review_note = %s, "
            # Keep the stored response in step, so an idempotent replay shows the outcome, not "held".
            "response = jsonb_set(jsonb_set(response, '{status}', to_jsonb(%s::text)), '{review}', %s) "
            "WHERE id = %s",
            (new_status, session["user_id"], decided_at, body.decision, body.note or None, new_status, Jsonb(review),
             payment_id),
        )
        outbox_id = None
        if body.report:
            evidence = {
                "paymentId": str(payment_id),
                "decision": body.decision,
                "note": body.note,
                "reasonCodes": [r["code"] for r in p["reasons"]],
                "score": p["score"],
                "analystId": str(session["user_id"]),
                "decidedAt": decided_at.isoformat(),
                "payeeType": p["payee_type"],
                "bank": settings.LEDGER_MSP,
            }
            outbox_id = ledger_outbox.enqueue(
                conn, payment_id=payment_id, id_hash=p["payee_hash"], id_type=p["payee_type"], severity=body.severity,
                source="ANALYST", evidence=evidence, requested_by=session["user_id"],
            )
        audit.enqueue(conn, "ANALYST_DECISION", {
            "paymentId": str(payment_id), "analystId": str(session["user_id"]), "decision": body.decision,
            "reported": body.report, "outboxId": outbox_id,
        })
    return {"paymentId": str(payment_id), "status": new_status, "reported": body.report, "outboxId": outbox_id}


# ---------------------------------------------------------------- ledger filings
@router.get("/filings")
def filings(session: dict = Depends(analyst)) -> dict:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT o.id, o.status, o.attempts, o.last_error, o.report_id, o.severity, o.created_at, o.done_at, "
            "       o.evidence_hash, o.next_attempt_at, p.payee_display, p.amount_paise "
            "FROM ledger_outbox o LEFT JOIN payments p ON p.id = o.payment_id ORDER BY o.id DESC LIMIT 50"
        ).fetchall()
    return {"filings": [
        {
            "id": r["id"],
            "status": r["status"],
            "attempts": r["attempts"],
            "lastError": r["last_error"],
            "reportId": r["report_id"],
            "severity": r["severity"],
            "payee": r["payee_display"],
            "amount": rupees(r["amount_paise"]) if r["amount_paise"] else None,
            "evidenceHash": r["evidence_hash"],
            "createdAt": r["created_at"].isoformat(),
            "doneAt": r["done_at"].isoformat() if r["done_at"] else None,
            "nextAttemptAt": r["next_attempt_at"].isoformat() if r["status"] == "pending" else None,
        }
        for r in rows
    ]}
