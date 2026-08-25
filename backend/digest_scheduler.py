"""
digest_send_time and the timezone offset are NOT in settings.RESTART_REQUIRED_KEYS
on purpose - they're meant to take effect live, without a container restart.
That means this can't use a single fixed CronTrigger(hour=X, minute=Y) the way
billing_scheduler's rollover check does (that job's schedule genuinely is
fixed). Instead this runs frequently and checks the *current* setting value
each time, firing only within the 5-minute window containing the configured
send time - and only once per day, using digest_log itself as the "already
sent today" record so a restart or a slow tick can't double-send.
"""
import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import settings
from digest import compose_and_send_digest, already_sent

log = logging.getLogger("digest_scheduler")


def check_and_send_digest():
    send_time = settings.get("digest_send_time") or "08:00"
    try:
        target_hour, target_minute = (int(x) for x in send_time.strip().split(":"))
    except Exception:
        log.warning("digest_send_time %r is not valid HH:MM - skipping this check", send_time)
        return

    offset_hours = settings.get("digest_timezone_offset_hours")
    if offset_hours is None:
        offset_hours = 2
    local_now = datetime.utcnow() + timedelta(hours=offset_hours)

    target_minutes_of_day = target_hour * 60 + target_minute
    now_minutes_of_day = local_now.hour * 60 + local_now.minute
    if abs(now_minutes_of_day - target_minutes_of_day) > 4:
        return

    report_date = (local_now - timedelta(days=1)).date()
    if already_sent(report_date.isoformat()):
        return

    compose_and_send_digest(for_date=report_date)


def start_digest_scheduler():
    sched = BackgroundScheduler()
    sched.add_job(check_and_send_digest, CronTrigger(minute="*/5"))
    sched.start()
    log.info("Digest scheduler started: checking every 5 min against the configured digest_send_time")
    return sched
