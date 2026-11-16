"""Shared request helpers: time, session-token hashing, the current-session dependency and client IP."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional

from fastapi import Cookie, HTTPException, Request

from . import db, settings
from .net import client_ip, parse_networks

TRUSTED_PROXIES = parse_networks(settings.TRUSTED_PROXIES)


def now() -> datetime:
    return datetime.now(timezone.utc)


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def request_ip(request: Request) -> Optional[str]:
    peer = request.client.host if request.client else None
    return client_ip(peer, request.headers.get("x-forwarded-for"), TRUSTED_PROXIES)


def current_session(sentinel_session: Optional[str] = Cookie(default=None)) -> dict:
    if not sentinel_session:
        raise HTTPException(401, "Not signed in")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT s.id AS session_id, s.user_id, s.credential_id, s.ip AS session_ip, s.created_at AS session_created_at, "
            "u.username, u.display_name, u.webauthn_user_id, u.role, s.ops_elevated_until, "
            "COALESCE(c.backed_up, false) AS synced_passkey "
            "FROM sessions s JOIN users u ON u.id = s.user_id "
            "LEFT JOIN credentials c ON c.id = s.credential_id "
            "WHERE s.token_hash = %s AND s.revoked_at IS NULL AND s.expires_at > now()",
            (token_hash(sentinel_session),),
        ).fetchone()
    if row is None:
        raise HTTPException(401, "Session expired or revoked")
    return row
