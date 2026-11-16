"""Slice 2c: look a payee up on the consortium fraud-intel ledger (through the ledger-gateway sidecar).

Fail-open, on purpose: if the ledger is slow or down, the payment is scored on every other signal and
the snapshot says "ledger unavailable". A ledger outage must not stop every UPI payment in the bank.
The cost: an attacker who can take the ledger down loses one signal against them, so outages are
recorded in the audit log and a circuit breaker stops each payment from waiting on a dead ledger.

The sidecar's answer is checked, not trusted: anything malformed counts as "unavailable".
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from . import settings

STATUSES = {"PENDING", "CONFIRMED", "DISPUTED", "OVERTURNED", "RETRACTED"}
FAILURES_TO_OPEN = 3      # consecutive failures before the breaker opens
COOL_DOWN_SECONDS = 30.0  # while open, payments skip the ledger instead of waiting for it


@dataclass
class LedgerResult:
    status: str                       # disabled | clear | hit | unavailable
    max_weight: float = 0.0
    hard_rule: bool = False           # a CONFIRMED report: at least two banks, not sandbox-only
    reports: list[dict] = field(default_factory=list)
    latency_ms: Optional[int] = None
    error: Optional[str] = None
    breaker_opened: bool = False      # this call opened the breaker: audit it once

    def snapshot(self) -> dict:
        """Analyst view and stored record. Report ids let an analyst open the report on the ledger."""
        out: dict = {"status": self.status}
        if self.status in ("clear", "hit"):
            out.update({"maxWeight": self.max_weight, "hardRule": self.hard_rule, "reports": self.reports,
                        "latencyMs": self.latency_ms})
        if self.error:
            out["error"] = self.error
        return out


class _Breaker:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.failures = 0
        self.open_until = 0.0

    def allow(self) -> bool:
        with self.lock:
            return time.monotonic() >= self.open_until

    def success(self) -> None:
        with self.lock:
            self.failures = 0
            self.open_until = 0.0

    def failure(self) -> bool:
        """Record a failure; True if this one opened the breaker."""
        with self.lock:
            self.failures += 1
            if self.failures >= FAILURES_TO_OPEN and time.monotonic() >= self.open_until:
                self.open_until = time.monotonic() + COOL_DOWN_SECONDS
                self.failures = 0
                return True
            return False

    def state(self) -> dict:
        with self.lock:
            remaining = self.open_until - time.monotonic()
            return {"open": remaining > 0, "secondsUntilRetry": max(0, math.ceil(remaining)),
                    "consecutiveFailures": self.failures}


breaker = _Breaker()


def _transport() -> Optional[httpx.BaseTransport]:
    """Tests replace this with a mock transport."""
    return None


def _client() -> httpx.Client:
    timeout = settings.LEDGER_TIMEOUT_MS / 1000
    return httpx.Client(base_url=settings.LEDGER_URL, timeout=httpx.Timeout(timeout, connect=timeout),
                        headers={"x-ledger-token": settings.LEDGER_TOKEN}, transport=_transport())


def _validate(result: object, sent: list[str]) -> tuple[float, bool, list[dict]]:
    if not isinstance(result, dict):
        raise ValueError("result is not an object")
    weight, hard, reports = result.get("maxWeight"), result.get("hardRuleEligible"), result.get("reports")
    if not isinstance(weight, (int, float)) or isinstance(weight, bool) or not 0 <= weight <= 1:
        raise ValueError("maxWeight out of range")
    if not isinstance(hard, bool) or not isinstance(reports, list) or len(reports) > 50:
        raise ValueError("bad hardRuleEligible or reports")
    clean = []
    for r in reports:
        if not isinstance(r, dict) or r.get("status") not in STATUSES or r.get("idHash") not in sent:
            raise ValueError("report with unknown status or for a hash we did not ask about")
        w = r.get("weight")
        if not isinstance(w, (int, float)) or isinstance(w, bool) or not 0 <= w <= 1:
            raise ValueError("report weight out of range")
        clean.append({
            "reportId": str(r.get("reportId", ""))[:100],
            "status": r["status"],
            "severity": str(r.get("severity", ""))[:10],
            "source": str(r.get("source", ""))[:30],
            "reporterOrg": str(r.get("reporterOrg", ""))[:30],
            "nonSandboxBanks": int(r.get("nonSandboxBanks", 0)),
            "weight": float(w),
        })
    # Recompute rather than believe: the summary must agree with the reports it summarises.
    if abs(max((r["weight"] for r in clean), default=0.0) - weight) > 1e-9:
        raise ValueError("maxWeight does not match the reports")
    if hard != any(r["status"] == "CONFIRMED" for r in clean):
        raise ValueError("hardRuleEligible does not match the reports")
    return float(weight), hard, clean


def query_payee(id_hashes: list[str]) -> LedgerResult:
    if not settings.LEDGER_URL:
        return LedgerResult("disabled")
    if not breaker.allow():
        s = breaker.state()
        return LedgerResult("unavailable", error=f"ledger skipped for {s['secondsUntilRetry']}s after repeated failures")
    started = time.monotonic()
    try:
        with _client() as client:
            r = client.post("/query-payee", json={"idHashes": id_hashes})
        if r.status_code != 200:
            kind = (r.json().get("kind") if r.headers.get("content-type", "").startswith("application/json") else None)
            raise RuntimeError(f"ledger-gateway answered {r.status_code}" + (f" ({kind})" if kind else ""))
        weight, hard, reports = _validate(r.json().get("result"), id_hashes)
    except httpx.TimeoutException:
        return LedgerResult("unavailable", error=f"no answer within {settings.LEDGER_TIMEOUT_MS} ms",
                            breaker_opened=breaker.failure())
    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
        return LedgerResult("unavailable", error=str(exc)[:200], breaker_opened=breaker.failure())
    breaker.success()
    return LedgerResult("hit" if reports else "clear", weight, hard, reports,
                        latency_ms=int((time.monotonic() - started) * 1000))


def status() -> dict:
    """For the operator: is the sidecar reachable, and is the breaker open?"""
    out: dict = {"configured": bool(settings.LEDGER_URL), "breaker": breaker.state()}
    if not settings.LEDGER_URL:
        return out
    try:
        with _client() as client:
            r = client.get("/health")
        out["gateway"] = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"status": r.status_code}
        out["reachable"] = r.status_code == 200
    except httpx.HTTPError as exc:
        out.update({"reachable": False, "gateway": {"error": type(exc).__name__}})
    return out
