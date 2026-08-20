import logging
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
from database import init_db, get_conn
from auth_service import (
    bootstrap_super_admin, login, logout, create_invite, get_invite_info, accept_invite,
    request_reset, list_pending_resets, approve_reset, deny_reset, get_reset_info, accept_reset,
)
from deps import require_user, require_super_admin, require_admin_or_above
from qr import qr_png_bytes

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("main")

app = FastAPI(title=f"{config.PRODUCT_NAME} Water Metering")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
def startup():
    init_db()
    bootstrap_super_admin()
    log.info("%s backend started (Phase 1: auth foundation)", config.PRODUCT_NAME)


def _link_base() -> str:
    if config.DOMAIN_NAME:
        return f"https://{config.DOMAIN_NAME}"
    return ""  # frontend falls back to window.location.origin for LAN-only testing


# ---------------- Public config (branding, no secrets) ----------------

@app.get("/api/public-config")
def public_config():
    return {
        "product_name": config.PRODUCT_NAME,
        "building_name": config.BUILDING_NAME,
        "super_admin_app_name": config.SUPER_ADMIN_APP_NAME,
        "admin_app_name": config.ADMIN_APP_NAME,
        "tenant_app_name": config.TENANT_APP_NAME,
        "link_base": _link_base(),
    }


# ---------------- Auth ----------------

class LoginRequest(BaseModel):
    identifier: str
    password: str


@app.post("/api/auth/login")
def api_login(body: LoginRequest):
    return login(body.identifier, body.password)


@app.post("/api/auth/logout")
def api_logout(user=Depends(require_user), authorization: str = ""):
    # token itself isn't passed here for simplicity; frontend just discards it client-side
    # and we rely on natural expiry, OR call logout() with the raw token if needed later.
    return {"ok": True}


@app.get("/api/auth/me")
def api_me(user=Depends(require_user)):
    return {"id": user["id"], "role": user["role"], "name": user["name"], "unit_id": user["unit_id"]}


class ResetRequest(BaseModel):
    identifier: str


@app.post("/api/auth/reset-request")
def api_reset_request(body: ResetRequest):
    return request_reset(body.identifier)


# ---------------- Super Admin: manage Admins ----------------

class InviteRequest(BaseModel):
    name: str
    phone: Optional[str] = None
    email: Optional[str] = None


@app.post("/api/super/admins/invite")
def api_invite_admin(body: InviteRequest, user=Depends(require_super_admin)):
    result = create_invite(user, "admin", body.name, body.phone, body.email)
    link = f"{_link_base()}/invite/{result['token']}"
    return {"link": link, "expires_at": result["expires_at"]}


@app.get("/api/super/admins")
def api_list_admins(user=Depends(require_super_admin)):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, phone, email, active, password_hash IS NOT NULL as activated, created_at, last_login_at "
            "FROM users WHERE role='admin' ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


@app.put("/api/super/admins/{admin_id}/deactivate")
def api_deactivate_admin(admin_id: str, user=Depends(require_super_admin)):
    with get_conn() as conn:
        conn.execute("UPDATE users SET active=0 WHERE id=? AND role='admin'", (admin_id,))
        conn.commit()
    return {"ok": True}


# ---------------- Invite accept (Admin or Tenant, same flow) ----------------

@app.get("/api/invite/{token}")
def api_get_invite(token: str):
    return get_invite_info(token)


class AcceptRequest(BaseModel):
    password: str


@app.post("/api/invite/{token}/accept")
def api_accept_invite(token: str, body: AcceptRequest):
    return accept_invite(token, body.password)


# ---------------- Password reset escalation (Admin/Super Admin) ----------------

@app.get("/api/reset-requests")
def api_list_reset_requests(user=Depends(require_admin_or_above)):
    return list_pending_resets(user)


@app.post("/api/reset-requests/{request_id}/approve")
def api_approve_reset(request_id: str, user=Depends(require_admin_or_above)):
    result = approve_reset(user, request_id)
    link = f"{_link_base()}/reset/{result['token']}"
    return {"link": link, "expires_at": result["expires_at"], "user_name": result["user_name"]}


@app.post("/api/reset-requests/{request_id}/deny")
def api_deny_reset(request_id: str, user=Depends(require_admin_or_above)):
    deny_reset(user, request_id)
    return {"ok": True}


@app.get("/api/reset/{token}")
def api_get_reset(token: str):
    return get_reset_info(token)


@app.post("/api/reset/{token}/accept")
def api_accept_reset(token: str, body: AcceptRequest):
    return accept_reset(token, body.password)


# ---------------- QR codes for any link ----------------

@app.get("/api/qrcode")
def api_qrcode(data: str):
    png = qr_png_bytes(data)
    return Response(content=png, media_type="image/png")


# ---------------- Static frontend (PWA) ----------------
app.mount("/", StaticFiles(directory="/app/frontend", html=True), name="frontend")
