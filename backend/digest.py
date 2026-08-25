"""
Daily digest: a per-unit usage summary for the previous day, sent as a
Telegram text message plus a CSV attachment with full detail, to
telegram_digest_chat_id. Separate from notify.send_audit_message's
audit-event channel - this is a scheduled rollup, not an event trigger.

Each active meter gets one digest_log row per day it's reported on,
recording which reading was used and whether the meter went quiet that day
("stale") - this is what already_sent() checks to avoid a duplicate send if
the scheduler's window happens to trigger more than once in a day.
"""
import csv
import io
import logging
from datetime import datetime, timedelta

import requests

import settings
from database import get_conn
from reports import _usage_between, _org_name

log = logging.getLogger("digest")

TELEGRAM_MESSAGE_LIMIT = 3500  # stay comfortably under Telegram's 4096-char cap


def _send_telegram_text(token, chat_id, text):
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": text},
        timeout=15,
    )
    r.raise_for_status()


def _send_telegram_document(token, chat_id, filename, content_bytes, caption=None):
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendDocument",
        data=data,
        files={"document": (filename, content_bytes, "text/csv")},
        timeout=30,
    )
    r.raise_for_status()


def already_sent(digest_date_str: str) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM digest_log WHERE digest_date=? LIMIT 1", (digest_date_str,)).fetchone()
        return row is not None


def compose_and_send_digest(for_date=None) -> bool:
    token = settings.get("telegram_bot_token")
    chat_id = settings.get("telegram_digest_chat_id")
    if not token or not chat_id:
        log.info("Digest not configured (missing bot token or digest chat id) - skipping")
        return False

    target_date = for_date or (datetime.utcnow().date() - timedelta(days=1))
    date_str = target_date.isoformat()
    period_start = f"{date_str}T00:00:00"
    period_end = f"{(target_date + timedelta(days=1)).isoformat()}T00:00:00"

    rows = []
    total_liters = 0.0
    stale_count = 0

    with get_conn() as conn:
        units = conn.execute(
            """SELECT u.id as unit_id, u.unit_number, m.id as meter_id, m.dev_eui,
                      ms.last_reading_at
               FROM units u
               LEFT JOIN meters m ON m.unit_id = u.id AND m.status='active'
               LEFT JOIN meter_status ms ON ms.meter_id = m.id
               ORDER BY u.unit_number"""
        ).fetchall()

        for u in units:
            if not u["meter_id"]:
                rows.append({"unit_number": u["unit_number"], "dev_eui": None, "status": "no meter",
                             "usage_l": None, "last_seen": None})
                continue

            start_val, end_val, _ = _usage_between(conn, u["unit_id"], period_start, period_end)
            last_seen = u["last_reading_at"]
            # "Stale" here means the meter itself went quiet for the day being
            # reported on - not that we lack a baseline entirely (that's the
            # separate "no data yet" case below, for a meter that's never reported).
            had_reading_in_window = conn.execute(
                """SELECT 1 FROM readings WHERE meter_id=? AND ts>=? AND ts<?
                   AND positive_cumulative_flow_m3 IS NOT NULL LIMIT 1""",
                (u["meter_id"], period_start, period_end),
            ).fetchone() is not None
            is_stale = not had_reading_in_window

            if start_val is None or end_val is None:
                rows.append({"unit_number": u["unit_number"], "dev_eui": u["dev_eui"], "status": "no data yet",
                             "usage_l": None, "last_seen": last_seen})
                stale_count += 1
                continue

            usage_l = round(max(end_val - start_val, 0) * 1000, 1)
            total_liters += usage_l
            if is_stale:
                stale_count += 1
            rows.append({"unit_number": u["unit_number"], "dev_eui": u["dev_eui"],
                         "status": "stale" if is_stale else "ok", "usage_l": usage_l, "last_seen": last_seen})

            conn.execute(
                """INSERT INTO digest_log (meter_id, digest_date, reading_used_m3, reading_used_at, was_stale)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(meter_id, digest_date) DO UPDATE SET
                     reading_used_m3=excluded.reading_used_m3, reading_used_at=excluded.reading_used_at,
                     was_stale=excluded.was_stale""",
                (u["meter_id"], date_str, end_val, last_seen, 1 if is_stale else 0),
            )
        conn.commit()

    org = _org_name()
    lines = [f"{org} - Daily Water Digest", f"Date: {date_str}", ""]
    for r in rows:
        if r["usage_l"] is None:
            lines.append(f"Unit {r['unit_number']}: {r['status']}")
        else:
            flag = " (stale)" if r["status"] == "stale" else ""
            lines.append(f"Unit {r['unit_number']}: {r['usage_l']:.1f} L{flag}")
    lines.append("")
    lines.append(f"Building total: {total_liters:.1f} L")
    if stale_count:
        lines.append(f"{stale_count} meter(s) had no reading for this period.")
    text = "\n".join(lines)

    csv_buf = io.StringIO()
    writer = csv.writer(csv_buf)
    writer.writerow(["Unit", "DevEUI", "Status", "Usage (L)", "Last Seen"])
    for r in rows:
        writer.writerow([r["unit_number"], r["dev_eui"] or "", r["status"],
                          r["usage_l"] if r["usage_l"] is not None else "", r["last_seen"] or ""])
    csv_bytes = csv_buf.getvalue().encode("utf-8")

    try:
        for i in range(0, len(text), TELEGRAM_MESSAGE_LIMIT):
            _send_telegram_text(token, chat_id, text[i:i + TELEGRAM_MESSAGE_LIMIT])
        _send_telegram_document(token, chat_id, f"digest-{date_str}.csv", csv_bytes,
                                 caption=f"Full detail - {date_str}")
        log.info("Digest sent for %s: %d units, %.1f L total, %d stale/no-data",
                  date_str, len(rows), total_liters, stale_count)
        return True
    except Exception as e:
        log.error("Failed to send digest for %s: %s", date_str, e)
        return False
