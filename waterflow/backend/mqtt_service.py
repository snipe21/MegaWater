import json
import logging
import threading
import time
import paho.mqtt.client as mqtt

from database import get_conn
import config

log = logging.getLogger("mqtt_service")

UPLINK_TOPIC = "application/+/device/+/event/up"
ACK_TOPIC = "application/+/device/+/event/ack"

_client = None
_application_id_cache = None


def _get_application_id():
    global _application_id_cache
    if _application_id_cache:
        return _application_id_cache
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM chirpstack_cache WHERE key='application_id'").fetchone()
        _application_id_cache = row["value"] if row else None
        return _application_id_cache


# ---------------- Uplink ingestion ----------------

def _handle_uplink(payload: dict):
    dev_eui = (payload.get("deviceInfo", {}).get("devEui") or "").upper()
    if not dev_eui:
        return
    obj = payload.get("object") or {}
    ts = payload.get("time") or payload.get("rxInfo", [{}])[0].get("time")

    with get_conn() as conn:
        meter = conn.execute("SELECT * FROM meters WHERE dev_eui=?", (dev_eui,)).fetchone()
        if not meter:
            # Unknown device uplinking — likely registered in ChirpStack directly rather
            # than through our "add meter" flow. Log it but don't silently store orphan
            # readings with no meter_id FK target.
            log.warning("Uplink from unregistered DevEUI %s — add it via the Admin PWA first", dev_eui)
            return
        meter_id = meter["id"]

        conn.execute(
            """INSERT INTO readings (meter_id, ts, positive_cumulative_flow_m3, reverse_cumulative_flow_m3,
               instantaneous_flow_m3h, temperature_c, battery_voltage, battery_low, flow_sensor_fault, raw_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (meter_id, ts, obj.get("positive_cumulative_flow_m3"), obj.get("reverse_cumulative_flow_m3"),
             obj.get("instantaneous_flow_m3h"), obj.get("temperature_c"), obj.get("battery_voltage"),
             1 if obj.get("battery_low") else 0, 1 if obj.get("flow_sensor_fault") else 0, json.dumps(obj)),
        )
        conn.execute(
            """INSERT INTO meter_status (meter_id, last_reading_m3, last_reading_at, battery_voltage, battery_low)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(meter_id) DO UPDATE SET
                 last_reading_m3=excluded.last_reading_m3, last_reading_at=excluded.last_reading_at,
                 battery_voltage=excluded.battery_voltage, battery_low=excluded.battery_low""",
            (meter_id, obj.get("positive_cumulative_flow_m3"), ts, obj.get("battery_voltage"),
             1 if obj.get("battery_low") else 0),
        )
        conn.execute("UPDATE meters SET last_seen_at=? WHERE id=?", (ts, meter_id))
        conn.commit()

    log.info("Stored reading for %s: %s m3", dev_eui, obj.get("positive_cumulative_flow_m3"))

    # Hook for Phase 3 billing evaluation (prepaid cutoff check) and telemetry-based
    # valve confirmation — imported lazily to avoid a circular import at module load time.
    try:
        import billing
        billing.evaluate_reading(meter_id, obj)
    except Exception as e:
        log.error("Billing evaluation failed for meter %s: %s", meter_id, e)

    if config.ENABLE_HA_MQTT_DISCOVERY:
        try:
            import ha_discovery
            ha_discovery.publish_reading(dev_eui, obj)
        except Exception as e:
            log.warning("HA MQTT discovery publish failed: %s", e)


# ---------------- Downlink ACK tracking ----------------

def _handle_ack(payload: dict):
    dev_eui = (payload.get("deviceInfo", {}).get("devEui") or "").upper()
    acknowledged = payload.get("acknowledged", False)
    if not dev_eui:
        return
    with get_conn() as conn:
        meter = conn.execute("SELECT id FROM meters WHERE dev_eui=?", (dev_eui,)).fetchone()
        if not meter:
            return
        cmd = conn.execute(
            """SELECT * FROM valve_commands WHERE meter_id=? AND status IN ('queued','sent_awaiting_rx_window')
               ORDER BY requested_at DESC LIMIT 1""",
            (meter["id"],),
        ).fetchone()
        if not cmd:
            return
        new_status = "mac_acked" if acknowledged else "failed"
        conn.execute(
            "UPDATE valve_commands SET status=?, delivered_at=CURRENT_TIMESTAMP WHERE id=?",
            (new_status, cmd["id"]),
        )
        conn.commit()
    log.info("Valve command %s for %s: %s", cmd["id"], dev_eui, new_status)


# ---------------- MQTT client lifecycle ----------------

def _on_connect(client, userdata, flags, rc, properties=None):
    if rc == 0:
        log.info("Connected to ChirpStack MQTT broker")
        client.subscribe(UPLINK_TOPIC)
        client.subscribe(ACK_TOPIC)
    else:
        log.error("MQTT connect failed rc=%s", rc)


def _on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except Exception as e:
        log.warning("Failed to parse MQTT payload: %s", e)
        return
    if "/event/up" in msg.topic:
        _handle_uplink(payload)
    elif "/event/ack" in msg.topic:
        _handle_ack(payload)


def start_mqtt_service():
    global _client
    if not config.CHIRPSTACK_MQTT_HOST:
        log.warning("chirpstack_mqtt_host not configured — MQTT service not started")
        return None

    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    if config.CHIRPSTACK_MQTT_USER:
        client.username_pw_set(config.CHIRPSTACK_MQTT_USER, config.CHIRPSTACK_MQTT_PASS)
    client.on_connect = _on_connect
    client.on_message = _on_message

    def run():
        while True:
            try:
                client.connect(config.CHIRPSTACK_MQTT_HOST, config.CHIRPSTACK_MQTT_PORT, keepalive=60)
                client.loop_forever()
            except Exception as e:
                log.error("MQTT connection error, retrying in 10s: %s", e)
                time.sleep(10)

    threading.Thread(target=run, daemon=True).start()
    _client = client
    return client


# ---------------- Downlink dispatch ----------------

def send_valve_command(dev_eui: str, open_valve: bool) -> bool:
    """
    Publishes the DECODED object form so ChirpStack runs our existing
    encodeDownlink codec server-side — we never duplicate the byte-encoding here.
    """
    if not _client:
        log.error("MQTT client not connected — cannot send valve command")
        return False
    app_id = _get_application_id()
    if not app_id:
        log.error("Application ID not cached — has bootstrap run?")
        return False

    topic = f"application/{app_id}/device/{dev_eui.lower()}/command/down"
    body = {
        "confirmed": True,
        "fPort": 85,
        "object": {"valve": "open" if open_valve else "close"},
    }
    _client.publish(topic, json.dumps(body), qos=1)
    log.info("Published valve %s command to %s", "open" if open_valve else "close", dev_eui)
    return True
