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
TRUST_MIN_PAISE = 500_00          # a payee is trusted only after a completed payment of at least ₹500 ...
TRUST_AGE_HOURS = 24              # ... made more than 24 hours ago, with no coaching signals
SPLIT_TOTAL_PAISE = 10_000_00     # several payments to one untrusted payee adding up to ₹10,000 in 24 h
UNTRUSTED_TOTAL_PAISE = 50_000_00 # ₹50,000 in 24 h to payees you do not trust yet
BEHAVIOUR_MEDIUM = 0.3
BEHAVIOUR_HIGH = 0.5


@dataclass
class Signals:
    amount_paise: int
    hour_ist: int                         # 0-23
    payee_type: str
    # Trusted = a completed payment of at least TRUST_MIN_PAISE, made without coaching signals, more than
    # 24 hours ago. A ₹1 "verification" payment, or anything sent while on a call, never creates trust.
    payee_trusted: bool
    payee_paid_recently: bool = False      # some completed payment to this payee in the last 24 hours
    payee_total_24h_paise: int = 0          # earlier amounts actually SENT (completed) to this payee in 24 h
    untrusted_total_24h_paise: int = 0      # earlier amounts actually SENT to untrusted payees in 24 h
    history_paise: list[int] = field(default_factory=list)   # this user's previous payment amounts
    payments_last_10_min: int = 0         # attempts, held or not: a burst of attempts is itself a signal
    fri_category: Optional[str] = None    # None | MEDIUM | HIGH | VERY_HIGH (payee mobile number)
    # Device and session context. In the web demo these come from the signal injector.
    sim_swap_hours_ago: Optional[float] = None
    new_device: bool = False
    integrity: str = "PASS"               # PASS | FAIL | UNKNOWN
    active_call: bool = False
    screen_sharing: bool = False
    accessibility_services: bool = False
    synced_passkey: bool = False
    # Behaviour model (slice 2b): None while the user's profile is still learning.
    behaviour_anomaly: Optional[float] = None
    behaviour_features: list[str] = field(default_factory=list)
    # True when the user has a behaviour profile but this payment came without usable behaviour data
    # (everything pasted, a scripted API call, or a rejected payload). Must not be a free pass.
    behaviour_missing: bool = False


@dataclass
class Reason:
    code: str
    points: int
    text: str                       # analyst wording: may contain details a customer must not see
    group: str                      # TRANSACTION | DEVICE | COACHING | TELECOM
    customer_code: str = ""
    customer_text: str = ""

    def as_dict(self) -> dict:
        """Full detail, for the stored record and the analyst view."""
        return {"code": self.code, "points": self.points, "text": self.text, "group": self.group}

    def customer_dict(self) -> dict:
        """No points and no FRI category: otherwise the payment form becomes an FRI lookup tool."""
        return {
            "code": self.customer_code or self.code,
            "text": self.customer_text or self.text,
            "group": self.group,
        }


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

    def customer_reasons(self) -> list[dict]:
        seen, out = set(), []
        for r in self.reasons:
            d = r.customer_dict()
            if d["code"] not in seen:
                seen.add(d["code"])
                out.append(d)
        return out


def rupees(paise: int) -> str:
    r = paise / 100
    return f"₹{r:,.0f}" if r == int(r) else f"₹{r:,.2f}"


def score(s: Signals) -> Decision:
    reasons: list[Reason] = []

    def add(code: str, points: int, text: str, group: str, customer_code: str = "", customer_text: str = "") -> None:
        reasons.append(Reason(code, points, text, group, customer_code, customer_text))

    # ---- transaction
    payee_running_total = s.payee_total_24h_paise + s.amount_paise
    if not s.payee_trusted:
        if s.payee_paid_recently:
            add("NEW_PAYEE", 15, "Payee first paid less than 24 hours ago, so not trusted yet", "TRANSACTION")
        else:
            add("NEW_PAYEE", 15, "First payment to this payee", "TRANSACTION")
        if s.payee_total_24h_paise > 0 and payee_running_total >= SPLIT_TOTAL_PAISE:
            add(
                "SPLIT_PAYMENTS",
                15,
                f"{rupees(payee_running_total)} sent to this payee across several payments in 24 hours",
                "TRANSACTION",
            )
        untrusted_running = s.untrusted_total_24h_paise + s.amount_paise
        if untrusted_running >= UNTRUSTED_TOTAL_PAISE:
            add(
                "UNTRUSTED_PAYEES_TOTAL",
                12,
                f"{rupees(untrusted_running)} sent to payees you have not paid before, in 24 hours",
                "TRANSACTION",
            )
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
        add("RAPID_PAYMENTS", 15, f"{s.payments_last_10_min} payment attempts in the last 10 minutes", "TRANSACTION")

    # ---- telecom
    if s.fri_category in FRI_POINTS:
        add(
            f"FRI_{s.fri_category}",
            FRI_POINTS[s.fri_category],
            f"Payee's mobile number is rated {s.fri_category.replace('_', ' ').title()} risk by DoT's FRI",
            "TELECOM",
            customer_code="TELECOM_FLAG",
            customer_text="This payee's mobile number has been flagged by telecom fraud-risk signals",
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

    # ---- behaviour: typing and mouse rhythm compared with this user's own clean history
    # Thresholds from scripts/eval_behaviour.py (rhythm features only, chosen on seed 11, synthetic users):
    # >= 0.3 flags ~0.5% of genuine attempts and ~84% of other people; >= 0.5 flags ~0% and ~55%.
    if s.behaviour_anomaly is not None and s.behaviour_anomaly >= BEHAVIOUR_MEDIUM:
        strong = s.behaviour_anomaly >= BEHAVIOUR_HIGH
        what = ", ".join(s.behaviour_features[:2]) or "typing and mouse rhythm"
        add(
            "BEHAVIOUR_ANOMALY_HIGH" if strong else "BEHAVIOUR_ANOMALY",
            20 if strong else 10,
            f"Typing and mouse use differ from your usual pattern ({what}; anomaly {s.behaviour_anomaly:.2f})",
            "DEVICE",
            customer_code="BEHAVIOUR_UNUSUAL",
            customer_text="The way this form was filled in is unusual for you",
        )

    if s.behaviour_missing:
        add(
            "BEHAVIOUR_MISSING",
            10,
            "No usable typing data although this user has a behaviour profile (pasted, scripted or rejected)",
            "DEVICE",
            customer_code="BEHAVIOUR_UNCONFIRMED",
            customer_text="We could not confirm it is you from how the form was filled in",
        )

    # ---- coaching: the real customer, talked into paying a scammer
    coaching = False
    if s.active_call:
        add("ACTIVE_CALL", 20, "A phone call is active during the payment", "COACHING")
        coaching = True
        # Cumulative, so splitting ₹30,000 into three ₹9,999 payments does not avoid it.
        if not s.payee_trusted and payee_running_total >= SPLIT_TOTAL_PAISE:
            add(
                "CALL_NEW_PAYEE_LARGE",
                15,
                f"On a call, sending {rupees(payee_running_total)} in 24 hours to a payee you do not trust yet",
                "COACHING",
            )
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
