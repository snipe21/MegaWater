import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import settings
from database import init_db, get_conn
from auth_service import (
    bootstrap_super_admin, login, logout, create_invite, get_invite_info, accept_invite,
    request_reset, list_pending_resets, approve_reset, deny_reset, get_reset_info, accept_reset,
    update_profile, deactivate_user, reissue_invite, change_own_password, hard_delete_user,
)
from deps import require_user, require_super_admin, require_admin_or_above, require_tenant
from qr import qr_png_bytes
from audit import log_action
import webauthn_service

import chirpstack_client
import mqtt_service
import ha_discovery
import units_meters
import billing
import billing_scheduler
import reports

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("main")

app = FastAPI(title="WaterFlow Water Metering")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
def startup():
    init_db()
    settings.seed_defaults()
    settings.load()  # must happen before any module below reads a setting
    bootstrap_super_admin()
    chirpstack_client.bootstrap_chirpstack()
    mqtt_service.start_mqtt_service()
    ha_discovery.start_ha_discovery_client()
    billing_scheduler.start_billing_scheduler()
    log.info("%s backend started (standalone, Phases 1-3)", settings.get("product_name"))


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
    domain = settings.get("domain_name")
    if domain:
        return f"https://{domain}"
    return ""  # frontend falls back to window.location.origin for LAN-only testing


# ---------------- Public config (branding, no secrets) ----------------

@app.get("/api/public-config")
def public_config():
    return {
        "product_name": settings.get("product_name"),
        "building_name": settings.get("building_name"),
        "super_admin_app_name": settings.get("super_admin_app_name"),
        "admin_app_name": settings.get("admin_app_name"),
        "tenant_app_name": settings.get("tenant_app_name"),
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


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


@app.post("/api/auth/change-password")
def api_change_password(body: ChangePasswordRequest, user=Depends(require_user)):
    return change_own_password(user, body.current_password, body.new_password)


# ---------------- WebAuthn (fingerprint / Face ID) ----------------

@app.post("/api/auth/webauthn/register/options")
def api_webauthn_register_options(user=Depends(require_user)):
    return webauthn_service.register_options(user)


class WebAuthnRegisterVerify(BaseModel):
    challenge_id: str
    credential: dict
    device_label: Optional[str] = None


@app.post("/api/auth/webauthn/register/verify")
def api_webauthn_register_verify(body: WebAuthnRegisterVerify, user=Depends(require_user)):
    return webauthn_service.register_verify(user, body.challenge_id, body.credential, body.device_label)


@app.get("/api/auth/webauthn/credentials")
def api_webauthn_list(user=Depends(require_user)):
    return webauthn_service.list_credentials(user)


@app.delete("/api/auth/webauthn/credentials/{credential_row_id}")
def api_webauthn_delete(credential_row_id: str, user=Depends(require_user)):
    return webauthn_service.remove_credential(user, credential_row_id)


@app.post("/api/auth/webauthn/login/options")
def api_webauthn_login_options():
    return webauthn_service.login_options()


class WebAuthnLoginVerify(BaseModel):
    challenge_id: str
    credential: dict


@app.post("/api/auth/webauthn/login/verify")
def api_webauthn_login_verify(body: WebAuthnLoginVerify):
    return webauthn_service.login_verify(body.challenge_id, body.credential)


# ---------------- Super Admin: system settings (replaces HA addon options) ----------------

@app.get("/api/super/settings")
def api_get_settings(user=Depends(require_super_admin)):
    return {
        "values": settings.get_all(),
        "restart_required_keys": sorted(settings.RESTART_REQUIRED_KEYS),
        "sensitive_keys": sorted(settings.SENSITIVE_KEYS),
    }


@app.put("/api/super/settings")
def api_update_settings(body: dict, user=Depends(require_super_admin)):
    restart_keys = settings.set_many(body)
    log_action(user, "settings_updated", "config", "settings", after={"keys": list(body.keys())})
    return {"ok": True, "restart_required": sorted(restart_keys)}


@app.get("/api/super/settings/export")
def api_export_settings(user=Depends(require_super_admin)):
    import json
    data = json.dumps(settings.export_dict(), indent=2)
    log_action(user, "settings_exported", "config", "settings")
    return Response(content=data, media_type="application/json", headers={
        "Content-Disposition": 'attachment; filename="waterflow-settings-backup.json"'
    })


@app.post("/api/super/settings/import")
def api_import_settings(body: dict, user=Depends(require_super_admin)):
    restart_keys = settings.import_dict(body)
    log_action(user, "settings_imported", "config", "settings")
    return {"ok": True, "restart_required": sorted(restart_keys)}


@app.post("/api/super/restart")
def api_restart(user=Depends(require_super_admin)):
    import os, threading, time
    log_action(user, "system_restart_triggered", "config", "system")

    def _exit_soon():
        time.sleep(1)  # give the response time to actually reach the browser first
        os._exit(0)

    threading.Thread(target=_exit_soon, daemon=True).start()
    return {"ok": True, "note": "Restarting now - Docker's restart policy will bring it back up in a few seconds."}


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


@app.delete("/api/super/admins/{admin_id}")
def api_delete_admin(admin_id: str, user=Depends(require_super_admin)):
    return hard_delete_user(user, admin_id)


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


@app.get("/api/admin/tenants")
def api_list_all_tenants(user=Depends(require_admin_or_above)):
    return units_meters.list_all_tenants()


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


@app.delete("/api/admin/tenants/{tenant_id}")
def api_delete_tenant(tenant_id: str, user=Depends(require_admin_or_above)):
    return hard_delete_user(user, tenant_id)


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
#
# no-cache (not no-store) on every response here: Cloudflare's edge caches
# static extensions like .js/.json by default even with no Cache-Control
# header at all, so without this a deploy can sit invisible behind
# Cloudflare's own cache regardless of anything done in the browser.
# no-cache still allows a cached copy to be reused, but only after
# revalidating with the origin (via the ETag/Last-Modified FileResponse
# already sets) - so every request gets this build's actual bytes.
import os
from fastapi.responses import FileResponse

FRONTEND_DIR = "/app/frontend"
NO_CACHE_HEADERS = {"Cache-Control": "no-cache, must-revalidate"}


@app.get("/{full_path:path}")
def spa_catch_all(full_path: str):
    candidate = os.path.join(FRONTEND_DIR, full_path)
    if full_path and os.path.isfile(candidate):
        return FileResponse(candidate, headers=NO_CACHE_HEADERS)
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"), headers=NO_CACHE_HEADERS)
