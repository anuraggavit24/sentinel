"""Sentinel audit-log service: append-only, hash-chained, Ed25519-signed log per stream."""
from __future__ import annotations

import hmac
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

from .chain import GENESIS, Row, canonical, entry_hash, sign, verify_rows

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://audit:audit@db:5432/audit")
# Demo key only. In production each bank keeps its signing key in an HSM, never in an env var.
SIGNING_SEED = bytes.fromhex(os.environ.get("AUDIT_SIGNING_SEED", "11" * 32))
DEMO_INSIDER = os.environ.get("DEMO_INSIDER", "0") == "1"
# Shared secret required on every write. The service signs whatever it is given, so without this
# anyone who can reach the port could append forged, validly signed entries.
AUDIT_TOKEN = os.environ.get("AUDIT_TOKEN", "")
# Fail closed: with no token configured, writes are refused unless this is explicitly switched on
# (local tests only). A forgotten setting must never silently open the log to forged entries.
ALLOW_UNAUTHENTICATED = os.environ.get("AUDIT_ALLOW_UNAUTHENTICATED", "0") == "1"
log = logging.getLogger("sentinel.audit-service")
if not AUDIT_TOKEN:
    if ALLOW_UNAUTHENTICATED:
        log.warning("AUDIT_TOKEN is not set and AUDIT_ALLOW_UNAUTHENTICATED=1: writes are unauthenticated (tests only)")
    else:
        log.warning("AUDIT_TOKEN is not set: all writes will be refused")

PRIVATE_KEY = Ed25519PrivateKey.from_private_bytes(SIGNING_SEED)
PUBLIC_KEY = PRIVATE_KEY.public_key()
STREAM_PATTERN = r"^[A-Za-z0-9_.:-]{1,100}$"

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id         BIGSERIAL PRIMARY KEY,
    stream     TEXT   NOT NULL,
    idx        BIGINT NOT NULL,
    ts         TEXT   NOT NULL,
    event_type TEXT   NOT NULL,
    payload    TEXT   NOT NULL,
    prev_hash  TEXT   NOT NULL,
    hash       TEXT   NOT NULL,
    sig        TEXT   NOT NULL,
    UNIQUE (stream, idx)
);
-- Optional client key so a retried delivery is stored once. Not part of the hashed content.
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS idem_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS audit_log_idem_key ON audit_log(idem_key) WHERE idem_key IS NOT NULL;
"""

def connect() -> psycopg.Connection:
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


# Bump when SCHEMA changes. DDL runs only when the stored version is older, serialised with an advisory
# lock: ALTER TABLE ... IF NOT EXISTS takes an ACCESS EXCLUSIVE lock even when nothing changes, so running
# it on every start would block appends during a restart (same bug found and fixed in the API).
SCHEMA_VERSION = 2
_MIGRATION_LOCK = 7_402_119


def migrate(conn: psycopg.Connection) -> bool:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_meta (id INT PRIMARY KEY CHECK (id = 1), version INT NOT NULL)")
    conn.commit()
    row = conn.execute("SELECT version FROM schema_meta WHERE id = 1").fetchone()
    if row and row["version"] >= SCHEMA_VERSION:
        return False
    conn.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK,))
    try:
        row = conn.execute("SELECT version FROM schema_meta WHERE id = 1").fetchone()
        if row and row["version"] >= SCHEMA_VERSION:
            return False
        with conn.transaction():
            conn.execute(SCHEMA)
            conn.execute(
                "INSERT INTO schema_meta (id, version) VALUES (1, %s) "
                "ON CONFLICT (id) DO UPDATE SET version = EXCLUDED.version",
                (SCHEMA_VERSION,),
            )
        return True
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK,))


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Postgres may still be starting when the API container boots, so retry for up to 60 s.
    deadline = time.time() + 60
    while True:
        try:
            with connect() as conn:
                migrate(conn)
            break
        except psycopg.OperationalError:
            if time.time() > deadline:
                raise
            time.sleep(1)
    yield


app = FastAPI(title="Sentinel audit log", version="0.1.0", lifespan=lifespan)


class EventIn(BaseModel):
    stream: str = Field(pattern=STREAM_PATTERN)
    eventType: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any]
    # Senders that retry (the API's outbox) pass a stable key; a repeat returns the stored entry.
    idempotencyKey: Optional[str] = Field(default=None, min_length=1, max_length=200)


class InsiderRewrite(BaseModel):
    stream: str = Field(pattern=STREAM_PATTERN)
    idx: int = Field(ge=0)
    payload: dict[str, Any]


def require_writer(x_audit_token: Optional[str] = Header(default=None)) -> None:
    if not AUDIT_TOKEN:
        if ALLOW_UNAUTHENTICATED:
            return
        raise HTTPException(503, "Audit service has no AUDIT_TOKEN configured; writes are disabled")
    if not x_audit_token or not hmac.compare_digest(x_audit_token.encode(), AUDIT_TOKEN.encode()):
        raise HTTPException(401, "Missing or invalid audit token")


def load_rows(conn: psycopg.Connection, stream: str) -> list[Row]:
    rows = conn.execute(
        "SELECT stream, idx, ts, event_type, payload, prev_hash, hash, sig FROM audit_log WHERE stream = %s ORDER BY idx",
        (stream,),
    ).fetchall()
    return [Row(**r) for r in rows]


@app.get("/health")
def health() -> dict:
    with connect() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/pubkey")
def pubkey() -> dict:
    raw = PUBLIC_KEY.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return {"algorithm": "Ed25519", "publicKey": raw.hex()}


@app.post("/events", status_code=201, dependencies=[Depends(require_writer)])
def append(event: EventIn, response: Response) -> dict:
    payload = canonical(event.payload)
    ts = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    with connect() as conn, conn.transaction():
        # Serialise appends per stream so two writers can never fork the chain.
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (event.stream,))
        if event.idempotencyKey:
            dup = conn.execute(
                "SELECT stream, idx, hash, prev_hash, ts FROM audit_log WHERE idem_key = %s", (event.idempotencyKey,)
            ).fetchone()
            if dup is not None:
                response.status_code = 200
                return {"stream": dup["stream"], "idx": dup["idx"], "hash": dup["hash"],
                        "prevHash": dup["prev_hash"], "ts": dup["ts"], "duplicate": True}
        last = conn.execute(
            "SELECT idx, hash FROM audit_log WHERE stream = %s ORDER BY idx DESC LIMIT 1", (event.stream,)
        ).fetchone()
        idx = 0 if last is None else last["idx"] + 1
        prev = GENESIS if last is None else last["hash"]
        h = entry_hash(event.stream, idx, ts, event.eventType, payload, prev)
        conn.execute(
            "INSERT INTO audit_log (stream, idx, ts, event_type, payload, prev_hash, hash, sig, idem_key) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (event.stream, idx, ts, event.eventType, payload, prev, h, sign(PRIVATE_KEY, h), event.idempotencyKey),
        )
    return {"stream": event.stream, "idx": idx, "hash": h, "prevHash": prev, "ts": ts, "duplicate": False}


@app.get("/streams/{stream}/root")
def root(stream: str) -> dict:
    with connect() as conn:
        last = conn.execute(
            "SELECT idx, hash FROM audit_log WHERE stream = %s ORDER BY idx DESC LIMIT 1", (stream,)
        ).fetchone()
    if last is None:
        raise HTTPException(404, f"Stream {stream} has no entries")
    return {"stream": stream, "idx": last["idx"], "root": last["hash"], "entries": last["idx"] + 1}


@app.get("/streams/{stream}/verify")
def verify(stream: str, anchor: Optional[str] = Query(default=None, pattern=r"^[0-9a-f]{64}$")) -> dict:
    with connect() as conn:
        rows = load_rows(conn, stream)
    if not rows:
        raise HTTPException(404, f"Stream {stream} has no entries")
    return {"stream": stream, **verify_rows(rows, PUBLIC_KEY, anchor).as_dict()}


@app.post("/demo/insider-rewrite", dependencies=[Depends(require_writer)])
def insider_rewrite(body: InsiderRewrite) -> dict:
    """
    DEMO ONLY (DEMO_INSIDER=1): simulates an insider with database access AND the signing key.
    Rewrites one entry, then recomputes and re-signs every later entry so the chain looks valid.
    Only the on-chain anchor can expose this.
    """
    if not DEMO_INSIDER:
        raise HTTPException(403, "Insider demo is disabled")
    with connect() as conn, conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (body.stream,))
        rows = load_rows(conn, body.stream)
        if body.idx >= len(rows):
            raise HTTPException(404, f"Entry {body.idx} does not exist")
        prev = GENESIS if body.idx == 0 else rows[body.idx - 1].hash
        for r in rows[body.idx:]:
            payload = canonical(body.payload) if r.idx == body.idx else r.payload
            h = entry_hash(r.stream, r.idx, r.ts, r.event_type, payload, prev)
            conn.execute(
                "UPDATE audit_log SET payload = %s, prev_hash = %s, hash = %s, sig = %s WHERE stream = %s AND idx = %s",
                (payload, prev, h, sign(PRIVATE_KEY, h), r.stream, r.idx),
            )
            prev = h
    return {"stream": body.stream, "rewrittenIdx": body.idx, "newRoot": prev}
