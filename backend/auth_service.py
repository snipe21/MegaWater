import logging
from datetime import datetime, timedelta
from fastapi import HTTPException

from database import get_conn
from security import hash_password, verify_password, new_raw_token, hash_token, new_id
from audit import log_action

log = logging.getLogger("auth_service")

SESSION_TTL_HOURS = 12
INVITE_TTL_DAYS = 7
RESET_TTL_HOURS = 24

DEFAULT_BOOTSTRAP_PASSWORD = "admin"  # login as "admin" / "admin" on first run - change immediately


# ---------------- Bootstrap ----------------

def bootstrap_super_admin():
    """
    Idempotent - only acts if no super_admin exists yet. Creates one with
    username "admin" (matched via the special-case in login() below) and a
    default password of "admin". This is meant to be changed immediately via
    change_own_password() once logged in - there's no HA config screen to
    manage this anymore, it's entirely self-service from the Super Admin PWA.
    """
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE role='super_admin' LIMIT 1").fetchone()
        if row:
            return  # already bootstrapped, nothing to do

        uid = new_id()
        conn.execute(
            "INSERT INTO users (id, role, name, password_hash, active) VALUES (?, 'super_admin', 'Super Admin', ?, 1)",
            (uid, hash_password(DEFAULT_BOOTSTRAP_PASSWORD)),
        )
        conn.commit()
    log.warning("Super Admin bootstrapped with DEFAULT credentials (admin/admin) - log in and change the password immediately.")
    log_action(None, "super_admin_bootstrapped_default", "user", uid,
               notes="Default admin/admin credentials - change immediately")


def change_own_password(user: dict, current_password: str, new_password: str) -> dict:
    """Self-service password change for any logged-in user - this is how the
    Super Admin replaces the default admin/admin bootstrap credential."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
        if not row or not verify_password(current_password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="Current password is incorrect")
        if len(new_password) < 8:
            raise HTTPException(status_code=400, detail="New password must be at least 8 characters")

        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(new_password), user["id"]))
        conn.commit()
    revoke_all_sessions(user["id"])
    log_action(user, "password_changed_self", "user", user["id"])
    return login("admin" if row["role"] == "super_admin" else (row["phone"] or row["email"]), new_password)


# ---------------- Login / Sessions ----------------

def issue_session(user: dict) -> dict:
    """Create a session for an already-verified user row and return the
    same {token, role, name, unit_id} shape password login returns. Shared
    by password login and WebAuthn (biometric) login so both end up with
    identical sessions."""
    with get_conn() as conn:
        token = new_raw_token()
        expires = (datetime.utcnow() + timedelta(hours=SESSION_TTL_HOURS)).isoformat()
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, role, unit_id, expires_at) VALUES (?, ?, ?, ?, ?)",
            (hash_token(token), user["id"], user["role"], user["unit_id"], expires),
        )
        conn.execute("UPDATE users SET last_login_at=CURRENT_TIMESTAMP WHERE id=?", (user["id"],))
        conn.commit()

    return {"token": token, "role": user["role"], "name": user["name"], "unit_id": user["unit_id"]}


def login(identifier: str, password: str) -> dict:
    with get_conn() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE (phone=? OR email=?) AND active=1",
            (identifier, identifier),
        ).fetchone()
        # allow super_admin login by role name too, since it has no phone/email
        if not user and identifier.lower() in ("admin", "superadmin", "super_admin"):
            user = conn.execute("SELECT * FROM users WHERE role='super_admin' LIMIT 1").fetchone()

        if not user or not verify_password(password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid credentials")
        user = dict(user)

    return issue_session(user)


def get_session_user(raw_token: str):
    with get_conn() as conn:
        sess = conn.execute(
            "SELECT * FROM sessions WHERE token_hash=? AND revoked=0", (hash_token(raw_token),)
        ).fetchone()
        if not sess or sess["expires_at"] < datetime.utcnow().isoformat():
            return None
        user = conn.execute("SELECT * FROM users WHERE id=? AND active=1", (sess["user_id"],)).fetchone()
        return dict(user) if user else None


def logout(raw_token: str):
    with get_conn() as conn:
        conn.execute("UPDATE sessions SET revoked=1 WHERE token_hash=?", (hash_token(raw_token),))
        conn.commit()


def revoke_all_sessions(user_id: str):
    with get_conn() as conn:
        conn.execute("UPDATE sessions SET revoked=1 WHERE user_id=?", (user_id,))
        conn.commit()


# ---------------- Invite flow (Super Admin -> Admin, Admin -> Tenant later) ----------------

def create_invite(actor_user: dict, role: str, name: str, phone: str = None,
                   email: str = None, unit_id: str = None) -> dict:
    if role == "admin" and actor_user["role"] != "super_admin":
        raise HTTPException(status_code=403, detail="Only Super Admin can invite Admins")
    if role == "tenant" and actor_user["role"] not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Only Admins can invite Tenants")

    uid = new_id()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO users (id, role, unit_id, name, phone, email, created_by, active)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
            (uid, role, unit_id, name, phone, email, actor_user["id"]),
        )
        raw_token = new_raw_token()
        expires = (datetime.utcnow() + timedelta(days=INVITE_TTL_DAYS)).isoformat()
        conn.execute(
            """INSERT INTO action_tokens (id, token_hash, purpose, target_user_id, issued_by, expires_at)
               VALUES (?, ?, 'invite', ?, ?, ?)""",
            (new_id(), hash_token(raw_token), uid, actor_user["id"], expires),
        )
        conn.commit()

    log_action(actor_user, f"{role}_invited", "user", uid, after={"name": name, "phone": phone})
    return {"user_id": uid, "token": raw_token, "expires_at": expires}


def get_invite_info(raw_token: str) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM action_tokens WHERE token_hash=? AND purpose='invite'",
            (hash_token(raw_token),),
        ).fetchone()
        if not row or row["consumed_at"] or row["expires_at"] < datetime.utcnow().isoformat():
            raise HTTPException(status_code=404, detail="Invalid or expired invite link")
        user = conn.execute("SELECT * FROM users WHERE id=?", (row["target_user_id"],)).fetchone()
        return {"name": user["name"], "role": user["role"]}


def update_profile(actor: dict, user_id: str, name: str, phone: str = None, email: str = None):
    """Edit a tenant's (or, by admins-managing-admins, another user's) name/phone/email.
    Does not touch password/activation state - separate concern from invites/resets."""
    with get_conn() as conn:
        target = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found")
        if target["role"] == "tenant" and actor["role"] not in ("admin", "super_admin"):
            raise HTTPException(status_code=403, detail="Not authorized")
        if target["role"] == "admin" and actor["role"] != "super_admin":
            raise HTTPException(status_code=403, detail="Not authorized")

        conn.execute("UPDATE users SET name=?, phone=?, email=? WHERE id=?", (name, phone, email, user_id))
        conn.commit()
    log_action(actor, "profile_updated", "user", user_id,
               before={"name": target["name"], "phone": target["phone"]},
               after={"name": name, "phone": phone})
    return {"ok": True}


def deactivate_user(actor: dict, user_id: str):
    """
    Frees a unit for a new tenant when someone moves out - soft-disable, never
    hard-delete, so billing/reading history stays intact and attributable.
    Also revokes any live session immediately, not just future logins.
    """
    with get_conn() as conn:
        target = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found")
        if target["role"] == "tenant" and actor["role"] not in ("admin", "super_admin"):
            raise HTTPException(status_code=403, detail="Not authorized")
        if target["role"] == "admin" and actor["role"] != "super_admin":
            raise HTTPException(status_code=403, detail="Not authorized")
        if target["role"] == "super_admin":
            raise HTTPException(status_code=400, detail="Cannot deactivate the Super Admin account")

        conn.execute("UPDATE users SET active=0 WHERE id=?", (user_id,))
        conn.commit()
    revoke_all_sessions(user_id)
    log_action(actor, f"{target['role']}_deactivated", "user", user_id, before={"name": target["name"]})
    return {"ok": True}


def hard_delete_user(actor: dict, user_id: str):
    """
    Permanent removal - separate from deactivate_user() on purpose. Requires
    the account to already be deactivated first (same two-step pattern as
    unit deletion): one explicit action frees the unit/turns off access,
    a second explicit action erases the record. Billing/reading history for
    the unit is untouched since it's keyed by unit_id, not by this user row.
    """
    with get_conn() as conn:
        target = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found")
        if target["role"] == "super_admin":
            raise HTTPException(status_code=400, detail="Cannot delete the Super Admin account")
        if target["role"] == "tenant" and actor["role"] not in ("admin", "super_admin"):
            raise HTTPException(status_code=403, detail="Not authorized")
        if target["role"] == "admin" and actor["role"] != "super_admin":
            raise HTTPException(status_code=403, detail="Not authorized")
        if target["active"]:
            raise HTTPException(status_code=400, detail="Deactivate this account before deleting it permanently")

        conn.execute("DELETE FROM webauthn_credentials WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM action_tokens WHERE target_user_id=?", (user_id,))
        conn.execute("DELETE FROM password_reset_requests WHERE requesting_user_id=?", (user_id,))
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        conn.commit()
    log_action(actor, f"{target['role']}_deleted_permanently", "user", user_id, before={"name": target["name"]})
    return {"ok": True}


def reissue_invite(actor: dict, user_id: str) -> dict:
    """
    'They lost their phone before ever setting a password' - the original invite
    link/QR was only ever shown once, so it can't be recovered, only replaced.
    Invalidates any old pending invite tokens for this user and issues a fresh one.
    """
    with get_conn() as conn:
        target = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not target:
            raise HTTPException(status_code=404, detail="User not found")
        if target["password_hash"]:
            raise HTTPException(status_code=400, detail="Account already activated - use password reset instead")
        if target["role"] == "tenant" and actor["role"] not in ("admin", "super_admin"):
            raise HTTPException(status_code=403, detail="Not authorized")
        if target["role"] == "admin" and actor["role"] != "super_admin":
            raise HTTPException(status_code=403, detail="Not authorized")

        conn.execute(
            "UPDATE action_tokens SET consumed_at=CURRENT_TIMESTAMP "
            "WHERE target_user_id=? AND purpose='invite' AND consumed_at IS NULL",
            (user_id,),
        )
        raw_token = new_raw_token()
        expires = (datetime.utcnow() + timedelta(days=INVITE_TTL_DAYS)).isoformat()
        conn.execute(
            """INSERT INTO action_tokens (id, token_hash, purpose, target_user_id, issued_by, expires_at)
               VALUES (?, ?, 'invite', ?, ?, ?)""",
            (new_id(), hash_token(raw_token), user_id, actor["id"], expires),
        )
        conn.commit()
    log_action(actor, f"{target['role']}_invite_reissued", "user", user_id)
    return {"token": raw_token, "expires_at": expires}


def accept_invite(raw_token: str, password: str) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM action_tokens WHERE token_hash=? AND purpose='invite'",
            (hash_token(raw_token),),
        ).fetchone()
        if not row or row["consumed_at"] or row["expires_at"] < datetime.utcnow().isoformat():
            raise HTTPException(status_code=404, detail="Invalid or expired invite link")

        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(password), row["target_user_id"]))
        conn.execute("UPDATE action_tokens SET consumed_at=CURRENT_TIMESTAMP WHERE id=?", (row["id"],))
        user = conn.execute("SELECT * FROM users WHERE id=?", (row["target_user_id"],)).fetchone()
        conn.commit()

    log_action(dict(user), "invite_accepted", "user", user["id"])
    return login(user["phone"] or user["email"], password)


# ---------------- Password reset escalation ----------------

def request_reset(identifier: str) -> dict:
    """Unauthenticated by necessity — the user is locked out. Looked up by phone/email."""
    with get_conn() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE (phone=? OR email=?) AND active=1", (identifier, identifier)
        ).fetchone()
        if not user:
            # Same response regardless of match, to avoid confirming which identifiers exist
            return {"ok": True}
        if user["role"] == "super_admin":
            raise HTTPException(status_code=400, detail="Super Admin password can only be changed via the HA addon configuration screen")

        req_id = new_id()
        conn.execute(
            "INSERT INTO password_reset_requests (id, requesting_user_id, status) VALUES (?, ?, 'pending')",
            (req_id, user["id"]),
        )
        conn.commit()

    log_action(dict(user), "password_reset_requested", "user", user["id"],
               notes=f"Escalates to: {'Super Admin' if user['role']=='admin' else 'any Admin'}")
    return {"ok": True}


def list_pending_resets(approver: dict) -> list:
    """Admins see tenant requests; Super Admin sees admin requests."""
    target_role = "tenant" if approver["role"] == "admin" else "admin"
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT prr.*, u.name, u.phone, u.role as target_role
               FROM password_reset_requests prr
               JOIN users u ON u.id = prr.requesting_user_id
               WHERE prr.status='pending' AND u.role=?
               ORDER BY prr.created_at ASC""",
            (target_role,),
        ).fetchall()
        return [dict(r) for r in rows]


def approve_reset(approver: dict, request_id: str) -> dict:
    with get_conn() as conn:
        req = conn.execute("SELECT * FROM password_reset_requests WHERE id=?", (request_id,)).fetchone()
        if not req or req["status"] != "pending":
            raise HTTPException(status_code=404, detail="Request not found or already handled")
        target_user = conn.execute("SELECT * FROM users WHERE id=?", (req["requesting_user_id"],)).fetchone()

        # Enforce escalation direction server-side, not just via the filtered list query
        if target_user["role"] == "tenant" and approver["role"] not in ("admin", "super_admin"):
            raise HTTPException(status_code=403, detail="Not authorized")
        if target_user["role"] == "admin" and approver["role"] != "super_admin":
            raise HTTPException(status_code=403, detail="Only Super Admin can approve Admin resets")

        raw_token = new_raw_token()
        expires = (datetime.utcnow() + timedelta(hours=RESET_TTL_HOURS)).isoformat()
        token_id = new_id()
        conn.execute(
            """INSERT INTO action_tokens (id, token_hash, purpose, target_user_id, issued_by, expires_at)
               VALUES (?, ?, 'password_reset', ?, ?, ?)""",
            (token_id, hash_token(raw_token), target_user["id"], approver["id"], expires),
        )
        conn.execute(
            "UPDATE password_reset_requests SET status='approved', approved_by=?, approved_at=CURRENT_TIMESTAMP, resulting_token_id=? WHERE id=?",
            (approver["id"], token_id, request_id),
        )
        conn.commit()

    log_action(approver, "password_reset_approved", "user", target_user["id"])
    return {"token": raw_token, "expires_at": expires, "user_name": target_user["name"]}


def deny_reset(approver: dict, request_id: str):
    with get_conn() as conn:
        conn.execute(
            "UPDATE password_reset_requests SET status='denied', approved_by=?, approved_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'",
            (approver["id"], request_id),
        )
        conn.commit()
    log_action(approver, "password_reset_denied", "password_reset_request", request_id)


def get_reset_info(raw_token: str) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM action_tokens WHERE token_hash=? AND purpose='password_reset'",
            (hash_token(raw_token),),
        ).fetchone()
        if not row or row["consumed_at"] or row["expires_at"] < datetime.utcnow().isoformat():
            raise HTTPException(status_code=404, detail="Invalid or expired reset link")
        user = conn.execute("SELECT * FROM users WHERE id=?", (row["target_user_id"],)).fetchone()
        return {"name": user["name"], "role": user["role"]}


def accept_reset(raw_token: str, new_password: str) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM action_tokens WHERE token_hash=? AND purpose='password_reset'",
            (hash_token(raw_token),),
        ).fetchone()
        if not row or row["consumed_at"] or row["expires_at"] < datetime.utcnow().isoformat():
            raise HTTPException(status_code=404, detail="Invalid or expired reset link")

        conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(new_password), row["target_user_id"]))
        conn.execute("UPDATE action_tokens SET consumed_at=CURRENT_TIMESTAMP WHERE id=?", (row["id"],))
        user = conn.execute("SELECT * FROM users WHERE id=?", (row["target_user_id"],)).fetchone()
        conn.commit()

    revoke_all_sessions(user["id"])  # force logout anywhere else, e.g. if the old password was compromised
    log_action(dict(user), "password_reset_completed", "user", user["id"])
    return login(user["phone"] or user["email"], new_password)
