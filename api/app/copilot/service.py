"""One copilot question: build sources, answer (model or extractive), check citations, log."""
from __future__ import annotations

import uuid

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from .. import audit, db, settings
from . import llm, sources as src

RATE_PER_MINUTE = 20


def use_model() -> bool:
    return settings.COPILOT_LLM != "off" and bool(settings.ANTHROPIC_API_KEY)


def ask(session: dict, payment_id: uuid.UUID, question: str) -> dict:
    with db.connect() as conn:
        recent = conn.execute(
            "SELECT count(*) AS n FROM copilot_log WHERE analyst_id = %s AND created_at > now() - interval '1 minute'",
            (session["user_id"],),
        ).fetchone()["n"]
        if recent >= RATE_PER_MINUTE:
            raise HTTPException(429, "Too many copilot questions; wait a minute")
        sources, payment = src.build(conn, payment_id, question)
    if not sources:
        raise HTTPException(404, "No such payment")
    if payment["user_id"] == session["user_id"]:
        raise HTTPException(403, "You cannot review your own payment")

    mode, warnings = "extractive", []
    if use_model():
        try:
            raw = llm.call_claude(sources, question)
            mode = "llm"
        except Exception as exc:  # noqa: BLE001 - any model failure falls back, visibly
            raw = llm.extractive(sources)
            warnings.append(f"language model unavailable ({str(exc)[:120]}); showing the relevant passages instead")
    else:
        raw = llm.extractive(sources)
    answer, used, cite_warnings = llm.check_citations(raw, sources, each_sentence=mode == "llm")
    warnings += cite_warnings
    result = {
        "mode": mode,
        "answer": answer,
        "unverified": not used,
        "warnings": warnings,
        "sources": [
            {"n": s.n, "kind": s.kind, "id": s.id, "title": s.title, "text": s.text, "cited": s.n in used}
            for s in sources
        ],
    }
    with db.connect() as conn, conn.transaction():
        conn.execute(
            "INSERT INTO copilot_log (analyst_id, payment_id, question, mode, answer, source_ids, warnings) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (session["user_id"], payment_id, question, mode, answer, [s.id for s in sources], Jsonb(warnings)),
        )
        audit.enqueue(conn, "COPILOT_QUERY", {"analystId": str(session["user_id"]), "paymentId": str(payment_id),
                                              "mode": mode, "sources": len(sources), "cited": used})
    return result
