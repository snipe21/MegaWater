import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from database import get_conn
from security import new_id
from billing import effective_tariff
from notify import send_audit_message

log = logging.getLogger("billing_scheduler")


def check_monthly_rollovers():
    """
    Runs daily. For every unit in monthly mode, checks whether its cycle has
    completed (cycle_start_date + cycle_length_days <= today). If so: snapshots
    the closed period into billing_periods, then starts a fresh cycle from
    today's reading.
    """
    today = datetime.utcnow().date()
    with get_conn() as conn:
        units = conn.execute(
            """SELECT bc.*, u.unit_number FROM billing_config bc
               JOIN units u ON u.id = bc.unit_id
               WHERE bc.mode='monthly' AND bc.current_cycle_start_date IS NOT NULL"""
        ).fetchall()

        for cfg in units:
            start_date = datetime.fromisoformat(cfg["current_cycle_start_date"]).date()
            cycle_len = cfg["cycle_length_days"] or 30
            if (today - start_date).days < cycle_len:
                continue  # not due yet

            current_reading = conn.execute(
                """SELECT ms.last_reading_m3 FROM meter_status ms
                   JOIN meters m ON m.id = ms.meter_id
                   WHERE m.unit_id=? AND m.status='active'""",
                (cfg["unit_id"],),
            ).fetchone()
            if not current_reading or current_reading["last_reading_m3"] is None:
                log.warning("Skipping rollover for unit %s — no reading available", cfg["unit_number"])
                continue

            end_reading = current_reading["last_reading_m3"]
            start_reading = cfg["current_cycle_start_reading_m3"] or end_reading
            consumption = round(max(end_reading - start_reading, 0), 3)
            tariff = effective_tariff(cfg["unit_id"])
            amount = round(consumption * tariff, 2)

            conn.execute(
                """INSERT INTO billing_periods (id, unit_id, period_start, period_end, start_reading_m3,
                   end_reading_m3, consumption_m3, tariff_used, amount_due_rand, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'closed')""",
                (new_id(), cfg["unit_id"], cfg["current_cycle_start_date"], today.isoformat(),
                 start_reading, end_reading, consumption, tariff, amount),
            )
            conn.execute(
                "UPDATE billing_config SET current_cycle_start_reading_m3=?, current_cycle_start_date=? WHERE unit_id=?",
                (end_reading, today.isoformat(), cfg["unit_id"]),
            )
            conn.commit()
            log.info("Closed monthly period for unit %s: %s m3, R%s", cfg["unit_number"], consumption, amount)
            send_audit_message(f"Monthly statement closed - Unit {cfg['unit_number']}: "
                                f"{consumption} m3, R{amount:.2f}")


def check_stalled_valve_commands():
    """Flags any valve command still undelivered after 48h - likely an offline meter."""
    cutoff = (datetime.utcnow() - timedelta(hours=48)).isoformat()
    with get_conn() as conn:
        stalled = conn.execute(
            """SELECT vc.*, m.dev_eui FROM valve_commands vc
               JOIN meters m ON m.id = vc.meter_id
               WHERE vc.status IN ('queued','sent_awaiting_rx_window') AND vc.requested_at < ?""",
            (cutoff,),
        ).fetchall()
        for cmd in stalled:
            send_audit_message(f"WARNING: Valve {cmd['command']} command for {cmd['dev_eui']} "
                                f"still undelivered after 48h - meter may be offline.")


def start_billing_scheduler():
    sched = BackgroundScheduler()
    sched.add_job(check_monthly_rollovers, CronTrigger(hour=2, minute=0))
    sched.add_job(check_stalled_valve_commands, CronTrigger(hour="*/6"))
    sched.start()
    log.info("Billing scheduler started: monthly rollover check daily 02:00, stalled-command check every 6h")
    return sched
