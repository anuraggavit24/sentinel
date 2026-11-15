"""Explainable, rule-based risk scoring (slice 2a). Pure functions: no database, no clock.

Every point in the score comes from a named reason, so the decision can be shown to the customer,
the fraud analyst and an auditor. Slice 2b adds ML models; these rules stay as the safety net.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Optional

ALLOW, STEP_UP, SANDBOX = "allow", "step_up", "sandbox"
STEP_UP_AT = 40
SANDBOX_AT = 70

FRI_POINTS = {"MEDIUM": 15, "HIGH": 30, "VERY_HIGH": 45}


@dataclass
class Signals:
    amount_paise: int
    hour_ist: int                         # 0-23
    payee_type: str
    payee_seen_before: bool
    history_paise: list[int] = field(default_factory=list)   # this user's previous payment amounts
    payments_last_10_min: int = 0
    fri_category: Optional[str] = None    # None | MEDIUM | HIGH | VERY_HIGH (payee mobile number)
    # Device and session context. In the web demo these come from the signal injector.
    sim_swap_hours_ago: Optional[float] = None
    new_device: bool = False
    integrity: str = "PASS"               # PASS | FAIL | UNKNOWN
    active_call: bool = False
    screen_sharing: bool = False
    accessibility_services: bool = False
    synced_passkey: bool = False


@dataclass
class Reason:
    code: str
    points: int
    text: str
    group: str        # TRANSACTION | DEVICE | COACHING | TELECOM

    def as_dict(self) -> dict:
        return {"code": self.code, "points": self.points, "text": self.text, "group": self.group}


@dataclass
class Decision:
    score: int
    band: str
    reasons: list[Reason]
    takeover_signals: bool
    coaching_signals: bool

    def as_dict(self) -> dict:
        return {
            "score": self.score,
            "band": self.band,
            "reasons": [r.as_dict() for r in self.reasons],
            "takeoverSignals": self.takeover_signals,
            "coachingSignals": self.coaching_signals,
        }


def rupees(paise: int) -> str:
    r = paise / 100
    return f"₹{r:,.0f}" if r == int(r) else f"₹{r:,.2f}"


def score(s: Signals) -> Decision:
    reasons: list[Reason] = []

    def add(code: str, points: int, text: str, group: str) -> None:
        reasons.append(Reason(code, points, text, group))

    # ---- transaction
    if not s.payee_seen_before:
        add("NEW_PAYEE", 15, "First payment to this payee", "TRANSACTION")
    if len(s.history_paise) >= 5:
        usual = median(s.history_paise)
        ratio = s.amount_paise / max(usual, 1)
        if ratio >= 10:
            add("AMOUNT_FAR_ABOVE_USUAL", 25, f"{ratio:.0f}× your usual payment of {rupees(int(usual))}", "TRANSACTION")
        elif ratio >= 3:
            add("AMOUNT_ABOVE_USUAL", 12, f"{ratio:.1f}× your usual payment of {rupees(int(usual))}", "TRANSACTION")
    elif s.amount_paise >= 50_000_00:
        add("LARGE_EARLY_PAYMENT", 15, f"Large payment ({rupees(s.amount_paise)}) with little payment history", "TRANSACTION")
    if s.amount_paise >= 2_00_000_00:
        add("VERY_LARGE_AMOUNT", 10, f"Amount above {rupees(2_00_000_00)}", "TRANSACTION")
    if 0 <= s.hour_ist < 5:
        add("NIGHT_TIME", 8, "Payment between midnight and 5 am", "TRANSACTION")
    if s.payments_last_10_min >= 3:
        add("RAPID_PAYMENTS", 15, f"{s.payments_last_10_min} payments in the last 10 minutes", "TRANSACTION")

    # ---- telecom
    if s.fri_category in FRI_POINTS:
        add(
            f"FRI_{s.fri_category}",
            FRI_POINTS[s.fri_category],
            f"Payee's mobile number is rated {s.fri_category.replace('_', ' ').title()} risk by DoT's FRI",
            "TELECOM",
        )
    takeover = False
    if s.sim_swap_hours_ago is not None and s.sim_swap_hours_ago < 72:
        recent = s.sim_swap_hours_ago < 24
        add(
            "RECENT_SIM_SWAP",
            40 if recent else 30,
            f"Your SIM was changed {s.sim_swap_hours_ago:.0f} hours ago",
            "TELECOM",
        )
        takeover = True

    # ---- device
    if s.new_device:
        add("NEW_DEVICE", 15, "Signed in from a device not seen before", "DEVICE")
    if s.integrity == "FAIL":
        add("INTEGRITY_FAILED", 30, "Device or app integrity check failed", "DEVICE")
        takeover = True
    if s.synced_passkey and s.new_device:
        add("SYNCED_PASSKEY_NEW_DEVICE", 10, "Synced passkey used on a new device", "DEVICE")

    # ---- coaching: the real customer, talked into paying a scammer
    coaching = False
    if s.active_call:
        add("ACTIVE_CALL", 20, "A phone call is active during the payment", "COACHING")
        coaching = True
        if not s.payee_seen_before and s.amount_paise >= 10_000_00:
            add("CALL_NEW_PAYEE_LARGE", 15, "On a call, paying a new payee more than ₹10,000", "COACHING")
    if s.screen_sharing:
        add("SCREEN_SHARING", 25, "The screen is being shared or recorded", "COACHING")
        coaching = True
    if s.accessibility_services:
        add("ACCESSIBILITY_SERVICE", 10, "An accessibility service that can control the screen is on", "COACHING")
        coaching = True

    total = min(sum(r.points for r in reasons), 100)

    # The sandbox shows a fake balance, so it is only for high-confidence account takeover:
    # a takeover signal (SIM swap or failed integrity) on a new device, with a high score.
    # Coached customers are real customers: they get a warning and step-up, never the sandbox.
    if total >= SANDBOX_AT and takeover and s.new_device:
        band = SANDBOX
    elif total >= STEP_UP_AT:
        band = STEP_UP
    else:
        band = ALLOW

    reasons.sort(key=lambda r: -r.points)
    return Decision(total, band, reasons, takeover, coaching)
