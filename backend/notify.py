import logging
import requests
import settings

log = logging.getLogger("notify")


def send_audit_message(text: str):
    token = settings.get("telegram_bot_token")
    chat_id = settings.get("telegram_audit_chat_id")
    if not token or not chat_id:
        return  # not configured yet — audit still recorded in DB regardless
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text},
            timeout=10,
        )
    except Exception as e:
        log.warning("Failed to send audit telegram message: %s", e)
