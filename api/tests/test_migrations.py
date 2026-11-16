"""Start-up must not take table locks once the schema is current. Needs DATABASE_URL."""
import os
import threading

import psycopg
import pytest

pytestmark = pytest.mark.skipif("DATABASE_URL" not in os.environ, reason="needs a real Postgres (DATABASE_URL)")


def test_second_start_runs_no_ddl():
    from app import db

    db.init()
    with db.connect() as conn:
        assert db.migrate(conn) is False


def test_startup_does_not_block_on_a_busy_payments_table():
    """A long transaction holds a lock on payments (like an in-flight payment). A new process starting
    up must not wait for it: before the fix, ALTER TABLE ... IF NOT EXISTS queued behind it."""
    from app import db

    db.init()
    holder = psycopg.connect(os.environ["DATABASE_URL"])
    holder.execute("LOCK TABLE payments IN ROW EXCLUSIVE MODE")  # what an INSERT holds
    done = threading.Event()

    def start():
        db.init()
        done.set()

    t = threading.Thread(target=start, daemon=True)
    t.start()
    finished = done.wait(timeout=5)
    holder.rollback()
    holder.close()
    t.join(timeout=5)
    assert finished, "start-up waited for a lock held by an in-flight transaction"


def test_old_database_without_schema_meta_is_migrated():
    from app import db

    db.init()
    with db.connect() as conn:
        conn.execute("DROP TABLE schema_meta")
        conn.commit()
        assert db.migrate(conn) is True
        assert conn.execute("SELECT version FROM schema_meta").fetchone()["version"] == db.SCHEMA_VERSION


def test_replaying_the_schema_keeps_rows_of_every_status():
    """Found in 3b: the schema is replayed in full on every upgrade. An older copy of a CHECK constraint
    (status IN completed/held/rejected) ran before the newer one and failed on 'sandboxed' rows, which
    would have broken the next upgrade of any database that had seen a sandboxed payment."""
    from app import db

    with db.connect() as conn:
        user = conn.execute("INSERT INTO users (username, display_name, webauthn_user_id) "
                            "VALUES (%s, 'Replay', %s) RETURNING id",
                            (f"replay{os.urandom(4).hex()}", os.urandom(16))).fetchone()["id"]
        for status in ("completed", "held", "rejected", "sandboxed"):
            conn.execute(
                "INSERT INTO payments (user_id, payee_type, payee_hash, payee_display, amount_paise, score, band, status, "
                "reasons, signals) VALUES (%s, 'UPI', 'h', 'd', 100, 0, 'allow', %s, '[]', '{}')", (user, status))
        conn.execute("UPDATE schema_meta SET version = 0")
        assert db.migrate(conn) is True   # the full schema again, over rows of every status
        assert conn.execute("SELECT count(*) AS n FROM payments WHERE user_id = %s", (user,)).fetchone()["n"] == 4
