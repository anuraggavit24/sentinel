"""Module 3a: this bank's writes to the consortium ledger, through an outbox.

An analyst's decision commits locally at once and queues a row here; a background drainer turns the
row into exactly one of:
- reportMule      nobody has an active report on this payee: file one (source ANALYST)
- confirmReport   another bank already reported it: add our confirmation instead of a duplicate
- already_ours    we already have an active report: nothing to write
Fabric's endorsement policy (2 of the 3 banks) is enforced by the peers; the Gateway collects it.

Delivery: a row is leased while it is being sent, retried with backoff on timeouts, outages and
commit conflicts, and marked failed (and audited) after MAX_ATTEMPTS or on a definite refusal.
Retrying is safe: every attempt first asks the ledger what is already there.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from typing import Optional

import httpx
from psycopg.types.json import Jsonb

from . import audit, db, settings

log = logging.getLogger("sentinel.ledger_outbox")
MAX_ATTEMPTS = 8
LEASE_SECONDS = 120          # a claimed row is invisible to other drainers for this long
ACTIVE = ("PENDING", "CONFIRMED", "DISPUTED")
CONFIRMABLE = ("PENDING", "CONFIRMED")
# Chaincode refusals that mean "the ledger changed under us": look again next round instead of failing.
RACE_MESSAGES = ("already exists for this identifier",)
ALREADY_CONFIRMED = "has already confirmed"


def evidence_hash(evidence: dict) -> str:
    """The case record stays in our database; the ledger gets this hash, so the bank can later prove
    what the report was based on without putting customer data on a shared ledger."""
    canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def enqueue(conn, *, payment_id, id_hash: str, id_type: str, severity: str, source: str, evidence: dict,
            requested_by) -> int:
    row = conn.execute(
        "INSERT INTO ledger_outbox (payment_id, id_hash, id_type, severity, source, evidence, evidence_hash, requested_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (payment_id, id_hash, id_type, severity, source, Jsonb(evidence), evidence_hash(evidence), requested_by),
    ).fetchone()
    return row["id"]


def _transport() -> Optional[httpx.BaseTransport]:
    """Tests replace this with a mock transport."""
    return None


def client() -> httpx.Client:
    return httpx.Client(base_url=settings.LEDGER_URL, timeout=httpx.Timeout(30.0, connect=5.0),
                        headers={"x-ledger-token": settings.LEDGER_WRITE_TOKEN}, transport=_transport())


class Retry(Exception):
    pass


class Refused(Exception):
    pass


def _post(c: httpx.Client, path: str, body: dict) -> dict:
    try:
        r = c.post(path, json=body)
    except httpx.HTTPError as exc:
        raise Retry(f"{type(exc).__name__} calling {path}") from exc
    data = {}
    if r.headers.get("content-type", "").startswith("application/json"):
        try:
            data = r.json()
        except ValueError:
            data = {}
    if r.status_code == 200 and isinstance(data.get("result"), dict):
        return data["result"]
    message = str(data.get("error") or f"HTTP {r.status_code}")[:300]
    if r.status_code == 422 or r.status_code == 400:
        raise Refused(message)
    raise Retry(message)  # 5xx: timeout, unavailable, commit conflict, not configured


def decide(reports: list[dict]) -> tuple[str, Optional[dict]]:
    """What to write, given what the ledger already holds about this payee."""
    ours = [r for r in reports if r.get("reporterOrg") == settings.LEDGER_MSP and r.get("status") in ACTIVE]
    if ours:
        return "already_ours", ours[0]
    others = [r for r in reports if r.get("reporterOrg") != settings.LEDGER_MSP and r.get("status") in CONFIRMABLE]
    if others:
        # Confirming a PENDING report is what moves it to CONFIRMED, so prefer that.
        others.sort(key=lambda r: 0 if r["status"] == "PENDING" else 1)
        return "confirm", others[0]
    return "report", None


def process(c: httpx.Client, row: dict) -> tuple[str, Optional[str]]:
    """Returns (final status, report id). Raises Retry or Refused."""
    found = _post(c, "/query-payee", {"idHashes": [row["id_hash"]]})
    reports = found.get("reports") if isinstance(found.get("reports"), list) else []
    action, target = decide(reports)
    if action == "already_ours":
        return "already_ours", target.get("reportId")
    if action == "confirm":
        try:
            _post(c, "/confirm-report", {"reportId": target["reportId"], "evidenceHash": row["evidence_hash"],
                                         "source": row["source"]})
        except Refused as exc:
            if ALREADY_CONFIRMED in str(exc):
                return "already_ours", target["reportId"]
            raise
        return "confirmed", target["reportId"]
    try:
        result = _post(c, "/report-mule", {"idHash": row["id_hash"], "idType": row["id_type"],
                                           "evidenceHash": row["evidence_hash"], "source": row["source"],
                                           "severity": row["severity"]})
    except Refused as exc:
        if any(m in str(exc) for m in RACE_MESSAGES):
            raise Retry(f"ledger changed while filing: {exc}") from exc
        raise
    return "reported", result.get("reportId")


def _backoff_seconds(attempts: int) -> int:
    return min(5 * 2 ** (attempts - 1), 300)


def drain_once(c: Optional[httpx.Client] = None, batch: int = 10) -> int:
    """Send due rows. Returns how many reached a final state."""
    if not settings.LEDGER_URL or not settings.LEDGER_WRITE_TOKEN:
        return 0
    with db.connect() as conn:
        claimed = conn.execute(
            "UPDATE ledger_outbox SET attempts = attempts + 1, next_attempt_at = now() + make_interval(secs => %s) "
            "WHERE id IN (SELECT id FROM ledger_outbox WHERE status = 'pending' AND next_attempt_at <= now() "
            "             ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED) "
            "RETURNING *",
            (LEASE_SECONDS, batch),
        ).fetchall()
    if not claimed:
        return 0
    own = c is None
    c = c or client()
    done = 0
    try:
        for row in claimed:
            done += _settle(c, row)
    finally:
        if own:
            c.close()
    return done


def _settle(c: httpx.Client, row: dict) -> int:
    try:
        status, report_id = process(c, row)
    except Retry as exc:
        final = row["attempts"] >= MAX_ATTEMPTS
        with db.connect() as conn, conn.transaction():
            conn.execute(
                "UPDATE ledger_outbox SET status = %s, last_error = %s, done_at = CASE WHEN %s THEN now() END, "
                "next_attempt_at = now() + make_interval(secs => %s) WHERE id = %s",
                ("failed" if final else "pending", str(exc)[:300], final, _backoff_seconds(row["attempts"]), row["id"]),
            )
            if final:
                audit.enqueue(conn, "LEDGER_WRITE_FAILED", {"outboxId": row["id"], "paymentId": _s(row["payment_id"]),
                                                            "attempts": row["attempts"], "error": str(exc)[:200]})
        return 1 if final else 0
    except Refused as exc:
        with db.connect() as conn, conn.transaction():
            conn.execute("UPDATE ledger_outbox SET status = 'failed', last_error = %s, done_at = now() WHERE id = %s",
                         (str(exc)[:300], row["id"]))
            audit.enqueue(conn, "LEDGER_WRITE_FAILED", {"outboxId": row["id"], "paymentId": _s(row["payment_id"]),
                                                        "attempts": row["attempts"], "error": f"refused: {str(exc)[:200]}"})
        return 1
    with db.connect() as conn, conn.transaction():
        conn.execute(
            "UPDATE ledger_outbox SET status = %s, report_id = %s, last_error = NULL, done_at = now() WHERE id = %s",
            (status, report_id, row["id"]),
        )
        audit.enqueue(conn, "LEDGER_REPORT_FILED", {"outboxId": row["id"], "paymentId": _s(row["payment_id"]),
                                                    "action": status, "reportId": report_id,
                                                    "evidenceHash": row["evidence_hash"]})
    return 1


def _s(v) -> Optional[str]:
    return str(v) if v is not None else None


class Drainer:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if not settings.LEDGER_URL or not settings.LEDGER_WRITE_TOKEN:
            log.info("ledger writes not configured: filings stay queued in ledger_outbox")
            return
        self._thread = threading.Thread(target=self._run, name="ledger-drainer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with client() as c:
                    drain_once(c)
            except Exception:  # noqa: BLE001 - keep draining after DB hiccups
                log.exception("ledger drainer error")
            self._stop.wait(settings.LEDGER_DRAIN_INTERVAL_SECONDS)
