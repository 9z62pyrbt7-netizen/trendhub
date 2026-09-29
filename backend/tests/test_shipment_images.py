"""Kargo ekranı: sipariş kalemleri ve ürün görselleri (yalnızca kesin eşleşme, salt okunur)."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.connectors.base import NormalizedLine, NormalizedOrder, NormalizedShipment

H = {"X-Requested-With": "TrendHub"}
LONG = "Kadın Hakiki Deri Çok Gözlü Fermuarlı Büyük Boy Omuz ve Sırt Çantası — Taba Kahverengi Özel Seri 2026"


@pytest.fixture
def client(engine):
    from app.main import app
    with TestClient(app) as c:
        assert c.post("/api/auth/login", json={"username": "admin", "password": "Admin-Password-123"},
                      headers=H).status_code == 200
        yield c


def _order(conn, store, number, lines):
    from app.services.orders_sync import upsert_orders
    upsert_orders(conn, store, [NormalizedOrder(
        external_order_id=number, marketplace_status="Shipped", internal_status="shipped",
        order_date=datetime.now(timezone.utc),
        lines=[NormalizedLine(f"{number}-{i}", sku, bc, name, qty, Decimal("100")) for i, (sku, bc, name, qty) in enumerate(lines)],
        shipments=[NormalizedShipment(external_package_id=f"P-{number}", carrier="Yurtiçi Kargo",
                                      tracking_number=f"TRK-{number}", marketplace_status="Shipped")])])


def _seed(engine):
    from app.services.orders_sync import ensure_store
    with engine.begin() as c:
        store = ensure_store(c, "trendyol", "1", "Trendyol")
        other = ensure_store(c, "hepsiburada", "2", "Hepsiburada")
        c.execute(text("""INSERT INTO products(sku, barcode, name, image_url, images) VALUES
            ('SKU-IMG', 'B-IMG', 'Görselli ürün', 'https://cdn.example/urun.jpg', '[]'),
            ('SKU-JSON', 'B-JSON', 'Images dizili ürün', NULL, '["https://cdn.example/dizi-1.jpg"]'),
            ('SKU-NOIMG', 'B-NOIMG', 'Görselsiz ürün', NULL, '[]'),
            ('SKU-LIST', 'B-LIST', 'İlan görselli ürün', NULL, '[]'),
            ('SKU-BAD', 'B-BAD', 'Güvensiz görsel', 'javascript:alert(1)', '[]')"""))
        pid = dict(c.execute(text("SELECT sku, id FROM products")).all())
        c.execute(text("""INSERT INTO marketplace_listings(store_id, product_id, external_product_id, barcode, title, image_url) VALUES
            (:s, :pl, 'L1', 'B-LIST', 'x', 'https://cdn.trendyol.example/list.jpg'),
            (:s, NULL, 'L2', 'B-ONLYBC', 'y', 'https://cdn.trendyol.example/barkod.jpg'),
            (:s, NULL, 'L3', 'B-AMBIG', 'z', 'https://cdn.trendyol.example/a.jpg'),
            (:s, NULL, 'L4', 'B-AMBIG', 'z', 'https://cdn.trendyol.example/b.jpg'),
            (:o, NULL, 'L5', 'B-OTHERSTORE', 'w', 'https://cdn.hb.example/w.jpg')"""),
                  {"s": store, "o": other, "pl": pid["SKU-LIST"]})
        sp = c.execute(text("INSERT INTO suppliers(code, name) VALUES ('cb', 'Çanta Bayim') RETURNING id")).scalar()
        c.execute(text("""INSERT INTO supplier_products(supplier_id, product_id, supplier_sku, name, color, variant)
                          VALUES (:s, :p, 'CB-1', 'x', 'Siyah', 'Büyük')"""), {"s": sp, "p": pid["SKU-IMG"]})
        _order(c, store, "TY-ONE", [("SKU-IMG", "B-IMG", "Görselli ürün", 1)])
        _order(c, store, "TY-MULTI", [("SKU-JSON", "B-JSON", "Images dizili ürün", 2), ("SKU-NOIMG", "B-NOIMG", LONG, 1),
                                      ("SKU-LIST", "B-LIST", "İlan görselli ürün", 3)])
        _order(c, store, "TY-BC", [(None, "B-ONLYBC", "Sadece barkod", 1), (None, "B-AMBIG", "Belirsiz barkod", 1),
                                   (None, "B-OTHERSTORE", "Başka mağaza", 1), ("SKU-BAD", "B-BAD", "Güvensiz", 1)])
        before = c.execute(text("SELECT md5(string_agg(o::text, ',' ORDER BY id)) FROM orders o")).scalar()
    return before


def test_shipments_include_items_with_exact_images(client, engine):
    before = _seed(engine)
    d = client.get("/api/shipments").json()
    by = {s["external_order_id"]: s for s in d["items"]}
    # Kargo bilgileri eskisi gibi
    one = by["TY-ONE"]
    assert one["carrier"] == "Yurtiçi Kargo" and one["tracking_number"] == "TRK-TY-ONE" and one["status_label"]
    # Tek ürünlü sipariş, katalog görseli + renk/varyant
    [it] = one["items"]
    assert it["image_url"] == "https://cdn.example/urun.jpg" and it["image_source"] == "product"
    assert (it["color"], it["variant"], it["quantity"]) == ("Siyah", "Büyük", 1)
    # Çok ürünlü sipariş: her ürün kendi görseliyle
    multi = {i["sku"]: i for i in by["TY-MULTI"]["items"]}
    assert len(multi) == 3
    assert multi["SKU-JSON"]["image_url"] == "https://cdn.example/dizi-1.jpg" and multi["SKU-JSON"]["quantity"] == 2
    assert multi["SKU-NOIMG"]["image_url"] is None and multi["SKU-NOIMG"]["product_name"] == LONG
    assert multi["SKU-LIST"]["image_url"] == "https://cdn.trendyol.example/list.jpg" and multi["SKU-LIST"]["image_source"] == "listing"
    # Barkod: yalnızca aynı mağazada ve tek anlamlı eşleşme
    bc = {i["barcode"]: i for i in by["TY-BC"]["items"]}
    assert bc["B-ONLYBC"]["image_url"] == "https://cdn.trendyol.example/barkod.jpg" and bc["B-ONLYBC"]["image_source"] == "listing_barcode"
    assert bc["B-AMBIG"]["image_url"] is None          # farklı görselli iki ilan: yanlış fotoğraf gösterilmez
    assert bc["B-OTHERSTORE"]["image_url"] is None     # başka mağazanın ilanı kullanılmaz
    assert bc["B-BAD"]["image_url"] is None            # javascript: URL asla dönmez
    # Salt okunur: siparişler değişmedi
    with engine.begin() as c:
        assert c.execute(text("SELECT md5(string_agg(o::text, ',' ORDER BY id)) FROM orders o")).scalar() == before


def test_shipments_filters_still_work(client, engine):
    _seed(engine)
    r = client.get("/api/shipments", params={"q": "TRK-TY-MULTI"}).json()
    assert r["total"] == 1 and len(r["items"][0]["items"]) == 3
    assert client.get("/api/shipments", params={"carrier": "Yurtiçi Kargo"}).json()["total"] == 3
    assert "Yurtiçi Kargo" in client.get("/api/shipments").json()["carriers"]


def test_shipments_empty(client):
    d = client.get("/api/shipments").json()
    assert d["items"] == [] and d["total"] == 0
