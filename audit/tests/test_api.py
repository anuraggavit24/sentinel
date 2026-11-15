"""Integration tests against a real Postgres. Set DATABASE_URL to run them; otherwise they are skipped."""
import os
import uuid

import psycopg
import pytest

pytestmark = pytest.mark.skipif("DATABASE_URL" not in os.environ, reason="needs a real Postgres (DATABASE_URL)")


@pytest.fixture(scope="module")
def client():
    os.environ["DEMO_INSIDER"] = "1"
    from fastapi.testclient import TestClient
    from app import main

    main.DEMO_INSIDER = True
    with TestClient(main.app) as c:
        yield c


def append_n(client, stream, n):
    for i in range(n):
        r = client.post("/events", json={"stream": stream, "eventType": "RISK_DECISION", "payload": {"i": i, "decision": "ALLOW"}})
        assert r.status_code == 201, r.text
    return client.get(f"/streams/{stream}/root").json()["root"]


def test_append_builds_a_linked_chain(client):
    s = f"t-{uuid.uuid4().hex[:8]}"
    a = client.post("/events", json={"stream": s, "eventType": "X", "payload": {"k": 1}}).json()
    b = client.post("/events", json={"stream": s, "eventType": "X", "payload": {"k": 2}}).json()
    assert a["idx"] == 0 and a["prevHash"] == "0" * 64
    assert b["idx"] == 1 and b["prevHash"] == a["hash"]


def test_verify_passes_and_matches_anchor(client):
    s = f"t-{uuid.uuid4().hex[:8]}"
    root = append_n(client, s, 5)
    v = client.get(f"/streams/{s}/verify", params={"anchor": root}).json()
    assert v["chainValid"] and v["anchorMatches"] and v["anchoredIdx"] == 4


def test_direct_database_edit_is_detected(client):
    s = f"t-{uuid.uuid4().hex[:8]}"
    root = append_n(client, s, 5)
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute("UPDATE audit_log SET payload = replace(payload, 'ALLOW', 'BLOCK') WHERE stream = %s AND idx = 2", (s,))
    v = client.get(f"/streams/{s}/verify", params={"anchor": root}).json()
    assert v["chainValid"] is False and v["firstBadIdx"] == 2 and v["anchorMatches"] is False


def test_insider_rewrite_only_caught_by_anchor(client):
    s = f"t-{uuid.uuid4().hex[:8]}"
    root = append_n(client, s, 5)
    r = client.post("/demo/insider-rewrite", json={"stream": s, "idx": 2, "payload": {"i": 2, "decision": "BLOCK"}})
    assert r.status_code == 200
    v = client.get(f"/streams/{s}/verify", params={"anchor": root}).json()
    assert v["chainValid"] is True and v["anchorMatches"] is False


def test_bad_stream_name_and_empty_stream(client):
    assert client.post("/events", json={"stream": "bad name!", "eventType": "X", "payload": {}}).status_code == 422
    assert client.get("/streams/nothing-here-123/root").status_code == 404
