import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from app.config import Settings
from app.connectors.amazon_tr import AmazonTrConnector
from app.connectors.base import CAP_ORDERS_READ, AuthError, NotSupported, RetryableError, WriteDisabled
from app.connectors.hepsiburada import HepsiburadaConnector
from app.connectors.http import RateLimiter, ResilientClient, backoff_delay
from app.connectors.trendyol import TrendyolConnector, aggregate_status, map_status
from app.domain import order_status as S


def settings(**kw):
    base = dict(database_url="postgresql://x@y/z")
    base.update(kw)
    return Settings(**base)


TY = dict(trendyol_seller_id="12345", trendyol_api_key="k", trendyol_api_secret="s", trendyol_rate_per_minute=6000)


def pkg(pid, number="TY1", status="Created", lines=None, modified=1_700_000_000_000, **extra):
    return {"id": pid, "orderNumber": number, "shipmentPackageStatus": status, "orderDate": 1_700_000_000_000,
            "lastModifiedDate": modified, "customerFirstName": "Ayşe", "customerLastName": "Y",
            "shipmentAddress": {"city": "İstanbul"}, "cargoProviderName": "Yurtiçi Kargo",
            "cargoTrackingNumber": 7330000001, "lines": lines if lines is not None else [
                {"id": pid * 10, "merchantSku": "SKU-1", "barcode": "869000", "productName": "Çanta",
                 "quantity": 2, "price": 249.90, "discount": 10}], **extra}


def test_credentials_missing_means_not_connected_everywhere():
    s = settings()
    for cls in (TrendyolConnector, HepsiburadaConnector, AmazonTrConnector):
        c = cls(s)
        assert not c.is_configured()
        check = c.test_connection()
        assert check.ok is False and check.message.startswith("Bağlı değil")


def test_placeholder_values_count_as_missing():
    c = TrendyolConnector(settings(trendyol_seller_id="CHANGE_ME", trendyol_api_key="k", trendyol_api_secret="s"))
    assert c.missing_credentials() == ["TRENDYOL_SELLER_ID"]


def test_hepsiburada_configured_but_unreachable_does_not_pretend():
    """Bilgi girilmiş ama API reddediyorsa bağlantı başarılı görünmez; ilan okuma varsayılan kapalı."""
    s = settings(hepsiburada_merchant_id="m", hepsiburada_username="u", hepsiburada_password="p")
    hb = HepsiburadaConnector(s, transport=httpx.MockTransport(lambda r: httpx.Response(401)), sleep=lambda _: None)
    assert hb.is_configured() and hb.capabilities == frozenset({CAP_ORDERS_READ})
    check = hb.test_connection()
    assert check.ok is False and "Yetkisiz" in check.message
    with pytest.raises(AuthError):
        hb.fetch_orders(datetime.now(timezone.utc) - timedelta(days=1), datetime.now(timezone.utc))


def test_write_operations_disabled_by_default():
    c = TrendyolConnector(settings(**TY))
    with pytest.raises(WriteDisabled):
        c.update_stock("869000", 3)
    with pytest.raises(WriteDisabled):
        c.update_price("869000", Decimal("10"))


def test_status_maps():
    assert map_status("Created") == S.NEW
    assert map_status("Invoiced") == S.AWAITING_SHIPMENT
    assert map_status("UnSupplied") == S.CANCELLED
    assert map_status("YeniBirStatu") == S.NEEDS_REVIEW
    assert aggregate_status([S.SHIPPED, S.CANCELLED]) == S.SHIPPED
    assert aggregate_status([S.SHIPPED, S.PREPARING]) == S.PREPARING
    assert aggregate_status([S.CANCELLED, S.RETURNED]) == S.RETURNED


def test_trendyol_normalize_merges_packages_and_dedupes():
    orders = TrendyolConnector.normalize([
        pkg(1, status="Shipped"),
        pkg(2, status="Picking", lines=[{"id": 21, "merchantSku": "SKU-2", "barcode": "b2", "productName": "Cüzdan",
                                         "quantity": 1, "price": 99}]),
        pkg(1, status="Delivered", modified=1_700_000_100_000),   # aynı paketin güncel hali
        pkg(3, number="TY2", status="Tuhaf"),
    ])
    by = {o.external_order_id: o for o in orders}
    o = by["TY1"]
    assert len(o.shipments) == 2 and len(o.lines) == 2
    assert o.internal_status == S.PREPARING
    assert o.customer_name == "Ayşe Y" and o.customer_city == "İstanbul"
    line = next(l for l in o.lines if l.sku == "SKU-1")
    assert line.quantity == 2 and line.unit_price == Decimal("249.9")
    assert by["TY2"].internal_status == S.NEEDS_REVIEW and "Tuhaf" in by["TY2"].review_reason


def test_trendyol_fetch_paginates_and_chunks_windows():
    calls = []

    def handler(request: httpx.Request):
        calls.append(dict(request.url.params))
        assert request.headers["User-Agent"] == "12345 - SelfIntegration"
        assert request.headers["Authorization"].startswith("Basic ")
        assert request.url.path == "/integration/order/sellers/12345/v2/orders"
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"content": [pkg(100 + page, number=f"N{page}")], "totalPages": 2})

    c = TrendyolConnector(settings(**TY), transport=httpx.MockTransport(handler), sleep=lambda s: None)
    until = datetime(2026, 9, 27, tzinfo=timezone.utc)
    orders = c.fetch_orders(until - timedelta(days=20), until)  # 2 pencere x 2 sayfa
    assert len(calls) == 4
    assert {o.external_order_id for o in orders} == {"N0", "N1"}
    assert all(int(p["endDate"]) - int(p["startDate"]) <= 14 * 86400 * 1000 for p in calls)


def test_retry_on_429_then_success_and_auth_error():
    attempts = {"n": 0}
    slept = []

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"ok": True})

    client = ResilientClient("https://x", transport=httpx.MockTransport(handler), sleep=slept.append)
    assert client.get_json("/a") == {"ok": True}
    assert slept == [2.0, 2.0]

    client = ResilientClient("https://x", transport=httpx.MockTransport(lambda r: httpx.Response(401)),
                             sleep=slept.append)
    with pytest.raises(AuthError):
        client.get_json("/a")

    client = ResilientClient("https://x", max_attempts=2, transport=httpx.MockTransport(lambda r: httpx.Response(503)),
                             sleep=lambda s: None)
    with pytest.raises(RetryableError):
        client.get_json("/a")


def test_backoff_and_rate_limiter():
    assert backoff_delay(1, rand=lambda: 1.0) == 1.0
    assert backoff_delay(4, rand=lambda: 1.0) == 8.0
    assert backoff_delay(20, cap=60, rand=lambda: 1.0) == 60
    t = {"now": 0.0}
    waits = []
    rl = RateLimiter(60, burst=2, clock=lambda: t["now"], sleep=waits.append)
    assert rl.acquire() == 0 and rl.acquire() == 0
    assert rl.acquire() == pytest.approx(1.0)   # 60/dk -> 1 sn


def test_trendyol_new_field_names_take_precedence_and_order_date_is_gmt3():
    """6 Nisan 2026 alan adı değişikliği: yeni adlar okunur, eski adlar yedek."""
    pkg_new = {"shipmentPackageId": 901, "orderNumber": "N-1", "shipmentPackageStatus": "Picking",
               # 2026-09-20 13:00 GMT+3 olarak gönderilen zaman damgası (= 10:00 UTC)
               "orderDate": int(datetime(2026, 9, 20, 13, 0, tzinfo=timezone.utc).timestamp() * 1000),
               "lines": [{"lineId": 5001, "stockCode": "CB-9", "barcode": "869", "productName": "Çanta",
                          "quantity": 2, "lineUnitPrice": 150.5, "lineGrossAmount": 320,
                          "lineSellerDiscount": 19, "lineTyDiscount": 5, "vatRate": 20,
                          # aynı pakette eski adlar da gelirse yeni adın değeri kullanılır
                          "id": 1, "merchantSku": "ESKI", "price": 1}]}
    (o,) = TrendyolConnector.normalize([pkg_new])
    line = o.lines[0]
    assert (line.external_line_id, line.sku, line.unit_price, line.discount, line.vat_rate) == (
        "5001", "CB-9", Decimal("150.5"), Decimal("19"), Decimal("20"))
    assert o.shipments[0].external_package_id == "901"
    assert o.order_date == datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc)

    # Birim fiyat yoksa brüt tutar / adet kullanılır
    (o2,) = TrendyolConnector.normalize([{"shipmentPackageId": 2, "orderNumber": "N-2", "status": "Created",
                                          "lines": [{"lineId": 7, "quantity": 4, "lineGrossAmount": 100}]}])
    assert o2.lines[0].unit_price == Decimal("25.00")


def test_trendyol_falls_back_to_v1_once_on_404_and_caps_lookback_to_30_days():
    paths, starts = [], []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/v2/orders"):
            return httpx.Response(404, json={"message": "not found"})
        starts.append(int(request.url.params["startDate"]))
        return httpx.Response(200, json={"content": [], "totalPages": 0})

    c = TrendyolConnector(settings(**TY), transport=httpx.MockTransport(handler), sleep=lambda s: None)
    until = datetime(2026, 9, 27, tzinfo=timezone.utc)
    c.fetch_orders(until - timedelta(days=90), until)
    assert paths.count("/integration/order/sellers/12345/v2/orders") == 1   # sadece bir kez denenir
    assert min(starts) >= int((until - timedelta(days=30)).timestamp() * 1000)


def test_trendyol_listings_disabled_by_default():
    from app.connectors.base import CAP_PRODUCTS_READ
    assert not TrendyolConnector(settings(**TY)).supports(CAP_PRODUCTS_READ)
    assert "doğrulanmadı" in TrendyolConnector(settings(**TY)).implementation_note
    assert TrendyolConnector(settings(**TY, trendyol_listings_enabled=True)).supports(CAP_PRODUCTS_READ)
