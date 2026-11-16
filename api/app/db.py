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

-- Module 2: every scored payment. Raw payee identifiers are never stored: only the keyed hash
-- (same as the consortium ledger) and a masked display string.
CREATE TABLE IF NOT EXISTS payments (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_id    UUID REFERENCES sessions(id) ON DELETE SET NULL,
    payee_type    TEXT NOT NULL,
    payee_hash    TEXT NOT NULL,
    payee_display TEXT NOT NULL,
    amount_paise  BIGINT NOT NULL CHECK (amount_paise > 0),
    score         INT NOT NULL,
    band          TEXT NOT NULL CHECK (band IN ('allow', 'step_up', 'sandbox')),
    status        TEXT NOT NULL CHECK (status IN ('completed', 'held')),
    reasons       JSONB NOT NULL,
    signals       JSONB NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS payments_user_idx ON payments(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS payments_user_payee_idx ON payments(user_id, payee_hash);
-- 2a review fixes (ALTER so databases created by the first 2a release are upgraded in place).
ALTER TABLE payments ADD COLUMN IF NOT EXISTS idem_key TEXT;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS request_hash TEXT;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS response JSONB;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS payee_trusted BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS coaching BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS trust_eligible BOOLEAN NOT NULL DEFAULT false;
CREATE UNIQUE INDEX IF NOT EXISTS payments_user_idem_idx ON payments(user_id, idem_key) WHERE idem_key IS NOT NULL;

-- Slice 2b: behaviour samples (timing statistics only, never keys or text). Only clean samples
-- (allowed, no coaching or takeover signals) train the user's profile.
CREATE TABLE IF NOT EXISTS behaviour_samples (
    id         BIGSERIAL PRIMARY KEY,
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    payment_id UUID REFERENCES payments(id) ON DELETE CASCADE,
    features   JSONB NOT NULL,
    clean      BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS behaviour_user_clean_idx ON behaviour_samples(user_id, clean, id DESC);
ALTER TABLE behaviour_samples ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'payment';

-- Guided enrolment: opened only after a fresh passkey check, for one session, for 10 minutes.
ALTER TABLE challenges DROP CONSTRAINT IF EXISTS challenges_purpose_check;
ALTER TABLE challenges ADD CONSTRAINT challenges_purpose_check
    CHECK (purpose IN ('register', 'add_device', 'login', 'reauth'));
CREATE TABLE IF NOT EXISTS behaviour_enrolment (
    user_id    UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    added      INT NOT NULL DEFAULT 0,
    opened_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);

-- Module 3a: fraud operations. Analysts are ordinary passkey users with a role, granted by the operator.
ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'customer';
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check CHECK (role IN ('customer', 'analyst'));
-- Acting as an analyst needs a fresh passkey check; it lasts a few minutes on this session only.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS ops_elevated_until TIMESTAMPTZ;
-- (payments_status_check is defined once, below in the 3b block: the whole schema is replayed on every
--  upgrade, so an older, narrower copy of the constraint here would reject newer rows.)
ALTER TABLE payments ADD COLUMN IF NOT EXISTS reviewed_by UUID REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS review_decision TEXT;
ALTER TABLE payments ADD COLUMN IF NOT EXISTS review_note TEXT;
CREATE INDEX IF NOT EXISTS payments_held_idx ON payments(created_at) WHERE status = 'held';
-- Ledger writes go through an outbox: the analyst's decision commits locally at once, the ledger write
-- is retried until Fabric accepts or definitely refuses it, and nothing is lost if the ledger is down.
CREATE TABLE IF NOT EXISTS ledger_outbox (
    id              BIGSERIAL PRIMARY KEY,
    payment_id      UUID REFERENCES payments(id) ON DELETE SET NULL,
    id_hash         TEXT NOT NULL,
    id_type         TEXT NOT NULL CHECK (id_type IN ('ACCOUNT', 'UPI', 'PHONE')),
    severity        TEXT NOT NULL CHECK (severity IN ('LOW', 'MEDIUM', 'HIGH')),
    source          TEXT NOT NULL CHECK (source IN ('ANALYST', 'CUSTOMER_COMPLAINT', 'SANDBOX_CAPTURE')),
    evidence        JSONB NOT NULL,       -- kept off-chain; only its hash goes on the ledger
    evidence_hash   TEXT NOT NULL,
    requested_by    UUID REFERENCES users(id) ON DELETE SET NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'reported', 'confirmed', 'already_ours', 'failed')),
    attempts        INT NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_error      TEXT,
    report_id       TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    done_at         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ledger_outbox_due_idx ON ledger_outbox(next_attempt_at) WHERE status = 'pending';
CREATE UNIQUE INDEX IF NOT EXISTS ledger_outbox_one_per_payment ON ledger_outbox(payment_id) WHERE payment_id IS NOT NULL;

-- Module 3b: the customer's own step-up, and the sandbox for suspected account takeover.
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS sandboxed_at TIMESTAMPTZ;
ALTER TABLE payments DROP CONSTRAINT IF EXISTS payments_status_check;
ALTER TABLE payments ADD CONSTRAINT payments_status_check CHECK (status IN ('completed', 'held', 'rejected', 'sandboxed'));
ALTER TABLE payments ADD COLUMN IF NOT EXISTS step_up JSONB;           -- the customer's answers and the outcome
ALTER TABLE payments ADD COLUMN IF NOT EXISTS step_up_flagged BOOLEAN NOT NULL DEFAULT false;  -- scam pattern: review first

-- Slice 2b part 2: volunteer typing study. Deliberately NOT linked to users: volunteers have no bank
-- account, and no name, IP address or user agent is stored. Invites are single use; the participant code
-- is issued at join time, so whoever handed out the invite cannot link it to the data.
CREATE TABLE IF NOT EXISTS research_invites (
    code_hash  BYTEA PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    used_at    TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS research_participants (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code_hash       BYTEA NOT NULL UNIQUE,
    consent_version TEXT NOT NULL,
    keyboard        TEXT NOT NULL CHECK (keyboard IN ('laptop', 'external', 'phone')),
    joined_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS research_samples (
    id             BIGSERIAL PRIMARY KEY,
    participant_id UUID NOT NULL REFERENCES research_participants(id) ON DELETE CASCADE,
    session_no     INT NOT NULL CHECK (session_no >= 1),
    prompt_no      INT NOT NULL CHECK (prompt_no >= 0),
    features       JSONB NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS research_samples_participant_idx ON research_samples(participant_id, id);

-- Demo-only device and telecom signals for a session (the web demo cannot read SIM or call state).
CREATE TABLE IF NOT EXISTS session_signals (
    session_id UUID PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    signals    JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
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


# Bump whenever SCHEMA or OUTBOX_SCHEMA changes. Start-up runs the DDL only when the stored version is
# older: statements like ALTER TABLE ... ADD COLUMN IF NOT EXISTS take an ACCESS EXCLUSIVE lock even when
# the column exists, so running them on every start deadlocked in-flight payments (found by a flaky
# test that starts several clients while others pay; in production, a rolling restart).
SCHEMA_VERSION = 9
_MIGRATION_LOCK = 7_402_118  # advisory lock id: one process migrates at a time


def migrate(conn) -> bool:
    """Returns True if DDL ran. Serialised across processes with a session advisory lock."""
    from .audit import OUTBOX_SCHEMA  # local import: audit imports db

    conn.execute("CREATE TABLE IF NOT EXISTS schema_meta (id INT PRIMARY KEY CHECK (id = 1), version INT NOT NULL)")
    conn.commit()
    current = conn.execute("SELECT version FROM schema_meta WHERE id = 1").fetchone()
    if current and current["version"] >= SCHEMA_VERSION:
        return False
    conn.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK,))
    try:
        current = conn.execute("SELECT version FROM schema_meta WHERE id = 1").fetchone()
        if current and current["version"] >= SCHEMA_VERSION:
            return False  # another process migrated while we waited
        with conn.transaction():
            conn.execute(SCHEMA)
            conn.execute(OUTBOX_SCHEMA)
            conn.execute(
                "INSERT INTO schema_meta (id, version) VALUES (1, %s) "
                "ON CONFLICT (id) DO UPDATE SET version = EXCLUDED.version",
                (SCHEMA_VERSION,),
            )
        return True
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK,))


def init(timeout_seconds: int = 60) -> None:
    deadline = time.time() + timeout_seconds
    while True:
        try:
            ensure_database()
            with connect() as conn:
                migrate(conn)
            return
        except psycopg.OperationalError:
            if time.time() > deadline:
                raise
            time.sleep(1)
