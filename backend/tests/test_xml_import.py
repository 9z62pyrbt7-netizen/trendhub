"""XML içe aktarma: ürün node tespiti, varyantlar, SKU/Price güveni, image1..N, onay kapısı, idempotent senkron.

ÇANTELLA FIXTURE: Kullanıcının bildirdiği alan listesi (Product, ProductCode, ProductName, Quantity, Price,
Currency, TaxRate, Barcode, Category, Description, Image1.., Brand, Variants) ve CNT-101 beklenen değerlerine
göre HAZIRLANMIŞ test verisidir; gerçek feed DEĞİLDİR. Gerçek feed ağ izni verildiğinde ayrıca doğrulanır.
"""
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.suppliers import fetch as fetcher
from app.suppliers.fields import suggest_mapping_detailed
from app.suppliers.parsing import analyze_xml, field_paths, parse
from defusedxml import ElementTree as SafeET

H = {"X-Requested-With": "TrendHub"}
URLS = {}

CANTELLA = """<?xml version="1.0" encoding="UTF-8"?>
<Products>
  <Product>
    <ProductCode>CNT-101</ProductCode><ProductName>Cantella Kadın Omuz Çantası Siyah</ProductName>
    <Quantity>12</Quantity><Price>415.9</Price><Currency>TRY</Currency><TaxRate>10</TaxRate>
    <Barcode>CNT-101</Barcode><Category>Kadın&gt;Omuz Çantaları</Category>
    <Description><![CDATA[<p>Suni deri, iç cepli.</p>]]></Description>
    <Image1>https://cdn.cantella.example/cnt-101-1.jpg</Image1><Image2>https://cdn.cantella.example/cnt-101-2.jpg</Image2>
    <Image3>https://cdn.cantella.example/cnt-101-3.jpg</Image3><Brand>Cantella</Brand><Variants></Variants>
  </Product>""" + "".join(f"""
  <Product>
    <ProductCode>CNT-{n}</ProductCode><ProductName>Cantella Çanta {n}</ProductName><Quantity>{(n * 7) % 15}</Quantity>
    <Price>{300 + n}.5</Price><Currency>TRY</Currency><TaxRate>10</TaxRate><Barcode>CNT-{n}</Barcode>
    <Category>Kadın&gt;El Çantaları</Category><Description>Açıklama {n}</Description>
    <Image1>https://cdn.cantella.example/cnt-{n}-1.jpg</Image1><Brand>Cantella</Brand><Variants></Variants>
  </Product>""" for n in range(102, 114)) + "\n</Products>"

VARIANT_XML = """<Urunler><Kategoriler><Kategori><Id>1</Id><Ad>Çanta</Ad></Kategori><Kategori><Id>2</Id><Ad>Cüzdan</Ad></Kategori></Kategoriler>""" + "".join(f"""
<Urun><UrunKodu>U{i}</UrunKodu><UrunAdi>Deri Çanta {i}</UrunAdi><Kategori><Id>77</Id><Ad>Çanta</Ad></Kategori>
  <BayiFiyati>450,00</BayiFiyati><Fiyat>899,90</Fiyat><KDV>20</KDV><Marka>Bayim</Marka>
  <Varyantlar>""" + "".join(f"<Varyant><Barkod>8690{i}{j}</Barkod><Renk>Renk{j}</Renk><Beden>B{j}</Beden><Stok>{j * 5}</Stok></Varyant>" for j in range(1, 4)) + """</Varyantlar></Urun>""" for i in range(1, 3)) + "</Urunler>"

MAP_CANTELLA = {"supplier_sku": "ProductCode", "name": "ProductName", "stock": "Quantity", "purchase_price": "Price",
                "currency": "Currency", "vat_rate": "TaxRate", "barcode": "Barcode", "category": "Category",
                "description": "Description", "images": "Image1,Image2,Image3", "brand": "Brand", "variant": "Variants"}


@pytest.fixture
def client(engine, monkeypatch):
    real = fetcher.fetch

    def handler(req):
        body = URLS.get(str(req.url))
        return httpx.Response(200, content=body.encode()) if body is not None else httpx.Response(404)
    monkeypatch.setattr(fetcher, "fetch", lambda url, auth=None, transport=None: real(url, auth, transport=httpx.MockTransport(handler)))
    from app.main import app
    with TestClient(app) as c:
        assert c.post("/api/auth/login", json={"username": "admin", "password": "Admin-Password-123"},
                      headers=H).status_code == 200
        yield c


def _supplier(client, name, url, body):
    URLS[url] = body
    r = client.post("/api/suppliers", json={"name": name, "sync_interval_minutes": 60,
                                            "connection": {"integration_type": "xml", "source_url": url}}, headers=H)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _mappings(m):
    return [{"target_field": k, "source_path": v} for k, v in m.items()]


# ------------------------------------------------------------------ saf fonksiyonlar
def test_node_detection_prefers_product_over_variants_and_categories():
    a = analyze_xml(SafeET.fromstring(VARIANT_XML))
    assert a["record_path"] == "Urun" and a["variant_path"] == "Varyantlar/Varyant"
    kinds = {c["path"]: c["kind"] for c in a["candidates"]}
    assert kinds["Urun/Varyantlar/Varyant"] == "variant" and kinds["Kategoriler/Kategori"] != "product"
    c = analyze_xml(SafeET.fromstring(CANTELLA))
    assert c["record_path"] == "Product" and c["variant_path"] is None


def test_variants_explode_with_inherited_parent_fields():
    recs, path = parse(VARIANT_XML.encode(), "xml", "Urun", {"variant_path": "Varyantlar/Varyant"})
    assert path == "Urun" and len(recs) == 6
    r = recs[1]
    assert r["UrunAdi"] == "Deri Çanta 1" and r["BayiFiyati"] == "450,00"            # ana ürün mirası
    assert r["Varyantlar/Varyant/Barkod"] == "869012" and r["Varyantlar/Varyant/Stok"] == "10"
    assert r["Varyantlar/Varyant/Renk"] == "Renk2" and r["Varyantlar/Varyant/Beden"] == "B2"


def test_sku_and_price_confidence():
    xml = ("<Products>" + "".join(f"<Product><Id>{i}</Id><Code>CAT-5</Code><StockCode>ABC-{i}</StockCode>"
                                  f"<Title>Bag {i}</Title><Price>799.90</Price><Qty>4</Qty></Product>" for i in range(3)) + "</Products>")
    recs, _ = parse(xml.encode(), "xml")
    s = suggest_mapping_detailed(field_paths(recs))
    assert s["supplier_sku"]["path"] == "StockCode" and s["supplier_sku"]["confidence"] == "high"
    assert s["purchase_price"]["path"] == "Price" and s["purchase_price"]["needs_confirmation"] is True
    # Yalnızca Code (hepsi aynı) + Id: Code SKU olmaz, Id düşük güvenle önerilir
    xml2 = "<P>" + "".join(f"<Product><Id>{i}</Id><Code>CAT-5</Code><Title>x{i}</Title></Product>" for i in range(3)) + "</P>"
    recs2, _ = parse(xml2.encode(), "xml")
    s2 = suggest_mapping_detailed(field_paths(recs2))
    assert s2["supplier_sku"]["path"] == "Id" and s2["supplier_sku"]["confidence"] == "low"
    # Açık bayi/alış alanı yüksek güvenle, doğrulama istemeden
    recs3, _ = parse(VARIANT_XML.encode(), "xml", "Urun", {"variant_path": "Varyantlar/Varyant"})
    s3 = suggest_mapping_detailed(field_paths(recs3))
    assert s3["purchase_price"]["path"] == "BayiFiyati" and s3["purchase_price"]["needs_confirmation"] is False
    assert s3["color"]["path"].endswith("Renk") and s3["size"]["path"].endswith("Beden")


def test_numbered_images_are_combined():
    recs, _ = parse(CANTELLA.encode(), "xml")
    s = suggest_mapping_detailed(field_paths(recs))
    assert s["images"]["path"] == "Image1,Image2,Image3"


# ------------------------------------------------------------------ sihirbaz + onay kapısı
def test_cantella_wizard_flow_approval_gate_and_cnt101(client, engine):
    url = "https://feed.cantella.example/0-cantellavaryantsiz.xml"
    sid = _supplier(client, "Çantella", url, CANTELLA)
    # Onaysız: senkron 409, zamanlayıcı iş açmaz, analiz hiçbir şey yazmaz
    assert client.post(f"/api/suppliers/{sid}/sync", headers=H).status_code == 409
    from app.services import sync_service
    with engine.begin() as c:
        assert sync_service.schedule_supplier_jobs(c) == []
    a = client.post(f"/api/suppliers/{sid}/import/analyze", json={}, headers=H).json()
    assert a["record_path"] == "Product" and a["total"] == 13
    assert {c["path"] for c in a["nodes"]["candidates"] if c["kind"] == "product"} == {"Product"}
    by = {m["target_field"]: m for m in a["mapping"]}
    assert by["supplier_sku"]["source_path"] == "ProductCode" and by["images"]["source_path"] == "Image1,Image2,Image3"
    assert by["purchase_price"]["source_path"] == "Price" and a["price_needs_confirmation"] is True
    assert len(a["samples"]) == 10 and a["summary"]["zero_stock"] >= 1
    with engine.begin() as c:
        assert c.execute(text("SELECT COUNT(*) FROM supplier_products")).scalar() == 0
    # Doğrulanmamış belirsiz 'Price' ile onay reddedilir; KDV durumu zorunlu
    body = {"record_path": "Product", "variant_path": None, "mappings": _mappings(MAP_CANTELLA),
            "price_vat_mode": "excluded"}
    r = client.post(f"/api/suppliers/{sid}/import/approve", json=body, headers=H)
    assert r.status_code == 422 and "alış fiyatı olduğunu doğrulayın" in r.json()["detail"]
    assert client.post(f"/api/suppliers/{sid}/import/approve", json={**body, "confirm_price": True, "price_vat_mode": "belki"},
                       headers=H).status_code == 422
    r = client.post(f"/api/suppliers/{sid}/import/approve", json={**body, "confirm_price": True}, headers=H)
    assert r.status_code == 200, r.text
    imp = r.json()["import"]
    assert imp["created_count"] == 13 and imp["error_count"] == 0
    pool = {p["supplier_sku"]: p for p in client.get("/api/supplier-products", params={"supplier_id": sid,
                                                                                         "page_size": 50}).json()["items"]}
    p = pool["CNT-101"]
    assert (p["barcode"], p["stock"], Decimal(str(p["cost"])), p["currency"], Decimal(str(p["vat_rate"]))) == \
        ("CNT-101", 12, Decimal("415.90"), "TRY", Decimal("10"))
    assert p["brand"] == "Cantella" and p["category"] == "Kadın>Omuz Çantaları" and p["image_count"] == 3
    assert p["sale_price"] is None                                    # Price satış fiyatına YAZILMADI
    assert Decimal(str(p["effective_cost"])) == Decimal("457.49")    # KDV hariç 415,90 + %10
    # Onaylı: zamanlayıcı artık planlar; aynı içerik ikinci kez: yeni/değişen yok (idempotent)
    with engine.begin() as c:
        assert len(sync_service.schedule_supplier_jobs(c)) == 1
    again = client.post(f"/api/suppliers/{sid}/import/approve", json={**body, "confirm_price": True}, headers=H).json()["import"]
    assert again["created_count"] == 0 and again["updated_count"] == 0
    # Eşleştirme değişirse onay düşer
    changed = client.put(f"/api/suppliers/{sid}/mappings", json={"mappings": [{"target_field": "brand", "source_path": "ProductName"}]},
                         headers=H).json()
    assert changed["approval_reset"] is True
    assert client.post(f"/api/suppliers/{sid}/sync", headers=H).status_code == 409
    # Price satış fiyatı olmadı: taslak fiyatı kurallarla maliyetten hesaplanır
    pid = client.post("/api/transfer/import", json={"supplier_product_ids": [p["id"]]}, headers=H).json()["product_ids"][0]
    with engine.begin() as c:
        cost, sale = c.execute(text("SELECT cost, sale_price FROM products WHERE id = :p"), {"p": pid}).one()
    assert cost == Decimal("457.49") and sale == 0


def test_variant_feed_imports_each_variant(client, engine):
    url = "https://feed.example/varyantli.xml"
    sid = _supplier(client, "Varyantlı", url, VARIANT_XML)
    a = client.post(f"/api/suppliers/{sid}/import/analyze", json={}, headers=H).json()
    assert a["record_path"] == "Urun" and a["variant_path"] == "Varyantlar/Varyant" and a["total"] == 6
    m = {"supplier_sku": "UrunKodu", "name": "UrunAdi", "purchase_price": "BayiFiyati", "barcode": "Varyantlar/Varyant/Barkod",
         "stock": "Varyantlar/Varyant/Stok", "color": "Varyantlar/Varyant/Renk", "size": "Varyantlar/Varyant/Beden",
         "vat_rate": "KDV", "brand": "Marka"}
    r = client.post(f"/api/suppliers/{sid}/import/approve", json={"record_path": "Urun", "variant_path": "Varyantlar/Varyant",
                    "mappings": _mappings(m), "price_vat_mode": "included"}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["import"]["created_count"] == 6
    pool = {p["supplier_sku"]: p for p in client.get("/api/supplier-products", params={"supplier_id": sid}).json()["items"]}
    v = pool["U1-869012"]
    assert (v["barcode"], v["stock"], v["color"], v["size"], v["parent_code"]) == ("869012", 10, "Renk2", "B2", "U1")
    assert v["name"] == "Deri Çanta 1" and Decimal(str(v["cost"])) == Decimal("450.00")
    # Varyant yolu tedarikçi düzenlemesinde korunur
    s = client.get(f"/api/suppliers/{sid}").json()
    client.put(f"/api/suppliers/{sid}", json={"name": s["supplier"]["name"], "sync_interval_minutes": 60,
                                              "connection": {"integration_type": "xml", "record_path": "Urun"}}, headers=H)
    with engine.begin() as c:
        opts = c.execute(text("SELECT options FROM supplier_connections WHERE supplier_id = :s"), {"s": sid}).scalar()
    assert opts.get("variant_path") == "Varyantlar/Varyant"


def test_malformed_xml_is_friendly_error(client):
    sid = _supplier(client, "Bozuk", "https://feed.example/bozuk.xml", "<Products><Product><Code>1</Product>")
    r = client.post(f"/api/suppliers/{sid}/import/analyze", json={}, headers=H)
    assert r.status_code == 422 and "XML okunamadı" in r.json()["detail"]


def test_foreign_currency_warning_in_analysis(client):
    sid = _supplier(client, "Dövizli", "https://feed.example/usd.xml", CANTELLA.replace("<Currency>TRY</Currency>", "<Currency>USD</Currency>"))
    a = client.post(f"/api/suppliers/{sid}/import/analyze", json={}, headers=H).json()
    assert any("USD" in w and "kur" in w for w in a["warnings"])
    assert a["samples"][0]["effective_cost"] is None and "Döviz kuru tanımlı değil" in a["samples"][0]["cost_note"]
