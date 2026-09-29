"""Ürün yaşam döngüsü, ürün havuzu filtreleri, renk/varyant eşleştirme, dashboard kontrol merkezi, dostça hatalar."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.test_suppliers import MAP_A, XML_A, _catalog, make_supplier, upload

H = {"X-Requested-With": "TrendHub"}
XML_COLOR = XML_A.replace("<Marka>Bayim</Marka><AlisFiyati>150,00</AlisFiyati>",
                          "<Marka>Bayim</Marka><Renk>Siyah</Renk><Beden>Büyük</Beden><AlisFiyati>150,00</AlisFiyati>")


@pytest.fixture
def client(engine):
    from app.main import app
    with TestClient(app) as c:
        assert c.post("/api/auth/login", json={"username": "admin", "password": "Admin-Password-123"},
                      headers=H).status_code == 200
        yield c


def test_color_variant_mapped_and_shown_in_pool(client):
    sid = make_supplier(client, "Çanta Bayim", {**MAP_A, "color": "Renk", "variant": "Beden"})
    r = upload(client, sid, XML_COLOR)
    assert r["created_count"] == 3
    items = {i["supplier_sku"]: i for i in client.get("/api/supplier-products", params={"supplier_id": sid}).json()["items"]}
    assert items["CB-100"]["color"] == "Siyah" and items["CB-100"]["variant"] == "Büyük"
    assert items["CB-200"]["color"] is None and items["CB-100"]["stores"] == []
    # Aynı içerik tekrar gelince 'değişti' sayılmaz
    assert upload(client, sid, XML_COLOR)["updated_count"] == 0


def test_mapping_suggestion_finds_color_and_variant():
    from app.suppliers.fields import suggest_mapping
    s = suggest_mapping(["Urun/Renk", "Urun/Beden", "Urun/UrunKodu"])
    got = {k: (v["source_path"] if isinstance(v, dict) else v) for k, v in s.items()} if isinstance(s, dict) else {
        m["target_field"]: m["source_path"] for m in s}
    assert got.get("color") == "Urun/Renk" and got.get("variant") == "Urun/Beden"


def test_pool_filters(client, engine):
    sid = make_supplier(client, "Çanta Bayim", MAP_A)
    upload(client, sid, XML_A)
    pool = lambda **q: {i["supplier_sku"] for i in client.get("/api/supplier-products", params={"supplier_id": sid, **q}).json()["items"]}  # noqa: E731
    assert pool(stock="out") == {"CB-300"} and pool(stock="in") == {"CB-100", "CB-200"}
    assert pool(category="sırt") == {"CB-100"}
    assert pool(price_min="100", price_max="200") == {"CB-100"}
    assert "CB-300" in pool(problematic="true") and "CB-100" not in pool(problematic="true")
    item = next(i for i in client.get("/api/supplier-products", params={"supplier_id": sid}).json()["items"] if i["supplier_sku"] == "CB-300")
    assert {"Barkod yok", "Görsel yok", "Stok yok"} <= set(item["problems"])
    # Mağazada olan / olmayan
    _catalog(client, sid)
    with engine.begin() as c:
        from app.services.orders_sync import ensure_store
        store = ensure_store(c, "trendyol", "1", "Trendyol")
        c.execute(text("""INSERT INTO marketplace_listings(store_id, external_product_id, barcode, title)
                          VALUES (:s, 'TY-1', '8690000000017', 'İlan')"""), {"s": store})
    assert pool(in_store="yes") == {"CB-100"} and "CB-100" not in pool(in_store="no")
    listed = next(i for i in client.get("/api/supplier-products", params={"supplier_id": sid}).json()["items"] if i["supplier_sku"] == "CB-100")
    assert listed["stores"] == ["Trendyol"]


def test_product_lifecycle_contains_all_sections(client, engine):
    sid = make_supplier(client, "Çanta Bayim", MAP_A)
    upload(client, sid, XML_A)
    pid = _catalog(client, sid)[0]
    client.post("/api/transfer/drafts", json={"product_ids": [pid], "marketplaces": ["trendyol"]}, headers=H)
    d = client.get(f"/api/products/{pid}/lifecycle").json()
    for key in ("product", "suppliers", "drafts", "listings", "orders", "shipments", "returns", "finance", "ads",
                "alerts", "publications", "stores"):
        assert key in d
    assert d["suppliers"][0]["supplier_name"] == "Çanta Bayim" and len(d["drafts"]) == 1
    assert d["finance"]["is_estimate"] is True
    assert client.get("/api/products/999999/lifecycle").status_code == 404


def test_dashboard_control_center_fields(client):
    d = client.get("/api/dashboard", params={"period": "this_month"}).json()
    for key in ("ad_spend", "alert_summary", "most_profitable", "critical_stock", "integrations", "suppliers"):
        assert key in d
    assert "ad_spend" in d["today"] and d["alert_summary"]["total"] == 0


def test_validation_errors_are_friendly_and_do_not_echo_input(client):
    secret = "cok-gizli-deger-12345"
    r = client.post("/api/users", json={"username": "x", "role": "hacker", "password": secret}, headers=H)
    assert r.status_code == 422
    body = r.json()
    assert body["detail"].startswith("Girilen bilgilerde hata var") and "role" in body["detail"]
    assert any(e["field"] == "role" and e["message"] == "geçersiz biçim" for e in body["errors"])
    assert secret not in r.text and "technical" in body
