"""Retrieval check for the copilot: do the right policy sections come back for typical analyst questions?

    python api/scripts/eval_copilot.py

The golden set below was written by the author for the author's own policy corpus, so this is an
in-sample check (it catches regressions; it is not evidence about questions nobody anticipated).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.copilot import retrieval  # noqa: E402

GOLDEN = [
    ("Why was this payment held?", "held-payment-review#when-a-payment-is-held"),
    ("Which payments should I look at first?", "held-payment-review#order-of-work"),
    ("Can I release a large first payment the customer confirmed on a call back?", "held-payment-review#when-to-release"),
    ("When should I reject a payment?", "held-payment-review#when-to-reject"),
    ("Should I report this payee to the consortium?", "held-payment-review#when-to-report-to-the-consortium"),
    ("The customer says police are on a video call and he must move money to a safe account", "scam-typologies#digital-arrest-and-impersonation-of-authorities"),
    ("Customer was told to update KYC and installed a remote support app", "scam-typologies#account-blocked-or-kyc-update"),
    ("She says money was sent to her by mistake and she has to return it", "scam-typologies#refund-reversal-or-wrong-transfer"),
    ("He earns money doing online tasks and now has to pay a deposit to withdraw", "scam-typologies#job-task-and-investment-scams"),
    ("What does a weight of 0.25 mean?", "consortium-ledger#weights-and-what-they-do-to-a-payment"),
    ("How does a report become confirmed?", "consortium-ledger#report-states"),
    ("The ledger was unavailable for this payment, is it clear?", "consortium-ledger#when-the-ledger-is-unavailable"),
    ("Why does the attacker see the payment as allowed?", "sandbox#why-we-sandbox"),
    ("Can a sandbox capture get a payee flagged everywhere?", "sandbox#sandbox-captures-on-the-ledger"),
    ("The customer answered no call but the payment showed an active call", "step-up#coached-customers"),
    ("Can the customer answer the step-up questions again?", "step-up#one-attempt"),
    ("Can I ask the customer for the OTP to verify them?", "customer-contact#what-never-to-ask"),
    ("Which number should I call the customer on?", "customer-contact#how-to-call"),
]


def evaluate(k: int = 4) -> dict:
    hits, rr = 0, 0.0
    misses = []
    for q, want in GOLDEN:
        got = [c.id for c, _ in retrieval.index().search(q, k)]
        if want in got:
            hits += 1
            rr += 1 / (got.index(want) + 1)
        else:
            misses.append((q, want, got))
    return {"questions": len(GOLDEN), "recall": hits / len(GOLDEN), "mrr": rr / len(GOLDEN), "misses": misses, "k": k}


if __name__ == "__main__":
    r = evaluate()
    print(f"{r['questions']} questions, {len(retrieval.policy_chunks())} policy sections")
    print(f"recall@{r['k']}: {r['recall']:.0%}   MRR: {r['mrr']:.2f}")
    for q, want, got in r["misses"]:
        print(f"  miss: {q!r} wanted {want}, got {got}")
