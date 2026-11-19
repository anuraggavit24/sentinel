"""The blockchain, made visible (for the "Ledger" page and the class demo).

- /ledger/chain     latest blocks from the real Fabric channel, with hash links and endorsers
- /ledger/tx/{id}   the block that holds a transaction (a ledger report id IS a Fabric transaction id)
- /ledger/lookup    analysts: walk one payee through what a payment does (normalise, keyed hash, ask the peer,
                    weight, points). Analyst-only, because answering "is this payee reported?" for anyone
                    would turn the bank into a lookup service for mule operators.

Customers see blocks and transactions without their arguments; analysts also see the arguments, which hold
only keyed hashes and report ids, never an account number.
"""
from __future__ import annotations

import re
from typing import Literal, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import db, ledger, settings
from .auth_deps import current_session
from .ops import analyst
from .risk.normalize import PayeeError, normalise
from .risk.rules import CONSORTIUM_CONFIRMED_POINTS, CONSORTIUM_WEIGHT_POINTS

router = APIRouter(prefix="/ledger")
TX = re.compile(r"^[0-9a-f]{64}$")


def _get(path: str) -> dict:
    if not settings.LEDGER_URL:
        raise HTTPException(503, "Ledger not connected (LEDGER_URL is not set)")
    try:
        with ledger._client() as c:
            c.timeout = httpx.Timeout(8.0, connect=2.0)  # several blocks, not one lookup
            r = c.get(path)
    except httpx.HTTPError as exc:
        raise HTTPException(503, f"Ledger unreachable ({type(exc).__name__})") from exc
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code == 422:
        raise HTTPException(404, "Not on the ledger")
    if r.status_code != 200:
        raise HTTPException(503, f"Ledger answered {r.status_code}: {data.get('error', '')}"[:200])
    return data


def _ours(tx_ids: list[str]) -> dict[str, dict]:
    """Which of these transactions were filed by our own analysts, for which payment."""
    if not tx_ids:
        return {}
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT report_id, status, source, payment_id FROM ledger_outbox WHERE report_id = ANY(%s)", (tx_ids,)
        ).fetchall()
    return {r["report_id"]: {"filing": r["status"], "source": r["source"]} for r in rows}


def _shape(block: dict, show_args: bool) -> dict:
    for t in block.get("txs", []):
        if not show_args:
            t["args"] = []
    return block


@router.get("/chain")
def chain(count: int = 8, session: dict = Depends(current_session)) -> dict:
    count = max(1, min(count, 12))
    data = _get(f"/chain/blocks?count={count}")
    show = session["role"] == "analyst"
    blocks = [_shape(b, show) for b in data.get("blocks", [])]
    # Hash links checked here too: block n+1 must point at block n's recomputed hash.
    for newer, older in zip(blocks, blocks[1:]):
        newer["linkVerified"] = newer["previousHash"] == older["hash"]
    if blocks:
        blocks[-1]["linkVerified"] = None  # its predecessor is not in this window
    ours = _ours([t["txId"] for b in blocks for t in b["txs"] if t.get("txId")])
    for b in blocks:
        for t in b["txs"]:
            if t.get("txId") in ours:
                t["ours"] = ours[t["txId"]]
    return {"info": data.get("info"), "blocks": blocks, "latencyMs": data.get("latencyMs"),
            "network": {"channel": "fraud-intel", "chaincode": "fraudintel", "endorsement": "OutOf(2, BankA, BankB, BankC)",
                        "orgs": [{"msp": "BankAMSP", "name": "Bank A", "role": "endorser", "us": settings.LEDGER_MSP == "BankAMSP"},
                                 {"msp": "BankBMSP", "name": "Bank B", "role": "endorser", "us": settings.LEDGER_MSP == "BankBMSP"},
                                 {"msp": "BankCMSP", "name": "Bank C", "role": "endorser", "us": settings.LEDGER_MSP == "BankCMSP"},
                                 {"msp": "RegulatorMSP", "name": "Regulator", "role": "reads, resolves disputes", "us": False}]}}


@router.get("/tx/{tx_id}")
def tx(tx_id: str, session: dict = Depends(current_session)) -> dict:
    if not TX.match(tx_id):
        raise HTTPException(400, "A transaction id is 64 lowercase hex characters")
    block = _shape(_get(f"/chain/tx/{tx_id}")["block"], session["role"] == "analyst")
    return {"block": block, "tx": next((t for t in block["txs"] if t["txId"] == tx_id), None)}


class LookupIn(BaseModel):
    payeeType: Literal["ACCOUNT", "UPI", "PHONE"]
    payee: dict[str, str]


@router.post("/lookup")
def lookup(body: LookupIn, session: dict = Depends(analyst)) -> dict:
    try:
        p = normalise(body.payeeType, body.payee)
    except PayeeError as exc:
        raise HTTPException(422, str(exc)) from exc
    h = p.id_hash(settings.PAYEE_HMAC_KEY)
    result = ledger.query_payee([h])
    snap = result.snapshot()
    if result.hard_rule:
        points: Optional[int] = CONSORTIUM_CONFIRMED_POINTS
        effect = f"+{CONSORTIUM_CONFIRMED_POINTS} points and the payment is always held"
    elif result.status in ("clear", "hit"):
        points = round(result.max_weight * CONSORTIUM_WEIGHT_POINTS)
        effect = f"+{points} points" if points else "no points"
    else:
        points, effect = None, "ledger not available: scored without it"
    return {
        "steps": [
            {"step": "Normalise", "detail": f"{p.type}: {p.display} (masked; the full form never leaves the bank)"},
            {"step": "Keyed hash", "detail": "HMAC-SHA256 with the consortium key: the ledger only ever sees this",
             "value": h},
            {"step": "Ask the ledger", "detail": f"queryPayee on {settings.LEDGER_MSP}'s peer",
             "value": f"{snap.get('latencyMs')} ms" if snap.get("latencyMs") is not None else snap.get("error", snap["status"])},
            {"step": "Weigh", "detail": "the chaincode's weight for the strongest report",
             "value": str(snap.get("maxWeight", "–"))},
            {"step": "Effect on a payment", "detail": effect},
        ],
        "ledger": snap,
        "points": points,
    }
