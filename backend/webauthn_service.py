"""
WebAuthn (fingerprint / Face ID) passwordless login.

Uses discoverable ("resident key") platform credentials scoped to
AuthenticatorAttachment.PLATFORM - i.e. the phone/laptop's own biometric
sensor, not a roaming USB security key. Because the credential is
discoverable, the login ceremony doesn't need an identifier up front: the
browser's fingerprint/Face ID prompt itself picks the right credential, and
its response tells us (via userHandle) which account just authenticated.

Registration always requires an existing authenticated session (you must
already be logged in with a password once to add a biometric credential to
your account) - this endpoint is never reachable by an unauthenticated
caller, so a stolen phone alone can't add a new fingerprint to someone
else's account.
"""
import json
import logging
from datetime import datetime, timedelta

import webauthn
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorAttachment,
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)
from fastapi import HTTPException

import settings
from database import get_conn
from security import new_id
from auth_service import issue_session
from audit import log_action

log = logging.getLogger("webauthn_service")

CHALLENGE_TTL_MINUTES = 5


def _rp():
    """Relying Party id/name/origin, derived from the same domain_name
    setting the rest of the app already uses for links/QR codes. WebAuthn
    requires this to be a real HTTPS domain (or 'localhost') - it will not
    work over a bare LAN IP or plain http://, since browsers tie
    credentials to the origin that created them."""
    domain = settings.get("domain_name")
    if not domain:
        raise HTTPException(
            status_code=400,
            detail="Set Domain Name in Super Admin > Settings first - biometric login needs a real HTTPS domain.",
        )
    return {
        "rp_id": domain,
        "rp_name": settings.get("product_name") or "WaterFlow",
        "origin": f"https://{domain}",
    }


def _store_challenge(purpose: str, challenge_bytes: bytes, user_id: str = None) -> str:
    row_id = new_id()
    expires = (datetime.utcnow() + timedelta(minutes=CHALLENGE_TTL_MINUTES)).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO webauthn_challenges (id, purpose, user_id, challenge, expires_at) VALUES (?, ?, ?, ?, ?)",
            (row_id, purpose, user_id, bytes_to_base64url(challenge_bytes), expires),
        )
        conn.commit()
    return row_id


def _consume_challenge(challenge_id: str, purpose: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM webauthn_challenges WHERE id=? AND purpose=?", (challenge_id, purpose)
        ).fetchone()
        if not row or row["consumed_at"] or row["expires_at"] < datetime.utcnow().isoformat():
            raise HTTPException(status_code=400, detail="Biometric prompt expired - please try again")
        conn.execute("UPDATE webauthn_challenges SET consumed_at=CURRENT_TIMESTAMP WHERE id=?", (challenge_id,))
        conn.commit()
    return dict(row)


# ---------------- Registration (add a device to the current account) ----------------

def register_options(user: dict) -> dict:
    rp = _rp()
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT credential_id FROM webauthn_credentials WHERE user_id=?", (user["id"],)
        ).fetchall()

    options = webauthn.generate_registration_options(
        rp_id=rp["rp_id"],
        rp_name=rp["rp_name"],
        user_id=user["id"].encode(),
        user_name=user.get("phone") or user.get("email") or "admin",
        user_display_name=user["name"],
        authenticator_selection=AuthenticatorSelectionCriteria(
            authenticator_attachment=AuthenticatorAttachment.PLATFORM,
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(r["credential_id"])) for r in existing
        ],
    )
    challenge_id = _store_challenge("register", options.challenge, user_id=user["id"])
    return {"challenge_id": challenge_id, "options": json.loads(webauthn.options_to_json(options))}


def register_verify(user: dict, challenge_id: str, credential: dict, device_label: str = None) -> dict:
    rp = _rp()
    chal = _consume_challenge(challenge_id, "register")
    if chal["user_id"] != user["id"]:
        raise HTTPException(status_code=400, detail="Biometric prompt does not match this session")

    try:
        result = webauthn.verify_registration_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(chal["challenge"]),
            expected_rp_id=rp["rp_id"],
            expected_origin=rp["origin"],
            require_user_verification=True,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not verify biometric registration: {e}")

    with get_conn() as conn:
        conn.execute(
            """INSERT INTO webauthn_credentials
               (id, user_id, credential_id, public_key, sign_count, device_label)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                new_id(),
                user["id"],
                bytes_to_base64url(result.credential_id),
                bytes_to_base64url(result.credential_public_key),
                result.sign_count,
                device_label or "This device",
            ),
        )
        conn.commit()
    log_action(user, "webauthn_credential_added", "user", user["id"], notes=device_label)
    return {"ok": True}


def list_credentials(user: dict) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, device_label, created_at, last_used_at FROM webauthn_credentials "
            "WHERE user_id=? ORDER BY created_at DESC",
            (user["id"],),
        ).fetchall()
    return [dict(r) for r in rows]


def remove_credential(user: dict, credential_row_id: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM webauthn_credentials WHERE id=? AND user_id=?", (credential_row_id, user["id"])
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Not found")
        conn.execute("DELETE FROM webauthn_credentials WHERE id=?", (credential_row_id,))
        conn.commit()
    log_action(user, "webauthn_credential_removed", "user", user["id"], notes=row["device_label"])
    return {"ok": True}


# ---------------- Login (biometric only, no password) ----------------

def login_options() -> dict:
    rp = _rp()
    options = webauthn.generate_authentication_options(
        rp_id=rp["rp_id"],
        user_verification=UserVerificationRequirement.REQUIRED,
        # No allow_credentials: this is a discoverable-credential (resident
        # key) login, so any platform authenticator registered for this
        # rp_id can respond - the browser's biometric prompt itself is the
        # picker, we don't narrow it down first.
    )
    challenge_id = _store_challenge("login", options.challenge)
    return {"challenge_id": challenge_id, "options": json.loads(webauthn.options_to_json(options))}


def login_verify(challenge_id: str, credential: dict) -> dict:
    rp = _rp()
    chal = _consume_challenge(challenge_id, "login")

    raw_id = base64url_to_bytes(credential.get("rawId", ""))
    cred_id_b64 = bytes_to_base64url(raw_id)

    with get_conn() as conn:
        stored = conn.execute(
            "SELECT * FROM webauthn_credentials WHERE credential_id=?", (cred_id_b64,)
        ).fetchone()
        if not stored:
            raise HTTPException(status_code=401, detail="Biometric credential not recognized")
        user = conn.execute("SELECT * FROM users WHERE id=? AND active=1", (stored["user_id"],)).fetchone()
        if not user:
            raise HTTPException(status_code=401, detail="Account not found or inactive")

    try:
        result = webauthn.verify_authentication_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(chal["challenge"]),
            expected_rp_id=rp["rp_id"],
            expected_origin=rp["origin"],
            credential_public_key=base64url_to_bytes(stored["public_key"]),
            credential_current_sign_count=stored["sign_count"],
            require_user_verification=True,
        )
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Biometric verification failed: {e}")

    with get_conn() as conn:
        conn.execute(
            "UPDATE webauthn_credentials SET sign_count=?, last_used_at=CURRENT_TIMESTAMP WHERE id=?",
            (result.new_sign_count, stored["id"]),
        )
        conn.commit()

    return issue_session(dict(user))


