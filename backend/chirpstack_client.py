"""
Wraps ChirpStack's REST API (the chirpstack-rest-api container, JSON/HTTP gateway
over the gRPC API). Two jobs:
  1. bootstrap_chirpstack() - idempotent, run once at startup: ensure tenant,
     application, and device profile exist on THIS building's ChirpStack instance,
     with our codec already pushed. Never requires touching the ChirpStack web UI.
  2. create_device()/delete_device()/get_device_status() - called when an admin
     adds/removes a meter in the PWA.

IMPORTANT — flagging honestly: the exact REST JSON field names below (tenant,
application, deviceProfile, macVersion enum strings, etc.) are ChirpStack v4's
documented shape from general knowledge, but I have not been able to test this
against your live ChirpStack instance. The first time you run the "add meter"
flow for real, if a call fails, check the error body against your ChirpStack's
own Swagger UI (usually at <chirpstack_rest_url>/api/... or via the ChirpStack
docs) and we'll adjust field names together — same as we debugged the gateway
step by step.
"""
import logging
import requests
from database import get_conn
import settings

log = logging.getLogger("chirpstack_client")

# Your validated codec from the L'Escalier setup — pushed automatically into
# every new building's device profile, no manual paste-into-ChirpStack-UI step.
DEVICE_PROFILE_CODEC = r'''
function decodeUplink(input) {
  var b = input.bytes;
  var result = { raw_hex: toHex(b) };
  if (b.length < 13 || b[0] !== 0x68) {
    return { data: result, errors: ["not a recognised 0x68 frame"] };
  }
  var i = 0;
  i++;
  result.meter_type = b[i++];
  var addr = b.slice(i, i + 7); i += 7;
  result.meter_address = toHex(addr.slice().reverse());
  result.control_code = b[i++];
  var len = b[i++];
  var dataStart = i;
  var di0 = b[i++];
  var di1 = b[i++];
  var ser = b[i++];
  result.data_id = toHex([di0, di1]);
  result.serial = ser;
  var posUnit = b[i++];
  result.positive_cumulative_flow_m3 = bcdLSB(b.slice(i, i + 4)) * flowFactor(posUnit);
  i += 4;
  var negUnit = b[i++];
  result.reverse_cumulative_flow_m3 = bcdLSB(b.slice(i, i + 4)) * flowFactor(negUnit);
  i += 4;
  var instUnit = b[i++];
  result.instantaneous_flow_m3h = bcdLSB(b.slice(i, i + 4)) * 0.0001;
  i += 4;
  result.temperature_c = bcdLSB(b.slice(i, i + 3)) * 0.01;
  i += 3;
  var consumed = i - dataStart;
  var remaining = len - consumed;
  if (remaining === 10) {
    var battRaw = b[i] + (b[i + 1] << 8); i += 2;
    result.battery_voltage = battRaw * 0.01;
    result.protocol_frame = "V2.0";
    var mm = bcdByte(b[i++]);
    var hh = bcdByte(b[i++]);
    var dd = bcdByte(b[i++]);
    var mo = bcdByte(b[i++]);
    var yy = bcdByte(b[i++]);
    var cc = bcdByte(b[i++]);
    result.meter_time = isoTime(cc * 100 + yy, mo, dd, hh, mm, 0);
  } else if (remaining === 9) {
    result.protocol_frame = "V1.0";
    var ss = bcdByte(b[i++]);
    var mi = bcdByte(b[i++]);
    var hr = bcdByte(b[i++]);
    var dy = bcdByte(b[i++]);
    var mn = bcdByte(b[i++]);
    var yr = bcdByte(b[i++]);
    var ct = bcdByte(b[i++]);
    result.meter_time = isoTime(ct * 100 + yr, mn, dy, hr, mi, ss);
  } else {
    result.frame_parse_warning = "unexpected field layout, remaining=" + remaining;
  }
  if (i + 1 < b.length) {
    var raw0 = b[i++];
    var raw1 = b[i++];
    var statusByte = raw1;
    result.battery_low = !!(statusByte & 0x04);
    result.temp_sensor_fault = !!(statusByte & 0x20);
    result.flow_sensor_fault = !!(statusByte & 0x40);
    result.status_raw = toHex([raw0, raw1]);
  }
  return { data: result };
}
function bcdByte(x) { return ((x >> 4) * 10) + (x & 0x0f); }
function bcdLSB(bytes) {
  var val = 0;
  for (var k = bytes.length - 1; k >= 0; k--) { val = val * 100 + bcdByte(bytes[k]); }
  return val;
}
function flowFactor(unitByte) {
  switch (unitByte) {
    case 0x2c: return 0.001;
    case 0x2d: return 0.01;
    case 0x2e: return 0.1;
    case 0x2f: return 1;
    default: return 0.001;
  }
}
function pad(n) { return n < 10 ? "0" + n : "" + n; }
function isoTime(year, mo, dd, hh, mi, ss) {
  return year + "-" + pad(mo) + "-" + pad(dd) + "T" + pad(hh) + ":" + pad(mi) + ":" + pad(ss);
}
function toHex(bytes) {
  return bytes.map(function (x) { return ("0" + x.toString(16)).slice(-2); }).join("").toUpperCase();
}
function encodeDownlink(input) {
  var open = input.data.valve === "open";
  var cmd = open ? 0x55 : 0x99;
  return { bytes: [0xa0, 0x17, 0x01, cmd], fPort: input.fPort || 85 };
}
'''

APPLICATION_NAME = "Water Meters"
DEVICE_PROFILE_NAME = "ZP Ultrasonic Water Meter"


def _headers():
    api_key = settings.get("chirpstack_api_key")
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _get(path, params=None):
    base = settings.get("chirpstack_rest_url")
    r = requests.get(f"{base}{path}", headers=_headers(), params=params, timeout=15)
    if not r.ok:
        raise requests.HTTPError(f"{r.status_code} from ChirpStack {path}: {r.text}", response=r)
    return r.json()


def _post(path, body):
    base = settings.get("chirpstack_rest_url")
    r = requests.post(f"{base}{path}", headers=_headers(), json=body, timeout=15)
    if not r.ok:
        # Surface ChirpStack's own error message instead of the generic
        # "500 Internal Server Error" requests gives by default - that message
        # threw away the actual reason (e.g. "device already exists").
        raise requests.HTTPError(f"{r.status_code} from ChirpStack {path}: {r.text}", response=r)
    return r.json() if r.text else {}


def _put(path, body):
    base = settings.get("chirpstack_rest_url")
    r = requests.put(f"{base}{path}", headers=_headers(), json=body, timeout=15)
    if not r.ok:
        raise requests.HTTPError(f"{r.status_code} from ChirpStack {path}: {r.text}", response=r)
    return r.json() if r.text else {}


def _delete(path):
    base = settings.get("chirpstack_rest_url")
    r = requests.delete(f"{base}{path}", headers=_headers(), timeout=15)
    r.raise_for_status()


def _cache_get(key):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM chirpstack_cache WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


def _cache_set(key, value):
    with get_conn() as conn:
        conn.execute("INSERT INTO chirpstack_cache (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        conn.commit()


def bootstrap_chirpstack():
    """Idempotent. Ensures tenant/application/device-profile exist, caches their IDs."""
    if not settings.get("chirpstack_rest_url") or not settings.get("chirpstack_api_key"):
        log.warning("ChirpStack REST URL/API key not configured — skipping bootstrap. "
                    "Meter management won't work until this is set.")
        return

    try:
        tenant_id = _cache_get("tenant_id")
        if not tenant_id:
            existing = _get("/api/tenants", params={"search": settings.get("chirpstack_tenant_name"), "limit": 10})
            match = next((t for t in existing.get("result", []) if t["name"] == settings.get("chirpstack_tenant_name")), None)
            if match:
                tenant_id = match["id"]
            else:
                created = _post("/api/tenants", {"tenant": {
                    "name": settings.get("chirpstack_tenant_name"),
                    "canHaveGateways": True,
                    "maxDeviceCount": 0,
                    "maxGatewayCount": 0,
                }})
                tenant_id = created["id"]
                log.info("Created ChirpStack tenant %s", settings.get("chirpstack_tenant_name"))
            _cache_set("tenant_id", tenant_id)

        app_id = _cache_get("application_id")
        if not app_id:
            existing = _get("/api/applications", params={"tenantId": tenant_id, "limit": 10})
            match = next((a for a in existing.get("result", []) if a["name"] == APPLICATION_NAME), None)
            if match:
                app_id = match["id"]
            else:
                created = _post("/api/applications", {"application": {
                    "name": APPLICATION_NAME, "tenantId": tenant_id,
                }})
                app_id = created["id"]
                log.info("Created ChirpStack application %s", APPLICATION_NAME)
            _cache_set("application_id", app_id)

        profile_id = _cache_get("device_profile_id")
        if not profile_id:
            existing = _get("/api/device-profiles", params={"tenantId": tenant_id, "limit": 10})
            match = next((p for p in existing.get("result", []) if p["name"] == DEVICE_PROFILE_NAME), None)
            if match:
                profile_id = match["id"]
            else:
                created = _post("/api/device-profiles", {"deviceProfile": {
                    "name": DEVICE_PROFILE_NAME,
                    "tenantId": tenant_id,
                    "region": settings.get("chirpstack_region"),
                    "macVersion": "LORAWAN_1_0_2",
                    "regParamsRevision": "A",
                    "supportsOtaa": True,
                    "payloadCodecRuntime": "JS",
                    "payloadCodecScript": DEVICE_PROFILE_CODEC,
                }})
                profile_id = created["id"]
                log.info("Created ChirpStack device profile %s", DEVICE_PROFILE_NAME)
            _cache_set("device_profile_id", profile_id)

        log.info("ChirpStack bootstrap complete: tenant=%s application=%s profile=%s",
                  tenant_id, app_id, profile_id)
    except requests.RequestException as e:
        log.error("ChirpStack bootstrap failed (will retry on next restart): %s", e)


def create_device(dev_eui: str, name: str, app_key: str):
    app_id = _cache_get("application_id")
    profile_id = _cache_get("device_profile_id")
    if not app_id or not profile_id:
        raise RuntimeError("ChirpStack not bootstrapped yet — check REST URL/API key config")

    _post("/api/devices", {"device": {
        "devEui": dev_eui,
        "name": name,
        "applicationId": app_id,
        "deviceProfileId": profile_id,
        "isDisabled": False,
    }})
    _post(f"/api/devices/{dev_eui}/keys", {"deviceKeys": {
        "devEui": dev_eui,
        "nwkKey": app_key,  # ChirpStack stores the LoRaWAN 1.0.x AppKey under nwkKey
    }})
    log.info("Registered device %s (%s) in ChirpStack", dev_eui, name)


def rename_device(dev_eui: str, new_name: str):
    """
    ChirpStack v4's Update RPC (PUT /api/devices/{dev_eui}) replaces the
    whole device object, per its own proto (UpdateDeviceRequest wraps a
    single full `device` message, same shape as Create) - there's no partial
    PATCH. Sending just {"name": ...} would blank out applicationId,
    deviceProfileId, and everything else ChirpStack didn't hear back about.
    So this reads the current device first, changes only the name, and
    writes the whole thing back - same pattern as a safe read-modify-write.
    """
    current = _get(f"/api/devices/{dev_eui}")
    device = current.get("device") or {}
    if not device:
        raise RuntimeError(f"ChirpStack has no device record for {dev_eui} to rename")
    device["name"] = new_name
    _put(f"/api/devices/{dev_eui}", {"device": device})
    log.info("Renamed ChirpStack device %s to %s", dev_eui, new_name)


def delete_device(dev_eui: str):
    _delete(f"/api/devices/{dev_eui}")
    log.info("Deleted device %s from ChirpStack", dev_eui)


def get_device_status(dev_eui: str):
    return _get(f"/api/devices/{dev_eui}")
