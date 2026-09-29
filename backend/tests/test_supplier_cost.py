"""Tedarikçi maliyeti: KDV dahil/hariç, para birimi güvenliği (kur yoksa TL sayılmaz), taslak/havuz uyarıları."""
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.domain.suppliers import effective_cost
from tests.test_suppliers import _catalog, make_supplier, upload

H = {"X-Requested-With": "TrendHub"}
D = Decimal
FX = {"TRY": D("1")}
MAP = {"supplier_sku": "Kod", "barcode": "Barkod", "name": "Ad", "category": "Kategori", "brand": "Marka",
       "purchase_price": "Alis", "currency": "ParaBirimi", "vat_rate": "KDV", "stock": "Stok", "images": "Resim"}


def _xml(cur="TRY", price="415,90", vat="10"):
    return (f"<Urunler><Urun><Kod>K-1</Kod><Barkod>869000111</Barkod><Ad>Omuz Çantası</Ad><Kategori>Kadın>Omuz</Kategori>"
            f"<Marka>M</Marka><Alis>{price}</Alis><ParaBirimi>{cur}</ParaBirimi><KDV>{vat}</KDV><Stok>12</Stok>"
            f"<Resim>https://cdn.example/k1.jpg</Resim></Urun></Urunler>")


@pytest.fixture
def client(engine):
    from app.main import app
    with TestClient(app) as c:
        assert c.post("/api/auth/login", json={"username": "admin", "password": "Admin-Password-123"},
                      headers=H).status_code == 200
        yield c


def test_effective_cost_rules():
    assert effective_cost(D("415.90"), "TRY", D("10"), "included", FX) == (D("415.90"), None)
    assert effective_cost(D("415.90"), "TL", D("10"), "excluded", FX) == (D("457.49"), None)       # +%10 KDV
    c, note = effective_cost(D("415.90"), "TRY", None, "excluded", FX)
    assert c is None and "KDV oranı yok" in note                                                     # uydurma yok
    c, note = effective_cost(D("10"), "USD", D("20"), "included", FX)
    assert c is None and "Döviz kuru tanımlı değil (USD)" in note                                    # sessizce TL değil
    c, note = effective_cost(D("10"), "usd", D("20"), "included", {**FX, "USD": D("34.10")})
    assert c == D("341.00") and "USD" in note
    c, note = effective_cost(D("100"), None, D("20"), None, FX)
    assert c == D("100.00") and "belirtilmedi" in note                                               # geriye uyumlu


def _supplier(client, mode):
    sid = make_supplier(client, "Test Tedarikçi", MAP)
    if mode:
        body = client.get(f"/api/suppliers/{sid}").json()["supplier"]
        r = client.put(f"/api/suppliers/{sid}", json={"name": body["name"], "price_vat_mode": mode,
                                                      "sync_interval_minutes": 0}, headers=H)
        assert r.status_code == 200, r.text
    return sid


def test_vat_excluded_supplier_price_becomes_vat_included_catalog_cost(client, engine):
    sid = _supplier(client, "excluded")
    upload(client, sid, _xml())
    pid = _catalog(client, sid)[0]
    with engine.begin() as c:
        cost, sale = c.execute(text("SELECT cost, sale_price FROM products WHERE id = :p"), {"p": pid}).one()
    assert cost == D("457.49")          # 415,90 + %10 KDV
    assert sale == 0                    # tedarikçi alış fiyatı satış fiyatına YAZILMAZ
    # Mod değişmezse düzenleme ayarı silmez
    body = client.get(f"/api/suppliers/{sid}").json()["supplier"]
    assert body["price_vat_mode"] == "excluded"
    client.put(f"/api/suppliers/{sid}", json={"name": body["name"], "sync_interval_minutes": 0}, headers=H)
    assert client.get(f"/api/suppliers/{sid}").json()["supplier"]["price_vat_mode"] == "excluded"


def test_foreign_currency_without_fx_is_not_used_as_try(client, engine):
    sid = _supplier(client, "included")
    upload(client, sid, _xml(cur="USD", price="12,50"))
    pool = client.get("/api/supplier-products", params={"supplier_id": sid}).json()["items"][0]
    assert pool["effective_cost"] is None and any("Döviz kuru tanımlı değil (USD)" in p for p in pool["problems"])
    pid = _catalog(client, sid)[0]
    with engine.begin() as c:
        assert c.execute(text("SELECT cost FROM products WHERE id = :p"), {"p": pid}).scalar() == 0   # 12,50 TL değil
    for cat in ("Kadın>Omuz",):
        client.put("/api/category-mappings", json={"marketplace": "trendyol", "source_category": cat,
                                                   "target_category_id": "1"}, headers=H)
    d = client.post("/api/transfer/drafts", json={"product_ids": [pid], "marketplaces": ["trendyol"]}, headers=H).json()
    draft = client.get("/api/listing-drafts", params={"ids": d["draft_ids"][0]}).json()["items"][0]
    assert draft["status"] == "invalid" and any("Döviz kuru tanımlı değil (USD)" in e for e in draft["errors"])
    # Kur tanımlanınca kullanılır
    assert client.put("/api/settings", json={"values": {"finance.fx_rates": {"USD": "34"}}}, headers=H).status_code == 200
    client.post("/api/listing-drafts/validate", json={"ids": d["draft_ids"]}, headers=H)
    draft = client.get("/api/listing-drafts", params={"ids": d["draft_ids"][0]}).json()["items"][0]
    assert D(str(draft["cost_basis"])) == D("425.00")
