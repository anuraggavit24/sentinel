"""Sentinel Module 1: passwordless login with passkeys (WebAuthn / FIDO2)."""
from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import psycopg
from fastapi import Cookie, Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from . import audit, db, payments, settings
from .auth_deps import current_session, now, request_ip, token_hash


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    drainer = audit.Drainer()
    drainer.start()
    yield
    drainer.stop()


app = FastAPI(title="Sentinel API", version="0.3.0", lifespan=lifespan)
app.include_router(payments.router)

USERNAME_PATTERN = r"^[a-z0-9_.-]{3,32}$"


# ---------------------------------------------------------------- models

class RegisterOptionsIn(BaseModel):
    username: str = Field(pattern=USERNAME_PATTERN)
    displayName: str = Field(min_length=1, max_length=64)


class LoginOptionsIn(BaseModel):
    # Optional: without a username the browser offers every passkey it holds for this site.
    username: Optional[str] = Field(default=None, pattern=USERNAME_PATTERN)


class CeremonyIn(BaseModel):
    challengeId: uuid.UUID
    credential: dict[str, Any]
    label: Optional[str] = Field(default=None, max_length=64)


# ---------------------------------------------------------------- helpers

def options_json(options: Any) -> dict:
    return json.loads(options_to_json(options))


def new_challenge(conn, purpose: str, challenge: bytes, **fields: Any) -> uuid.UUID:
    cols = ["purpose", "challenge", "expires_at", *fields.keys()]
    vals = [purpose, challenge, now() + timedelta(seconds=settings.CHALLENGE_TTL_SECONDS), *fields.values()]
    row = conn.execute(
        f"INSERT INTO challenges ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) RETURNING id",
        vals,
    ).fetchone()
    return row["id"]


def consume_challenge(conn, challenge_id: uuid.UUID, purpose: str) -> dict:
    """Atomically marks the challenge used. A second attempt, or an expired one, gets nothing back."""
    row = conn.execute(
        "UPDATE challenges SET used_at = now() "
        "WHERE id = %s AND purpose = %s AND used_at IS NULL AND expires_at > now() RETURNING *",
        (challenge_id, purpose),
    ).fetchone()
    if row is None:
        raise HTTPException(400, "Challenge is invalid, expired or already used")
    return row


def credential_id_from(credential: dict) -> bytes:
    raw = credential.get("rawId") or credential.get("id")
    if not isinstance(raw, str):
        raise HTTPException(400, "Credential is missing its id")
    try:
        return base64url_to_bytes(raw)
    except Exception as exc:  # noqa: BLE001 - malformed client input
        raise HTTPException(400, "Credential id is not valid base64url") from exc


def descriptor(cred_id: bytes, transports: list[str]) -> PublicKeyCredentialDescriptor:
    known = {t.value for t in AuthenticatorTransport}
    return PublicKeyCredentialDescriptor(
        id=cred_id, transports=[AuthenticatorTransport(t) for t in transports if t in known] or None
    )


def start_session(conn, response: Response, request: Request, user_id: uuid.UUID, cred_id: bytes) -> None:
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, credential_id, user_agent, ip, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (
            token_hash(token),
            user_id,
            cred_id,
            (request.headers.get("user-agent") or "")[:200],
            request_ip(request),
            now() + timedelta(hours=settings.SESSION_TTL_HOURS),
        ),
    )
    response.set_cookie(
        settings.COOKIE_NAME,
        token,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        max_age=settings.SESSION_TTL_HOURS * 3600,
        path="/",
    )


def save_credential(conn, user_id: uuid.UUID, verified: Any, credential: dict, label: Optional[str]) -> None:
    transports = credential.get("response", {}).get("transports") or []
    conn.execute(
        "INSERT INTO credentials (id, user_id, public_key, sign_count, transports, device_type, backed_up, label, last_used_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())",  # registering is the first use
        (
            verified.credential_id,
            user_id,
            verified.credential_public_key,
            verified.sign_count,
            [t for t in transports if isinstance(t, str)],
            verified.credential_device_type.value,
            verified.credential_backed_up,
            (label or "Passkey").strip()[:64] or "Passkey",
        ),
    )


def verify_new_credential(row: dict, body: CeremonyIn) -> Any:
    try:
        return verify_registration_response(
            credential=body.credential,
            expected_challenge=bytes(row["challenge"]),
            expected_rp_id=settings.RP_ID,
            expected_origin=settings.RP_ORIGINS,
            require_user_verification=True,
        )
    except InvalidRegistrationResponse as exc:
        raise HTTPException(400, f"Passkey registration failed: {exc}") from exc


# ---------------------------------------------------------------- registration (new user)

@app.post("/auth/register/options")
def register_options(body: RegisterOptionsIn) -> dict:
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM users WHERE username = %s", (body.username,)).fetchone():
            raise HTTPException(409, "Username is already taken")
        webauthn_id = secrets.token_bytes(32)  # opaque, never the username: it is stored on the authenticator
        options = generate_registration_options(
            rp_id=settings.RP_ID,
            rp_name=settings.RP_NAME,
            user_id=webauthn_id,
            user_name=body.username,
            user_display_name=body.displayName,
            timeout=settings.CHALLENGE_TTL_SECONDS * 1000,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,  # discoverable: login never lists credential ids
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
        )
        cid = new_challenge(
            conn,
            "register",
            options.challenge,
            pending_username=body.username,
            pending_display_name=body.displayName,
            pending_webauthn_id=webauthn_id,
        )
    return {"challengeId": str(cid), "options": options_json(options)}


@app.post("/auth/register/verify")
def register_verify(body: CeremonyIn, request: Request, response: Response) -> dict:
    try:
        with db.connect() as conn, conn.transaction():
            row = consume_challenge(conn, body.challengeId, "register")
            verified = verify_new_credential(row, body)
            user = conn.execute(
                "INSERT INTO users (username, display_name, webauthn_user_id) VALUES (%s, %s, %s) RETURNING id, username",
                (row["pending_username"], row["pending_display_name"], row["pending_webauthn_id"]),
            ).fetchone()
            save_credential(conn, user["id"], verified, body.credential, body.label)
            start_session(conn, response, request, user["id"], verified.credential_id)
            audit.enqueue(
                conn, "USER_REGISTERED", {"userId": str(user["id"]), "deviceType": verified.credential_device_type.value}
            )
    except psycopg.errors.UniqueViolation as exc:
        # Two sign-ups raced for the same username (or the passkey already exists). The UNIQUE
        # constraint is the real guard; a pre-check would still leave a window between check and insert.
        raise HTTPException(409, "Username was taken while you were registering") from exc
    return {"username": user["username"], "registered": True}


# ---------------------------------------------------------------- login

@app.post("/auth/login/options")
def login_options(body: LoginOptionsIn) -> dict:
    """
    Identical response for every caller: allowCredentials is always empty and the database is not
    consulted. Listing a real user's credential ids (the old behaviour) told anyone which usernames
    exist. Passkeys are discoverable, so the browser finds the right one without the list.
    """
    options = generate_authentication_options(
        rp_id=settings.RP_ID,
        allow_credentials=[],
        user_verification=UserVerificationRequirement.REQUIRED,
        timeout=settings.CHALLENGE_TTL_SECONDS * 1000,
    )
    with db.connect() as conn:
        # The typed username (if any) is only remembered so verify can insist on it.
        cid = new_challenge(conn, "login", options.challenge, pending_username=body.username)
    return {"challengeId": str(cid), "options": options_json(options)}


def user_handle_matches(credential: dict, webauthn_user_id: bytes) -> bool:
    """A discoverable passkey returns the user handle it was created with; it must be this account's."""
    handle = (credential.get("response") or {}).get("userHandle")
    if not handle:
        return True  # optional in the spec; the credential id lookup already ties it to one account
    try:
        return base64url_to_bytes(handle) == webauthn_user_id
    except Exception:  # noqa: BLE001 - malformed client input
        return False


@app.post("/auth/login/verify")
def login_verify(body: CeremonyIn, request: Request, response: Response) -> dict:
    cred_id = credential_id_from(body.credential)
    with db.connect() as conn, conn.transaction():
        row = consume_challenge(conn, body.challengeId, "login")
        cred = conn.execute(
            "SELECT c.*, u.username, u.webauthn_user_id FROM credentials c JOIN users u ON u.id = c.user_id "
            "WHERE c.id = %s FOR UPDATE",
            (cred_id,),
        ).fetchone()
        # Same error for every failure, so verify does not reveal which usernames exist either.
        if (
            cred is None
            or (row["pending_username"] is not None and cred["username"] != row["pending_username"])
            or not user_handle_matches(body.credential, bytes(cred["webauthn_user_id"]))
        ):
            raise HTTPException(400, "Passkey not recognised for this account")
        try:
            # py_webauthn checks the sign counter BEFORE the signature, so a forged response (any key,
            # a known credential id, a low counter) would look like a cloned authenticator. Verify the
            # signature with the counter check disabled (current = 0), then compare counters ourselves:
            # a counter that goes backwards on a VALID signature is a real clone signal.
            verified = verify_authentication_response(
                credential=body.credential,
                expected_challenge=bytes(row["challenge"]),
                expected_rp_id=settings.RP_ID,
                expected_origin=settings.RP_ORIGINS,
                credential_public_key=bytes(cred["public_key"]),
                credential_current_sign_count=0,
                require_user_verification=True,
            )
        except InvalidAuthenticationResponse as exc:
            raise HTTPException(400, f"Passkey sign-in failed: {exc}") from exc
        stored = cred["sign_count"]
        if (stored > 0 or verified.new_sign_count > 0) and verified.new_sign_count <= stored:
            # The main transaction rolls back when we raise, so record this on its own connection.
            audit.record_now(
                "POSSIBLE_CLONED_AUTHENTICATOR",
                {
                    "userId": str(cred["user_id"]),
                    "credentialId": bytes_to_base64url(cred_id),
                    "storedCount": stored,
                    "presentedCount": verified.new_sign_count,
                },
            )
            raise HTTPException(400, "Sign counter went backwards: possible cloned authenticator")
        conn.execute(
            "UPDATE credentials SET sign_count = %s, last_used_at = now() WHERE id = %s",
            (verified.new_sign_count, cred_id),
        )
        start_session(conn, response, request, cred["user_id"], cred_id)
        audit.enqueue(conn, "LOGIN_SUCCESS", {"userId": str(cred["user_id"])})
    return {"username": cred["username"], "signedIn": True}


@app.post("/auth/logout")
def logout(response: Response, session: dict = Depends(current_session)) -> dict:
    with db.connect() as conn, conn.transaction():
        conn.execute("UPDATE sessions SET revoked_at = now() WHERE id = %s", (session["session_id"],))
        audit.enqueue(conn, "LOGOUT", {"userId": str(session["user_id"])})
    response.delete_cookie(settings.COOKIE_NAME, path="/")
    return {"signedOut": True}


# ---------------------------------------------------------------- account: devices and sessions

@app.get("/me")
def me(session: dict = Depends(current_session)) -> dict:
    with db.connect() as conn:
        creds = conn.execute(
            "SELECT id, label, device_type, backed_up, created_at, last_used_at FROM credentials "
            "WHERE user_id = %s ORDER BY created_at",
            (session["user_id"],),
        ).fetchall()
        sessions = conn.execute(
            "SELECT id, user_agent, ip, created_at, expires_at FROM sessions "
            "WHERE user_id = %s AND revoked_at IS NULL AND expires_at > now() ORDER BY created_at DESC",
            (session["user_id"],),
        ).fetchall()
    return {
        "username": session["username"],
        "displayName": session["display_name"],
        "devices": [
            {
                "id": bytes_to_base64url(bytes(c["id"])),
                "label": c["label"],
                "deviceType": c["device_type"],
                "synced": c["backed_up"],
                "createdAt": c["created_at"].isoformat(),
                "lastUsedAt": c["last_used_at"].isoformat() if c["last_used_at"] else None,
            }
            for c in creds
        ],
        "sessions": [
            {
                "id": str(s["id"]),
                "current": s["id"] == session["session_id"],
                "userAgent": s["user_agent"],
                "ip": s["ip"],
                "createdAt": s["created_at"].isoformat(),
                "expiresAt": s["expires_at"].isoformat(),
            }
            for s in sessions
        ],
    }


@app.post("/me/devices/options")
def add_device_options(session: dict = Depends(current_session)) -> dict:
    with db.connect() as conn:
        existing = conn.execute("SELECT id, transports FROM credentials WHERE user_id = %s", (session["user_id"],)).fetchall()
        options = generate_registration_options(
            rp_id=settings.RP_ID,
            rp_name=settings.RP_NAME,
            user_id=bytes(session["webauthn_user_id"]),
            user_name=session["username"],
            user_display_name=session["display_name"],
            timeout=settings.CHALLENGE_TTL_SECONDS * 1000,
            exclude_credentials=[descriptor(bytes(c["id"]), c["transports"]) for c in existing],
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,  # discoverable: login never lists credential ids
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
        )
        cid = new_challenge(conn, "add_device", options.challenge, user_id=session["user_id"])
    return {"challengeId": str(cid), "options": options_json(options)}


@app.post("/me/devices/verify")
def add_device_verify(body: CeremonyIn, session: dict = Depends(current_session)) -> dict:
    with db.connect() as conn, conn.transaction():
        row = consume_challenge(conn, body.challengeId, "add_device")
        if row["user_id"] != session["user_id"]:
            raise HTTPException(400, "Challenge belongs to another account")
        verified = verify_new_credential(row, body)
        if conn.execute("SELECT 1 FROM credentials WHERE id = %s", (verified.credential_id,)).fetchone():
            raise HTTPException(409, "This passkey is already registered")
        save_credential(conn, session["user_id"], verified, body.credential, body.label)
        audit.enqueue(conn, "DEVICE_ADDED", {"userId": str(session["user_id"])})
    return {"added": True}


@app.delete("/me/devices/{device_id}")
def remove_device(device_id: str, session: dict = Depends(current_session)) -> dict:
    try:
        cred_id = base64url_to_bytes(device_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, "Invalid device id") from exc
    with db.connect() as conn, conn.transaction():
        # Lock all of this user's passkeys first. Without the lock, two removals running at the same
        # time both see "2 passkeys", each deletes one, and the account is left with none.
        mine = [
            bytes(r["id"])
            for r in conn.execute(
                "SELECT id FROM credentials WHERE user_id = %s ORDER BY id FOR UPDATE", (session["user_id"],)
            ).fetchall()
        ]
        count = len(mine)
        if cred_id not in mine:
            raise HTTPException(404, "Device not found")
        if count <= 1:
            raise HTTPException(409, "You cannot remove your only passkey; add another device first")
        conn.execute("DELETE FROM credentials WHERE id = %s", (cred_id,))
        # Sessions opened with the removed passkey end too.
        conn.execute(
            "UPDATE sessions SET revoked_at = now() WHERE credential_id = %s AND revoked_at IS NULL", (cred_id,)
        )
        audit.enqueue(conn, "DEVICE_REMOVED", {"userId": str(session["user_id"])})
    return {"removed": True}


@app.delete("/me/sessions/{session_id}")
def revoke_session(session_id: uuid.UUID, session: dict = Depends(current_session)) -> dict:
    with db.connect() as conn, conn.transaction():
        row = conn.execute(
            "UPDATE sessions SET revoked_at = now() WHERE id = %s AND user_id = %s AND revoked_at IS NULL RETURNING id",
            (session_id, session["user_id"]),
        ).fetchone()
        if row is not None:
            audit.enqueue(conn, "SESSION_REVOKED", {"userId": str(session["user_id"]), "sessionId": str(session_id)})
    if row is None:
        raise HTTPException(404, "Session not found")
    return {"revoked": True}


@app.get("/health")
def health() -> dict:
    with db.connect() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok", "auditBacklog": audit.backlog()}
