"""Denetim kaydı (audit log). Kayıtlar yalnızca eklenir, değiştirilmez."""
import json

from sqlalchemy import text
from sqlalchemy.engine import Connection


def log_audit(conn: Connection, *, actor: str, action: str, user_id: int | None = None,
              entity_type: str | None = None, entity_id=None, ip: str | None = None,
              details: dict | None = None) -> None:
    conn.execute(text("""
        INSERT INTO audit_logs(user_id, actor, action, entity_type, entity_id, ip, details)
        VALUES (:user_id, :actor, :action, :entity_type, :entity_id, :ip, CAST(:details AS JSONB))
    """), {
        "user_id": user_id, "actor": actor, "action": action, "entity_type": entity_type,
        "entity_id": None if entity_id is None else str(entity_id), "ip": ip,
        "details": json.dumps(details or {}, default=str, ensure_ascii=False),
    })
