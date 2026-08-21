import logging
import requests
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_AUDIT_CHAT_ID

log = logging.getLogger("notify")


def send_audit_message(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_AUDIT_CHAT_ID:
        return  # not configured yet — audit still recorded in DB regardless
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_AUDIT_CHAT_ID, "text": text},
            timeout=10,
        )
    except Exception as e:
        log.warning("Failed to send audit telegram message: %s", e)
