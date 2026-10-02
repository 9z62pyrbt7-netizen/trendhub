"""İç olay modeli + Event Router.

Kaynaktan bağımsız olay tipleri (ajanlar Trendyol uç noktalarına bağımlı olmaz):
    ORDER_CREATED, ORDER_UPDATED            ← Trendyol webhook (sipariş paketi statüsü) VEYA sipariş polling'i
    RETURN_CREATED, RETURN_UPDATED          ← iade polling'i (getClaims) — Trendyol iade webhook'u YOK
    FINANCE_TRANSACTION_CREATED             ← cari hesap polling'i — finans webhook'u YOK
    PAYOUT_UPDATED                          ← cari hesap polling'i (kayıt ödeme talimatına bağlandı)
    QUESTION_CREATED                        ← soru polling'i — soru webhook'u YOK
    STOCK_CHANGED                           ← iç kaynaklar (tedarikçi senkronu); Trendyol stok webhook'u YOK
    PRODUCT_UPDATED                         ← (tanımlı; şu an üreticisi yok — ürün V2 senkronu açılınca)

Akış: kaynak → doğrula → kaydet (platform_events, dedupe_key UNIQUE) → kuyruk → worker (`process_pending`) → router.
Router her olayda CEO'yu ÇALIŞTIRMAZ; yalnızca ilgili hesaplamayı yapar. LLM hiç çağrılmaz. Yalnızca kritik bir risk
(ör. reklamdaki ürünün stoğu bitti) AI döngüsünü (deterministik ajanlar + brief) erkene çeker.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ...db import row, rows

log = logging.getLogger("trendhub.platform.events")

EVENT_TYPES = ("ORDER_CREATED", "ORDER_UPDATED", "RETURN_CREATED", "RETURN_UPDATED", "PRODUCT_UPDATED", "QUESTION_CREATED",
               "FINANCE_TRANSACTION_CREATED", "PAYOUT_UPDATED", "STOCK_CHANGED")
# Olay → yapılacak işler (rapor/ekran için; gerçek iş `_handle` içinde)
ROUTES = {
    "ORDER_CREATED": ["order_upsert", "profit_recalc"],
    "ORDER_UPDATED": ["order_upsert", "profit_recalc"],
    "RETURN_CREATED": ["profit_recalc", "cx_signal"],
    "RETURN_UPDATED": ["profit_recalc", "cx_signal"],
    "FINANCE_TRANSACTION_CREATED": ["actual_finance_apply", "cash_position"],
    "PAYOUT_UPDATED": ["cash_position"],
    "QUESTION_CREATED": ["cx_signal"],
    "STOCK_CHANGED": ["inventory_guard", "ads_scaling_guard"],
    "PRODUCT_UPDATED": ["catalog_note"],
}
MAX_ATTEMPTS = 8


def emit(conn: Connection, *, source: str, marketplace: str, event_type: str, dedupe_key: str, entity_type: str | None = None,
         entity_ref: str | None = None, payload: dict | None = None) -> int | None:
    """Olayı kaydeder. Aynı dedupe_key ile ikinci kez gelirse None döner (idempotent; yeniden işlenmez)."""
    assert event_type in EVENT_TYPES, event_type
    return conn.execute(text("""
        INSERT INTO platform_events(source, marketplace, event_type, dedupe_key, entity_type, entity_ref, payload)
        VALUES (:s, :m, :t, :k, :et, :er, CAST(:p AS JSONB))
        ON CONFLICT (dedupe_key) DO NOTHING RETURNING id"""),
        {"s": source, "m": marketplace, "t": event_type, "k": dedupe_key[:500], "et": entity_type, "er": entity_ref,
         "p": json.dumps(payload or {}, default=str, ensure_ascii=False)}).scalar()


def _claim(conn: Connection, limit: int) -> list[dict]:
    return rows(conn, """
        WITH next AS (SELECT id FROM platform_events
                       WHERE ((status IN ('pending', 'failed') AND next_attempt_at <= NOW())
                              -- worker işlerken öldüyse (yeniden başlatma) 10 dk sonra olay tekrar alınır
                              OR (status = 'processing' AND next_attempt_at < NOW() - INTERVAL '10 minutes'))
                         AND attempts < :max
                       ORDER BY received_at, id FOR UPDATE SKIP LOCKED LIMIT :n)
        UPDATE platform_events e SET status = 'processing', attempts = e.attempts + 1, next_attempt_at = NOW()
          FROM next WHERE e.id = next.id
        RETURNING e.*""", n=limit, max=MAX_ATTEMPTS)


def process_pending(engine: Engine, limit: int = 500) -> dict:
    """Bekleyen olayları işler. Her olay kendi transaction'ında; hata diğerlerini etkilemez, backoff ile tekrar denenir."""
    with engine.begin() as conn:
        batch = _claim(conn, limit)
    out = {"processed": 0, "failed": 0, "ignored": 0, "ai_cycle": False}
    trigger_ai = False
    for ev in sorted(batch, key=lambda e: (e["received_at"], e["id"])):
        try:
            with engine.begin() as conn:
                actions, status, critical = _handle(conn, ev)
                conn.execute(text("""UPDATE platform_events SET status = :st, actions = CAST(:a AS JSONB), processed_at = NOW(),
                                     last_error = NULL WHERE id = :id"""),
                             {"st": status, "a": json.dumps(actions, default=str), "id": ev["id"]})
            trigger_ai = trigger_ai or critical
            out["ignored" if status == "ignored" else "processed"] += 1
        except Exception as exc:  # noqa: BLE001 — olay kaybolmaz; backoff ile tekrar denenir
            log.exception("Olay #%s işlenemedi", ev["id"])
            with engine.begin() as conn:
                conn.execute(text("""UPDATE platform_events SET status = 'failed', last_error = :e,
                                     next_attempt_at = NOW() + make_interval(secs => LEAST(3600, 30 * POWER(2, attempts)))
                                     WHERE id = :id"""), {"e": f"{exc.__class__.__name__}: {str(exc)[:300]}", "id": ev["id"]})
            out["failed"] += 1
    if trigger_ai:
        from .. import jobs
        from ..sync_service import AI_CYCLE
        with engine.begin() as conn:
            out["ai_cycle"] = bool(jobs.enqueue(conn, AI_CYCLE, payload={"trigger": "critical_event"}, idempotency_key=AI_CYCLE,
                                                max_attempts=1))
    return out


# ------------------------------------------------------------------ işleyiciler
def _store_id(conn: Connection, marketplace: str) -> int | None:
    return conn.execute(text("""SELECT s.id FROM stores s JOIN marketplaces m ON m.id = s.marketplace_id
                                 WHERE m.code = :m ORDER BY s.id LIMIT 1"""), {"m": marketplace}).scalar()


def _handle(conn: Connection, ev: dict) -> tuple[list[dict], str, bool]:
    t, p = ev["event_type"], ev["payload"] or {}
    acts: list[dict] = []
    critical = False
    if t in ("ORDER_CREATED", "ORDER_UPDATED"):
        if ev["source"] == "webhook":
            return _webhook_order(conn, ev)
        acts.append({"action": "profit_recalc", "result": "sipariş senkronunda yapıldı", "order_id": p.get("order_id")})
    elif t in ("RETURN_CREATED", "RETURN_UPDATED"):
        from .returns import apply_refund
        rid, sid = p.get("return_id"), _store_id(conn, ev["marketplace"])
        ok = bool(rid and sid and apply_refund(conn, sid, int(rid)))
        acts.append({"action": "profit_recalc", "result": "yeniden hesaplandı" if ok else "sipariş/kalem eşleşmedi"})
        acts.append({"action": "cx_signal", "result": "iade sebebi CX analizine dahil (okumada hesaplanır)"})
    elif t == "FINANCE_TRANSACTION_CREATED":
        from .finance import apply_entry
        sid = _store_id(conn, ev["marketplace"])
        oid = apply_entry(conn, sid, int(p["entry_id"])) if sid and p.get("entry_id") else None
        acts.append({"action": "actual_finance_apply", "result": f"sipariş #{oid} gerçek finansla yeniden hesaplandı" if oid
                     else "kâra aktarılacak eşleşme yok (nakit defterinde)"})
        acts.append({"action": "cash_position", "result": "bekleyen hakediş okumada güncel defterden hesaplanır"})
    elif t == "PAYOUT_UPDATED":
        acts.append({"action": "cash_position", "result": f"ödeme talimatı {p.get('payment_order_id')} işlendi"})
    elif t == "QUESTION_CREATED":
        acts.append({"action": "cx_signal", "result": f"kategori: {p.get('category')}"})
    elif t == "STOCK_CHANGED":
        pid = p.get("product_id")
        in_ads = bool(pid and conn.execute(text("""SELECT 1 FROM ad_campaign_products x JOIN ad_campaigns c ON c.id = x.campaign_id
                                                     WHERE x.product_id = :p AND c.status = 'active' LIMIT 1"""), {"p": pid}).first())
        out = p.get("available") is not None and int(p["available"]) <= 0
        acts.append({"action": "inventory_guard", "result": "stok bitti" if out else "stok değişti"})
        if out and in_ads:
            critical = True
            acts.append({"action": "ads_scaling_guard", "result": "aktif reklamdaki ürünün stoğu bitti → AI döngüsü erkene alındı (brief)"})
    else:
        acts.append({"action": "none"})
    return acts, "done", critical


def _webhook_order(conn: Connection, ev: dict) -> tuple[list[dict], str, bool]:
    """Webhook sipariş paketi. Eski (yeniden gönderilmiş) paket yeni bilginin üzerine YAZILMAZ."""
    from ...connectors.trendyol import TrendyolConnector
    from .. import jobs
    from ..orders_sync import SyncStats, upsert_order
    pkg = (ev["payload"] or {}).get("package") or {}
    pid, lm = str(pkg.get("shipmentPackageId") or pkg.get("id") or ""), int(pkg.get("lastModifiedDate") or 0)
    newer = conn.execute(text("""SELECT 1 FROM platform_events WHERE source = 'webhook' AND entity_ref = :p AND id <> :id
                                   AND status = 'done' AND COALESCE((payload->'package'->>'lastModifiedDate')::bigint, 0) > :lm LIMIT 1"""),
                         {"p": pid, "id": ev["id"], "lm": lm}).first()
    if newer:
        return [{"action": "order_upsert", "result": "daha yeni bir olay zaten işlenmiş; eski (yeniden oynatılan) olay yok sayıldı"}], "ignored", False
    sid = _store_id(conn, "trendyol")
    if sid is None:
        return [{"action": "order_upsert", "result": "Trendyol mağazası tanımlı değil"}], "ignored", False
    number = str(pkg.get("orderNumber") or "")
    other_packages = conn.execute(text("""SELECT COUNT(*) FROM shipments sh JOIN orders o ON o.id = sh.order_id
                                           WHERE o.store_id = :s AND o.external_order_id = :n AND sh.external_package_id <> :p"""),
                                  {"s": sid, "n": number, "p": pid}).scalar()
    if other_packages:
        # Çok paketli sipariş: tek paketten sipariş statüsü türetilmez; normal senkron tüm paketleri birlikte okur.
        from ..sync_service import ORDERS_SYNC
        jobs.enqueue(conn, ORDERS_SYNC, marketplace="trendyol", payload={"lookback_days": 2, "trigger": "webhook"},
                     idempotency_key=f"{ORDERS_SYNC}:trendyol")
        return [{"action": "order_upsert", "result": "çok paketli sipariş → sipariş senkronu kuyruğa alındı"}], "done", False
    orders = TrendyolConnector.normalize([pkg])
    if not orders:
        return [{"action": "order_upsert", "result": "paket sipariş numarası taşımıyor"}], "ignored", False
    stats = SyncStats(received=1)
    oid = upsert_order(conn, sid, orders[0], stats)
    return [{"action": "order_upsert", "result": f"sipariş #{oid} ({'yeni' if stats.created else 'güncellendi'})", "order_id": oid},
            {"action": "profit_recalc", "result": "yapıldı"}], "done", False


def recent(conn: Connection, limit: int = 50) -> list[dict]:
    return rows(conn, """SELECT id, source, marketplace, event_type, entity_type, entity_ref, status, attempts, last_error, actions,
                                received_at, processed_at FROM platform_events ORDER BY id DESC LIMIT :n""", n=limit)


def stats(conn: Connection) -> dict:
    r = row(conn, """SELECT COUNT(*) FILTER (WHERE status = 'pending') AS pending, COUNT(*) FILTER (WHERE status = 'failed') AS failed,
                            COUNT(*) FILTER (WHERE status = 'done' AND processed_at > NOW() - INTERVAL '1 day') AS done_24h,
                            COUNT(*) FILTER (WHERE source = 'webhook' AND received_at > NOW() - INTERVAL '1 day') AS webhook_24h
                       FROM platform_events""")
    return {k: int(v or 0) for k, v in r.items()}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
