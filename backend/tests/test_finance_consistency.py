"""Finans regresyon testleri: tek kaynak, indirim çift düşülmez, marj/markup paydaları, KDV modları.

Örnek sipariş (satış 499,90 TL · satıcı indirimi 50 TL ciroya yansımış · maliyet 250 · komisyon %20 ·
hizmet bedeli 8,49 · kargo 45 · reklam 15 · KDV %20).
"""
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.connectors.base import NormalizedLine, NormalizedOrder, NormalizedShipment
from app.domain.pricing import PricingRule, estimate_profit
from app.services.finance_service import recalculate_order

H = {"X-Requested-With": "TrendHub"}
D = Decimal


@pytest.fixture
def client(engine):
    from app.main import app
    with TestClient(app) as c:
        assert c.post("/api/auth/login", json={"username": "admin", "password": "Admin-Password-123"},
                      headers=H).status_code == 200
        yield c


def _set(engine, **values):
    import json
    with engine.begin() as c:
        for k, v in values.items():
            c.execute(text("""INSERT INTO app_settings(key, value) VALUES (:k, CAST(:v AS JSONB))
                              ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"""),
                      {"k": k.replace("__", "."), "v": json.dumps(v)})


def _order(engine, number="FIN-1", status="delivered", refund=None):
    from app.services.orders_sync import ensure_store, upsert_orders
    _set(engine, finance__service_fee_per_order=8.49)
    with engine.begin() as c:
        c.execute(text("""INSERT INTO products(sku, barcode, name, cost, vat_rate) VALUES ('FIN-SKU','FIN-BC','Çanta',250,20)
                          ON CONFLICT (sku) WHERE sku IS NOT NULL DO NOTHING"""))
        store = ensure_store(c, "trendyol", "1", "Trendyol")
        upsert_orders(c, store, [NormalizedOrder(
            external_order_id=number, marketplace_status="Delivered", internal_status=status,
            order_date=datetime.now(timezone.utc),
            lines=[NormalizedLine(f"{number}-L", "FIN-SKU", "FIN-BC", "Çanta", 1, D("499.90"), discount=D("50.00"),
                                  vat_rate=D("20"))],
            shipments=[NormalizedShipment(f"{number}-P", "Yurtiçi Kargo", "T1")])])
        oid = c.execute(text("SELECT id FROM orders WHERE external_order_id = :n"), {"n": number}).scalar()
        c.execute(text("UPDATE shipments SET cost = 45 WHERE order_id = :o"), {"o": oid})
        c.execute(text("""INSERT INTO financial_transactions(order_id, source, external_ref, kind, amount, occurred_at)
                          VALUES (:o, 'manual', :r, 'advertising', 15, NOW())"""), {"o": oid, "r": f"{number}-ad"})
        if refund is not None:
            item = c.execute(text("SELECT id FROM order_items WHERE order_id = :o"), {"o": oid}).scalar()
            c.execute(text("""INSERT INTO financial_transactions(order_id, order_item_id, source, external_ref, kind,
                              amount, occurred_at) VALUES (:o, :i, 'manual', :r, 'refund', :a, NOW())"""),
                      {"o": oid, "i": item, "r": f"{number}-rf", "a": refund})
        recalculate_order(c, oid)
    return oid


def _screens(client, oid):
    """Aynı siparişin tüm ekranlardaki (KDV öncesi, KDV sonrası) kârı."""
    q = {"period": "today"}
    detail = client.get(f"/api/orders/{oid}").json()
    lst = next(o for o in client.get("/api/orders").json()["items"] if o["id"] == oid)
    sku = client.get("/api/reports/sku", params=q).json()["items"][0]
    dash = client.get("/api/dashboard", params=q).json()
    fin = client.get("/api/finance/summary", params=q).json()
    st = {x["key"]: D(str(x["amount"])) for x in client.get("/api/finance/statement", params=q).json()["lines"]}
    pairs = {
        "detay": (detail["profit_before_vat"], detail["profit_after_vat"]),
        "liste": (lst["profit_before_vat"], lst["profit_after_vat"]),
        "sku": (sku["net_profit"], sku["net_profit_after_tax"]),
        "dashboard": (dash["summary"]["orders"]["net_profit"], dash["summary"]["orders"]["net_profit_after_tax"]),
        "dashboard_mp": (next(m for m in dash["by_marketplace"] if m["code"] == "trendyol")["net_profit"],
                         next(m for m in dash["by_marketplace"] if m["code"] == "trendyol")["net_profit_after_tax"]),
        "finans": (fin["orders"]["net_profit"], fin["orders"]["net_profit_after_tax"]),
        "tablo": (st["net_profit"], st["net_after_vat"]),
    }
    return {k: (D(str(a)), D(str(b))) for k, (a, b) in pairs.items()}, detail, st


def test_discount_is_not_deducted_twice_and_all_screens_agree(client, engine):
    oid = _order(engine)
    screens, detail, st = _screens(client, oid)
    # 499,90 − 250 − 99,98 − 8,49 − 45 − 15 = 81,43 ; KDV = 83,32 − 41,67 = 41,65 ; sonrası 39,78
    assert set(screens.values()) == {(D("81.43"), D("39.78"))}, screens
    assert st["gross_sales"] == D("499.90") and st["net_sales"] == D("499.90")   # indirim tekrar düşülmedi
    assert st["discounts"] == D("50.00")                                         # yalnızca bilgi satırı
    lines = client.get("/api/finance/statement", params={"period": "today"}).json()["lines"]
    assert next(x for x in lines if x["key"] == "discounts")["in_total"] is False
    assert D(str(detail["discount"])) == D("50.00")


def test_margin_uses_net_sales_and_markup_is_separate(client, engine):
    oid = _order(engine, refund="100.00")    # kısmi iade: net satış = 499,90 − 100 = 399,90
    d = client.get(f"/api/orders/{oid}").json()
    assert D(str(d["net_sales"])) == D("399.90")
    p = D(str(d["profit_after_vat"]))
    assert D(str(d["margin_after_vat"])) == (p / D("399.90")).quantize(D("0.0001"))          # net marj
    assert D(str(d["markup_after_vat"])) == (p / D("250")).quantize(D("0.0001"))             # maliyet üzeri
    assert d["margin_after_vat"] != d["markup_after_vat"]
    fin = client.get("/api/finance/summary", params={"period": "today"}).json()
    assert D(str(fin["margin_after_tax"])) == (D(str(fin["net_profit_after_tax"])) / D("399.90")).quantize(D("0.0001"))


@pytest.mark.parametrize("cmode,emode,before,vat,after", [
    ("unset", "unset", "81.43", "41.65", "39.78"),         # varsayılan = önceki davranış (geriye uyumlu)
    ("excluded", "unset", "61.43", "21.65", "39.78"),      # komisyon +%20 KDV gider, aynı KDV indirilir
    ("included", "unset", "81.43", "24.99", "56.44"),      # komisyonun içindeki KDV indirilir
    ("excluded", "included", "61.43", "10.24", "51.19"),   # + kargo/hizmet/reklam KDV'si indirilir (61,43 − 10,24)
])
def test_commission_and_expense_vat_modes(client, engine, cmode, emode, before, vat, after):
    _set(engine, finance__commission_vat_mode=cmode, finance__expense_vat_mode=emode)
    oid = _order(engine)
    screens, detail, _ = _screens(client, oid)
    assert set(screens.values()) == {(D(before), D(after))}, screens
    assert D(str(detail["vat_estimate"])) == D(vat)
    notes = client.get("/api/finance/summary", params={"period": "today"}).json()["vat"]
    assert notes["complete"] is (cmode != "unset" and emode != "unset")


def test_legacy_order_without_items_has_unknown_vat(client, engine):
    with engine.begin() as c:
        store = c.execute(text("INSERT INTO stores(marketplace_id, name) SELECT id, 'T' FROM marketplaces WHERE code='trendyol' RETURNING id")).scalar()
        c.execute(text("""INSERT INTO orders(store_id, external_order_id, status, gross_revenue, net_profit, order_date)
                          VALUES (:s, 'LEGACY-1', 'Delivered', 300, 50, NOW())"""), {"s": store})
    o = next(x for x in client.get("/api/orders").json()["items"] if x["external_order_id"] == "LEGACY-1")
    assert o["vat_known"] is False and o["profit_after_vat"] is None and D(str(o["net_profit"])) == D("50")
    fin = client.get("/api/finance/summary", params={"period": "today"}).json()
    assert fin["orders"]["orders_without_items"] == 1 and fin["vat"]["complete"] is False


def test_cancelled_order_has_zero_profit_everywhere(client, engine):
    oid = _order(engine, status="cancelled")
    d = client.get(f"/api/orders/{oid}").json()
    assert D(str(d["profit_before_vat"])) == 0 and D(str(d["profit_after_vat"])) == 0
    assert client.get("/api/finance/summary", params={"period": "today"}).json()["orders"]["orders"] == 0


def test_draft_pricing_uses_same_vat_model():
    base = PricingRule(commission_rate=D("0.20"), shipping_cost=D("45"), fixed_cost=D("8.49"))
    e = estimate_profit(D("499.90"), D("250"), base, D("20"))
    assert e.vat == D("41.65") and e.margin == (e.profit / D("499.90")).quantize(D("0.0001"))
    assert e.markup == (e.profit / D("250")).quantize(D("0.0001"))
    excl = PricingRule(commission_rate=D("0.20"), shipping_cost=D("45"), fixed_cost=D("8.49"), commission_vat_mode="excluded")
    x = estimate_profit(D("499.90"), D("250"), excl, D("20"))
    assert x.commission == D("119.98") and x.commission_vat == D("20.00") and x.profit == e.profit   # KDV'si indirilir


def test_vat_settings_are_validated(client):
    bad = client.put("/api/settings", json={"values": {"finance.commission_vat_mode": "belki"}}, headers=H)
    assert bad.status_code == 422
    assert client.put("/api/settings", json={"values": {"finance.fx_rates": {"usd": "34.1"}}}, headers=H).status_code == 200
    items = {i["key"]: i for i in client.get("/api/settings").json()["items"]}
    assert items["finance.fx_rates"]["value"] == {"USD": "34.1"}
    assert set(items["finance.commission_vat_mode"]["choices"]) == {"unset", "included", "excluded"}
    assert client.put("/api/settings", json={"values": {"finance.fx_rates": {"TRY": "2"}}}, headers=H).status_code == 422
