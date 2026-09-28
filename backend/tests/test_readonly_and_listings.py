"""Salt okunurluk garantisi, ilan senkronizasyonu, Amazon connector'ı ve iş sahipliği."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text

from app.config import Settings
from app.connectors import registry
from app.connectors.amazon_tr import AmazonTrConnector
from app.connectors.base import AuthError, WriteDisabled
from app.connectors.http import ResilientClient
from app.connectors.trendyol import TrendyolConnector
from app.domain import order_status as S
from app.services import jobs, sync_service
from app.services.listings_sync import import_unlinked_as_products, upsert_listings
from app.services.orders_sync import ensure_store

BASE = dict(database_url="postgresql://x@y/z")
TY = dict(trendyol_seller_id="555", trendyol_api_key="k", trendyol_api_secret="s", trendyol_rate_per_minute=6000,
          trendyol_listings_enabled=True)
AMZ = dict(amazon_sp_seller_id="A1SELLER", amazon_sp_client_id="cid", amazon_sp_client_secret="csec",
           amazon_sp_refresh_token="rt")


# ------------------------------------------------------------- salt okunurluk
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_client_refuses_write_methods_before_network(method):
    calls = []
    client = ResilientClient("https://x", transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200)))
    with pytest.raises(WriteDisabled):
        client.request(method, "/anything")
    assert calls == []   # istek ağa hiç çıkmadı


def test_connectors_only_issue_get_requests_during_full_sync():
    """Trendyol sipariş + ilan senkronu boyunca giden tüm istekler GET olmalı."""
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"content": [], "totalPages": 0})

    c = TrendyolConnector(Settings(**BASE, **TY), transport=httpx.MockTransport(handler), sleep=lambda s: None)
    now = datetime.now(timezone.utc)
    c.fetch_orders(now - timedelta(days=30), now)
    c.fetch_listings()
    c.test_connection()
    assert seen and all(m == "GET" for m, _ in seen)


def test_write_enabled_flag_still_has_no_write_implementation():
    c = TrendyolConnector(Settings(**BASE, **TY, connector_write_enabled=True))
    with pytest.raises(Exception) as e:
        c.update_stock("869", 1)
    assert "uygulanmadı" in str(e.value)


# ------------------------------------------------------------------ ilanlar
def ty_product(barcode, stock_code, **kw):
    return {"id": "x" + barcode, "barcode": barcode, "stockCode": stock_code, "title": "Deri Çanta " + barcode,
            "quantity": 7, "salePrice": 499.9, "listPrice": 599.9, "vatRate": 20, "brand": "Marka",
            "categoryName": "Omuz Çantası", "approved": True, "archived": False, "onSale": True,
            "images": [{"url": "https://cdn/x.jpg"}], **kw}


def test_trendyol_listing_normalization():
    li = TrendyolConnector.normalize_listing(ty_product("869001", "CB-1"))
    assert (li.external_product_id, li.sku, li.stock, li.price, li.status) == ("869001", "CB-1", 7, Decimal("499.9"), "on_sale")
    assert TrendyolConnector.normalize_listing(ty_product("2", "b", archived=True)).status == "archived"
    assert TrendyolConnector.normalize_listing(ty_product("3", "c", onSale=False)).status == "not_on_sale"
    assert TrendyolConnector.normalize_listing(ty_product("4", "", approved=False)).sku is None


def test_listings_sync_is_idempotent_links_products_and_never_touches_local_stock(engine, monkeypatch):
    with engine.begin() as c:
        c.execute(text("INSERT INTO products(sku, name, cost, stock) VALUES ('CB-1', 'Yerel', 100, 3)"))
    pages = {0: [ty_product("869001", "CB-1"), ty_product("869002", "CB-2")], 1: [ty_product("869003", "")]}

    def handler(request):
        assert request.method == "GET" and request.url.path == "/integration/product/sellers/555/products"
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"content": pages[page], "totalPages": 2})

    monkeypatch.setitem(registry.CONNECTOR_CLASSES, "trendyol",
                        lambda st: TrendyolConnector(st, transport=httpx.MockTransport(handler), sleep=lambda x: None))
    s = Settings(**BASE, **TY)
    r1 = sync_service.run_listings_sync(engine, "trendyol", s)
    r2 = sync_service.run_listings_sync(engine, "trendyol", s)
    assert r1["upserted"] == r2["upserted"] == 3 and r1["linked"] == 1
    with engine.begin() as c:
        assert c.execute(text("SELECT COUNT(*) FROM marketplace_listings")).scalar() == 3
        # yerel stok ve ürün adı değişmedi
        assert tuple(c.execute(text("SELECT stock, name FROM products WHERE sku = 'CB-1'")).one()) == (3, "Yerel")
        created = import_unlinked_as_products(c)
        assert created == 1   # yalnızca SKU'lu ve bağlanmamış CB-2
        assert import_unlinked_as_products(c) == 0
        assert c.execute(text("SELECT cost FROM products WHERE sku = 'CB-2'")).scalar() == 0
        assert c.execute(text("SELECT COUNT(*) FROM marketplace_listings WHERE product_id IS NULL")).scalar() == 1


def test_listings_api_and_import(client_factory, engine):
    client, login = client_factory
    with engine.begin() as c:
        store = ensure_store(c, "trendyol", "555", "Trendyol")
        from app.connectors.base import NormalizedListing
        upsert_listings(c, store, [NormalizedListing("B1", "B1", "SKU-A", "Çanta A", Decimal("100"), None, 2, "on_sale")])
    login("admin", "Admin-Password-123")
    d = client.get("/api/listings?unlinked=true").json()
    assert d["total"] == 1 and d["items"][0]["marketplace_name"] == "Trendyol"
    r = client.post("/api/listings/import-products", headers={"X-Requested-With": "TrendHub"})
    assert r.status_code == 200 and r.json()["created"] == 1
    assert client.get("/api/listings?unlinked=true").json()["total"] == 0


# ------------------------------------------------------------------- Amazon
def amazon_handler(log, orders_pages, items, token_status=200):
    def handler(request: httpx.Request):
        log.append((request.method, request.url.host, request.url.path))
        if request.url.host == "api.amazon.com":
            assert request.method == "POST" and request.url.path == "/auth/o2/token"
            body = dict(x.split("=") for x in request.content.decode().split("&"))
            assert body["grant_type"] == "refresh_token" and body["refresh_token"] == "rt"
            if token_status != 200:
                return httpx.Response(token_status, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "AT", "expires_in": 3600})
        assert request.method == "GET" and request.headers["x-amz-access-token"] == "AT"
        if request.url.path == "/orders/v0/orders":
            assert request.url.params["MarketplaceIds"] == "A33AVAJ2PDY3EV"
            token = request.url.params.get("NextToken")
            return httpx.Response(200, json={"payload": orders_pages[token]})
        oid = request.url.path.split("/")[4]
        return httpx.Response(200, json={"payload": {"AmazonOrderId": oid, "OrderItems": items[oid]}})
    return handler


def test_amazon_fetch_orders_paginates_and_normalizes():
    log = []
    pages = {
        None: {"Orders": [{"AmazonOrderId": "405-1", "OrderStatus": "Unshipped", "PurchaseDate": "2026-09-20T10:00:00Z",
                           "LastUpdateDate": "2026-09-21T10:00:00Z", "OrderTotal": {"CurrencyCode": "TRY", "Amount": "300.00"},
                           "ShippingAddress": {"City": "İzmir"}}], "NextToken": "T2"},
        "T2": {"Orders": [{"AmazonOrderId": "405-2", "OrderStatus": "Canceled", "PurchaseDate": "2026-09-20T11:00:00Z"}]},
    }
    items = {"405-1": [{"OrderItemId": "I1", "SellerSKU": "CB-1", "ASIN": "B0X", "Title": "Çanta", "QuantityOrdered": 2,
                        "ItemPrice": {"CurrencyCode": "TRY", "Amount": "300.00"}, "PromotionDiscount": {"Amount": "20.00"}}],
             "405-2": [{"OrderItemId": "I2", "SellerSKU": "CB-2", "QuantityOrdered": 0}]}
    c = AmazonTrConnector(Settings(**BASE, **AMZ), transport=httpx.MockTransport(amazon_handler(log, pages, items)),
                          sleep=lambda s: None)
    orders = c.fetch_orders(datetime(2026, 9, 1, tzinfo=timezone.utc), datetime.now(timezone.utc))
    by = {o.external_order_id: o for o in orders}
    o1 = by["405-1"]
    assert o1.internal_status == S.PREPARING and o1.customer_city == "İzmir"
    assert o1.lines[0].unit_price == Decimal("140.00") and o1.lines[0].quantity == 2
    assert by["405-2"].internal_status == S.CANCELLED and by["405-2"].lines == []
    # token bir kez alındı, diğer tüm istekler GET
    assert sum(1 for m, h, _ in log if h == "api.amazon.com") == 1
    assert all(m == "GET" for m, h, _ in log if h != "api.amazon.com")


def test_amazon_bad_refresh_token_is_auth_error_and_not_retried():
    log = []
    c = AmazonTrConnector(Settings(**BASE, **AMZ), transport=httpx.MockTransport(amazon_handler(log, {}, {}, token_status=400)),
                          sleep=lambda s: None)
    with pytest.raises(AuthError):
        c.fetch_orders(datetime.now(timezone.utc) - timedelta(days=1), datetime.now(timezone.utc))
    assert len(log) == 1
    assert c.test_connection().ok is False


def test_amazon_incremental_sync_uses_watermark(engine, monkeypatch):
    seen_since = []

    class Fake(AmazonTrConnector):
        def fetch_orders(self, since, until):
            seen_since.append(since)
            return []

    monkeypatch.setitem(registry.CONNECTOR_CLASSES, "amazon_tr", Fake)
    s = Settings(**BASE, **AMZ)
    sync_service.run_orders_sync(engine, "amazon_tr", {}, s)
    sync_service.run_orders_sync(engine, "amazon_tr", {}, s)
    first, second = seen_since
    assert datetime.now(timezone.utc) - first > timedelta(days=13)          # ilk: tam pencere
    assert datetime.now(timezone.utc) - second < timedelta(hours=1, minutes=5)  # sonra: watermark - 1 saat


# -------------------------------------------------------------- iş sahipliği
def test_job_result_is_not_written_by_a_worker_that_lost_the_job(conn):
    jobs.enqueue(conn, "orders.sync", marketplace="trendyol", idempotency_key="own")
    job = jobs.claim(conn, "w-old")
    assert jobs.touch(conn, job["id"], "w-old")
    conn.execute(text("UPDATE sync_jobs SET locked_at = NOW() - INTERVAL '10 minutes'"))
    assert jobs.requeue_stale(conn) == 1
    job2 = jobs.claim(conn, "w-new")
    assert job2["id"] == job["id"]
    assert jobs.complete(conn, job["id"], {"x": 1}, worker_id="w-old") is False
    assert jobs.fail(conn, job, "eski worker") == "lost"
    assert jobs.touch(conn, job["id"], "w-old") is False
    assert jobs.complete(conn, job2["id"], {"ok": 1}, worker_id="w-new") is True


def test_keepalive_refreshes_lock_during_long_job(engine, monkeypatch):
    from app.worker import Worker
    monkeypatch.setattr(jobs, "HEARTBEAT_SECONDS", 0.05)
    w = Worker(engine=engine, settings=Settings(**BASE))
    with engine.begin() as c:
        jobs.enqueue(c, "orders.sync", marketplace="trendyol", idempotency_key="long")
        job = jobs.claim(c, w.worker_id)
        c.execute(text("UPDATE sync_jobs SET locked_at = NOW() - INTERVAL '1 hour'"))
    import time
    with w._keepalive(job["id"]):
        time.sleep(0.3)
    with engine.connect() as c:
        assert c.execute(text("SELECT locked_at > NOW() - INTERVAL '1 minute' FROM sync_jobs")).scalar()


def test_listing_import_requires_operator_and_sync_kind_validation(client_factory, engine):
    from app.security import hash_password
    client, login = client_factory
    with engine.begin() as c:
        c.execute(text("INSERT INTO users(username, password_hash, role) VALUES ('izle', :h, 'viewer')"),
                  {"h": hash_password("Viewer-Password-1")})
    login("izle", "Viewer-Password-1")
    H = {"X-Requested-With": "TrendHub"}
    assert client.get("/api/listings").status_code == 200
    assert client.post("/api/listings/import-products", headers=H).status_code == 403
    assert client.post("/api/integrations/trendyol/sync", json={"kind": "listings"}, headers=H).status_code == 403
    login("admin", "Admin-Password-123")
    assert client.post("/api/integrations/trendyol/sync", json={"kind": "stok-yaz"}, headers=H).status_code == 422
    r = client.post("/api/integrations/hepsiburada/sync", json={"kind": "listings"}, headers=H)
    assert r.status_code == 409   # bağlı değil
