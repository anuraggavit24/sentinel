"""Best-effort audit events to the audit-log service. Never blocks or fails a login."""
import logging

import httpx

from . import settings

log = logging.getLogger("sentinel.audit")


def send(event_type: str, payload: dict) -> None:
    if not settings.AUDIT_URL:
        return
    try:
        httpx.post(
            f"{settings.AUDIT_URL}/events",
            json={"stream": settings.AUDIT_STREAM, "eventType": event_type, "payload": payload},
            timeout=1.0,
        )
    except httpx.HTTPError as exc:  # fail open: the audit log is evidence, not a gate
        log.warning("audit event %s not recorded: %s", event_type, exc)
