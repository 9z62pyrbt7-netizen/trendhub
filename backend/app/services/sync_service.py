"""İş tiplerinin çalıştırılması (worker tarafından çağrılır)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine

from ..connectors.base import CAP_ORDERS_READ, CAP_PRODUCTS_READ, NotConfigured, NotSupported
from ..config import get_settings
from ..connectors.registry import all_connectors, get_connector
from . import jobs
from .events import record_event, resolve_fingerprint
from .listings_sync import upsert_listings
from .orders_sync import ensure_store, upsert_orders

log = logging.getLogger("trendhub.sync")

ORDERS_SYNC = "orders.sync"
ORDERS_DEEP_SYNC = "orders.deep_sync"
LISTINGS_SYNC = "listings.sync"
INTEGRATION_CHECK = "integration.check"
SUPPLIER_SYNC = "supplier.sync"
ALERTS_SCAN = "alerts.scan"
ALERTS_SCAN_EVERY_MINUTES = 15
STOREFRONT_MAINTENANCE = "storefront.maintenance"
STOREFRONT_MAINTENANCE_EVERY_MINUTES = 10
STOREFRONT_NOTIFY = "storefront.notify"
AI_CYCLE = "ai.cycle"
DEFAULT_LOOKBACK_DAYS = 14
DEEP_LOOKBACK_DAYS = 30   # Trendyol getShipmentPackages en fazla 1 ay geriye izin verir
MAX_LOOKBACK_DAYS = 30

JOB_LABELS_TR = {
    ORDERS_SYNC: "Sipariş senkronizasyonu",
    ORDERS_DEEP_SYNC: "Derin sipariş senkronizasyonu (30 gün)",
    LISTINGS_SYNC: "Ürün/ilan senkronizasyonu",
    INTEGRATION_CHECK: "Bağlantı testi",
    SUPPLIER_SYNC: "Tedarikçi senkronizasyonu",
    ALERTS_SCAN: "Uyarı taraması",
    "listing.publish": "Ürün yayınlama (onaylı)",
    STOREFRONT_MAINTENANCE: "Web mağazası bakımı (ödeme beklemeleri, sepetler, kargo bildirimi, tedarikçi taslakları, e-fatura)",
    STOREFRONT_NOTIFY: "Web siparişi bildirimleri (e-posta/SMS)",
    AI_CYCLE: "AI Control Center döngüsü (analiz + öneri; hiçbir aksiyon uygulamaz)",
}


def run_storefront_maintenance(engine: Engine) -> dict:
    """Web mağazası bakımı. Her adım ayrı işlemde çalışır; biri hata verirse diğerleri yine çalışır."""
    from ..storefront.checkout import expire_stale
    from . import einvoice, order_notifications, supplier_forwarding
    out: dict = {}
    steps = (("reservations", expire_stale), ("shipped_notifications", order_notifications.scan_shipped),
             ("supplier_forwarding", supplier_forwarding.run_auto), ("einvoice", einvoice.process),
             ("notify_retry", order_notifications.schedule_retry))
    for name, fn in steps:
        try:
            with engine.begin() as conn:
                out[name] = fn(conn)
        except Exception as exc:  # noqa: BLE001
            log.exception("Web mağazası bakımı adımı başarısız: %s", name)
            out[name] = {"error": f"{exc.__class__.__name__}: {str(exc)[:200]}"}
    return out


def _require(connector, capability: str, what: str):
    if not connector.is_configured():
        raise NotConfigured(f"{connector.name} bağlı değil: eksik " + ", ".join(connector.missing_credentials()))
    if not connector.supports(capability):
        raise NotSupported(f"{connector.name}: {what} henüz uygulanmadı")


def run_orders_sync(engine: Engine, marketplace: str, payload: dict, settings=None) -> dict:
    connector = get_connector(marketplace, settings)
    _require(connector, CAP_ORDERS_READ, "sipariş senkronizasyonu")

    days = min(int(payload.get("lookback_days") or DEFAULT_LOOKBACK_DAYS), MAX_LOOKBACK_DAYS)
    until = datetime.now(timezone.utc)
    since = until - timedelta(days=days)
    if connector.incremental and not payload.get("lookback_days"):
        # Artımlı connector: son başarılı senkrondan 1 saat örtüşmeyle devam et.
        with engine.begin() as conn:
            store_id = ensure_store(conn, marketplace, connector.store_external_id(), connector.name)
            watermark = conn.execute(text(
                "SELECT synced_until FROM sync_state WHERE store_id = :s AND resource = 'orders'"),
                {"s": store_id}).scalar()
        if watermark is not None:
            since = max(since, watermark - timedelta(hours=1))

    # Ağ çağrısı transaction DIŞINDA yapılır (uzun süre kilit tutmamak için).
    orders = connector.fetch_orders(since, until)

    with engine.begin() as conn:
        store_id = ensure_store(conn, marketplace, connector.store_external_id(), connector.name)
        stats = upsert_orders(conn, store_id, orders)
        conn.execute(text("""
            INSERT INTO sync_state(store_id, resource, synced_until, updated_at)
            VALUES (:s, 'orders', :u, NOW())
            ON CONFLICT (store_id, resource) DO UPDATE SET synced_until = EXCLUDED.synced_until, updated_at = NOW()
        """), {"s": store_id, "u": until})
        conn.execute(text("UPDATE marketplaces SET last_sync_at = NOW(), updated_at = NOW() WHERE code = :c"),
                     {"c": marketplace})
        if stats.errors:
            record_event(conn, level="warning", source=f"sync:{marketplace}",
                         message=f"{len(stats.errors)} sipariş işlenemedi", details={"errors": stats.errors[:20]},
                         fingerprint=f"sync-errors:{marketplace}")
        else:
            resolve_fingerprint(conn, f"sync-errors:{marketplace}")
            resolve_fingerprint(conn, f"job:{ORDERS_SYNC}:{marketplace}")
    return {"window": [since.isoformat(), until.isoformat()], **stats.as_dict()}


def run_listings_sync(engine: Engine, marketplace: str, settings=None) -> dict:
    connector = get_connector(marketplace, settings)
    _require(connector, CAP_PRODUCTS_READ, "ürün/ilan okuma")
    listings = connector.fetch_listings()  # ağ çağrısı transaction dışında
    with engine.begin() as conn:
        store_id = ensure_store(conn, marketplace, connector.store_external_id(), connector.name)
        stats = upsert_listings(conn, store_id, listings)
        if stats.errors:
            record_event(conn, level="warning", source=f"listings:{marketplace}",
                         message=f"{len(stats.errors)} ilan işlenemedi", details={"errors": stats.errors[:20]},
                         fingerprint=f"listings-errors:{marketplace}")
        else:
            resolve_fingerprint(conn, f"listings-errors:{marketplace}")
            resolve_fingerprint(conn, f"job:{LISTINGS_SYNC}:{marketplace}")
    return stats.as_dict()


def run_integration_check(engine: Engine, marketplace: str, settings=None) -> dict:
    connector = get_connector(marketplace, settings)
    check = connector.test_connection()
    with engine.begin() as conn:
        from .marketplace_credentials import record_test
        record_test(conn, marketplace, check.ok, check.message)
    return {"ok": check.ok, "message": check.message}


def run_alerts_scan(engine: Engine) -> dict:
    from .alerts import scan
    with engine.begin() as conn:
        return scan(conn)


def _scan_after(engine: Engine, result: dict) -> dict:
    """Senkron sonrası uyarı taraması; tarama hatası senkron sonucunu bozmaz."""
    try:
        run_alerts_scan(engine)
    except Exception:  # noqa: BLE001
        log.exception("Senkron sonrası uyarı taraması başarısız")
    return result


def execute(engine: Engine, job: dict, settings=None) -> dict:
    payload = job.get("payload") or {}
    t = job["job_type"]
    if t == ALERTS_SCAN:
        return run_alerts_scan(engine)
    if t == STOREFRONT_MAINTENANCE:
        return run_storefront_maintenance(engine)
    if t == AI_CYCLE:
        from .ai.ceo import run_cycle
        return run_cycle(engine)
    if t == STOREFRONT_NOTIFY:
        from . import order_notifications
        return order_notifications.deliver_pending(engine.begin)
    if t in (ORDERS_SYNC, ORDERS_DEEP_SYNC, LISTINGS_SYNC, SUPPLIER_SYNC):
        return _scan_after(engine, _execute_sync(engine, job, payload, settings))
    if t == INTEGRATION_CHECK:
        return run_integration_check(engine, job["marketplace"], settings)
    if t == "listing.publish":
        from .publishing import run_publish_job
        return run_publish_job(engine, job, settings)
    raise NotSupported(f"Bilinmeyen iş tipi: {t}")


def _execute_sync(engine: Engine, job: dict, payload: dict, settings=None) -> dict:
    t = job["job_type"]
    if t == ORDERS_SYNC:
        return run_orders_sync(engine, job["marketplace"], payload, settings)
    if t == ORDERS_DEEP_SYNC:
        return run_orders_sync(engine, job["marketplace"], {"lookback_days": DEEP_LOOKBACK_DAYS}, settings)
    if t == LISTINGS_SYNC:
        return run_listings_sync(engine, job["marketplace"], settings)
    from .supplier_sync import run_supplier_sync
    return run_supplier_sync(engine, int(payload["supplier_id"]), trigger=payload.get("trigger") or "schedule",
                             job_id=job["id"])


def schedule_plan(interval_minutes: int) -> list[tuple[str, str, int]]:
    """(iş tipi, gereken yetenek, aralık dakika). Hepsi salt okunur."""
    return [
        (ORDERS_SYNC, CAP_ORDERS_READ, interval_minutes),
        (ORDERS_DEEP_SYNC, CAP_ORDERS_READ, 24 * 60),   # geç gelen iade/teslim statüleri için
        (LISTINGS_SYNC, CAP_PRODUCTS_READ, 6 * 60),
        (INTEGRATION_CHECK, CAP_ORDERS_READ, 60),
    ]


def schedule_due_jobs(conn, interval_minutes: int, settings=None) -> list[int]:
    """Yapılandırılmış connector'lar için vadesi gelen işleri kuyruğa ekler.

    Aynı tip iş son `aralık` içinde oluşturulduysa veya hâlâ bekliyor/çalışıyorsa
    yeni iş eklenmez. Connector'ın desteklemediği işler hiç planlanmaz."""
    created = []
    max_attempts = (settings or get_settings()).job_max_attempts
    for c in all_connectors(settings):
        if not c.is_configured():
            continue
        for job_type, capability, every in schedule_plan(interval_minutes):
            if not c.supports(capability):
                continue
            if job_type == ORDERS_DEEP_SYNC and c.incremental:
                continue  # artımlı connector'lar değişen siparişleri zaten yakalar
            recent = conn.execute(text("""
                SELECT 1 FROM sync_jobs WHERE job_type = :t AND marketplace = :m
                   AND (status IN ('queued','running') OR created_at > NOW() - make_interval(mins => :i))
                 LIMIT 1
            """), {"t": job_type, "m": c.code, "i": every}).first()
            if recent:
                continue
            job_id = jobs.enqueue(conn, job_type, marketplace=c.code, payload={},
                                  idempotency_key=f"{job_type}:{c.code}", max_attempts=max_attempts)
            if job_id:
                created.append(job_id)
    return created


def schedule_supplier_jobs(conn) -> list[int]:
    """Kaynak adresi olan aktif tedarikçiler için vadesi gelen `supplier.sync` işlerini kuyruğa ekler.

    Senkron sıklığı 0 olan tedarikçi otomatik senkronize edilmez (yalnızca elle). Alan eşleştirmesi
    kullanıcı tarafından onaylanmamış tedarikçi hiç planlanmaz (ilk aktarım onayla başlar)."""
    created = []
    due = conn.execute(text("""
        SELECT s.id FROM suppliers s JOIN supplier_connections c ON c.supplier_id = s.id
         WHERE s.is_active AND s.sync_interval_minutes > 0 AND c.integration_type IN ('xml','api','csv')
           AND c.source_url_enc IS NOT NULL
           AND s.mapping_approved_at IS NOT NULL      -- onaylanmamış eşleştirmeyle zamanlanmış senkron YOK
           AND NOT EXISTS (SELECT 1 FROM sync_jobs j WHERE j.idempotency_key = 'supplier.sync:' || s.id
                            AND (j.status IN ('queued','running')
                                 OR j.created_at > NOW() - make_interval(mins => s.sync_interval_minutes)))
         ORDER BY s.id
    """)).scalars().all()
    for sid in due:
        job_id = jobs.enqueue(conn, SUPPLIER_SYNC, payload={"supplier_id": sid, "trigger": "schedule"},
                              idempotency_key=f"{SUPPLIER_SYNC}:{sid}", max_attempts=3)
        if job_id:
            created.append(job_id)
    return created


def schedule_alerts_scan(conn) -> int | None:
    """Uyarı taramasını periyodik olarak kuyruğa ekler (yalnızca okuma + alerts tablosu)."""
    recent = conn.execute(text("""SELECT 1 FROM sync_jobs WHERE job_type = :t
                                   AND (status IN ('queued','running') OR created_at > NOW() - make_interval(mins => :i))
                                 LIMIT 1"""), {"t": ALERTS_SCAN, "i": ALERTS_SCAN_EVERY_MINUTES}).first()
    if recent:
        return None
    return jobs.enqueue(conn, ALERTS_SCAN, payload={}, idempotency_key=ALERTS_SCAN, max_attempts=2)


def schedule_storefront_maintenance(conn) -> int | None:
    """Web mağazası: süresi dolan Havale/EFT ve kart ödeme beklemelerini kapatır (stok ayırması bırakılır)."""
    recent = conn.execute(text("""SELECT 1 FROM sync_jobs WHERE job_type = :t
                                   AND (status IN ('queued','running') OR created_at > NOW() - make_interval(mins => :i))
                                 LIMIT 1"""), {"t": STOREFRONT_MAINTENANCE, "i": STOREFRONT_MAINTENANCE_EVERY_MINUTES}).first()
    if recent:
        return None
    return jobs.enqueue(conn, STOREFRONT_MAINTENANCE, payload={}, idempotency_key=STOREFRONT_MAINTENANCE, max_attempts=2)


def schedule_ai_cycle(conn) -> int | None:
    """AI Control Center: ajan döngüsü (salt analiz + öneri). ai.enabled=false ise planlanmaz."""
    from . import app_settings
    if not app_settings.get(conn, "ai.enabled", True):
        return None
    every = max(15, int(app_settings.get(conn, "ai.cycle_minutes", 60) or 60))
    recent = conn.execute(text("""SELECT 1 FROM sync_jobs WHERE job_type = :t
                                   AND (status IN ('queued','running') OR created_at > NOW() - make_interval(mins => :i))
                                 LIMIT 1"""), {"t": AI_CYCLE, "i": every}).first()
    if recent:
        return None
    return jobs.enqueue(conn, AI_CYCLE, payload={}, idempotency_key=AI_CYCLE, max_attempts=1)
