"""
Replaces the old HA-addon-options.json config source entirely. All settings
that used to live in config.yaml/docker-compose env vars now live in the
`settings` DB table, editable from the Super Admin PWA's Settings tab -
including export/import as a JSON backup, per your request.

A handful of fields (marked in RESTART_REQUIRED_KEYS) are only read once at
process startup by the modules that use them (MQTT client, ChirpStack client,
etc) - changing them via the API updates the DB immediately but needs the
"Restart WaterFlow" button (or a container restart) to actually take effect.
This mirrors exactly how it worked with HA addon options before, just without
needing Home Assistant to get there.
"""
import json
import logging
from database import get_conn

log = logging.getLogger("settings")

DEFAULTS = {
    # Branding
    "product_name": "WaterFlow",
    "building_name": "",
    "super_admin_app_name": "WaterFlow Super Admin",
    "admin_app_name": "WaterFlow Admin",
    "tenant_app_name": "WaterFlow",

    # ChirpStack connection
    "chirpstack_mqtt_host": "",
    "chirpstack_mqtt_port": 1883,
    "chirpstack_mqtt_user": "",
    "chirpstack_mqtt_pass": "",
    "chirpstack_rest_url": "",
    "chirpstack_api_key": "",
    "chirpstack_region": "EU868",
    "chirpstack_tenant_name": "",

    # Domain
    "domain_name": "",

    # Optional HA MQTT discovery bonus layer
    "enable_ha_mqtt_discovery": False,
    "ha_mqtt_host": "core-mosquitto",
    "ha_mqtt_port": 1883,
    "ha_mqtt_user": "",
    "ha_mqtt_pass": "",

    # Notifications
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_user": "",
    "smtp_pass": "",
    "smtp_from": "",
    "telegram_bot_token": "",
    "telegram_audit_chat_id": "",
    "telegram_digest_chat_id": "",
    "digest_send_time": "08:00",
    "digest_timezone_offset_hours": 2,
}

# Changing any of these requires a process restart to take effect - the
# modules that use them (mqtt_service, chirpstack_client, ha_discovery) only
# read the runtime cache once at startup.
RESTART_REQUIRED_KEYS = {
    "chirpstack_mqtt_host", "chirpstack_mqtt_port", "chirpstack_mqtt_user", "chirpstack_mqtt_pass",
    "chirpstack_rest_url", "chirpstack_api_key", "chirpstack_region", "chirpstack_tenant_name",
    "enable_ha_mqtt_discovery", "ha_mqtt_host", "ha_mqtt_port", "ha_mqtt_user", "ha_mqtt_pass",
}

# Fields that are secrets - flagged so the frontend can show a "contains
# secrets" warning on export, not used to block/mask anything server-side.
SENSITIVE_KEYS = {
    "chirpstack_mqtt_pass", "chirpstack_api_key", "ha_mqtt_pass",
    "smtp_pass", "telegram_bot_token",
}

_cache = {}


def seed_defaults():
    """Idempotent - inserts any default keys not already present in the DB.
    Never overwrites an existing value, so this is safe to call every startup."""
    with get_conn() as conn:
        existing = {row["key"] for row in conn.execute("SELECT key FROM settings").fetchall()}
        for key, val in DEFAULTS.items():
            if key not in existing:
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?)",
                    (key, json.dumps(val)),
                )
        conn.commit()


def load():
    """Populates the in-memory runtime cache from the DB. Call at startup,
    and again after an import, so restart-not-required fields apply live."""
    global _cache
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        _cache = {row["key"]: json.loads(row["value"]) for row in rows}
    log.info("Settings loaded: %d keys", len(_cache))


def get(key: str, default=None):
    if key in _cache:
        return _cache[key]
    return DEFAULTS.get(key, default)


def get_all() -> dict:
    """Full settings dict, defaults filled in for any missing keys - used by
    the Settings tab and by export."""
    result = dict(DEFAULTS)
    result.update(_cache)
    return result


def set_many(updates: dict) -> set:
    """Writes updates to DB and refreshes the cache. Returns the subset of
    changed keys that need a restart, so the API/frontend can warn accordingly."""
    changed_restart_keys = set()
    with get_conn() as conn:
        for key, value in updates.items():
            if key not in DEFAULTS:
                continue  # ignore unknown keys rather than erroring - safer for import of older/newer exports
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )
            if key in RESTART_REQUIRED_KEYS:
                changed_restart_keys.add(key)
        conn.commit()
    load()
    return changed_restart_keys


def export_dict() -> dict:
    return get_all()


def import_dict(data: dict) -> set:
    return set_many(data)
