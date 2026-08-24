import logging
from datetime import datetime, timedelta
from fastapi import HTTPException

from database import get_conn
from security import new_id
from audit import log_action
from notify import send_audit_message
import mqtt_service

log = logging.getLogger("billing")

DEFAULT_TARIFF_RAND_PER_KL = 35.0  # fallback if no config value set anywhere


def get_global_tariff() -> float:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM chirpstack_cache WHERE key='global_tariff'").fetchone()
        return float(row["value"]) if row else DEFAULT_TARIFF_RAND_PER_KL


def set_global_tariff(actor, rate_rand_per_kl: float):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO chirpstack_cache (key, value) VALUES ('global_tariff', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(rate_rand_per_kl),),
        )
        conn.commit()
    log_action(actor, "tariff_changed", "config", "global_tariff", after={"rate_rand_per_kl": rate_rand_per_kl})


def effective_tariff(unit_id: str) -> float:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT tariff_override_rand_per_kl FROM billing_config WHERE unit_id=?", (unit_id,)
        ).fetchone()
        if row and row["tariff_override_rand_per_kl"]:
            return row["tariff_override_rand_per_kl"]
    return get_global_tariff()


def get_config(unit_id: str) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM billing_config WHERE unit_id=?", (unit_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="No billing config for this unit")
        return dict(row)


def _latest_reading_m3(conn, unit_id: str):
    row = conn.execute(
        """SELECT ms.last_reading_m3 FROM meter_status ms
           JOIN meters m ON m.id = ms.meter_id
           WHERE m.unit_id=? AND m.status='active'""",
        (unit_id,),
    ).fetchone()
    return row["last_reading_m3"] if row else None


# ---------------- Mode switching (mid-cycle edge case) ----------------

def switch_mode(actor, unit_id: str, new_mode: str):
    """
    Handles the mid-cycle switch edge case explicitly rather than silently
    dropping data:
      monthly -> prepaid: closes out the current monthly period as of NOW
        (a partial-period billing_periods snapshot, clearly marked), then
        starts prepaid with baseline = current reading, zero balance until
        the admin records a top-up.
      prepaid -> monthly: any unused prepaid balance is NOT auto-converted to
        a monthly credit — that's a business decision I'm not making silently.
        The remaining liters are logged clearly in the audit trail (and shown
        in the API response) for the admin to handle manually (e.g. a manual
        credit note, or refund) — then a fresh monthly cycle starts today.
    """
    if new_mode not in ("prepaid", "monthly"):
        raise HTTPException(status_code=400, detail="mode must be 'prepaid' or 'monthly'")

    with get_conn() as conn:
        cfg = conn.execute("SELECT * FROM billing_config WHERE unit_id=?", (unit_id,)).fetchone()
        if not cfg:
            raise HTTPException(status_code=404, detail="Unit not found")
        if cfg["mode"] == new_mode:
            return {"ok": True, "note": "already in that mode"}

        current_reading = _latest_reading_m3(conn, unit_id)
        note = None

        if cfg["mode"] == "monthly" and new_mode == "prepaid":
            if cfg["current_cycle_start_reading_m3"] is not None and current_reading is not None:
                period_id = new_id()
                consumption = round(max(current_reading - cfg["current_cycle_start_reading_m3"], 0), 3)
                amount = round(consumption * effective_tariff(unit_id), 2)
                conn.execute(
                    """INSERT INTO billing_periods (id, unit_id, period_start, period_end, start_reading_m3,
                       end_reading_m3, consumption_m3, tariff_used, amount_due_rand, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'closed')""",
                    (period_id, unit_id, cfg["current_cycle_start_date"], datetime.utcnow().isoformat(),
                     cfg["current_cycle_start_reading_m3"], current_reading, consumption,
                     effective_tariff(unit_id), amount),
                )
                note = f"Closed partial monthly period on mode switch: {consumption} m3, R{amount}"

            conn.execute(
                """UPDATE billing_config SET mode='prepaid', baseline_reading_m3=?, target_reading_m3=?,
                   balance_liters_remaining=0 WHERE unit_id=?""",
                (current_reading, current_reading, unit_id),
            )

        else:  # prepaid -> monthly
            leftover_liters = 0
            if cfg["target_reading_m3"] is not None and current_reading is not None:
                leftover_liters = round(max(cfg["target_reading_m3"] - current_reading, 0) * 1000, 1)
            note = (f"Switched from prepaid to monthly with {leftover_liters} L unused prepaid balance "
                    f"- NOT auto-converted, handle manually (credit note / refund) if applicable.")
            conn.execute(
                """UPDATE billing_config SET mode='monthly', current_cycle_start_reading_m3=?,
                   current_cycle_start_date=?, cycle_start_day=? WHERE unit_id=?""",
                (current_reading, datetime.utcnow().date().isoformat(), datetime.utcnow().day, unit_id),
            )

        conn.commit()

    log_action(actor, "billing_mode_changed", "unit", unit_id,
               before={"mode": cfg["mode"]}, after={"mode": new_mode}, notes=note)
    return {"ok": True, "note": note}


# ---------------- Prepaid ----------------

def record_topup(actor, unit_id: str, amount_rand: float):
    with get_conn() as conn:
        cfg = conn.execute("SELECT * FROM billing_config WHERE unit_id=?", (unit_id,)).fetchone()
        if not cfg or cfg["mode"] != "prepaid":
            raise HTTPException(status_code=400, detail="Unit is not in prepaid mode")

        current_reading = _latest_reading_m3(conn, unit_id)
        if current_reading is None:
            raise HTTPException(status_code=400, detail="No meter reading yet for this unit")

        tariff = effective_tariff(unit_id)
        liters_purchased = round((amount_rand / tariff) * 1000, 1)
        baseline = max(cfg["target_reading_m3"] or current_reading, current_reading)
        target = round(baseline + (liters_purchased / 1000), 3)

        conn.execute(
            """UPDATE billing_config SET baseline_reading_m3=?, target_reading_m3=?,
               balance_liters_remaining=?, valve_target_state='open' WHERE unit_id=?""",
            (current_reading, target, round((target - current_reading) * 1000, 1), unit_id),
        )
        topup_id = new_id()
        conn.execute(
            """INSERT INTO topup_transactions (id, unit_id, amount_rand, liters_purchased, tariff_used,
               baseline_before, target_after, recorded_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (topup_id, unit_id, amount_rand, liters_purchased, tariff, current_reading, target, actor["id"]),
        )
        conn.commit()
        new_balance = round((target - current_reading) * 1000, 1)

    meter = _active_meter_for_unit(unit_id)
    if meter and cfg["valve_target_state"] == "closed":
        _queue_valve_command(meter["dev_eui"], meter["id"], "open", "prepaid_topup_reopen", actor["id"])

    log_action(actor, "topup_recorded", "unit", unit_id,
               after={"amount_rand": amount_rand, "liters_purchased": liters_purchased, "target_reading_m3": target})
    return {
        "target_reading_m3": target,
        "liters_purchased": liters_purchased,
        "tariff_used": tariff,
        "balance_liters_remaining": new_balance,
    }


def evaluate_reading(meter_id: str, obj: dict):
    """Called from mqtt_service on every uplink. Prepaid cutoff check + telemetry-based valve confirmation."""
    current = obj.get("positive_cumulative_flow_m3")
    if current is None:
        return

    with get_conn() as conn:
        meter = conn.execute("SELECT * FROM meters WHERE id=?", (meter_id,)).fetchone()
        if not meter or not meter["unit_id"]:
            return
        cfg = conn.execute("SELECT * FROM billing_config WHERE unit_id=?", (meter["unit_id"],)).fetchone()
        if not cfg:
            return

        if (cfg["mode"] == "prepaid" and cfg["target_reading_m3"] is not None
                and current >= cfg["target_reading_m3"] and cfg["valve_target_state"] != "closed"
                and not cfg["valve_command_pending"]):
            conn.execute(
                "UPDATE billing_config SET valve_target_state='closed', valve_command_pending=1 WHERE unit_id=?",
                (meter["unit_id"],),
            )
            conn.commit()
            _queue_valve_command(meter["dev_eui"], meter_id, "close", "prepaid_auto_cutoff", None)

        pending_cmd = conn.execute(
            "SELECT * FROM valve_commands WHERE meter_id=? AND status='mac_acked' ORDER BY requested_at DESC LIMIT 1",
            (meter_id,),
        ).fetchone()
        if pending_cmd:
            flow = obj.get("instantaneous_flow_m3h", 0) or 0
            confirmed = (pending_cmd["command"] == "close" and flow < 0.001) or \
                        (pending_cmd["command"] == "open" and flow >= 0)
            if confirmed:
                conn.execute(
                    "UPDATE valve_commands SET status='confirmed_by_telemetry', confirmed_at=CURRENT_TIMESTAMP WHERE id=?",
                    (pending_cmd["id"],),
                )
                conn.execute(
                    "UPDATE meter_status SET valve_confirmed_state=? WHERE meter_id=?",
                    (pending_cmd["command"], meter_id),
                )
                conn.execute("UPDATE billing_config SET valve_command_pending=0 WHERE unit_id=?", (meter["unit_id"],))
                conn.commit()
                send_audit_message(f"Valve {pending_cmd['command']} confirmed by telemetry - {meter['dev_eui']}")


# ---------------- Manual cutoff (Monthly mode, arrears) ----------------

def manual_cutoff(actor, unit_id: str, open_valve: bool):
    meter = _active_meter_for_unit(unit_id)
    if not meter:
        raise HTTPException(status_code=400, detail="No active meter on this unit")
    with get_conn() as conn:
        conn.execute(
            "UPDATE billing_config SET valve_target_state=?, valve_command_pending=1 WHERE unit_id=?",
            ("open" if open_valve else "closed", unit_id),
        )
        conn.commit()
    reason = "manual_admin" if open_valve else "monthly_arrears_cutoff"
    _queue_valve_command(meter["dev_eui"], meter["id"], "open" if open_valve else "close", reason, actor["id"])
    log_action(actor, f"valve_manual_{'open' if open_valve else 'close'}", "unit", unit_id)


def _active_meter_for_unit(unit_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM meters WHERE unit_id=? AND status='active'", (unit_id,)).fetchone()
        return dict(row) if row else None


def _queue_valve_command(dev_eui: str, meter_id: str, command: str, reason: str, requested_by):
    with get_conn() as conn:
        cmd_id = new_id()
        conn.execute(
            """INSERT INTO valve_commands (id, meter_id, command, reason, requested_by, status)
               VALUES (?, ?, ?, ?, ?, 'sent_awaiting_rx_window')""",
            (cmd_id, meter_id, command, reason, requested_by),
        )
        conn.commit()
    sent = mqtt_service.send_valve_command(dev_eui, open_valve=(command == "open"))
    log.info("Queued valve %s for %s (reason=%s, dispatch_ok=%s)", command, dev_eui, reason, sent)
    send_audit_message(f"Valve {command} queued for {dev_eui} ({reason}). "
                        f"Class A device - delivers on next check-in, not instant.")
