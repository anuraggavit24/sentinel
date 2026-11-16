"""A fresh passkey check by an already signed-in user, before something consequential: building a
typing profile (slice 2b) or acting as a fraud analyst (Module 3). Shared so both get the same rules:
user verification required, signature checked before our own sign-counter comparison, the challenge
bound to this user, single use."""
from __future__ import annotations

import uuid

from fastapi import HTTPException
from webauthn import generate_authentication_options, verify_authentication_response
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.exceptions import InvalidAuthenticationResponse
from webauthn.helpers.structs import PublicKeyCredentialDescriptor, UserVerificationRequirement

from . import audit, settings


def options(conn, session: dict) -> dict:
    """Listing this user's own credentials is fine: they are already signed in."""
    from .main import new_challenge, options_json  # local import: main includes the routers using this

    creds = conn.execute("SELECT id FROM credentials WHERE user_id = %s", (session["user_id"],)).fetchall()
    opts = generate_authentication_options(
        rp_id=settings.RP_ID,
        allow_credentials=[PublicKeyCredentialDescriptor(id=bytes(c["id"])) for c in creds],
        user_verification=UserVerificationRequirement.REQUIRED,
        timeout=settings.CHALLENGE_TTL_SECONDS * 1000,
    )
    cid = new_challenge(conn, "reauth", opts.challenge, user_id=session["user_id"])
    return {"challengeId": str(cid), "options": options_json(opts)}


def verify(conn, session: dict, challenge_id: uuid.UUID, credential: dict, during: str) -> None:
    """Raises HTTPException(400) unless this user just proved presence with one of their passkeys.
    Must run inside the caller's transaction (it locks the credential row)."""
    from .main import consume_challenge, credential_id_from

    cred_id = credential_id_from(credential)
    row = consume_challenge(conn, challenge_id, "reauth")
    if row["user_id"] != session["user_id"]:
        raise HTTPException(400, "This passkey check belongs to another account")
    cred = conn.execute(
        "SELECT * FROM credentials WHERE id = %s AND user_id = %s FOR UPDATE", (cred_id, session["user_id"])
    ).fetchone()
    if cred is None:
        raise HTTPException(400, "Passkey not recognised for this account")
    try:
        # Signature first (counter check disabled), then our own counter comparison: same as sign-in.
        verified = verify_authentication_response(
            credential=credential,
            expected_challenge=bytes(row["challenge"]),
            expected_rp_id=settings.RP_ID,
            expected_origin=settings.RP_ORIGINS,
            credential_public_key=bytes(cred["public_key"]),
            credential_current_sign_count=0,
            require_user_verification=True,
        )
    except InvalidAuthenticationResponse as exc:
        raise HTTPException(400, f"Passkey check failed: {exc}") from exc
    stored = cred["sign_count"]
    if (stored > 0 or verified.new_sign_count > 0) and verified.new_sign_count <= stored:
        audit.record_now(
            "POSSIBLE_CLONED_AUTHENTICATOR",
            {"userId": str(session["user_id"]), "credentialId": bytes_to_base64url(cred_id), "during": during},
        )
        raise HTTPException(400, "Sign counter went backwards: possible cloned authenticator")
    conn.execute("UPDATE credentials SET sign_count = %s, last_used_at = now() WHERE id = %s",
                 (verified.new_sign_count, cred_id))
