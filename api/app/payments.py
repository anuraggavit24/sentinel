"""Module 2a: score payments with explainable rules, plus the operator-only demo signal injector."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from . import audit, db, settings
from .auth_deps import current_session
from .risk import behaviour as bh
from .risk import fri
from .risk.normalize import PayeeError, normalise
from .risk.rules import ALLOW, BEHAVIOUR_MEDIUM, TRUST_AGE_HOURS, TRUST_MIN_PAISE, Signals, rupees, score

router = APIRouter()
IST = timezone(timedelta(hours=5, minutes=30))  # fixed offset: India has no daylight saving
MAX_AMOUNT_PAISE = 1_00_00_000_00  # ₹1 crore
# ASCII digits only. Python's \\d also matches Arabic-Indic and other digits, and Decimal accepts
# "Infinity", "NaN" and "1e5", so the pattern is checked before anything is converted.
AMOUNT_RE = re.compile(r"^[0-9]{1,9}(\.[0-9]{1,2})?$")
IDEM_RE = re.compile(r"^[A-Za-z0-9_-]{8,100}$")


def ist_hour() -> int:
    """Separate function so tests can pin the clock."""
    return datetime.now(IST).hour


# ---------------------------------------------------------------- operator-only demo signals

class DemoSignals(BaseModel):
    simSwapHoursAgo: Optional[float] = Field(default=None, ge=0, le=10_000)
    newDevice: bool = False
    integrity: Literal["PASS", "FAIL", "UNKNOWN"] = "PASS"
    activeCall: bool = False
    screenSharing: bool = False
    accessibilityServices: bool = False


def require_operator(x_demo_operator_token: Optional[str] = Header(default=None)) -> None:
    if not settings.DEMO_SIGNALS:
        raise HTTPException(404, "Demo signals are disabled")
    check_operator_token(x_demo_operator_token)


def check_operator_token(x_demo_operator_token: Optional[str]) -> None:
    """The operator token alone, for operator tools that do not depend on demo signals (the study)."""
    if not settings.DEMO_OPERATOR_TOKEN:
        raise HTTPException(503, "DEMO_OPERATOR_TOKEN is not configured")
    if not x_demo_operator_token or not hmac.compare_digest(
        x_demo_operator_token.encode(), settings.DEMO_OPERATOR_TOKEN.encode()
    ):
        raise HTTPException(401, "Operator token required")


def load_signals(conn, session_id) -> DemoSignals:
    row = conn.execute("SELECT signals FROM session_signals WHERE session_id = %s", (session_id,)).fetchone()
    return DemoSignals(**row["signals"]) if row else DemoSignals()


def _active_session(conn, session_id: uuid.UUID) -> dict:
    row = conn.execute(
        "SELECT id, user_id FROM sessions WHERE id = %s AND revoked_at IS NULL AND expires_at > now()", (session_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Session not found or no longer active")
    return row


@router.get("/demo/sessions/{session_id}/signals", dependencies=[Depends(require_operator)])
def get_signals(session_id: uuid.UUID) -> dict:
    with db.connect() as conn:
        _active_session(conn, session_id)
        return load_signals(conn, session_id).model_dump()


@router.put("/demo/sessions/{session_id}/signals", dependencies=[Depends(require_operator)])
def put_signals(session_id: uuid.UUID, body: DemoSignals) -> dict:
    with db.connect() as conn, conn.transaction():
        target = _active_session(conn, session_id)
        conn.execute(
            "INSERT INTO session_signals (session_id, signals) VALUES (%s, %s) "
            "ON CONFLICT (session_id) DO UPDATE SET signals = EXCLUDED.signals, updated_at = now()",
            (session_id, Jsonb(body.model_dump())),
        )
        # Injected signals are recorded, so a demo decision can never be passed off as a real one.
        audit.enqueue(
            conn,
            "DEMO_SIGNALS_SET",
            {"userId": str(target["user_id"]), "sessionId": str(session_id), "signals": body.model_dump()},
        )
    return body.model_dump()


@router.get("/demo/fri", dependencies=[Depends(require_operator)])
def demo_fri() -> dict:
    return {"mock": True, "numbers": fri.DEMO_FRI}


# ---------------------------------------------------------------- payments

class PaymentIn(BaseModel):
    payeeType: Literal["ACCOUNT", "UPI", "PHONE"]
    payee: dict[str, str]
    amount: str = Field(description="Rupees, ASCII digits, up to 2 decimal places, e.g. '2500' or '2,500.50'")
    # Timing statistics from the browser (slice 2b). Optional: older clients and API callers omit it.
    behaviour: Optional[dict] = None


def to_paise(amount: str) -> int:
    cleaned = amount.replace(",", "").strip()
    if not AMOUNT_RE.match(cleaned):
        raise HTTPException(422, "Amount must be a number like 2500 or 2500.50 (digits 0-9, up to 2 decimals)")
    paise = int(Decimal(cleaned) * 100)
    if paise <= 0:
        raise HTTPException(422, "Amount must be more than zero")
    if paise > MAX_AMOUNT_PAISE:
        raise HTTPException(422, f"Amount cannot exceed {rupees(MAX_AMOUNT_PAISE)}")
    return paise


def request_fingerprint(body: PaymentIn) -> str:
    return hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()


def present(stored: dict, replayed: bool) -> dict:
    """The stored response always holds the analyst view; strip it unless the analyst view is enabled."""
    out = {k: v for k, v in stored.items() if k != "analyst"}
    if settings.SHOW_ANALYST_VIEW:
        out["analyst"] = stored["analyst"]
    out["replayed"] = replayed
    return out


@router.post("/payments/score")
def score_payment(
    body: PaymentIn,
    session: dict = Depends(current_session),
    idempotency_key: Optional[str] = Header(default=None),
) -> dict:
    if not settings.PAYEE_HMAC_KEY:
        raise HTTPException(503, "Payments are unavailable: PAYEE_HMAC_KEY is not configured")
    if not idempotency_key or not IDEM_RE.match(idempotency_key):
        raise HTTPException(400, "Send an Idempotency-Key header (8-100 letters, digits, - or _), new for each payment")
    paise = to_paise(body.amount)
    try:
        payee = normalise(body.payeeType, body.payee)
    except PayeeError as exc:
        raise HTTPException(422, str(exc)) from exc
    payee_hash = payee.id_hash(settings.PAYEE_HMAC_KEY)
    phone = payee.phone or (fri.phone_from_upi(payee.normalised) if payee.type == "UPI" else None)
    fingerprint = request_fingerprint(body)
    user_id = session["user_id"]
    behaviour_note = None
    try:
        sample = bh.validate(body.behaviour)
        if sample is None:
            behaviour_note = (
                "no typing data sent" if not body.behaviour
                else f"fewer than {bh.MIN_KEYSTROKES} timed keystrokes (pasted or autofilled?)"
            )
    except bh.BehaviourInvalid as exc:
        sample, behaviour_note = None, f"rejected: {exc}"

    with db.connect() as conn, conn.transaction():
        # One scoring at a time per user: otherwise parallel requests each count the others as not yet
        # committed, and a burst slips past the velocity, payee-trust and running-total rules.
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('payments:' || %s::text, 0))", (str(user_id),))

        previous = conn.execute(
            "SELECT request_hash, response FROM payments WHERE user_id = %s AND idem_key = %s",
            (user_id, idempotency_key),
        ).fetchone()
        if previous is not None:
            if previous["request_hash"] != fingerprint:
                raise HTTPException(409, "This Idempotency-Key was already used for a different payment")
            return present(previous["response"], replayed=True)

        trust = conn.execute(
            "SELECT "
            "  bool_or(status = 'completed' AND trust_eligible AND created_at <= now() - make_interval(hours => %s)) AS trusted, "
            "  bool_or(status = 'completed' AND created_at > now() - interval '24 hours') AS paid_recently, "
            # Only money actually sent counts towards "sent" totals: a held payment moved nothing.
            "  COALESCE(sum(amount_paise) FILTER (WHERE status = 'completed' AND created_at > now() - interval '24 hours'), 0) AS total_24h "
            "FROM payments WHERE user_id = %s AND payee_hash = %s",
            (TRUST_AGE_HOURS, user_id, payee_hash),
        ).fetchone()
        untrusted_total = conn.execute(
            "SELECT COALESCE(sum(amount_paise), 0) AS t FROM payments "
            "WHERE user_id = %s AND NOT payee_trusted AND status = 'completed' AND created_at > now() - interval '24 hours'",
            (user_id,),
        ).fetchone()["t"]
        history = [
            r["amount_paise"]
            for r in conn.execute(
                "SELECT amount_paise FROM payments WHERE user_id = %s AND status = 'completed' "
                "ORDER BY created_at DESC LIMIT 50",
                (user_id,),
            ).fetchall()
        ]
        recent = conn.execute(
            "SELECT count(*) AS n FROM payments WHERE user_id = %s AND created_at > now() - interval '10 minutes'",
            (user_id,),
        ).fetchone()["n"]
        demo = load_signals(conn, session["session_id"]) if settings.DEMO_SIGNALS else DemoSignals()
        profile = [
            r["features"]
            for r in conn.execute(
                "SELECT features FROM behaviour_samples WHERE user_id = %s AND clean ORDER BY id DESC LIMIT %s",
                (user_id, bh.PROFILE_WINDOW),
            ).fetchall()
        ][::-1]
        behaviour = bh.evaluate(profile, sample)
        enrolled = len(profile) >= bh.ENROL_MIN

        trusted = bool(trust["trusted"])
        signals = Signals(
            amount_paise=paise,
            hour_ist=ist_hour(),
            payee_type=payee.type,
            payee_trusted=trusted,
            payee_paid_recently=bool(trust["paid_recently"]),
            payee_total_24h_paise=int(trust["total_24h"]),
            untrusted_total_24h_paise=int(untrusted_total),
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
            behaviour_anomaly=behaviour.anomaly,
            behaviour_features=[bh.FEATURES[f][3] for f in behaviour.top_features],
            behaviour_missing=enrolled and sample is None,
        )
        decision = score(signals)
        status = "completed" if decision.band == ALLOW else "held"  # Module 3 acts on held payments
        # Only a meaningful payment made without coaching signals can later make this payee trusted.
        trust_eligible = status == "completed" and not decision.coaching_signals and paise >= TRUST_MIN_PAISE
        snapshot = {
            "payeeTrusted": trusted,
            "payeePaidRecently": signals.payee_paid_recently,
            "payeeTotal24h": rupees(signals.payee_total_24h_paise) if signals.payee_total_24h_paise else None,
            "untrustedTotal24h": rupees(signals.untrusted_total_24h_paise) if signals.untrusted_total_24h_paise else None,
            "historyCount": len(history),
            "paymentsLast10Min": recent,
            "friCategory": signals.fri_category,
            "hourIst": signals.hour_ist,
            "syncedPasskey": signals.synced_passkey,
            "injected": demo.model_dump() if settings.DEMO_SIGNALS else None,
            "operatorSignalsOn": settings.DEMO_SIGNALS and demo != DemoSignals(),
            "consortium": "not connected yet (slice 2c)",
            "behaviour": {**behaviour.as_dict(), "enrolMin": bh.ENROL_MIN, "note": behaviour_note},
        }
        # Why this payment did or did not teach the profile, so a stuck "learning" status is explainable.
        why = []
        if sample is None:
            why.append("no usable typing data")
        if behaviour.anomaly is not None and behaviour.anomaly >= BEHAVIOUR_MEDIUM:
            # Never learn from typing that already looks like someone else: otherwise an attacker whose
            # payments slip through would slowly teach the profile their own rhythm.
            why.append("typing did not match the profile")
        if status != "completed":
            why.append("payment held for checks")
        if decision.coaching_signals:
            why.append("coaching signals were on")
        if decision.takeover_signals:
            why.append("takeover signals were on")
        not_learned = "; ".join(why) or None
        clean = not_learned is None
        snapshot["behaviour"].update({"captured": sample is not None, "learned": not_learned is None,
                                      "notLearnedBecause": not_learned})
        payment_id = uuid.uuid4()
        stored = {
            "paymentId": str(payment_id),
            "status": status,
            "payee": payee.display,
            "amount": rupees(paise),
            "band": decision.band,
            "reasons": decision.customer_reasons(),
            "engine": "rules-v2 + behaviour robust-z",
            "analyst": {
                "score": decision.score,
                "reasons": [r.as_dict() for r in decision.reasons],
                "takeoverSignals": decision.takeover_signals,
                "coachingSignals": decision.coaching_signals,
                "signals": snapshot,
            },
        }
        conn.execute(
            "INSERT INTO payments (id, user_id, session_id, payee_type, payee_hash, payee_display, amount_paise, "
            "score, band, status, reasons, signals, idem_key, request_hash, response, payee_trusted, coaching, "
            "trust_eligible) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                payment_id, user_id, session["session_id"], payee.type, payee_hash, payee.display, paise,
                decision.score, decision.band, status, Jsonb([r.as_dict() for r in decision.reasons]), Jsonb(snapshot),
                idempotency_key, fingerprint, Jsonb(stored), trusted, decision.coaching_signals, trust_eligible,
            ),
        )
        if sample is not None:
            # Only clean payments teach the profile what "normal" looks like for this user.
            conn.execute(
                "INSERT INTO behaviour_samples (user_id, payment_id, features, clean) VALUES (%s, %s, %s, %s)",
                (user_id, payment_id, Jsonb(sample), clean),
            )
        audit.enqueue(
            conn,
            "RISK_DECISION",
            {
                "paymentId": str(payment_id),
                "userId": str(user_id),
                "score": decision.score,
                "band": decision.band,
                "reasonCodes": [r.code for r in decision.reasons],
                "payeeType": payee.type,
                "amountPaise": paise,
            },
        )

    return present(stored, replayed=False)


@router.get("/payments")
def list_payments(session: dict = Depends(current_session)) -> dict:
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, payee_type, payee_display, amount_paise, score, band, status, created_at "
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
                "band": r["band"],
                "status": r["status"],
                **({"score": r["score"]} if settings.SHOW_ANALYST_VIEW else {}),
                "createdAt": r["created_at"].isoformat(),
            }
            for r in rows
        ]
    }
