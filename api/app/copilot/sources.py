"""The sources a copilot answer may draw on: the case itself, the ledger (as scored and live now), this
bank's earlier decisions on the same payee, and the retrieved policy sections. All text sent onward is
already minimised: masked payee and customer, no identifiers, no IP addresses, no device details."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .. import ledger
from ..risk.rules import rupees
from . import retrieval


@dataclass
class Source:
    n: int
    kind: str      # case | ledger | history | policy
    id: str
    title: str
    text: str


def _mask(name: str) -> str:
    return (name[:1] + "•••") if name else "•••"


def _age(ts: datetime) -> str:
    mins = int((datetime.now(timezone.utc) - ts).total_seconds() // 60)
    if mins < 1:
        return "just now"
    return f"{mins} minutes ago" if mins < 120 else f"{mins // 60} hours ago"


def _ledger_line(snap: dict | None) -> str:
    if not snap:
        return "not asked"
    st = snap.get("status")
    if st == "disabled":
        return "ledger not connected"
    if st == "unavailable":
        return f"ledger unavailable ({snap.get('error', 'no answer')}); treat as unknown, not as clear"
    if st == "clear":
        return "no reports about this payee"
    reps = "; ".join(
        f"{r['status']} report by {r['reporterOrg']} (source {r['source']}, severity {r['severity']}, "
        f"{r['nonSandboxBanks']} non-sandbox bank(s), weight {r['weight']})"
        for r in snap.get("reports", [])
    )
    return f"max weight {snap.get('maxWeight')}, confirmed: {'yes' if snap.get('hardRule') else 'no'}; {reps}"


def case_source(p: dict, customer_name: str, other_customers: int) -> str:
    reasons = "; ".join(f"+{r['points']} {r['code']}: {r['text']}" for r in p["reasons"]) or "none"
    parts = [
        f"Payment of {rupees(p['amount_paise'])} to {p['payee_display']} ({p['payee_type']}) by customer "
        f"{_mask(customer_name)}, started {_age(p['created_at'])}.",
        f"Status: {p['status']}. Risk score {p['score']} (band {p['band']}).",
        f"Reasons: {reasons}.",
        f"Coaching signals during the payment (active call or screen sharing): {'yes' if p['coaching'] else 'no'}.",
        f"Other customers of this bank who tried to pay this payee in the last 7 days: {other_customers}.",
    ]
    su = p.get("step_up")
    if su:
        a = su["answers"]
        parts.append(
            "Customer step-up answers: someone on a call asked them to pay: "
            f"{'yes' if a['onCall'] else 'no'}; promised a refund, prize, job or told account at risk: "
            f"{'yes' if a['promised'] else 'no'}; knows the payee: {'yes' if a['knowsPayee'] else 'no'}. "
            + (f"Kept held because: {'; '.join(su['blockers'])}." if su["blockers"] else "Released by the customer.")
        )
    else:
        parts.append("Customer step-up: not answered.")
    return " ".join(parts)


def build(conn, payment_id, question: str, k_policy: int = 4) -> tuple[list[Source], dict]:
    p = conn.execute(
        "SELECT p.*, u.display_name, "
        "  (SELECT count(DISTINCT q.user_id) FROM payments q WHERE q.payee_hash = p.payee_hash "
        "     AND q.user_id <> p.user_id AND q.created_at > now() - interval '7 days') AS other_customers "
        "FROM payments p JOIN users u ON u.id = p.user_id WHERE p.id = %s",
        (payment_id,),
    ).fetchone()
    if p is None:
        return [], {}
    sources: list[Source] = []

    def add(kind, sid, title, text):
        sources.append(Source(len(sources) + 1, kind, sid, title, text))

    add("case", f"case:{payment_id}", "This case", case_source(p, p["display_name"], p["other_customers"]))

    live = ledger.query_payee([p["payee_hash"]]).snapshot()
    scored = (p["signals"] or {}).get("consortium")
    add("ledger", f"ledger:{p['payee_hash'][:12]}", "Consortium ledger",
        f"When the payment was scored: {_ledger_line(scored)}. Asked again now: {_ledger_line(live)}.")

    hist = conn.execute(
        "SELECT status, review_decision, review_note, amount_paise, created_at FROM payments "
        "WHERE payee_hash = %s AND id <> %s ORDER BY created_at DESC LIMIT 6",
        (p["payee_hash"], payment_id),
    ).fetchall()
    if hist:
        lines = []
        for h in hist:
            line = f"{rupees(h['amount_paise'])} {_age(h['created_at'])}: {h['status']}"
            if h["review_decision"]:
                line += f", decision {h['review_decision']}"
            if h["review_note"]:
                line += f", analyst note: {h['review_note'][:200]}"
            lines.append(line)
        add("history", f"history:{p['payee_hash'][:12]}", "Earlier payments to this payee at our bank", "; ".join(lines) + ".")
    else:
        add("history", f"history:{p['payee_hash'][:12]}", "Earlier payments to this payee at our bank",
            "No earlier payments to this payee at our bank.")

    query = retrieval.expand(question, [r["code"] for r in p["reasons"]], p["step_up_flagged"])
    for chunk, _score in retrieval.index().search(query, k_policy):
        add("policy", chunk.id, chunk.title, chunk.text)
    return sources, p
