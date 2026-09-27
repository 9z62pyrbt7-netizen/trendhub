"""İş tiplerinin çalıştırılması (worker tarafından çağrılır)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.engine import Engine

from ..connectors.base import CAP_ORDERS_READ, NotConfigured, NotSupported
from ..connectors.registry import all_connectors, get_connector
from . import jobs
from .events import record_event, resolve_fingerprint
from .orders_sync import ensure_store, upsert_orders

ORDERS_SYNC = "orders.sync"
INTEGRATION_CHECK = "integration.check"
DEFAULT_LOOKBACK_DAYS = 14
MAX_LOOKBACK_DAYS = 90


def run_orders_sync(engine: Engine, marketplace: str, payload: dict, settings=None) -> dict:
    connector = get_connector(marketplace, settings)
    if not connector.is_configured():
        raise NotConfigured(f"{connector.name} bağlı değil: eksik " + ", ".join(connector.missing_credentials()))
    if not connector.supports(CAP_ORDERS_READ):
        raise NotSupported(f"{connector.name}: sipariş senkronizasyonu henüz uygulanmadı")

    days = min(int(payload.get("lookback_days") or DEFAULT_LOOKBACK_DAYS), MAX_LOOKBACK_DAYS)
    until = datetime.now(timezone.utc)
    since = until - timedelta(days=days)

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


def run_integration_check(engine: Engine, marketplace: str, settings=None) -> dict:
    connector = get_connector(marketplace, settings)
    check = connector.test_connection()
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE marketplaces SET last_check_at = NOW(), last_check_ok = :ok, last_check_message = :m,
                   updated_at = NOW() WHERE code = :c
        """), {"ok": check.ok, "m": check.message[:500], "c": marketplace})
    return {"ok": check.ok, "message": check.message}


def execute(engine: Engine, job: dict, settings=None) -> dict:
    payload = job.get("payload") or {}
    if job["job_type"] == ORDERS_SYNC:
        return run_orders_sync(engine, job["marketplace"], payload, settings)
    if job["job_type"] == INTEGRATION_CHECK:
        return run_integration_check(engine, job["marketplace"], settings)
    raise NotSupported(f"Bilinmeyen iş tipi: {job['job_type']}")


def schedule_due_jobs(conn, interval_minutes: int, settings=None) -> list[int]:
    """Yapılandırılmış ve sipariş okumayı destekleyen her connector için,
    son `interval_minutes` içinde oluşturulmuş bir iş yoksa yeni iş ekler."""
    created = []
    for c in all_connectors(settings):
        if not (c.is_configured() and c.supports(CAP_ORDERS_READ)):
            continue
        recent = conn.execute(text("""
            SELECT 1 FROM sync_jobs WHERE job_type = :t AND marketplace = :m
               AND (status IN ('queued','running') OR created_at > NOW() - make_interval(mins => :i))
             LIMIT 1
        """), {"t": ORDERS_SYNC, "m": c.code, "i": interval_minutes}).first()
        if recent:
            continue
        job_id = jobs.enqueue(conn, ORDERS_SYNC, marketplace=c.code, payload={},
                              idempotency_key=f"{ORDERS_SYNC}:{c.code}")
        if job_id:
            created.append(job_id)
    return created
