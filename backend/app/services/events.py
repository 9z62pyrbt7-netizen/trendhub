"""Sistem olayları (Sistem / Hatalar ekranı).

Aynı `fingerprint` ile açık bir olay varsa yeni satır açılmaz, sayaç artar;
böylece tekrarlayan hatalar ekranı doldurmaz.
"""
import json

from sqlalchemy import text
from sqlalchemy.engine import Connection


def record_event(conn: Connection, *, level: str, source: str, message: str,
                 details: dict | None = None, fingerprint: str | None = None) -> None:
    params = {"level": level, "source": source, "message": message[:2000],
              "details": json.dumps(details or {}, default=str, ensure_ascii=False),
              "fingerprint": fingerprint}
    if fingerprint:
        updated = conn.execute(text("""
            UPDATE system_events
               SET occurrences = occurrences + 1, last_occurred_at = NOW(),
                   message = :message, details = CAST(:details AS JSONB), level = :level
             WHERE fingerprint = :fingerprint AND resolved_at IS NULL
        """), params).rowcount
        if updated:
            return
    conn.execute(text("""
        INSERT INTO system_events(level, source, message, details, fingerprint)
        VALUES (:level, :source, :message, CAST(:details AS JSONB), :fingerprint)
        ON CONFLICT (fingerprint) WHERE resolved_at IS NULL AND fingerprint IS NOT NULL
        DO UPDATE SET occurrences = system_events.occurrences + 1, last_occurred_at = NOW()
    """), params)


def resolve_fingerprint(conn: Connection, fingerprint: str) -> None:
    conn.execute(text("""UPDATE system_events SET resolved_at = NOW()
                          WHERE fingerprint = :f AND resolved_at IS NULL"""), {"f": fingerprint})
