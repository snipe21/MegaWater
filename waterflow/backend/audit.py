import json
from database import get_conn
from notify import send_audit_message


def log_action(actor_user, action_type: str, target_type: str = None, target_id: str = None,
                before=None, after=None, notes: str = None):
    """
    actor_user: dict-like with 'id' and 'role', or None for system-initiated actions.
    Writes the audit row AND pushes to Telegram in one place, so nothing can be
    written without being logged.
    """
    actor_id = actor_user["id"] if actor_user else None
    actor_role = actor_user["role"] if actor_user else "system"

    with get_conn() as conn:
        conn.execute(
            """INSERT INTO audit_log (actor_user_id, actor_role, action_type, target_type,
               target_id, before_value, after_value, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (actor_id, actor_role, action_type, target_type, target_id,
             json.dumps(before) if before is not None else None,
             json.dumps(after) if after is not None else None,
             notes),
        )
        conn.commit()

    actor_label = f"{actor_role}:{actor_id}" if actor_id else "system"
    msg = f"🔧 {action_type}\nBy: {actor_label}\nTarget: {target_type or '-'} {target_id or ''}"
    if notes:
        msg += f"\n{notes}"
    send_audit_message(msg)
