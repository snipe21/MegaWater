from fastapi import HTTPException
from database import get_conn
from security import new_id
from audit import log_action
import chirpstack_client


def list_units_with_meters(role_scope_unit_id=None):
    with get_conn() as conn:
        q = """
            SELECT u.id as unit_id, u.unit_number, u.notes,
                   m.id as meter_id, m.dev_eui, m.status as meter_status_col,
                   ms.last_reading_m3, ms.last_reading_at, ms.battery_voltage,
                   ms.battery_low, ms.valve_confirmed_state,
                   t.name as tenant_name, t.id as tenant_id,
                   bc.mode as billing_mode
            FROM units u
            LEFT JOIN meters m ON m.unit_id = u.id AND m.status = 'active'
            LEFT JOIN meter_status ms ON ms.meter_id = m.id
            LEFT JOIN users t ON t.unit_id = u.id AND t.role = 'tenant' AND t.active = 1
            LEFT JOIN billing_config bc ON bc.unit_id = u.id
        """
        params = ()
        if role_scope_unit_id:
            q += " WHERE u.id = ?"
            params = (role_scope_unit_id,)
        q += " ORDER BY u.unit_number"
        rows = conn.execute(q, params).fetchall()
        return [dict(r) for r in rows]


def list_unassigned_meters():
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM meters WHERE status='unassigned' ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]


def create_unit(actor, unit_number: str, notes: str = None):
    uid = new_id()
    with get_conn() as conn:
        existing = conn.execute("SELECT id FROM units WHERE unit_number=?", (unit_number,)).fetchone()
        if existing:
            raise HTTPException(status_code=400, detail="Unit number already exists")
        conn.execute("INSERT INTO units (id, unit_number, notes) VALUES (?, ?, ?)", (uid, unit_number, notes))
        # default billing config: monthly, no override tariff, valve open
        conn.execute(
            "INSERT INTO billing_config (unit_id, mode, valve_target_state) VALUES (?, 'monthly', 'open')",
            (uid,),
        )
        conn.commit()
    log_action(actor, "unit_created", "unit", uid, after={"unit_number": unit_number})
    return {"id": uid, "unit_number": unit_number}


def add_meter(actor, dev_eui: str, app_key: str, unit_id: str = None) -> dict:
    dev_eui = dev_eui.strip().upper()
    with get_conn() as conn:
        existing = conn.execute("SELECT id FROM meters WHERE dev_eui=?", (dev_eui,)).fetchone()
        if existing:
            raise HTTPException(status_code=400, detail="A meter with this DevEUI already exists")

        device_name = f"Meter-{dev_eui[-6:]}"
        if unit_id:
            unit = conn.execute("SELECT unit_number FROM units WHERE id=?", (unit_id,)).fetchone()
            if unit:
                device_name = f"Unit-{unit['unit_number']}"

    try:
        chirpstack_client.create_device(dev_eui, device_name, app_key)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"ChirpStack registration failed: {e}")

    meter_id = new_id()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO meters (id, dev_eui, unit_id, chirpstack_device_name, status)
               VALUES (?, ?, ?, ?, ?)""",
            (meter_id, dev_eui, unit_id, device_name, "active" if unit_id else "unassigned"),
        )
        conn.execute("INSERT INTO meter_status (meter_id) VALUES (?)", (meter_id,))
        conn.commit()

    log_action(actor, "meter_added", "meter", meter_id, after={"dev_eui": dev_eui, "unit_id": unit_id})
    return {"id": meter_id, "dev_eui": dev_eui}


def assign_meter_to_unit(actor, meter_id: str, unit_id: str):
    with get_conn() as conn:
        meter = conn.execute("SELECT * FROM meters WHERE id=?", (meter_id,)).fetchone()
        if not meter:
            raise HTTPException(status_code=404, detail="Meter not found")
        conn.execute("UPDATE meters SET unit_id=?, status='active' WHERE id=?", (unit_id, meter_id))
        conn.commit()
    log_action(actor, "meter_assigned", "meter", meter_id,
               before={"unit_id": meter["unit_id"]}, after={"unit_id": unit_id})


def remove_meter(actor, meter_id: str):
    with get_conn() as conn:
        meter = conn.execute("SELECT * FROM meters WHERE id=?", (meter_id,)).fetchone()
        if not meter:
            raise HTTPException(status_code=404, detail="Meter not found")
        try:
            chirpstack_client.delete_device(meter["dev_eui"])
        except Exception as e:
            # Still remove locally even if ChirpStack delete fails (e.g. already gone) —
            # log it so it's visible, don't block the admin from cleaning up their own records.
            log_action(actor, "chirpstack_delete_failed", "meter", meter_id, notes=str(e))
        conn.execute("UPDATE meters SET status='removed', unit_id=NULL WHERE id=?", (meter_id,))
        conn.commit()
    log_action(actor, "meter_removed", "meter", meter_id, before={"dev_eui": meter["dev_eui"]})


def get_unit_for_tenant(tenant_unit_id: str):
    with get_conn() as conn:
        row = conn.execute(
            """SELECT u.*, m.id as meter_id, ms.last_reading_m3, ms.last_reading_at,
                      ms.battery_voltage, ms.battery_low, ms.valve_confirmed_state,
                      bc.mode, bc.balance_liters_remaining, bc.current_cycle_start_date
               FROM units u
               LEFT JOIN meters m ON m.unit_id = u.id AND m.status='active'
               LEFT JOIN meter_status ms ON ms.meter_id = m.id
               LEFT JOIN billing_config bc ON bc.unit_id = u.id
               WHERE u.id=?""",
            (tenant_unit_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Unit not found")
        return dict(row)
