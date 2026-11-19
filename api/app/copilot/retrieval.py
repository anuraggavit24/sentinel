"""Lexical retrieval (BM25) over the policy playbook. No embeddings service: deterministic, testable,
and nothing leaves the bank to retrieve. Chunks are the '## ' sections of the policy documents."""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

POLICY_DIR = Path(__file__).parent / "policies"
STOP = set("""a an the and or of to in on for is are be been was were it its this that with as at by from
not no do does did can could should would will may might you your we our they their them he she his her
if then than so such which what who whom when where why how about into over after before any all each
there here have has had also only just more most other some very""".split())


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > 4 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def tokens(text: str) -> list[str]:
    return [_stem(w) for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP and len(w) > 1]


@dataclass(frozen=True)
class Chunk:
    id: str          # file#section-slug
    title: str       # "Document title › Section"
    text: str


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


@lru_cache(maxsize=1)
def policy_chunks() -> tuple[Chunk, ...]:
    out = []
    for path in sorted(POLICY_DIR.glob("*.md")):
        doc_title, section, buf = path.stem, None, []
        for line in path.read_text(encoding="utf-8").splitlines() + ["## __end__"]:
            if line.startswith("# "):
                doc_title = line[2:].strip()
            elif line.startswith("## "):
                if section and buf:
                    out.append(Chunk(f"{path.stem}#{_slug(section)}", f"{doc_title} › {section}", " ".join(buf).strip()))
                section, buf = line[3:].strip(), []
            elif section and line.strip():
                buf.append(line.strip())
    return tuple(out)


class BM25:
    def __init__(self, chunks: tuple[Chunk, ...], k1: float = 1.4, b: float = 0.75):
        self.chunks = chunks
        self.docs = [tokens(c.title + " " + c.text) for c in chunks]
        self.tf = [Counter(d) for d in self.docs]
        self.avg = sum(len(d) for d in self.docs) / max(1, len(self.docs))
        df = Counter(t for d in self.docs for t in set(d))
        n = len(self.docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.k1, self.b = k1, b

    def search(self, query: str, k: int = 4) -> list[tuple[Chunk, float]]:
        q = tokens(query)
        scored = []
        for i, tf in enumerate(self.tf):
            s = 0.0
            for t in q:
                if t in tf:
                    f = tf[t]
                    s += self.idf[t] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * len(self.docs[i]) / self.avg))
            if s > 0:
                scored.append((self.chunks[i], s))
        scored.sort(key=lambda x: (-x[1], x[0].id))
        return scored[:k]


@lru_cache(maxsize=1)
def index() -> BM25:
    return BM25(policy_chunks())


# Reason codes from the risk engine, translated into the words the playbook uses, so a question like
# "what should I do?" still retrieves the sections about this case's actual signals.
EXPANSION = {
    "ACTIVE_CALL": "active call coaching scam digital arrest",
    "CALL_NEW_PAYEE_LARGE": "active call large first payment new payee digital arrest",
    "SCREEN_SHARING": "screen sharing remote support KYC",
    "ACCESSIBILITY_SERVICE": "remote control app screen KYC",
    "RECENT_SIM_SWAP": "SIM swap takeover sandbox",
    "NEW_DEVICE": "new device takeover",
    "SYNCED_PASSKEY_NEW_DEVICE": "new device takeover",
    "INTEGRITY_FAILED": "device integrity takeover sandbox",
    "CONSORTIUM_CONFIRMED": "CONFIRMED consortium ledger reject report",
    "CONSORTIUM_REPORTED": "consortium ledger report pending weight",
    "NEW_PAYEE": "new payee first payment",
    "LARGE_EARLY_PAYMENT": "large first payment",
    "VERY_LARGE_AMOUNT": "large amount",
    "RAPID_PAYMENTS": "many small payments quick succession",
    "SPLIT_PAYMENTS": "many small payments quick succession",
    "UNTRUSTED_PAYEES_TOTAL": "repeated payments new payees growing amounts",
    "BEHAVIOUR_ANOMALY": "typing rhythm unlike customer takeover",
    "TELECOM": "telecom mule",
}


def expand(question: str, reason_codes: list[str], scam_pattern: bool) -> str:
    extra = [EXPANSION[c] for c in reason_codes if c in EXPANSION]
    if scam_pattern:
        extra.append("step-up answers scam pattern call promised")
    return question + " " + " ".join(extra)
