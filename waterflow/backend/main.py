import logging
from datetime import datetime, timedelta
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
    update_profile, deactivate_user, reissue_invite,
)
from deps import require_user, require_super_admin, require_admin_or_above, require_tenant
from qr import qr_png_bytes

import chirpstack_client
import mqtt_service
import ha_discovery
import units_meters
import billing
import billing_scheduler
import reports

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("main")

app = FastAPI(title=f"{config.PRODUCT_NAME} Water Metering")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
def startup():
    init_db()
    bootstrap_super_admin()
    chirpstack_client.bootstrap_chirpstack()
    mqtt_service.start_mqtt_service()
    ha_discovery.start_ha_discovery_client()
    billing_scheduler.start_billing_scheduler()
    log.info("%s backend started (Phase 1-3: auth, meters, billing)", config.PRODUCT_NAME)


def _resolve_period(period: str, start: Optional[str], end: Optional[str]):
    now = datetime.utcnow()
    if period == "custom":
        if not start or not end:
            raise HTTPException(status_code=400, detail="start and end required for custom period")
        return start, end, "Custom"
    if period == "daily":
        return (now - timedelta(days=1)).isoformat(), now.isoformat(), "Daily"
    if period == "weekly":
        return (now - timedelta(days=7)).isoformat(), now.isoformat(), "Weekly"
    if period == "monthly":
        return (now - timedelta(days=30)).isoformat(), now.isoformat(), "Monthly"
    raise HTTPException(status_code=400, detail="invalid period")


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


# ==================== PHASE 2: Units & Meters ====================

class UnitCreate(BaseModel):
    unit_number: str
    notes: Optional[str] = None


@app.post("/api/admin/units")
def api_create_unit(body: UnitCreate, user=Depends(require_admin_or_above)):
    return units_meters.create_unit(user, body.unit_number, body.notes)


@app.put("/api/admin/units/{unit_id}")
def api_update_unit(unit_id: str, body: UnitCreate, user=Depends(require_admin_or_above)):
    return units_meters.update_unit(user, unit_id, body.unit_number, body.notes)


@app.delete("/api/admin/units/{unit_id}")
def api_delete_unit(unit_id: str, user=Depends(require_admin_or_above)):
    return units_meters.delete_unit(user, unit_id)


@app.get("/api/admin/units")
def api_list_units(user=Depends(require_admin_or_above)):
    return units_meters.list_units_with_meters()


@app.get("/api/admin/meters/unassigned")
def api_unassigned_meters(user=Depends(require_admin_or_above)):
    return units_meters.list_unassigned_meters()


class MeterCreate(BaseModel):
    dev_eui: str
    app_key: str
    unit_id: Optional[str] = None


@app.post("/api/admin/meters")
def api_add_meter(body: MeterCreate, user=Depends(require_admin_or_above)):
    return units_meters.add_meter(user, body.dev_eui, body.app_key, body.unit_id)


class MeterAssign(BaseModel):
    unit_id: str


@app.put("/api/admin/meters/{meter_id}/assign")
def api_assign_meter(meter_id: str, body: MeterAssign, user=Depends(require_admin_or_above)):
    units_meters.assign_meter_to_unit(user, meter_id, body.unit_id)
    return {"ok": True}


@app.delete("/api/admin/meters/{meter_id}")
def api_remove_meter(meter_id: str, user=Depends(require_admin_or_above)):
    units_meters.remove_meter(user, meter_id)
    return {"ok": True}


# ---------------- Tenant invite (reuses the Phase 1 invite machinery) ----------------

class TenantInvite(BaseModel):
    unit_id: str
    name: str
    phone: Optional[str] = None
    email: Optional[str] = None


@app.post("/api/admin/tenants/invite")
def api_invite_tenant(body: TenantInvite, user=Depends(require_admin_or_above)):
    result = create_invite(user, "tenant", body.name, body.phone, body.email, unit_id=body.unit_id)
    link = f"{_link_base()}/invite/{result['token']}"
    return {"link": link, "expires_at": result["expires_at"]}


@app.get("/api/admin/units/{unit_id}/tenants")
def api_list_unit_tenants(unit_id: str, user=Depends(require_admin_or_above)):
    return units_meters.list_unit_tenants(unit_id)


class TenantUpdate(BaseModel):
    name: str
    phone: Optional[str] = None
    email: Optional[str] = None


@app.put("/api/admin/tenants/{tenant_id}")
def api_update_tenant(tenant_id: str, body: TenantUpdate, user=Depends(require_admin_or_above)):
    return update_profile(user, tenant_id, body.name, body.phone, body.email)


@app.put("/api/admin/tenants/{tenant_id}/deactivate")
def api_deactivate_tenant(tenant_id: str, user=Depends(require_admin_or_above)):
    return deactivate_user(user, tenant_id)


@app.post("/api/admin/tenants/{tenant_id}/resend-invite")
def api_resend_tenant_invite(tenant_id: str, user=Depends(require_admin_or_above)):
    result = reissue_invite(user, tenant_id)
    link = f"{_link_base()}/invite/{result['token']}"
    return {"link": link, "expires_at": result["expires_at"]}


# ==================== PHASE 3: Tariff & Billing ====================

@app.get("/api/admin/tariff")
def api_get_tariff(user=Depends(require_admin_or_above)):
    return {"rate_rand_per_kl": billing.get_global_tariff()}


class TariffUpdate(BaseModel):
    rate_rand_per_kl: float


@app.put("/api/admin/tariff")
def api_set_tariff(body: TariffUpdate, user=Depends(require_admin_or_above)):
    billing.set_global_tariff(user, body.rate_rand_per_kl)
    return {"ok": True}


@app.get("/api/admin/units/{unit_id}/billing")
def api_get_billing(unit_id: str, user=Depends(require_admin_or_above)):
    return billing.get_config(unit_id)


class ModeSwitch(BaseModel):
    mode: str


@app.put("/api/admin/units/{unit_id}/billing/mode")
def api_switch_mode(unit_id: str, body: ModeSwitch, user=Depends(require_admin_or_above)):
    return billing.switch_mode(user, unit_id, body.mode)


class TopupRequest(BaseModel):
    amount_rand: float


@app.post("/api/admin/units/{unit_id}/topup")
def api_topup(unit_id: str, body: TopupRequest, user=Depends(require_admin_or_above)):
    return billing.record_topup(user, unit_id, body.amount_rand)


class ValveRequest(BaseModel):
    open: bool


@app.post("/api/admin/units/{unit_id}/valve")
def api_manual_valve(unit_id: str, body: ValveRequest, user=Depends(require_admin_or_above)):
    billing.manual_cutoff(user, unit_id, body.open)
    return {"ok": True}


# ---------------- Reports (Admin) ----------------

@app.get("/api/admin/units/{unit_id}/report.pdf")
def api_admin_unit_report(unit_id: str, period: str = "daily", start: Optional[str] = None,
                           end: Optional[str] = None, user=Depends(require_admin_or_above)):
    s, e, label = _resolve_period(period, start, end)
    pdf = reports.build_unit_usage_pdf(unit_id, s, e, label)
    return Response(content=pdf, media_type="application/pdf",
                     headers={"Content-Disposition": f'attachment; filename="{unit_id}_{period}.pdf"'})


@app.get("/api/admin/reports/building.pdf")
def api_admin_building_report(period: str = "daily", start: Optional[str] = None,
                               end: Optional[str] = None, user=Depends(require_admin_or_above)):
    s, e, label = _resolve_period(period, start, end)
    pdf = reports.build_building_summary_pdf(s, e, label)
    return Response(content=pdf, media_type="application/pdf",
                     headers={"Content-Disposition": f'attachment; filename="building_{period}.pdf"'})


@app.get("/api/admin/units/{unit_id}/statements")
def api_list_statements(unit_id: str, user=Depends(require_admin_or_above)):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM billing_periods WHERE unit_id=? ORDER BY period_end DESC", (unit_id,)
        ).fetchall()
        return [dict(r) for r in rows]


@app.get("/api/admin/statements/{statement_id}/pdf")
def api_admin_statement_pdf(statement_id: str, user=Depends(require_admin_or_above)):
    pdf = reports.build_statement_pdf(statement_id)
    return Response(content=pdf, media_type="application/pdf",
                     headers={"Content-Disposition": f'attachment; filename="statement_{statement_id}.pdf"'})


# ==================== Tenant-facing (strictly scoped to session's own unit) ====================

@app.get("/api/tenant/me/unit")
def api_tenant_unit(user=Depends(require_tenant)):
    return units_meters.get_unit_for_tenant(user["unit_id"])


@app.get("/api/tenant/me/report.pdf")
def api_tenant_report(period: str = "daily", start: Optional[str] = None, end: Optional[str] = None,
                       user=Depends(require_tenant)):
    s, e, label = _resolve_period(period, start, end)
    pdf = reports.build_unit_usage_pdf(user["unit_id"], s, e, label)
    return Response(content=pdf, media_type="application/pdf",
                     headers={"Content-Disposition": f'attachment; filename="my_usage_{period}.pdf"'})


@app.get("/api/tenant/me/statements")
def api_tenant_statements(user=Depends(require_tenant)):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM billing_periods WHERE unit_id=? ORDER BY period_end DESC", (user["unit_id"],)
        ).fetchall()
        return [dict(r) for r in rows]


@app.get("/api/tenant/me/statements/{statement_id}/pdf")
def api_tenant_statement_pdf(statement_id: str, user=Depends(require_tenant)):
    # Ownership check BEFORE generating anything - a tenant must never be able to
    # pull another unit's statement by guessing/incrementing an ID.
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM billing_periods WHERE id=? AND unit_id=?", (statement_id, user["unit_id"])
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Statement not found")
    pdf = reports.build_statement_pdf(statement_id)
    return Response(content=pdf, media_type="application/pdf",
                     headers={"Content-Disposition": f'attachment; filename="statement_{statement_id}.pdf"'})


# ---------------- Static frontend (PWA, with SPA fallback) ----------------
# StaticFiles alone only serves index.html for "/" itself - client-side routes
# like /admin, /super, /tenant, /invite/{token}, /reset/{token} aren't real files
# on disk, so they'd 404 without this fallback. Real static assets (app.js,
# style.css, manifest.json, sw.js, icons/*) are served directly; everything
# else falls back to index.html so app.js's own router can take over.
import os
from fastapi.responses import FileResponse

FRONTEND_DIR = "/app/frontend"


@app.get("/{full_path:path}")
def spa_catch_all(full_path: str):
    candidate = os.path.join(FRONTEND_DIR, full_path)
    if full_path and os.path.isfile(candidate):
        return FileResponse(candidate)
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
