"""Database connection, schema and start-up (creates the database if it does not exist yet)."""
import time

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from . import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username         TEXT NOT NULL UNIQUE,
    display_name     TEXT NOT NULL,
    webauthn_user_id BYTEA NOT NULL UNIQUE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS credentials (
    id           BYTEA PRIMARY KEY,
    user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    public_key   BYTEA NOT NULL,
    sign_count   BIGINT NOT NULL,
    transports   TEXT[] NOT NULL DEFAULT '{}',
    device_type  TEXT NOT NULL,
    backed_up    BOOLEAN NOT NULL,
    label        TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS credentials_user_idx ON credentials(user_id);

-- One row per ceremony. Single use, short-lived. Pending registrations keep the user details
-- here, so an abandoned sign-up never reserves a username.
CREATE TABLE IF NOT EXISTS challenges (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    purpose              TEXT NOT NULL CHECK (purpose IN ('register', 'add_device', 'login')),
    challenge            BYTEA NOT NULL,
    user_id              UUID REFERENCES users(id) ON DELETE CASCADE,
    pending_username     TEXT,
    pending_display_name TEXT,
    pending_webauthn_id  BYTEA,
    expires_at           TIMESTAMPTZ NOT NULL,
    used_at              TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sessions (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token_hash    BYTEA NOT NULL UNIQUE,
    user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    credential_id BYTEA,
    user_agent    TEXT,
    ip            TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL,
    revoked_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions(user_id);
"""


def connect() -> psycopg.Connection:
    return psycopg.connect(settings.DATABASE_URL, row_factory=dict_row)


def ensure_database() -> None:
    """Create the target database if missing (the shared Postgres container starts with 'audit' only)."""
    params = conninfo_to_dict(settings.DATABASE_URL)
    target = params.get("dbname")
    admin = make_conninfo(settings.DATABASE_URL, dbname="postgres")
    with psycopg.connect(admin, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target,)).fetchone()
        if not exists:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target)))


def init(timeout_seconds: int = 60) -> None:
    deadline = time.time() + timeout_seconds
    while True:
        try:
            ensure_database()
            from .audit import OUTBOX_SCHEMA  # local import: audit imports db

            with connect() as conn:
                conn.execute(SCHEMA)
                conn.execute(OUTBOX_SCHEMA)
            return
        except psycopg.OperationalError:
            if time.time() > deadline:
                raise
            time.sleep(1)
