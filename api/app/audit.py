"""
Audit events via a transactional outbox.

Why not send events directly: FastAPI background tasks are dropped whenever the endpoint raises,
so the most important security events (a cloned authenticator) were never recorded, and any
audit-service outage silently lost events.

How it works:
- enqueue(conn, ...) writes the event into `audit_outbox` inside the caller's transaction, so the
  event exists if and only if the action it describes was committed.
- record_now(...) writes on its own autocommitted connection, for events about requests that are
  about to be rejected (their main transaction rolls back, but the event must survive).
- A single background drainer posts pending rows to the audit-log service in id order, with retries.
  If the service is down, events wait in Postgres instead of disappearing.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

import httpx
from psycopg.types.json import Jsonb

from . import db, settings

log = logging.getLogger("sentinel.audit")

OUTBOX_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_outbox (
    id          BIGSERIAL PRIMARY KEY,
    event_type  TEXT NOT NULL,
    payload     JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at     TIMESTAMPTZ,
    attempts    INT NOT NULL DEFAULT 0,
    last_error  TEXT
);
CREATE INDEX IF NOT EXISTS audit_outbox_pending_idx ON audit_outbox(id) WHERE sent_at IS NULL;
"""


def enqueue(conn, event_type: str, payload: dict) -> None:
    """Record an event as part of the caller's transaction."""
    conn.execute(
        "INSERT INTO audit_outbox (event_type, payload) VALUES (%s, %s)", (event_type, Jsonb(payload))
    )


def record_now(event_type: str, payload: dict) -> None:
    """Record an event that must survive even though the current request is being rejected."""
    with db.connect() as conn:  # separate connection, commits on exit
        enqueue(conn, event_type, payload)


def drain_once(client: httpx.Client, batch: int = 50) -> int:
    """Send pending events in id order. Stops at the first failure so the stream order is preserved."""
    sent = 0
    with db.connect() as conn, conn.transaction():
        rows = conn.execute(
            "SELECT id, event_type, payload FROM audit_outbox WHERE sent_at IS NULL "
            "ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED",
            (batch,),
        ).fetchall()
        for row in rows:
            try:
                r = client.post(
                    f"{settings.AUDIT_URL}/events",
                    json={
                        "stream": settings.AUDIT_STREAM,
                        "eventType": row["event_type"],
                        "payload": row["payload"],
                        # Stable per outbox row: if we crash after the audit service stored it but
                        # before we mark it sent, the retry is recognised and not stored twice.
                        "idempotencyKey": f"{settings.AUDIT_STREAM}:outbox:{row['id']}",
                    },
                    headers={"x-audit-token": settings.AUDIT_TOKEN} if settings.AUDIT_TOKEN else {},
                    timeout=2.0,
                )
                r.raise_for_status()
            except httpx.HTTPError as exc:
                conn.execute(
                    "UPDATE audit_outbox SET attempts = attempts + 1, last_error = %s WHERE id = %s",
                    (str(exc)[:500], row["id"]),
                )
                log.warning("audit event %s (outbox id %s) not delivered yet: %s", row["event_type"], row["id"], exc)
                break
            conn.execute("UPDATE audit_outbox SET sent_at = now() WHERE id = %s", (row["id"],))
            sent += 1
    return sent


class Drainer:
    """One background thread per API process. Run a single API instance to keep strict ordering."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if not settings.AUDIT_URL:
            log.info("AUDIT_URL is empty: events stay in audit_outbox and are not forwarded")
            return
        self._thread = threading.Thread(target=self._run, name="audit-drainer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        with httpx.Client() as client:
            while not self._stop.is_set():
                try:
                    drain_once(client)
                except Exception:  # noqa: BLE001 - keep draining after DB hiccups
                    log.exception("audit drainer error")
                self._stop.wait(settings.AUDIT_DRAIN_INTERVAL_SECONDS)


def pending_events(event_type: Optional[str] = None) -> list[dict]:
    """Used by tests and the health endpoint."""
    with db.connect() as conn:
        if event_type:
            rows = conn.execute(
                "SELECT event_type, payload FROM audit_outbox WHERE event_type = %s ORDER BY id", (event_type,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT event_type, payload FROM audit_outbox ORDER BY id").fetchall()
    return [{"eventType": r["event_type"], "payload": r["payload"]} for r in rows]


def backlog() -> int:
    with db.connect() as conn:
        return conn.execute("SELECT count(*) AS n FROM audit_outbox WHERE sent_at IS NULL").fetchone()["n"]


