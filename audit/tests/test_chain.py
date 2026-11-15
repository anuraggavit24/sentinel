from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.chain import GENESIS, Row, canonical, entry_hash, sign, verify_rows

KEY = Ed25519PrivateKey.from_private_bytes(bytes.fromhex("22" * 32))
PUB = KEY.public_key()


def build(n: int, stream: str = "s1") -> list[Row]:
    rows, prev = [], GENESIS
    for i in range(n):
        payload = canonical({"n": i, "decision": "ALLOW"})
        ts = f"2026-11-15T10:00:0{i}.000000+00:00"
        h = entry_hash(stream, i, ts, "RISK_DECISION", payload, prev)
        rows.append(Row(stream, i, ts, "RISK_DECISION", payload, prev, h, sign(KEY, h)))
        prev = h
    return rows


def test_valid_chain_matches_its_anchor():
    rows = build(5)
    res = verify_rows(rows, PUB, anchor=rows[-1].hash)
    assert res.chain_valid and res.anchor_matches and res.anchored_idx == 4
    assert res.recomputed_root == rows[-1].hash


def test_anchor_taken_earlier_still_proves_the_prefix():
    rows = build(6)
    res = verify_rows(rows, PUB, anchor=rows[3].hash)
    assert res.chain_valid and res.anchor_matches and res.anchored_idx == 3


def test_edited_content_is_caught_at_the_edited_entry():
    rows = build(5)
    rows[2].payload = rows[2].payload.replace("ALLOW", "BLOCK")
    res = verify_rows(rows, PUB, anchor=build(5)[-1].hash)
    assert not res.chain_valid and res.first_bad_idx == 2
    assert "content" in res.reason
    assert res.anchor_matches is False


def test_deleted_entry_is_caught():
    rows = build(5)
    del rows[2]
    res = verify_rows(rows, PUB)
    assert not res.chain_valid and res.first_bad_idx == 2


def test_rehashed_without_the_key_fails_the_signature():
    rows = build(4)
    rows[1].payload = canonical({"n": 1, "decision": "BLOCK"})
    rows[1].hash = entry_hash(rows[1].stream, 1, rows[1].ts, rows[1].event_type, rows[1].payload, rows[1].prev_hash)
    res = verify_rows(rows, PUB)
    assert not res.chain_valid and res.first_bad_idx == 1


def test_insider_with_key_passes_internal_checks_but_fails_the_anchor():
    original = build(5)
    anchor = original[-1].hash
    rows = build(5)
    prev = rows[1].hash
    for r in rows[2:]:
        if r.idx == 2:
            r.payload = canonical({"n": 2, "decision": "BLOCK"})
        r.prev_hash = prev
        r.hash = entry_hash(r.stream, r.idx, r.ts, r.event_type, r.payload, prev)
        r.sig = sign(KEY, r.hash)
        prev = r.hash
    res = verify_rows(rows, PUB, anchor=anchor)
    assert res.chain_valid is True
    assert res.anchor_matches is False


def test_canonical_json_is_order_independent():
    assert canonical({"b": 1, "a": [1, {"d": 2, "c": 3}]}) == canonical({"a": [1, {"c": 3, "d": 2}], "b": 1})
