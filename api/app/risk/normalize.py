"""Payee identifiers: validation, normalisation and keyed hashing.

The same normalisation and HMAC key are used for the consortium ledger (scenario 4, slice 2c), so a
payee hashed here matches the idHash other banks reported. Raw identifiers are never stored; only
the hash and a masked display string.
"""
from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass

IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
ACCOUNT_RE = re.compile(r"^[0-9]{9,18}$")
UPI_RE = re.compile(r"^[a-z0-9._-]{2,256}@[a-z][a-z0-9]{1,63}$")
MOBILE_RE = re.compile(r"^[6-9][0-9]{9}$")


class PayeeError(ValueError):
    """Raised with a message that is safe to show to the user."""


@dataclass(frozen=True)
class Payee:
    type: str          # ACCOUNT | UPI | PHONE
    normalised: str    # e.g. "HDFC0000123|50100012345678", "anurag@okhdfc", "+919876543210"
    display: str       # masked, safe to show and store

    def id_hash(self, key: str) -> str:
        return hmac.new(key.encode(), self.normalised.encode(), hashlib.sha256).hexdigest()

    @property
    def phone(self) -> str | None:
        return self.normalised if self.type == "PHONE" else None


def _mask(s: str, keep: int = 4) -> str:
    return ("•" * max(len(s) - keep, 0)) + s[-keep:]


def normalise_account(ifsc: str, account: str) -> Payee:
    ifsc_n = re.sub(r"\s+", "", ifsc or "").upper()
    acct_n = re.sub(r"[\s-]+", "", account or "")
    if not IFSC_RE.match(ifsc_n):
        raise PayeeError("IFSC must look like HDFC0001234 (4 letters, a zero, then 6 letters or digits)")
    if not ACCOUNT_RE.match(acct_n):
        raise PayeeError("Account number must be 9 to 18 digits")
    return Payee("ACCOUNT", f"{ifsc_n}|{acct_n}", f"{ifsc_n} · {_mask(acct_n)}")


def normalise_upi(vpa: str) -> Payee:
    v = (vpa or "").strip().lower()
    if not UPI_RE.match(v):
        raise PayeeError("UPI ID must look like name@bank")
    name, handle = v.split("@", 1)
    shown = (name[:2] + "•" * max(len(name) - 2, 1)) if len(name) > 2 else name
    return Payee("UPI", v, f"{shown}@{handle}")


def normalise_phone(phone: str) -> Payee:
    digits = re.sub(r"[\s()-]+", "", phone or "")
    if digits.startswith("+91"):
        digits = digits[3:]
    elif digits.startswith("0091"):
        digits = digits[4:]
    elif len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if not MOBILE_RE.match(digits):
        raise PayeeError("Mobile number must be a 10-digit Indian number starting with 6, 7, 8 or 9")
    return Payee("PHONE", f"+91{digits}", f"+91 {_mask(digits)}")


def normalise(payee_type: str, fields: dict) -> Payee:
    t = (payee_type or "").upper()
    if t == "ACCOUNT":
        return normalise_account(str(fields.get("ifsc", "")), str(fields.get("account", "")))
    if t == "UPI":
        return normalise_upi(str(fields.get("upi", "")))
    if t == "PHONE":
        return normalise_phone(str(fields.get("phone", "")))
    raise PayeeError("Payee type must be ACCOUNT, UPI or PHONE")
