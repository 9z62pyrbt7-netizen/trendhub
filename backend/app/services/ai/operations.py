"""Operasyon ajanı (Workflow Optimizer uyarlaması): sorun görünce CEO'ya olay (incident) açar, sorun bitince kapatır.

Döngüde çalışır (AGENT_ORDER). Olaylar tekilleştirilir (açık olay başına tek satır, tekrar sayısı artar).
"""
from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ...db import rows
from .config import thresholds


def _detect(conn: Connection) -> list[dict]:
    from .tools import operations_health
    th = thresholds(conn)
    h = operations_health(conn)
    out = []
    if h["failed_jobs_24h"] >= int(th["failed_jobs_incident"]):
        out.append({"key": "jobs_failed", "severity": "warning", "category": "queue",
                    "title": f"Son 24 saatte {h['failed_jobs_24h']} kuyruk işi başarısız oldu",
                    "details": {"by_type": h["failed_job_types"]}})
    if h["stuck_jobs"]:
        out.append({"key": "jobs_stuck", "severity": "warning", "category": "queue",
                    "title": f"{h['stuck_jobs']} iş 30 dakikadan uzun süredir 'running' durumunda (worker takılmış olabilir)", "details": {}})
    if h["unprocessed_orders"]:
        out.append({"key": "orders_unprocessed", "severity": "warning", "category": "orders",
                    "title": f"{h['unprocessed_orders']} sipariş {th['unprocessed_order_hours']} saatten uzun süredir işlenmedi",
                    "details": {"oldest": h["oldest_unprocessed"]}})
    if h["unmatched_order_lines_30d"]:
        out.append({"key": "order_lines_unmatched", "severity": "info", "category": "data",
                    "title": f"Son 30 günde {h['unmatched_order_lines_30d']} sipariş satırı ürüne eşlenmedi (kâr analizinde görünmez)",
                    "details": {}})
    for s in h["stale_sources"]:
        out.append({"key": f"source:{s['code']}", "severity": "critical" if s["freshness"] == "ERROR" else "warning",
                    "category": "integration", "title": f"{s['label']}: {s['freshness']}", "details": s})
    if h["platform_events_failed_7d"]:
        out.append({"key": "platform_events_failed", "severity": "warning", "category": "integration",
                    "title": f"Son 7 günde {h['platform_events_failed_7d']} platform olayı işlenemedi", "details": {}})
    for e in h["open_system_errors"][:5]:
        out.append({"key": f"system:{e['source']}", "severity": "warning", "category": "api",
                    "title": f"Sistem hatası ({e['source']}): {e['message']}", "details": {"occurrences": e["occurrences"]}})
    return out


def run_operations(conn: Connection, ctx) -> dict:
    from .proposals import activity
    ctx.sources += ["sync_jobs", "worker_heartbeats", "orders", "system_events", "platform_events", "veri kaynakları"]
    found = _detect(conn)
    keys = {f["key"] for f in found}
    opened = 0
    for f in found:
        r = conn.execute(text("""UPDATE ai_incidents SET occurrences = occurrences + 1, last_seen = NOW(), title = :t,
                                        details = CAST(:d AS JSONB), severity = :s
                                  WHERE dedupe_key = :k AND status = 'open' RETURNING id"""),
                         {"k": f["key"], "t": f["title"], "d": json.dumps(f["details"], default=str), "s": f["severity"]}).first()
        if r:
            continue
        iid = conn.execute(text("""INSERT INTO ai_incidents(dedupe_key, severity, category, title, details)
                                   VALUES (:k, :s, :c, :t, CAST(:d AS JSONB)) RETURNING id"""),
                           {"k": f["key"], "s": f["severity"], "c": f["category"], "t": f["title"],
                            "d": json.dumps(f["details"], default=str)}).scalar()
        conn.execute(text("""INSERT INTO ai_agent_messages(from_agent, to_agent, kind, content, data)
                             VALUES ('operations', 'ceo', 'incident', :m, CAST(:d AS JSONB))"""),
                     {"m": f["title"], "d": json.dumps({"incident_id": iid, "severity": f["severity"]})})
        activity(conn, f"Operasyon olay açtı (#{iid}): {f['title']}", agent="operations", kind="incident",
                 level="error" if f["severity"] == "critical" else "warning", run_id=ctx.run_id)
        opened += 1
    resolved = [r["id"] for r in rows(conn, """UPDATE ai_incidents SET status = 'resolved', resolved_at = NOW()
                                                 WHERE status = 'open' AND opened_by = 'operations' AND NOT (dedupe_key = ANY(:k))
                                                 RETURNING id""", k=list(keys))]
    for iid in resolved:
        activity(conn, f"Olay #{iid} kapandı (koşul ortadan kalktı)", agent="operations", kind="incident", level="success",
                 run_id=ctx.run_id)
    if any(f["severity"] == "critical" for f in found):
        ctx.warnings.append("Kritik operasyon olayı açık")
    ctx.output = {"detected": len(found), "opened": opened, "resolved": len(resolved)}
    return ctx.output


def open_incidents(conn: Connection) -> list[dict]:
    return rows(conn, """SELECT id, severity, category, title, occurrences, first_seen, last_seen FROM ai_incidents
                          WHERE status = 'open' ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, id""")
