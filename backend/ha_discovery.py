"""
Optional bonus layer - only active if enable_ha_mqtt_discovery is true in config.
Publishes to Home Assistant's OWN MQTT broker (not ChirpStack's) using HA's MQTT
Discovery format, so meter readings show up as native HA sensor entities.
Entirely separate connection from mqtt_service.py's ChirpStack connection -
see design doc section 5.4 for why these two brokers are never the same one.
"""
import json
import logging
import threading
import paho.mqtt.client as mqtt
import settings

log = logging.getLogger("ha_discovery")

_client = None
_announced = set()  # dev_euis we've already sent discovery config for, avoid re-announcing every uplink


def start_ha_discovery_client():
    global _client
    if not settings.get("enable_ha_mqtt_discovery"):
        return None
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    if settings.get("ha_mqtt_user"):
        client.username_pw_set(settings.get("ha_mqtt_user"), settings.get("ha_mqtt_pass"))

    def run():
        try:
            client.connect(settings.get("ha_mqtt_host"), settings.get("ha_mqtt_port"), keepalive=60)
            client.loop_start()
            log.info("Connected to HA's MQTT broker for discovery")
        except Exception as e:
            log.warning("Could not connect to HA MQTT broker: %s", e)

    threading.Thread(target=run, daemon=True).start()
    _client = client
    return client


def _announce(dev_eui: str):
    base = f"waterflow/{dev_eui}"
    sensors = {
        "reading": ("Water Reading", "m³", "water"),
        "battery": ("Battery Voltage", "V", "voltage"),
        "flow": ("Instantaneous Flow", "m³/h", None),
    }
    for key, (name, unit, device_class) in sensors.items():
        config_topic = f"homeassistant/sensor/waterflow_{dev_eui}_{key}/config"
        payload = {
            "name": f"{name} {dev_eui[-6:]}",
            "state_topic": f"{base}/state",
            "value_template": f"{{{{ value_json.{key} }}}}",
            "unit_of_measurement": unit,
            "unique_id": f"waterflow_{dev_eui}_{key}",
            "device": {"identifiers": [f"waterflow_{dev_eui}"], "name": f"Water Meter {dev_eui[-6:]}",
                       "manufacturer": "ZP", "model": "Ultrasonic Valve-Controlled Water Meter"},
        }
        if device_class:
            payload["device_class"] = device_class
        _client.publish(config_topic, json.dumps(payload), retain=True)
    _announced.add(dev_eui)


def publish_reading(dev_eui: str, obj: dict):
    if not _client:
        return
    if dev_eui not in _announced:
        _announce(dev_eui)
    state = {
        "reading": obj.get("positive_cumulative_flow_m3"),
        "battery": obj.get("battery_voltage"),
        "flow": obj.get("instantaneous_flow_m3h"),
    }
    _client.publish(f"waterflow/{dev_eui}/state", json.dumps(state))
