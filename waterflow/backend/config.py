"""
Reads config two ways, transparently:
1. HA Supervisor addon: /data/options.json (written by Supervisor from config.yaml schema)
2. Plain docker-compose: environment variables (see docker-compose.yml)

Supervisor options.json takes priority if present; falls back to env vars.
This lets the exact same Docker image run either as a real HA addon or as a
standalone container, per building, without code changes.
"""
import json
import os

_OPTIONS_PATH = "/data/options.json"


def _load_supervisor_options() -> dict:
    if os.path.exists(_OPTIONS_PATH):
        try:
            with open(_OPTIONS_PATH) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


_opts = _load_supervisor_options()


def _get(key: str, default=None, env_key: str = None):
    if key in _opts and _opts[key] not in (None, ""):
        return _opts[key]
    env_key = env_key or key.upper()
    val = os.environ.get(env_key)
    return val if val not in (None, "") else default


# ---- Branding ----
PRODUCT_NAME = _get("product_name", "WaterFlow")
BUILDING_NAME = _get("building_name", "")
SUPER_ADMIN_APP_NAME = _get("super_admin_app_name", f"{PRODUCT_NAME} Super Admin")
ADMIN_APP_NAME = _get("admin_app_name", f"{PRODUCT_NAME} Admin")
TENANT_APP_NAME = _get("tenant_app_name", PRODUCT_NAME)

# ---- Super Admin root credential ----
SUPER_ADMIN_PASSWORD = _get("super_admin_password", "")

# ---- ChirpStack connection ----
CHIRPSTACK_MQTT_HOST = _get("chirpstack_mqtt_host", "")
CHIRPSTACK_MQTT_PORT = int(_get("chirpstack_mqtt_port", 1883))
CHIRPSTACK_MQTT_USER = _get("chirpstack_mqtt_user", "")
CHIRPSTACK_MQTT_PASS = _get("chirpstack_mqtt_pass", "")
CHIRPSTACK_REST_URL = _get("chirpstack_rest_url", "")
CHIRPSTACK_API_KEY = _get("chirpstack_api_key", "")
CHIRPSTACK_REGION = _get("chirpstack_region", "EU868")
CHIRPSTACK_TENANT_NAME = _get("chirpstack_tenant_name", "")

# ---- Domain ----
DOMAIN_NAME = _get("domain_name", "")

# ---- Optional HA MQTT discovery bonus layer ----
ENABLE_HA_MQTT_DISCOVERY = str(_get("enable_ha_mqtt_discovery", False)).lower() in ("1", "true", "yes")
HA_MQTT_HOST = _get("ha_mqtt_host", "core-mosquitto")
HA_MQTT_PORT = int(_get("ha_mqtt_port", 1883))
HA_MQTT_USER = _get("ha_mqtt_user", "")
HA_MQTT_PASS = _get("ha_mqtt_pass", "")

# ---- Notifications (used from Phase 4 onward, loaded now so config is complete) ----
SMTP_HOST = _get("smtp_host", "")
SMTP_PORT = int(_get("smtp_port", 587))
SMTP_USER = _get("smtp_user", "")
SMTP_PASS = _get("smtp_pass", "")
SMTP_FROM = _get("smtp_from", SMTP_USER)
TELEGRAM_BOT_TOKEN = _get("telegram_bot_token", "")
TELEGRAM_AUDIT_CHAT_ID = _get("telegram_audit_chat_id", "")
TELEGRAM_DIGEST_CHAT_ID = _get("telegram_digest_chat_id", "")
DIGEST_SEND_TIME = _get("digest_send_time", "08:00")

DB_PATH = _get("db_path", "/data/waterflow.db", env_key="DB_PATH")
