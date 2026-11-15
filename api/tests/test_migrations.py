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
