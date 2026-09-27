from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import text

from app.config import Settings
from app.connectors.base import NormalizedLine, NormalizedOrder, NormalizedShipment
from app.domain import order_status as S
from app.services import jobs, sync_service
from app.services.orders_sync import ensure_store, upsert_orders


def order(status=S.NEW, raw="Created", price="100", qty=1, lines=None, shipments=None, number="TY-1"):
    return NormalizedOrder(
        external_order_id=number, marketplace_status=raw, internal_status=status,
        order_date=datetime(2026, 9, 20, 10, tzinfo=timezone.utc), customer_name="Test", customer_city="Ankara",
        lines=lines or [NormalizedLine("L1", "SKU-1", "869", "Çanta", qty, Decimal(price))],
        shipments=shipments if shipments is not None else [NormalizedShipment("P1", "Aras", "123")],
    )


def test_upsert_is_idempotent_and_computes_finance(conn):
    conn.execute(text("INSERT INTO products(sku, name, cost) VALUES ('SKU-1', 'Çanta', 40)"))
    conn.execute(text("UPDATE app_settings SET value = '15' WHERE key = 'finance.default_shipping_cost'"))
    store = ensure_store(conn, "trendyol", "999", "Trendyol")
    assert ensure_store(conn, "trendyol", "999", "Trendyol") == store

    s1 = upsert_orders(conn, store, [order()])
    s2 = upsert_orders(conn, store, [order()])
    assert (s1.created, s2.created, s2.unchanged) == (1, 0, 1)
    assert conn.execute(text("SELECT COUNT(*) FROM orders")).scalar() == 1
    assert conn.execute(text("SELECT COUNT(*) FROM order_items")).scalar() == 1
    assert conn.execute(text("SELECT COUNT(*) FROM shipments")).scalar() == 1
    assert conn.execute(text("SELECT COUNT(*) FROM order_status_history")).scalar() == 1

    o = conn.execute(text("SELECT * FROM orders")).mappings().one()
    # ciro 100, maliyet 40, komisyon %20 = 20, kargo 15 (varsayılan), hizmet 0
    assert o["gross_revenue"] == Decimal("100.00")
    assert o["product_cost"] == Decimal("40.00")
    assert o["commission"] == Decimal("20.00")
    assert o["shipping_cost"] == Decimal("15.00")
    assert o["net_profit"] == Decimal("25.00")
    assert o["finance_is_estimate"] is True
    assert o["internal_status"] == S.NEW and o["status"] == "Created"


def test_status_progress_and_no_regression(conn):
    store = ensure_store(conn, "trendyol", "999", "Trendyol")
    upsert_orders(conn, store, [order()])
    conn.execute(text("UPDATE orders SET internal_status = 'sent_to_supplier'"))
    upsert_orders(conn, store, [order(S.PREPARING, "Picking")])
    assert conn.execute(text("SELECT internal_status FROM orders")).scalar() == S.SENT_TO_SUPPLIER
    upsert_orders(conn, store, [order(S.SHIPPED, "Shipped")])
    assert conn.execute(text("SELECT internal_status FROM orders")).scalar() == S.SHIPPED
    upsert_orders(conn, store, [order(S.RETURNED, "Returned")])
    o = conn.execute(text("SELECT internal_status, refund_cost, gross_revenue FROM orders")).mappings().one()
    assert o["internal_status"] == S.RETURNED and o["refund_cost"] == o["gross_revenue"]


def test_cancelled_order_has_no_revenue(conn):
    store = ensure_store(conn, "trendyol", "999", "Trendyol")
    upsert_orders(conn, store, [order(S.CANCELLED, "Cancelled")])
    o = conn.execute(text("SELECT gross_revenue, net_profit FROM orders")).mappings().one()
    assert o["gross_revenue"] == 0 and o["net_profit"] == 0


def test_actual_commission_from_ledger_overrides_estimate(conn):
    store = ensure_store(conn, "trendyol", "999", "Trendyol")
    upsert_orders(conn, store, [order()])
    oid, iid = conn.execute(text("SELECT order_id, id FROM order_items")).one()
    conn.execute(text("""INSERT INTO financial_transactions(order_id, order_item_id, source, external_ref, kind, amount, occurred_at)
                         VALUES (:o, :i, 'settlement', 'x1', 'commission', 7.5, NOW())"""), {"o": oid, "i": iid})
    from app.services.finance_service import recalculate_order
    recalculate_order(conn, oid)
    assert conn.execute(text("SELECT commission FROM orders")).scalar() == Decimal("7.50")


def test_legacy_order_without_items_is_not_touched(conn):
    conn.execute(text("INSERT INTO stores(marketplace_id, name) VALUES (1, 'x')"))
    conn.execute(text("""INSERT INTO orders(store_id, external_order_id, status, internal_status, gross_revenue, net_profit)
                         VALUES (1, 'OLD', 'Created', 'new', 500, 123)"""))
    from app.services.finance_service import recalculate_order
    assert recalculate_order(conn, 1) is None
    assert conn.execute(text("SELECT net_profit FROM orders")).scalar() == Decimal("123.00")


def test_bad_order_does_not_block_others(conn):
    store = ensure_store(conn, "trendyol", "999", "Trendyol")
    bad = order(number="BAD")
    bad.internal_status = "gecersiz"  # CHECK kısıtını ihlal eder
    stats = upsert_orders(conn, store, [bad, order(number="OK")])
    assert stats.created == 1 and len(stats.errors) == 1
    assert conn.execute(text("SELECT external_order_id FROM orders")).scalar() == "OK"


def test_job_queue_idempotency_claim_and_backoff(conn):
    a = jobs.enqueue(conn, "orders.sync", marketplace="trendyol", idempotency_key="k1", max_attempts=2)
    b = jobs.enqueue(conn, "orders.sync", marketplace="trendyol", idempotency_key="k1")
    assert a and b is None
    job = jobs.claim(conn, "w1")
    assert job["id"] == a and job["attempts"] == 1
    assert jobs.claim(conn, "w2") is None
    assert jobs.fail(conn, job, "geçici", retryable=True, retry_after=0) == jobs.QUEUED
    job = jobs.claim(conn, "w1")
    assert job["attempts"] == 2
    assert jobs.fail(conn, job, "yine", retryable=True) == jobs.DEAD
    ev = conn.execute(text("SELECT level, occurrences FROM system_events")).mappings().one()
    assert ev["level"] == "error"
    # tamamlanmış/ölü iş varken aynı anahtarla yeni iş eklenebilir
    assert jobs.enqueue(conn, "orders.sync", marketplace="trendyol", idempotency_key="k1")


def test_non_retryable_failure_and_stale_requeue(conn):
    jobs.enqueue(conn, "orders.sync", marketplace="trendyol", idempotency_key="k2")
    job = jobs.claim(conn, "w1")
    assert jobs.fail(conn, job, "yetkisiz", retryable=False) == jobs.FAILED
    jobs.enqueue(conn, "orders.sync", marketplace="trendyol", idempotency_key="k3")
    jobs.claim(conn, "w1")
    conn.execute(text("UPDATE sync_jobs SET locked_at = NOW() - INTERVAL '1 hour' WHERE status = 'running'"))
    assert jobs.requeue_stale(conn) == 1


def test_scheduler_only_for_configured_connectors(conn):
    base = dict(database_url="postgresql://x@y/z")
    assert sync_service.schedule_due_jobs(conn, 15, Settings(**base)) == []
    s = Settings(**base, trendyol_seller_id="1", trendyol_api_key="k", trendyol_api_secret="s",
                 hepsiburada_merchant_id="m", hepsiburada_username="u", hepsiburada_password="p")
    created = sync_service.schedule_due_jobs(conn, 15, s)
    assert len(created) == 1   # Hepsiburada senkronizasyonu uygulanmadığı için planlanmaz
    assert sync_service.schedule_due_jobs(conn, 15, s) == []   # interval dolmadan tekrar yok
    assert conn.execute(text("SELECT marketplace FROM sync_jobs")).scalar() == "trendyol"


def test_worker_runs_orders_sync_end_to_end(engine, monkeypatch):
    import httpx

    from app.connectors import registry
    from app.connectors.trendyol import TrendyolConnector
    from app.worker import Worker

    s = Settings(database_url="postgresql://x@y/z", trendyol_seller_id="555", trendyol_api_key="k",
                 trendyol_api_secret="s", trendyol_rate_per_minute=6000)
    payload = {"content": [{"id": 1, "orderNumber": "W1", "shipmentPackageStatus": "Invoiced",
                            "orderDate": 1_790_000_000_000, "lines": [
                                {"id": 11, "merchantSku": "S", "barcode": "B", "productName": "P", "quantity": 1, "price": 50}]}],
               "totalPages": 1}
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    monkeypatch.setitem(registry.CONNECTOR_CLASSES, "trendyol",
                        lambda settings: TrendyolConnector(settings, transport=transport, sleep=lambda x: None))
    w = Worker(engine=engine, settings=s)
    w.schedule()
    assert w.run_once() is True
    with engine.connect() as c:
        job = c.execute(text("SELECT status, result FROM sync_jobs")).mappings().one()
        assert job["status"] == "succeeded" and job["result"]["created"] == 1
        assert c.execute(text("SELECT internal_status FROM orders")).scalar() == S.AWAITING_SHIPMENT
        assert c.execute(text("SELECT last_sync_at IS NOT NULL FROM marketplaces WHERE code='trendyol'")).scalar()
        assert c.execute(text("SELECT COUNT(*) FROM worker_heartbeats")).scalar() == 1
    if w._leader_conn is not None:
        w._leader_conn.close()


def test_worker_marks_auth_error_as_failed_without_retry(engine, monkeypatch):
    import httpx

    from app.connectors import registry
    from app.connectors.trendyol import TrendyolConnector
    from app.worker import Worker

    s = Settings(database_url="postgresql://x@y/z", trendyol_seller_id="555", trendyol_api_key="k",
                 trendyol_api_secret="bad", trendyol_rate_per_minute=6000)
    transport = httpx.MockTransport(lambda r: httpx.Response(401))
    monkeypatch.setitem(registry.CONNECTOR_CLASSES, "trendyol",
                        lambda settings: TrendyolConnector(settings, transport=transport, sleep=lambda x: None))
    with engine.begin() as c:
        jobs.enqueue(c, "orders.sync", marketplace="trendyol", idempotency_key="x")
    w = Worker(engine=engine, settings=s)
    assert w.run_once()
    with engine.connect() as c:
        assert c.execute(text("SELECT status FROM sync_jobs")).scalar() == "failed"


def test_returned_order_refunds_commission_and_restocks_by_default(conn):
    conn.execute(text("INSERT INTO products(sku, name, cost) VALUES ('SKU-1', 'Çanta', 40)"))
    conn.execute(text("UPDATE app_settings SET value = '15' WHERE key = 'finance.default_shipping_cost'"))
    store = ensure_store(conn, "trendyol", "999", "Trendyol")
    upsert_orders(conn, store, [order(S.RETURNED, "Returned")])
    o = conn.execute(text("SELECT gross_revenue, refund_cost, commission, product_cost, net_profit FROM orders")).mappings().one()
    assert o["refund_cost"] == o["gross_revenue"] == Decimal("100.00")
    assert o["commission"] == 0 and o["product_cost"] == 0
    assert o["net_profit"] == Decimal("-15.00")   # yalnızca kargo kaybı

    conn.execute(text("UPDATE app_settings SET value = 'true' WHERE key = 'finance.return_product_cost_is_loss'"))
    from app.services.finance_service import recalculate_order
    recalculate_order(conn, conn.execute(text("SELECT id FROM orders")).scalar())
    assert conn.execute(text("SELECT net_profit FROM orders")).scalar() == Decimal("-55.00")
