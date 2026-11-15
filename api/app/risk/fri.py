"""MOCK of DoT's Financial Fraud Risk Indicator. Live FRI is only available to regulated entities via
DoT's Digital Intelligence Platform, so these demo numbers stand in for it and the UI says so."""
from __future__ import annotations

import re
from typing import Optional

DEMO_FRI = {
    "+919999900001": "MEDIUM",
    "+919999900002": "HIGH",
    "+919999900003": "VERY_HIGH",
}
_UPI_MOBILE = re.compile(r"^([6-9][0-9]{9})@")


def lookup(phone_e164: Optional[str]) -> Optional[str]:
    return DEMO_FRI.get(phone_e164) if phone_e164 else None


def phone_from_upi(vpa: str) -> Optional[str]:
    """Many UPI IDs are the mobile number (9876543210@ybl). FRI applies to those too."""
    m = _UPI_MOBILE.match(vpa)
    return f"+91{m.group(1)}" if m else None
