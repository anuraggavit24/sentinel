"""Demo bank network (the class demo): three banks, one-click logins, real Fabric underneath.

Story it supports: customers of different banks pay the same scammer. Each victim reports it to their own
bank, which files on the shared ledger *as that bank* (its own key, its own peer). One bank's report makes
the payee PENDING; a second bank's makes it CONFIRMED; after that every bank blocks payments to it.

Every allowed payment is also recorded on the ledger: the details in a private data collection that only
the two banks in the payment hold, and a SHA-256 of them on the shared channel for everyone.

Money moves in this database (off-chain), as it would in each bank's core system. The ledger holds
fraud intelligence and tamper-evident payment receipts, never the money itself.

DEMO ONLY. Logins have no password or passkey: anyone who can reach the API can be any demo customer.
Off unless DEMO_LOGIN=1, and the routes do not exist otherwise. The demo accounts are separate from the
real (passkey) users and payments.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import uuid
from datetime import timedelta
from typing import Literal, Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, Cookie, Depends, HTTPException, Response
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from . import db, settings
from .auth_deps import now, token_hash
from .ledger_outbox import evidence_hash
from .risk.normalize import PayeeError, normalise_upi

log = logging.getLogger("sentinel.demo")
router = APIRouter(prefix="/demo")

COOKIE = "sentinel_demo"
SESSION_HOURS = 8
OPENING_PAISE = 1_00_000_00          # ₹1,00,000
MAX_TRANSFER_RUPEES = 1_00_000
BANKS = {"banka": "BankAMSP", "bankb": "BankBMSP", "bankc": "BankCMSP"}
VIEWERS = ["BankAMSP", "BankBMSP", "BankCMSP", "RegulatorMSP"]
BANK_NAMES = {"BankAMSP": "Bank A", "BankBMSP": "Bank B", "BankCMSP": "Bank C", "RegulatorMSP": "Regulator"}
CUSTOMERS = [  # fictional people
    ("BankAMSP", "Asha Rao", "asha@banka"),
    ("BankAMSP", "Arjun Mehta", "arjun@banka"),
    ("BankBMSP", "Bilal Khan", "bilal@bankb"),
    ("BankBMSP", "Bhavna Iyer", "bhavna@bankb"),
    ("BankCMSP", "Chitra Nair", "chitra@bankc"),
    ("BankCMSP", "Chetan Das", "chetan@bankc"),
]
SCAMMER_NAME = "Quick Refund Desk"


def scammer_upi(round_no: int) -> str:
    """A fresh scammer identifier per story round, so each round starts with a clean ledger record."""
    return f"quick.refund.r{round_no}@bankc"


def enabled() -> None:
    if not settings.DEMO_LOGIN:
        raise HTTPException(404, "Not found")


router.dependencies.append(Depends(enabled))


# ---------------------------------------------------------------- seeding

def _round(conn) -> int:
    row = conn.execute("SELECT round FROM demo_meta WHERE id = 1").fetchone()
    return row["round"] if row else 1


def ensure_seeded(conn) -> None:
    if conn.execute("SELECT 1 FROM demo_accounts LIMIT 1").fetchone():
        return
    conn.execute("SELECT pg_advisory_xact_lock(7402119)")
    if conn.execute("SELECT 1 FROM demo_accounts LIMIT 1").fetchone():
        return
    conn.execute("INSERT INTO demo_meta (id, round) VALUES (1, 1) ON CONFLICT (id) DO NOTHING")
    for bank, name, upi in CUSTOMERS:
        conn.execute("INSERT INTO demo_accounts (bank, name, upi, kind, balance_paise) VALUES (%s, %s, %s, 'customer', %s)",
                     (bank, name, upi, OPENING_PAISE))
    conn.execute("INSERT INTO demo_accounts (bank, name, upi, kind, balance_paise) VALUES ('BankCMSP', %s, %s, 'scammer', 0)",
                 (SCAMMER_NAME, scammer_upi(_round(conn))))


# ---------------------------------------------------------------- sessions

def demo_account(sentinel_demo: Optional[str] = Cookie(default=None)) -> dict:
    if not sentinel_demo:
        raise HTTPException(401, "Pick a demo customer first")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT a.* FROM demo_sessions s JOIN demo_accounts a ON a.id = s.account_id "
            "WHERE s.token_hash = %s AND s.expires_at > now() AND a.kind = 'customer'",
            (token_hash(sentinel_demo),),
        ).fetchone()
    if not row:
        raise HTTPException(401, "Demo session expired: pick a customer again")
    return row


# ---------------------------------------------------------------- ledger gateway

class LedgerDown(Exception):
    pass


class LedgerRefused(Exception):
    pass


def _transport() -> Optional[httpx.BaseTransport]:
    """Tests replace this with a mock transport."""
    return None


def _gateway(write: bool = False, timeout: float = 4.0) -> httpx.Client:
    if not settings.LEDGER_URL:
        raise LedgerDown("ledger not connected (LEDGER_URL is not set)")
    token = settings.LEDGER_WRITE_TOKEN if write else settings.LEDGER_TOKEN
    if write and not token:
        raise LedgerDown("ledger writes are off (LEDGER_WRITE_TOKEN is not set)")
    return httpx.Client(base_url=settings.LEDGER_URL, timeout=httpx.Timeout(timeout, connect=2.0),
                        headers={"x-ledger-token": token}, transport=_transport())


def _call(method: str, path: str, body: Optional[dict] = None, *, write: bool = False, timeout: float = 4.0) -> dict:
    try:
        with _gateway(write, timeout) as c:
            r = c.request(method, path, json=body)
    except httpx.HTTPError as exc:
        raise LedgerDown(f"ledger unreachable ({type(exc).__name__})") from exc
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code == 200:
        return data
    message = str(data.get("error") or f"HTTP {r.status_code}")[:300]
    if r.status_code in (400, 422):
        raise LedgerRefused(message)
    raise LedgerDown(f"ledger answered {r.status_code}: {message}")


def payee_hash(upi: str) -> str:
    return normalise_upi(upi).id_hash(settings.PAYEE_HMAC_KEY)


def check_payee(bank: str, upi: str) -> dict:
    """The ledger lookup a bank makes before sending money, as that bank. Fail-open, like real payments."""
    try:
        res = _call("POST", "/query-payee", {"org": bank, "idHashes": [payee_hash(upi)]}, timeout=3.0)["result"]
    except (LedgerDown, LedgerRefused, KeyError) as exc:
        return {"status": "unavailable", "error": str(exc)[:200]}
    reports = res.get("reports") or []
    return {
        "status": "hit" if reports else "clear",
        "maxWeight": res.get("maxWeight", 0),
        "confirmed": bool(res.get("hardRuleEligible")),
        "reports": [{"reportId": r.get("reportId"), "status": r.get("status"), "reporterOrg": r.get("reporterOrg"),
                     "banks": r.get("nonSandboxBanks"), "weight": r.get("weight")} for r in reports],
        "askedAs": bank,
    }


def canonical(record: dict) -> bytes:
    """The exact bytes the chaincode stores (sorted keys, no spaces), so hashes can be compared here."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ---------------------------------------------------------------- views

def _rupees(paise: int) -> float:
    return paise / 100


def _transfer_view(t: dict, me: dict) -> dict:
    sent = t["from_account"] == me["id"]
    return {
        "id": str(t["id"]),
        "direction": "sent" if sent else "received",
        "counterparty": t["to_upi"] if sent else t["from_upi"],
        "counterpartyName": t.get("to_name") if sent else t.get("from_name"),
        "counterpartyBank": t["to_bank"] if sent else t["from_bank"],
        "amount": _rupees(t["amount_paise"]),
        "verdict": t["verdict"],
        "reason": t["reason"],
        "ledgerCheck": t["ledger_check"],
        "ledgerStatus": t["ledger_status"],
        "ledgerTx": t["ledger_tx"],
        "ledgerBlock": t["ledger_block"],
        "ledgerError": t["ledger_error"],
        "report": t["report"] if sent else None,
        "tampered": t["record_tampered"] is not None if sent else None,
        "createdAt": t["created_at"].isoformat(),
    }


TRANSFER_SELECT = (
    "SELECT t.*, f.upi AS from_upi, f.name AS from_name, f.bank AS from_bank, r.name AS to_name "
    "FROM demo_transfers t JOIN demo_accounts f ON f.id = t.from_account LEFT JOIN demo_accounts r ON r.id = t.to_account "
)


def _me(conn, account: dict) -> dict:
    acct = conn.execute("SELECT * FROM demo_accounts WHERE id = %s", (account["id"],)).fetchone()
    rows = conn.execute(TRANSFER_SELECT + "WHERE t.from_account = %s OR t.to_account = %s ORDER BY t.created_at DESC LIMIT 30",
                        (acct["id"], acct["id"])).fetchall()
    return {
        "account": {"id": acct["id"], "name": acct["name"], "upi": acct["upi"], "bank": acct["bank"],
                    "bankName": BANK_NAMES[acct["bank"]], "balance": _rupees(acct["balance_paise"])},
        "transfers": [_transfer_view(t, acct) for t in rows],
        "scammer": scammer_upi(_round(conn)),
    }


# ---------------------------------------------------------------- routes: login

@router.get("/accounts")
def accounts() -> dict:
    with db.connect() as conn:
        ensure_seeded(conn)
        rows = conn.execute("SELECT id, bank, name, upi, kind FROM demo_accounts ORDER BY bank, id").fetchall()
        round_no = _round(conn)
    banks = []
    for bank in ["BankAMSP", "BankBMSP", "BankCMSP"]:
        banks.append({"bank": bank, "name": BANK_NAMES[bank],
                      "customers": [{"id": r["id"], "name": r["name"], "upi": r["upi"]}
                                    for r in rows if r["bank"] == bank and r["kind"] == "customer"]})
    return {"banks": banks, "scammer": {"name": SCAMMER_NAME, "upi": scammer_upi(round_no), "bank": "BankCMSP"},
            "round": round_no, "ledger": bool(settings.LEDGER_URL), "ledgerWrites": bool(settings.LEDGER_WRITE_TOKEN)}


class LoginIn(BaseModel):
    accountId: int


@router.post("/login")
def login(body: LoginIn, response: Response) -> dict:
    with db.connect() as conn:
        ensure_seeded(conn)
        acct = conn.execute("SELECT * FROM demo_accounts WHERE id = %s AND kind = 'customer'", (body.accountId,)).fetchone()
        if not acct:
            raise HTTPException(404, "No such demo customer")
        token = secrets.token_urlsafe(32)
        conn.execute("DELETE FROM demo_sessions WHERE expires_at < now()")
        conn.execute("INSERT INTO demo_sessions (token_hash, account_id, expires_at) VALUES (%s, %s, %s)",
                     (token_hash(token), acct["id"], now() + timedelta(hours=SESSION_HOURS)))
        out = _me(conn, acct)
    response.set_cookie(COOKIE, token, httponly=True, secure=settings.COOKIE_SECURE, samesite="strict",
                        max_age=SESSION_HOURS * 3600, path="/")
    return out


@router.post("/logout")
def logout(response: Response, sentinel_demo: Optional[str] = Cookie(default=None)) -> dict:
    if sentinel_demo:
        with db.connect() as conn:
            conn.execute("DELETE FROM demo_sessions WHERE token_hash = %s", (token_hash(sentinel_demo),))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(account: dict = Depends(demo_account)) -> dict:
    with db.connect() as conn:
        return _me(conn, account)


# ---------------------------------------------------------------- routes: transfers

class TransferIn(BaseModel):
    toUpi: str = Field(min_length=3, max_length=100)
    amount: float = Field(gt=0, le=MAX_TRANSFER_RUPEES)
    acknowledgeWarning: bool = False


def record_on_ledger(transfer_id: str) -> None:
    """Background: the sending bank records the payment (private details + public hash). Never raises."""
    with db.connect() as conn:
        t = conn.execute("SELECT t.*, f.bank AS from_bank FROM demo_transfers t JOIN demo_accounts f ON f.id = t.from_account "
                         "WHERE t.id = %s", (transfer_id,)).fetchone()
    if not t or t["ledger_status"] not in ("pending", "failed") or not t["record"]:
        return
    try:
        out = _call("POST", "/record-payment", {"org": t["from_bank"], "payment": t["record"]}, write=True, timeout=30.0)
        status, tx, block, error = "recorded", out.get("txId"), out.get("blockNumber"), None
    except (LedgerDown, LedgerRefused) as exc:
        if "already recorded" in str(exc):  # a retry after a lost answer: it is on the ledger
            status, tx, block, error = "recorded", t["ledger_tx"], t["ledger_block"], None
        else:
            status, tx, block, error = "failed", None, None, str(exc)[:300]
            log.warning("recordPayment %s failed: %s", transfer_id, exc)
    with db.connect() as conn:
        conn.execute("UPDATE demo_transfers SET ledger_status = %s, ledger_tx = %s, ledger_block = %s, ledger_error = %s "
                     "WHERE id = %s", (status, tx, block, error, transfer_id))


@router.post("/transfers")
def transfer(body: TransferIn, background: BackgroundTasks, account: dict = Depends(demo_account)) -> dict:
    try:
        payee = normalise_upi(body.toUpi)
    except PayeeError as exc:
        raise HTTPException(400, str(exc)) from exc
    handle = payee.normalised.split("@", 1)[1]
    if handle not in BANKS:
        raise HTTPException(400, "The demo network has three banks: use a UPI ID ending @banka, @bankb or @bankc")
    if payee.normalised == account["upi"]:
        raise HTTPException(400, "You cannot pay yourself")
    paise = round(body.amount * 100)
    to_bank = BANKS[handle]

    # 1. Ask the shared ledger about the payee, as the sending bank.
    check = check_payee(account["bank"], payee.normalised)
    if check["status"] == "unavailable":
        verdict, reason = "ALLOWED", "Ledger unavailable: the payment was checked without it"
    elif check.get("confirmed"):
        banks = sorted({r["reporterOrg"] for r in check["reports"] if r.get("reporterOrg")})
        verdict, reason = "BLOCKED", "Blocked: this payee is CONFIRMED as a scammer on the shared ledger by more than one bank"
        check["blockedBecause"] = banks
    elif check["status"] == "hit":
        if not body.acknowledgeWarning:
            raise HTTPException(409, detail={"needsAck": True, "check": check,
                                             "message": "Another bank has reported this payee. Send anyway?"})
        verdict, reason = "WARNED", "Sent after a warning: one bank has reported this payee"
    else:
        verdict, reason = "ALLOWED", "No reports for this payee on the shared ledger"

    tid = str(uuid.uuid4())
    with db.connect() as conn, conn.transaction():
        receiver = conn.execute("SELECT * FROM demo_accounts WHERE upi = %s", (payee.normalised,)).fetchone()
        ids = sorted({account["id"]} | ({receiver["id"]} if receiver else set()))
        locked = {r["id"]: r for r in conn.execute(
            "SELECT * FROM demo_accounts WHERE id = ANY(%s) ORDER BY id FOR UPDATE", (ids,)).fetchall()}
        sender = locked[account["id"]]
        record = None
        if verdict != "BLOCKED":
            if sender["balance_paise"] < paise:
                raise HTTPException(400, f"Not enough balance (₹{_rupees(sender['balance_paise']):,.2f})")
            conn.execute("UPDATE demo_accounts SET balance_paise = balance_paise - %s WHERE id = %s", (paise, sender["id"]))
            if receiver:
                conn.execute("UPDATE demo_accounts SET balance_paise = balance_paise + %s WHERE id = %s", (paise, receiver["id"]))
            # What goes to the ledger: exactly these fields (the chaincode rejects anything else).
            record = {
                "paymentId": tid,
                "fromBank": sender["bank"],
                "toBank": to_bank,
                "payer": sender["upi"],
                "payerName": sender["name"],
                "payee": payee.normalised,
                "amountPaise": paise,
                "verdict": verdict,
                "createdAt": now().isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "salt": secrets.token_hex(32),
            }
        ledger_status = "pending" if record and settings.LEDGER_URL and settings.LEDGER_WRITE_TOKEN else "none"
        conn.execute(
            "INSERT INTO demo_transfers (id, from_account, to_upi, to_bank, to_account, amount_paise, verdict, reason, "
            "ledger_check, record, ledger_status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (tid, sender["id"], payee.normalised, to_bank, receiver["id"] if receiver else None, paise, verdict, reason,
             Jsonb(check), Jsonb(record) if record else None, ledger_status),
        )
    if ledger_status == "pending":
        background.add_task(record_on_ledger, tid)
    with db.connect() as conn:
        out = _me(conn, account)
    out["result"] = {"id": tid, "verdict": verdict, "reason": reason, "check": check}
    return out


def _own_transfer(conn, transfer_id: str, account: dict) -> dict:
    try:
        uuid.UUID(transfer_id)
    except ValueError as exc:
        raise HTTPException(404, "No such transfer") from exc
    t = conn.execute("SELECT * FROM demo_transfers WHERE id = %s AND from_account = %s", (transfer_id, account["id"])).fetchone()
    if not t:
        raise HTTPException(404, "No such transfer")
    return t


@router.post("/transfers/{transfer_id}/retry-ledger")
def retry_ledger(transfer_id: str, background: BackgroundTasks, account: dict = Depends(demo_account)) -> dict:
    with db.connect() as conn:
        t = _own_transfer(conn, transfer_id, account)
        if t["ledger_status"] != "failed":
            raise HTTPException(409, "Only a failed ledger record can be retried")
        conn.execute("UPDATE demo_transfers SET ledger_status = 'pending', ledger_error = NULL WHERE id = %s", (transfer_id,))
    background.add_task(record_on_ledger, transfer_id)
    with db.connect() as conn:
        return _me(conn, account)


@router.post("/transfers/{transfer_id}/report")
def report(transfer_id: str, account: dict = Depends(demo_account)) -> dict:
    """The customer says "this was a scam". Their bank files on the ledger, as itself."""
    bank = account["bank"]
    with db.connect() as conn:
        t = _own_transfer(conn, transfer_id, account)
    if t["verdict"] == "BLOCKED":
        raise HTTPException(409, "No money left your account: there is nothing to report")
    if t["report"]:
        raise HTTPException(409, "Already reported")
    id_hash = payee_hash(t["to_upi"])
    # The case file stays with the bank; the ledger gets its hash.
    evidence = {"complaint": "customer reported a scam", "transferId": str(t["id"]), "bank": bank,
                "amountPaise": t["amount_paise"], "reportedAt": now().isoformat()}
    ev_hash = evidence_hash(evidence)
    try:
        found = _call("POST", "/query-payee", {"org": bank, "idHashes": [id_hash]}, timeout=4.0)["result"]
        reports = found.get("reports") or []
        active = ("PENDING", "CONFIRMED", "DISPUTED")
        ours = [r for r in reports if r.get("reporterOrg") == bank and r.get("status") in active]
        others = [r for r in reports if r.get("reporterOrg") != bank and r.get("status") in ("PENDING", "CONFIRMED")]
        if ours:
            action, report_id, tx, block = "already_reported", ours[0]["reportId"], None, None
        elif others:
            others.sort(key=lambda r: 0 if r["status"] == "PENDING" else 1)
            report_id = others[0]["reportId"]
            try:
                out = _call("POST", "/confirm-report", {"org": bank, "reportId": report_id, "evidenceHash": ev_hash,
                                                        "source": "CUSTOMER_COMPLAINT"}, write=True, timeout=30.0)
                action, tx, block = "confirmed", out.get("txId"), out.get("blockNumber")
            except LedgerRefused as exc:
                if "already confirmed" not in str(exc):
                    raise
                action, tx, block = "already_reported", None, None
        else:
            out = _call("POST", "/report-mule", {"org": bank, "idHash": id_hash, "idType": "UPI", "evidenceHash": ev_hash,
                                                 "source": "CUSTOMER_COMPLAINT", "severity": "HIGH"}, write=True, timeout=30.0)
            action, report_id, tx, block = "reported", out["result"].get("reportId"), out.get("txId"), out.get("blockNumber")
        after = check_payee(bank, t["to_upi"])
    except LedgerDown as exc:
        raise HTTPException(503, f"Could not reach the ledger: {exc}") from exc
    except LedgerRefused as exc:
        raise HTTPException(409, f"The ledger refused: {exc}") from exc
    status_after = next((r["status"] for r in after.get("reports", []) if r.get("reportId") == report_id), None)
    result = {"action": action, "bank": bank, "reportId": report_id, "txId": tx, "block": block,
              "statusAfter": status_after, "evidenceHash": ev_hash, "at": now().isoformat()}
    with db.connect() as conn:
        conn.execute("UPDATE demo_transfers SET report = %s, reported_at = now(), evidence = %s WHERE id = %s",
                     (Jsonb(result), Jsonb(evidence), transfer_id))
        out = _me(conn, account)
    out["result"] = result
    return out


@router.post("/transfers/{transfer_id}/tamper")
def tamper(transfer_id: str, account: dict = Depends(demo_account)) -> dict:
    """Demo: someone with database access edits the bank's copy of a payment (amount x10)."""
    with db.connect() as conn:
        t = _own_transfer(conn, transfer_id, account)
        if not t["record"]:
            raise HTTPException(409, "This transfer has no ledger record")
        changed = dict(t["record"], amountPaise=t["record"]["amountPaise"] * 10)
        conn.execute("UPDATE demo_transfers SET record_tampered = %s WHERE id = %s", (Jsonb(changed), transfer_id))
        return _me(conn, account)


@router.post("/transfers/{transfer_id}/restore")
def restore(transfer_id: str, account: dict = Depends(demo_account)) -> dict:
    with db.connect() as conn:
        _own_transfer(conn, transfer_id, account)
        conn.execute("UPDATE demo_transfers SET record_tampered = NULL WHERE id = %s", (transfer_id,))
        return _me(conn, account)


# ---------------------------------------------------------------- routes: the ledger as each bank sees it

@router.get("/ledger")
def ledger_view(viewer: Optional[str] = None, account: dict = Depends(demo_account)) -> dict:
    """Recent payments as one organisation's peer returns them. The rows are built from the ledger's answer
    (and, for a bank in the payment, its own database copy), never from other banks' databases."""
    as_org = viewer or account["bank"]
    if as_org not in VIEWERS:
        raise HTTPException(400, f"viewer must be one of {', '.join(VIEWERS)}")
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT t.id, t.ledger_tx, t.ledger_block, t.record, t.record_tampered, f.bank AS from_bank, t.to_bank "
            "FROM demo_transfers t JOIN demo_accounts f ON f.id = t.from_account "
            "WHERE t.ledger_status = 'recorded' ORDER BY t.created_at DESC LIMIT 10"
        ).fetchall()
    out: list[dict] = []
    error = None
    for r in rows:
        try:
            view = _call("GET", f"/payment/{r['id']}?org={as_org}")["result"]
        except (LedgerDown, LedgerRefused) as exc:
            error = str(exc)[:200]
            break
        item = {
            "paymentId": str(r["id"]),
            "txId": view.get("public", {}).get("txId") or r["ledger_tx"],
            "block": r["ledger_block"],
            "access": view.get("access"),
            "public": view.get("public"),
            "details": view.get("details"),
            "fabricHash": view.get("fabricHash"),
            "fabricHashMatches": view.get("fabricHashMatches"),
            "note": view.get("note"),
        }
        # A bank in the payment also checks its own database copy against the hash on the chain.
        if as_org in (r["from_bank"], r["to_bank"]) and r["record"]:
            copy = r["record_tampered"] or r["record"]
            item["bankCopy"] = {
                "amount": _rupees(copy["amountPaise"]),
                "hash": sha256_hex(canonical(copy)),
                "matchesChain": sha256_hex(canonical(copy)) == (view.get("public") or {}).get("dataHash"),
                "tampered": r["record_tampered"] is not None,
            }
        out.append(item)
    return {"viewer": as_org, "viewerName": BANK_NAMES[as_org], "payments": out, "error": error}


@router.get("/chain")
def chain(count: int = 6, account: dict = Depends(demo_account)) -> dict:
    """The latest blocks (same data as the Ledger page), for the demo's live chain strip."""
    from .chainview import _get

    data = _get(f"/chain/blocks?count={max(1, min(count, 12))}")
    blocks = data.get("blocks", [])
    for newer, older in zip(blocks, blocks[1:]):
        newer["linkVerified"] = newer["previousHash"] == older["hash"]
    if blocks:
        blocks[-1]["linkVerified"] = None
    return {"info": data.get("info"), "blocks": blocks}


@router.get("/keys")
def keys(account: dict = Depends(demo_account)) -> dict:
    """Every organisation's certificates and public keys (private keys never leave their owners)."""
    try:
        return _call("GET", "/identities")
    except (LedgerDown, LedgerRefused) as exc:
        raise HTTPException(503, f"Could not reach the ledger: {exc}") from exc


@router.post("/reset")
def reset(account: dict = Depends(demo_account)) -> dict:
    """Start the story again: balances back, transfers cleared here, a new scammer identifier.
    Nothing is removed from the ledger: a blockchain cannot delete, which is the point."""
    with db.connect() as conn, conn.transaction():
        conn.execute("DELETE FROM demo_transfers")
        conn.execute("UPDATE demo_meta SET round = round + 1 WHERE id = 1")
        new = scammer_upi(_round(conn))
        conn.execute("UPDATE demo_accounts SET balance_paise = %s WHERE kind = 'customer'", (OPENING_PAISE,))
        conn.execute("UPDATE demo_accounts SET upi = %s, balance_paise = 0 WHERE kind = 'scammer'", (new,))
    with db.connect() as conn:
        return _me(conn, account)
