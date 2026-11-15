"""Pure hash-chain logic: hashing, signing and verification. No database code here."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

GENESIS = "0" * 64


def canonical(obj: object) -> str:
    """Deterministic JSON: sorted keys, no whitespace, UTF-8 kept as-is."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def entry_hash(stream: str, idx: int, ts: str, event_type: str, payload: str, prev_hash: str) -> str:
    """SHA-256 over every field of the entry, including the previous entry's hash (the chain link)."""
    material = canonical(
        {"stream": stream, "idx": idx, "ts": ts, "eventType": event_type, "payload": payload, "prevHash": prev_hash}
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def sign(private_key: Ed25519PrivateKey, hash_hex: str) -> str:
    return private_key.sign(bytes.fromhex(hash_hex)).hex()


def signature_ok(public_key: Ed25519PublicKey, hash_hex: str, sig_hex: str) -> bool:
    try:
        public_key.verify(bytes.fromhex(sig_hex), bytes.fromhex(hash_hex))
        return True
    except (InvalidSignature, ValueError):
        return False


@dataclass
class Row:
    stream: str
    idx: int
    ts: str
    event_type: str
    payload: str
    prev_hash: str
    hash: str
    sig: str


@dataclass
class VerifyResult:
    entries: int
    chain_valid: bool
    first_bad_idx: Optional[int]
    reason: Optional[str]
    recomputed_root: Optional[str]
    anchor: Optional[str]
    anchor_matches: Optional[bool]
    anchored_idx: Optional[int]

    def as_dict(self) -> dict:
        return {
            "entries": self.entries,
            "chainValid": self.chain_valid,
            "firstBadIdx": self.first_bad_idx,
            "reason": self.reason,
            "recomputedRoot": self.recomputed_root,
            "anchor": self.anchor,
            "anchorMatches": self.anchor_matches,
            "anchoredIdx": self.anchored_idx,
        }


def verify_rows(rows: Iterable[Row], public_key: Ed25519PublicKey, anchor: Optional[str] = None) -> VerifyResult:
    """
    Two independent checks:
    1. Internal integrity: every link, hash and signature as stored. Catches anyone without the signing key.
    2. Anchor match: recompute the chain purely from content and look for the on-chain root.
       Catches an insider who rewrote history and re-signed it, because the anchor lives on the ledger.
    """
    rows = list(rows)
    first_bad: Optional[int] = None
    reason: Optional[str] = None
    stored_prev = GENESIS
    recomputed_prev = GENESIS
    recomputed: list[str] = []

    for expected_idx, r in enumerate(rows):
        content_hash = entry_hash(r.stream, r.idx, r.ts, r.event_type, r.payload, r.prev_hash)
        if first_bad is None:
            if r.idx != expected_idx:
                first_bad, reason = expected_idx, "missing or reordered entry"
            elif r.prev_hash != stored_prev:
                first_bad, reason = r.idx, "link to previous entry is broken"
            elif content_hash != r.hash:
                first_bad, reason = r.idx, "entry content does not match its stored hash"
            elif not signature_ok(public_key, r.hash, r.sig):
                first_bad, reason = r.idx, "signature is invalid"
        stored_prev = r.hash

        h = entry_hash(r.stream, r.idx, r.ts, r.event_type, r.payload, recomputed_prev)
        recomputed.append(h)
        recomputed_prev = h

    anchor_matches: Optional[bool] = None
    anchored_idx: Optional[int] = None
    if anchor is not None:
        anchor_matches = anchor in recomputed
        if anchor_matches:
            anchored_idx = recomputed.index(anchor)

    return VerifyResult(
        entries=len(rows),
        chain_valid=first_bad is None,
        first_bad_idx=first_bad,
        reason=reason,
        recomputed_root=recomputed[-1] if recomputed else None,
        anchor=anchor,
        anchor_matches=anchor_matches,
        anchored_idx=anchored_idx,
    )
