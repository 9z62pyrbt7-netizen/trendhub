"""Hepsiburada connector: salt okunur sipariş/paket okuma, kimlik doğrulama, sayfalama, eşleme."""
import base64
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from app.config import Settings
from app.connectors.base import CAP_ORDERS_READ, CAP_PRODUCTS_READ, WriteDisabled
from app.connectors.hepsiburada import HepsiburadaConnector, map_status, parse_date
from app.domain import order_status as S

HB = dict(hepsiburada_merchant_id="7d3f-merchant", hepsiburada_username="acme_dev", hepsiburada_password="pw-secret",
          hepsiburada_rate_per_minute=6000)


def conn(handler, **kw):
    s = Settings(database_url="postgresql://x@y/z", **{**HB, **kw})
    return HepsiburadaConnector(s, transport=httpx.MockTransport(handler), sleep=lambda _: None)


def package(num, order="HB-1", status="Open", items=None):
    return {"id": f"id-{num}", "packageNumber": num, "status": status, "customerName": "Ali Veli",
            "orderDate": "2026-09-27T14:30:00", "cargoCompany": "HepsiJET", "shippingCity": "İstanbul",
            "items": items if items is not None else [
                {"lineItemId": f"L-{num}", "orderNumber": order, "sku": "HBV000001", "merchantSku": "CB-001",
                 "name": "Deri Çanta", "quantity": 2, "unitPrice": {"amount": 450.5, "currency": "TRY"},
                 "totalPrice": {"amount": 901.0, "currency": "TRY"}, "vatRate": 20}]}


def test_packages_request_uses_documented_path_auth_user_agent_and_pagination():
    calls = []

    def handler(req: httpx.Request):
        calls.append(req)
        offset = int(req.url.params["offset"])
        items = [package(f"P{offset + i}", order=f"HB-{offset + i}") for i in range(50 if offset == 0 else 3)]
        return httpx.Response(200, json={"items": items, "limit": 50, "offset": offset, "totalcount": 53, "pagecount": 2})
    c = conn(handler)
    until = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    orders = c.fetch_orders(until - timedelta(days=2), until)
    assert len(orders) == 53
    r = calls[0]
    assert r.method == "GET" and r.url.host == "oms-external.hepsiburada.com"
    assert r.url.path == "/packages/merchantid/7d3f-merchant"
    assert r.headers["user-agent"] == "acme_dev"
    assert r.headers["authorization"] == "Basic " + base64.b64encode(b"acme_dev:pw-secret").decode()
    # Tarihler Türkiye saatiyle "YYYY-MM-DD HH:mm"
    assert r.url.params["begindate"] == "2026-09-26 15:00" and r.url.params["enddate"] == "2026-09-28 15:00"
    assert [int(x.url.params["offset"]) for x in calls] == [0, 50]
    assert all(x.method == "GET" for x in calls)


def test_normalize_groups_items_by_order_and_maps_fields():
    pkgs = [package("P1", order="HB-9", status="Unpacked"),
            package("P2", order="HB-9", status="Packed", items=[
                {"lineItemId": "L-2", "orderNumber": "HB-9", "merchantSku": "CB-002", "name": "Cüzdan",
                 "quantity": 1, "totalPrice": {"amount": 199.9}, "vatRate": 20}]),
            package("P3", order="HB-10", status="Delivered")]
    orders = {o.external_order_id: o for o in HepsiburadaConnector.normalize(pkgs)}
    o = orders["HB-9"]
    assert o.internal_status == S.PREPARING and o.marketplace_status == "Packed,Unpacked"
    assert [ln.sku for ln in o.lines] == ["CB-001", "CB-002"]
    assert o.lines[0].unit_price == Decimal("450.5") and o.lines[0].quantity == 2
    assert o.lines[1].unit_price == Decimal("199.90")   # totalPrice / adet
    assert {sh.external_package_id for sh in o.shipments} == {"P1", "P2"}
    assert o.customer_city == "İstanbul"
    # GMT+3 yerel saat -> UTC
    assert o.order_date == datetime(2026, 9, 27, 11, 30, tzinfo=timezone.utc)
    assert orders["HB-10"].internal_status == S.DELIVERED


def test_unknown_status_goes_to_review_and_nothing_is_invented():
    o = HepsiburadaConnector.normalize([package("P1", status="YeniBilinmeyen")])[0]
    assert o.internal_status == S.NEEDS_REVIEW and "eşlenemedi" in o.review_reason
    assert map_status("CancelledByMerchant") == S.CANCELLED and map_status("InTransit") == S.SHIPPED
    assert HepsiburadaConnector.normalize([]) == []
    assert parse_date("2026-09-27T10:00:00Z") == datetime(2026, 9, 27, 10, tzinfo=timezone.utc)


def test_connection_check_and_readonly_client():
    ok = conn(lambda r: httpx.Response(200, json={"items": [], "totalcount": 0}))
    assert ok.test_connection().ok is True
    bad_shape = conn(lambda r: httpx.Response(200, json={"unexpected": 1}))
    assert bad_shape.test_connection().ok is False
    with pytest.raises(WriteDisabled):
        ok.oms.request("POST", "/packages/merchantid/x")
    assert not hasattr(ok, "update_stock_bulk")
    with pytest.raises(WriteDisabled):
        ok.update_stock("869", 1)


def test_listings_off_by_default_and_readable_when_enabled():
    c = conn(lambda r: httpx.Response(200, json={}))
    assert c.capabilities == frozenset({CAP_ORDERS_READ})
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"listings": [
            {"HepsiburadaSku": "HBV1", "MerchantSku": "CB-001", "Price": 499.9, "AvailableStock": 7, "IsSalable": True},
            {"hepsiburadaSku": "HBV2", "merchantSku": "CB-002", "price": 10, "availableStock": 0, "isSuspended": True}],
            "totalCount": 2})
    c2 = conn(handler, hepsiburada_listings_enabled=True)
    assert CAP_PRODUCTS_READ in c2.capabilities
    ls = c2.fetch_listings()
    assert seen[0].url.host == "listing-external.hepsiburada.com" and seen[0].url.path == "/listings/merchantid/7d3f-merchant"
    assert [(x.sku, x.price, x.stock, x.status) for x in ls] == [
        ("CB-001", Decimal("499.9"), 7, "on_sale"), ("CB-002", Decimal("10"), 0, "not_on_sale")]
