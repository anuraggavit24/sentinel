"""A software passkey: real P-256 keys, real CBOR, real signatures. Lets tests run full WebAuthn ceremonies."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Optional

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

UP, UV, AT = 0x01, 0x04, 0x40  # user present, user verified, attested credential data included


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class SoftAuthenticator:
    def __init__(self, rp_id: str = "localhost", origin: str = "http://localhost:5173", start_counter: int = 0):
        self.rp_id = rp_id
        self.origin = origin
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(32)
        self.counter = start_counter

    def _rp_hash(self) -> bytes:
        return hashlib.sha256(self.rp_id.encode()).digest()

    def _cose_public_key(self) -> bytes:
        nums = self.key.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")})

    def create(self, options: dict, origin: Optional[str] = None) -> dict:
        client_data = json.dumps(
            {"type": "webauthn.create", "challenge": options["challenge"], "origin": origin or self.origin, "crossOrigin": False}
        ).encode()
        auth_data = (
            self._rp_hash()
            + bytes([UP | UV | AT])
            + self.counter.to_bytes(4, "big")
            + bytes(16)  # AAGUID
            + len(self.cred_id).to_bytes(2, "big")
            + self.cred_id
            + self._cose_public_key()
        )
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        return {
            "id": b64u(self.cred_id),
            "rawId": b64u(self.cred_id),
            "type": "public-key",
            "response": {"clientDataJSON": b64u(client_data), "attestationObject": b64u(attestation), "transports": ["internal"]},
            "clientExtensionResults": {},
            "authenticatorAttachment": "platform",
        }

    def get(self, options: dict, counter: Optional[int] = None, origin: Optional[str] = None) -> dict:
        self.counter = self.counter + 1 if counter is None else counter
        client_data = json.dumps(
            {"type": "webauthn.get", "challenge": options["challenge"], "origin": origin or self.origin, "crossOrigin": False}
        ).encode()
        auth_data = self._rp_hash() + bytes([UP | UV]) + self.counter.to_bytes(4, "big")
        signature = self.key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        return {
            "id": b64u(self.cred_id),
            "rawId": b64u(self.cred_id),
            "type": "public-key",
            "response": {"clientDataJSON": b64u(client_data), "authenticatorData": b64u(auth_data), "signature": b64u(signature)},
            "clientExtensionResults": {},
            "authenticatorAttachment": "platform",
        }
