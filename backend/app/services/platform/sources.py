"""Dış veri kaynaklarının tazelik / hata durumu.

Her kaynak için: last_successful_sync, last_attempt, freshness_status, error_count, source.
Bağlantı durumu (connection): CONNECTED | PERMISSION_DENIED | NO_DATA | UNSUPPORTED | ERROR | NOT_CONFIGURED
Tazelik (freshness):        FRESH | STALE | DEGRADED | ERROR | NEVER

    FRESH    son başarılı senkron eşik içinde ve son deneme başarılı
    DEGRADED son deneme HATA verdi ama son başarılı veri hâlâ eşik içinde (eski veriyle çalışılıyor)
    STALE    son başarılı veri eşikten eski
    ERROR    hiç başarılı veri yok ve son deneme hata verdi / ya da hata + eşik aşıldı
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ...connectors.base import AuthError, ConnectorError
from ...db import row, rows

# kaynak kodu -> (etiket, sync_state.resource, eşik anahtarı / saat)
TRENDYOL_SOURCES = {
    "orders": ("Trendyol Siparişler", "orders"),
    "finance": ("Trendyol Finans (cari hesap ekstresi)", "finance"),
    "returns": ("Trendyol İadeler", "returns"),
    "questions": ("Trendyol Soru-Cevap", "questions"),
    "seller": ("Trendyol Satıcı Bilgileri", "seller"),
    "webhooks": ("Trendyol Webhook yapılandırması", "webhooks"),
}


def stale_hours(th: dict, resource: str) -> float:
    return {"orders": th["data_stale_hours"], "finance": th["finance_stale_hours"], "returns": th["returns_stale_hours"],
            "questions": th["questions_stale_hours"], "seller": 24 * 7, "webhooks": 24 * 7}.get(resource, 24)


def classify_error(exc: Exception) -> str:
    if isinstance(exc, AuthError):
        return "PERMISSION_DENIED"
    if isinstance(exc, ConnectorError) and "HTTP 404" in str(exc):
        return "UNSUPPORTED"
    return "ERROR"


def mark_attempt(conn: Connection, store_id: int, resource: str) -> None:
    conn.execute(text("""
        INSERT INTO sync_state(store_id, resource, last_attempt_at, updated_at) VALUES (:s, :r, NOW(), NOW())
        ON CONFLICT (store_id, resource) DO UPDATE SET last_attempt_at = NOW(), updated_at = NOW()"""),
        {"s": store_id, "r": resource})


def mark_success(conn: Connection, store_id: int, resource: str, *, count: int, latest: datetime | None,
                 synced_until: datetime | None = None, meta: dict | None = None) -> None:
    status = "CONNECTED" if count else "NO_DATA"
    conn.execute(text("""
        INSERT INTO sync_state(store_id, resource, synced_until, last_attempt_at, last_success_at, error_count, last_error,
                               status, record_count, latest_record_at, meta, updated_at)
        VALUES (:s, :r, :u, NOW(), NOW(), 0, NULL, :st, :n, :lt, CAST(:m AS JSONB), NOW())
        ON CONFLICT (store_id, resource) DO UPDATE SET
            synced_until = COALESCE(EXCLUDED.synced_until, sync_state.synced_until),
            last_attempt_at = NOW(), last_success_at = NOW(), error_count = 0, last_error = NULL, status = EXCLUDED.status,
            record_count = EXCLUDED.record_count,
            latest_record_at = GREATEST(EXCLUDED.latest_record_at, sync_state.latest_record_at),
            meta = sync_state.meta || EXCLUDED.meta, updated_at = NOW()"""),
        {"s": store_id, "r": resource, "u": synced_until, "st": status, "n": count, "lt": latest,
         "m": json.dumps(meta or {}, default=str)})


def mark_failure(conn: Connection, store_id: int, resource: str, exc: Exception) -> str:
    status = classify_error(exc)
    conn.execute(text("""
        INSERT INTO sync_state(store_id, resource, last_attempt_at, error_count, last_error, status, updated_at)
        VALUES (:s, :r, NOW(), 1, :e, :st, NOW())
        ON CONFLICT (store_id, resource) DO UPDATE SET last_attempt_at = NOW(), error_count = sync_state.error_count + 1,
            last_error = EXCLUDED.last_error, status = EXCLUDED.status, updated_at = NOW()"""),
        {"s": store_id, "r": resource, "e": f"{exc.__class__.__name__}: {str(exc)[:300]}", "st": status})
    return status


def freshness(state: dict | None, stale_h: float, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    if not state or (state.get("last_success_at") is None and state.get("last_attempt_at") is None):
        return "NEVER"
    ok_at, err = state.get("last_success_at"), int(state.get("error_count") or 0)
    if ok_at is None:
        return "ERROR"
    within = ok_at >= now - timedelta(hours=stale_h)
    if err:
        return "DEGRADED" if within else "ERROR"
    return "FRESH" if within else "STALE"


def age_hours(ts: datetime | None, now: datetime | None = None) -> float | None:
    if ts is None:
        return None
    return round(((now or datetime.now(timezone.utc)) - ts).total_seconds() / 3600, 1)


def trendyol_state(conn: Connection, resource: str) -> dict | None:
    return row(conn, """
        SELECT st.* FROM sync_state st JOIN stores s ON s.id = st.store_id JOIN marketplaces m ON m.id = s.marketplace_id
         WHERE m.code = 'trendyol' AND st.resource = :r ORDER BY st.last_attempt_at DESC NULLS LAST LIMIT 1""", r=resource)


def finance_status(conn: Connection) -> dict:
    """Pazaryeri finans verisinin durumu: CEO/risk motoru para harcamadan önce buna bakar."""
    from ..ai.config import thresholds
    th = thresholds(conn)
    st = trendyol_state(conn, "finance")
    f = freshness(st, th["finance_stale_hours"])
    # Defterde kayıt varsa (kısmi kesintide alınmış olsa bile) kararlar ona dayanabilir → tazelik kuralı uygulanır.
    has_entries = bool(conn.execute(text("SELECT 1 FROM marketplace_finance_entries LIMIT 1")).first())
    return {"state": st, "freshness": f, "age_hours": age_hours(st.get("last_success_at") if st else None),
            "stale_hours": th["finance_stale_hours"], "connected": bool((st and st.get("last_success_at")) or has_entries)}


def data_sources(conn: Connection) -> list[dict]:
    """AI Control Center → Veri kaynakları ekranı. Her önemli metriğin kaynağı ve tazeliği."""
    from ...connectors.registry import get_connector
    from ..ai.config import thresholds
    th = thresholds(conn)
    now = datetime.now(timezone.utc)
    try:
        c = get_connector("trendyol")
        configured, caps = c.is_configured(), set(c.capabilities)
    except Exception:  # noqa: BLE001
        configured, caps = False, set()
    out = []
    cap_for = {"orders": "orders.read", "finance": "settlements.read", "returns": "returns.read",
               "questions": "questions.read", "seller": "seller.read", "webhooks": "webhooks.read"}
    for code, (label, resource) in TRENDYOL_SOURCES.items():
        st = trendyol_state(conn, resource)
        if not configured:
            conn_status = "NOT_CONFIGURED"
        elif cap_for[code] not in caps:
            conn_status = "DISABLED"
        else:
            conn_status = (st or {}).get("status") or ("CONNECTED" if st and st.get("last_success_at") else "NEVER")
        sh = stale_hours(th, resource)
        out.append({"code": f"trendyol.{code}", "label": label, "kind": "api", "connection": conn_status,
                    "freshness": freshness(st, sh, now),
                    "last_successful_sync": (st or {}).get("last_success_at"), "last_attempt": (st or {}).get("last_attempt_at"),
                    "error_count": int((st or {}).get("error_count") or 0), "last_error": (st or {}).get("last_error"),
                    "record_count": (st or {}).get("record_count"), "latest_record_at": (st or {}).get("latest_record_at"),
                    "stale_after_hours": sh, "age_hours": age_hours((st or {}).get("last_success_at"), now)})
    wh = row(conn, """SELECT MAX(received_at) AS last, COUNT(*) FILTER (WHERE received_at > NOW() - INTERVAL '1 day') AS day,
                             COUNT(*) FILTER (WHERE status = 'failed') AS failed
                        FROM platform_events WHERE source = 'webhook'""")
    out.append({"code": "trendyol.webhook_inbox", "label": "Trendyol webhook (gelen olaylar)", "kind": "push",
                "connection": "RECEIVING" if wh["last"] else "NO_EVENTS",
                "freshness": "FRESH" if wh["last"] and wh["last"] > now - timedelta(days=1) else "NEVER" if not wh["last"] else "STALE",
                "last_successful_sync": wh["last"], "last_attempt": wh["last"], "error_count": int(wh["failed"] or 0),
                "record_count": int(wh["day"] or 0), "note": "Webhook yalnızca sipariş paketi statülerini taşır; diğer veriler polling ile gelir."})
    perf = row(conn, "SELECT MAX(perf_date) AS d FROM ad_performance")["d"]
    spend = row(conn, "SELECT MAX(spend_date) AS d FROM ad_spend")["d"]
    last_ads = max([x for x in (perf, spend) if x is not None], default=None)
    ads_stale = last_ads is None or last_ads < now.date() - timedelta(days=int(th["ads_data_stale_days"]))
    out.append({"code": "ads.manual", "label": "Reklam verisi (elle / CSV — Trendyol reklam API'si bağlı değil)", "kind": "manual",
                "connection": "MANUAL", "freshness": "NEVER" if last_ads is None else ("STALE" if ads_stale else "FRESH"),
                "last_successful_sync": last_ads, "stale_after_hours": int(th["ads_data_stale_days"]) * 24})
    cash = row(conn, "SELECT MAX(updated_at) AS at, COUNT(*) FILTER (WHERE kind = 'cash') AS n FROM ai_capital_accounts")
    cash_stale = cash["at"] is not None and cash["at"] < now - timedelta(days=int(th["cash_stale_days"]))
    out.append({"code": "cash.manual", "label": "Kasa / banka nakdi (elle — banka entegrasyonu yok)", "kind": "manual",
                "connection": "MANUAL" if cash["n"] else "MISSING",
                "freshness": "NEVER" if not cash["n"] else ("STALE" if cash_stale else "FRESH"),
                "last_successful_sync": cash["at"], "stale_after_hours": int(th["cash_stale_days"]) * 24})
    cost = row(conn, """SELECT COUNT(*) AS n, COUNT(*) FILTER (WHERE COALESCE(i.unit_cost, 0) = 0) AS missing
                          FROM order_items i JOIN orders o ON o.id = i.order_id
                         WHERE o.order_date > NOW() - INTERVAL '30 days' AND o.internal_status <> 'cancelled'""")
    out.append({"code": "cost.local", "label": "Ürün maliyeti (tedarikçi / yerel maliyet kaydı)", "kind": "local",
                "connection": "LOCAL", "freshness": "FRESH" if not cost["missing"] else "DEGRADED",
                "record_count": int(cost["n"] or 0), "missing": int(cost["missing"] or 0)})
    return out


def recent_states(conn: Connection) -> list[dict]:
    return rows(conn, "SELECT * FROM sync_state ORDER BY updated_at DESC")
