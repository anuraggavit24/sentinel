"""Module 2a: score payments with explainable rules, plus the demo signal injector."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from . import audit, db, settings
from .auth_deps import current_session
from .risk import fri
from .risk.normalize import PayeeError, normalise
from .risk.rules import ALLOW, Signals, rupees, score

router = APIRouter()
IST = timezone(timedelta(hours=5, minutes=30))  # fixed offset: India has no daylight saving
MAX_AMOUNT_PAISE = 1_00_00_000_00  # ₹1 crore


def ist_hour() -> int:
    """Separate function so tests can pin the clock."""
    return datetime.now(IST).hour


# ---------------------------------------------------------------- demo signals

class DemoSignals(BaseModel):
    simSwapHoursAgo: Optional[float] = Field(default=None, ge=0, le=10_000)
    newDevice: bool = False
    integrity: Literal["PASS", "FAIL", "UNKNOWN"] = "PASS"
    activeCall: bool = False
    screenSharing: bool = False
    accessibilityServices: bool = False


def require_demo() -> None:
    if not settings.DEMO_SIGNALS:
        raise HTTPException(404, "Demo signals are disabled")


def load_signals(conn, session_id) -> DemoSignals:
    row = conn.execute("SELECT signals FROM session_signals WHERE session_id = %s", (session_id,)).fetchone()
    return DemoSignals(**row["signals"]) if row else DemoSignals()


@router.get("/demo/signals", dependencies=[Depends(require_demo)])
def get_signals(session: dict = Depends(current_session)) -> dict:
    with db.connect() as conn:
        return load_signals(conn, session["session_id"]).model_dump()


@router.put("/demo/signals", dependencies=[Depends(require_demo)])
def put_signals(body: DemoSignals, session: dict = Depends(current_session)) -> dict:
    with db.connect() as conn, conn.transaction():
        conn.execute(
            "INSERT INTO session_signals (session_id, signals) VALUES (%s, %s) "
            "ON CONFLICT (session_id) DO UPDATE SET signals = EXCLUDED.signals, updated_at = now()",
            (session["session_id"], Jsonb(body.model_dump())),
        )
        # Injected signals are recorded, so a demo decision can never be passed off as a real one.
        audit.enqueue(conn, "DEMO_SIGNALS_SET", {"userId": str(session["user_id"]), "signals": body.model_dump()})
    return body.model_dump()


@router.get("/demo/fri", dependencies=[Depends(require_demo)])
def demo_fri() -> dict:
    return {"mock": True, "numbers": fri.DEMO_FRI}


# ---------------------------------------------------------------- payments

class PaymentIn(BaseModel):
    payeeType: Literal["ACCOUNT", "UPI", "PHONE"]
    payee: dict[str, str]
    amount: str = Field(description="Rupees, up to 2 decimal places, e.g. '2500' or '2500.50'")


def to_paise(amount: str) -> int:
    try:
        value = Decimal(amount.replace(",", "").strip())
    except (InvalidOperation, AttributeError) as exc:
        raise HTTPException(422, "Amount must be a number") from exc
    if value != value.quantize(Decimal("0.01")):
        raise HTTPException(422, "Amount can have at most 2 decimal places")
    paise = int(value * 100)
    if paise <= 0:
        raise HTTPException(422, "Amount must be more than zero")
    if paise > MAX_AMOUNT_PAISE:
        raise HTTPException(422, f"Amount cannot exceed {rupees(MAX_AMOUNT_PAISE)}")
    return paise


@router.post("/payments/score")
def score_payment(body: PaymentIn, session: dict = Depends(current_session)) -> dict:
    paise = to_paise(body.amount)
    try:
        payee = normalise(body.payeeType, body.payee)
    except PayeeError as exc:
        raise HTTPException(422, str(exc)) from exc
    payee_hash = payee.id_hash(settings.PAYEE_HMAC_KEY)
    phone = payee.phone or (fri.phone_from_upi(payee.normalised) if payee.type == "UPI" else None)

    with db.connect() as conn, conn.transaction():
        seen = conn.execute(
            "SELECT 1 FROM payments WHERE user_id = %s AND payee_hash = %s AND status = 'completed' LIMIT 1",
            (session["user_id"], payee_hash),
        ).fetchone() is not None
        history = [
            r["amount_paise"]
            for r in conn.execute(
                "SELECT amount_paise FROM payments WHERE user_id = %s AND status = 'completed' "
                "ORDER BY created_at DESC LIMIT 50",
                (session["user_id"],),
            ).fetchall()
        ]
        recent = conn.execute(
            "SELECT count(*) AS n FROM payments WHERE user_id = %s AND created_at > now() - interval '10 minutes'",
            (session["user_id"],),
        ).fetchone()["n"]
        demo = load_signals(conn, session["session_id"]) if settings.DEMO_SIGNALS else DemoSignals()

        signals = Signals(
            amount_paise=paise,
            hour_ist=ist_hour(),
            payee_type=payee.type,
            payee_seen_before=seen,
            history_paise=history,
            payments_last_10_min=recent,
            fri_category=fri.lookup(phone),
            sim_swap_hours_ago=demo.simSwapHoursAgo,
            new_device=demo.newDevice,
            integrity=demo.integrity,
            active_call=demo.activeCall,
            screen_sharing=demo.screenSharing,
            accessibility_services=demo.accessibilityServices,
            synced_passkey=bool(session["synced_passkey"]),
        )
        decision = score(signals)
        status = "completed" if decision.band == ALLOW else "held"  # Module 3 acts on held payments
        snapshot = {
            "payeeSeenBefore": seen,
            "historyCount": len(history),
            "paymentsLast10Min": recent,
            "friCategory": signals.fri_category,
            "hourIst": signals.hour_ist,
            "syncedPasskey": signals.synced_passkey,
            "injected": demo.model_dump() if settings.DEMO_SIGNALS else None,
            "consortium": "not connected yet (slice 2c)",
        }
        row = conn.execute(
            "INSERT INTO payments (user_id, session_id, payee_type, payee_hash, payee_display, amount_paise, "
            "score, band, status, reasons, signals) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "RETURNING id, created_at",
            (
                session["user_id"], session["session_id"], payee.type, payee_hash, payee.display, paise,
                decision.score, decision.band, status, Jsonb([r.as_dict() for r in decision.reasons]), Jsonb(snapshot),
            ),
        ).fetchone()
        audit.enqueue(
            conn,
            "RISK_DECISION",
            {
                "paymentId": str(row["id"]),
                "userId": str(session["user_id"]),
                "score": decision.score,
                "band": decision.band,
                "reasonCodes": [r.code for r in decision.reasons],
                "payeeType": payee.type,
                "amountPaise": paise,
            },
        )

    return {
        "paymentId": str(row["id"]),
        "status": status,
        "payee": payee.display,
        "amount": rupees(paise),
        **decision.as_dict(),
        "engine": "rules-v1",
        "signals": snapshot,
    }


@router.get("/payments")
def list_payments(session: dict = Depends(current_session)) -> dict:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, payee_type, payee_display, amount_paise, score, band, status, reasons, created_at "
            "FROM payments WHERE user_id = %s ORDER BY created_at DESC LIMIT 20",
            (session["user_id"],),
        ).fetchall()
    return {
        "payments": [
            {
                "id": str(r["id"]),
                "payeeType": r["payee_type"],
                "payee": r["payee_display"],
                "amount": rupees(r["amount_paise"]),
                "score": r["score"],
                "band": r["band"],
                "status": r["status"],
                "reasonCodes": [x["code"] for x in r["reasons"]],
                "createdAt": r["created_at"].isoformat(),
            }
            for r in rows
        ]
    }
