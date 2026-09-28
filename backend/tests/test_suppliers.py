"""Çoklu tedarikçi mimarisi: ayrıştırma, eşleştirme, secret saklama, senkron, katalog, teklifler, aktarım."""
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.connectors.base import RetryableError
from app.domain.pricing import PricingRule, estimate_profit, round_price, suggest_price
from app.domain.suppliers import Offer, effective_stock, select_offer
from app.security import hash_password
from app.services import supplier_sync, sync_service
from app.suppliers import fetch as fetcher
from app.suppliers.fields import suggest_mapping
from app.suppliers.mapping import apply_mapping
from app.suppliers.parsing import ParseError, field_paths, parse
from app.suppliers.secrets import SecretStoreError, decrypt, encrypt, mask_url, read_env_secret

H = {"X-Requested-With": "TrendHub"}
SECRET_TOKEN = "gizli-bayi-token-9f8e7d6c5b"
FEED_URL = f"https://xml.tedarikci.example/urunler.xml?bayi=123&token={SECRET_TOKEN}"

# Çanta Bayim tarzı (Türkçe alan adlı) örnek XML — gerçek bir tedarikçinin sözleşmesi DEĞİLDİR.
XML_A = """<?xml version="1.0" encoding="UTF-8"?>
<Urunler>
  <Urun>
    <UrunKodu>CB-100</UrunKodu><Barkod>8690000000017</Barkod><UrunAdi>Deri Sırt Çantası</UrunAdi>
    <Kategori>Çanta &gt; Sırt Çantası</Kategori><Marka>Bayim</Marka><AlisFiyati>150,00</AlisFiyati>
    <PiyasaFiyati>399,90</PiyasaFiyati><Stok>40</Stok><KDV>20</KDV><Desi>2</Desi>
    <Aciklama>Hakiki deri</Aciklama>
    <Resimler><Resim>https://cdn.example/cb100-1.jpg</Resim><Resim>https://cdn.example/cb100-2.jpg</Resim></Resimler>
  </Urun>
  <Urun>
    <UrunKodu>CB-200</UrunKodu><Barkod>8690000000024</Barkod><UrunAdi>Omuz Çantası</UrunAdi>
    <Kategori>Çanta &gt; Omuz Çantası</Kategori><Marka>Bayim</Marka><AlisFiyati>1.250,50</AlisFiyati>
    <Stok>5</Stok><KDV>20</KDV>
    <Resimler><Resim>https://cdn.example/cb200.jpg</Resim></Resimler>
  </Urun>
  <Urun>
    <UrunKodu>CB-300</UrunKodu><UrunAdi>Barkodsuz Cüzdan</UrunAdi><Kategori>Cüzdan</Kategori>
    <AlisFiyati>80</AlisFiyati><Stok>0</Stok>
  </Urun>
</Urunler>"""

CSV_B = ("sku;ean;title;brand;category;cost;qty;image\n"
         "B-1;8690000000017;Deri Sirt Cantasi;Bayim;Canta;142,00;15;https://b.example/1.jpg\n"
         "B-9;8690000000099;Kartlik;Diger;Cuzdan;30,00;100;https://b.example/9.jpg\n")

MAP_A = {"supplier_sku": "UrunKodu", "barcode": "Barkod", "name": "UrunAdi", "category": "Kategori",
         "brand": "Marka", "purchase_price": "AlisFiyati", "sale_price": "PiyasaFiyati", "stock": "Stok",
         "vat_rate": "KDV", "desi": "Desi", "description": "Aciklama", "images": "Resimler/Resim"}
MAP_B = {"supplier_sku": "sku", "barcode": "ean", "name": "title", "brand": "brand", "category": "category",
         "purchase_price": "cost", "stock": "qty", "images": "image"}


# ------------------------------------------------------------------ yardımcılar
@pytest.fixture
def client(engine):
    from app.main import app
    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"username": "admin", "password": "Admin-Password-123"}, headers=H)
        assert r.status_code == 200
        yield c


def _mappings(m: dict) -> dict:
    return {"mappings": [{"target_field": k, "source_path": v} for k, v in m.items()]}


def make_supplier(client, name, mapping, **conn) -> int:
    body = {"name": name, "priority": conn.pop("priority", 100),
            "connection": {"integration_type": conn.pop("integration_type", "manual"), **conn}}
    r = client.post("/api/suppliers", json=body, headers=H)
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert client.put(f"/api/suppliers/{sid}/mappings", json=_mappings(mapping), headers=H).status_code == 200
    return sid


def upload(client, sid, content: str):
    r = client.post(f"/api/suppliers/{sid}/upload", content=content.encode(), headers={**H, "Content-Type": "text/plain"})
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------ ayrıştırma
def test_parse_xml_detects_records_nested_lists_and_turkish_numbers():
    records, path = parse(XML_A.encode(), "xml")
    assert path == "Urun" and len(records) == 3
    assert records[0]["Resimler/Resim"] == ["https://cdn.example/cb100-1.jpg", "https://cdn.example/cb100-2.jpg"]
    m = apply_mapping(records[1], {k: {"source_path": v} for k, v in MAP_A.items()})
    assert m.ok and m.values["purchase_price"] == Decimal("1250.50") and m.values["stock"] == 5
    assert m.values["images"] == ["https://cdn.example/cb200.jpg"]
    # Kayıt yolu kök dahil de yazılabilir
    assert parse(XML_A.encode(), "xml", "Urunler/Urun")[1] == "Urun"


def test_parse_json_csv_and_attributes():
    js = b'{"data": {"items": [{"code": "J1", "price": {"amount": "12.5"}, "images": ["a", "b"]}]}}'
    recs, path = parse(js, "api", "data/items")
    assert path == "data/items" and recs[0]["price/amount"] == "12.5" and recs[0]["images"] == ["a", "b"]
    csv_recs, _ = parse(CSV_B.encode("cp1254"), "csv")
    assert csv_recs[0]["cost"] == "142,00" and csv_recs[1]["sku"] == "B-9"
    x = b'<r><p id="7" stok="3"><ad>X</ad></p><p id="8"><ad>Y</ad></p></r>'
    recs, _ = parse(x, "xml")
    assert recs[0]["@id"] == "7" and recs[0]["@stok"] == "3" and recs[1]["ad"] == "Y"
    assert {f["path"] for f in field_paths(recs)} >= {"@id", "ad"}


def test_xml_with_dtd_or_external_entity_is_rejected():
    evil = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><r><p><a>&x;</a></p></r>'
    with pytest.raises(ParseError):
        parse(evil, "xml")
    with pytest.raises(ParseError):
        parse(b"   ", "xml")


def test_mapping_suggestion_covers_turkish_and_english_names():
    s = suggest_mapping(["UrunKodu", "Barkod", "UrunAdi", "AlisFiyati", "Stok", "Resimler/Resim", "KDV", "Marka"])
    assert s == {"supplier_sku": "UrunKodu", "barcode": "Barkod", "name": "UrunAdi", "brand": "Marka",
                 "purchase_price": "AlisFiyati", "stock": "Stok", "vat_rate": "KDV", "images": "Resimler/Resim"}
    s2 = suggest_mapping(["product_code", "sku", "ean", "title", "quantity", "cost"])
    assert s2["supplier_sku"] == "product_code" and s2["barcode"] == "ean" and s2["stock"] == "quantity"
    m = apply_mapping({"UrunAdi": "x"}, {"supplier_sku": {"source_path": "UrunKodu"}, "name": {"source_path": "UrunAdi"}})
    assert not m.ok and any("supplier_sku" in e for e in m.errors)


# ------------------------------------------------------------------ secret'lar
def test_secret_encryption_masking_and_env_restrictions(monkeypatch):
    tok = encrypt(SECRET_TOKEN)
    assert SECRET_TOKEN not in tok and decrypt(tok) == SECRET_TOKEN
    shown = mask_url(FEED_URL)
    assert SECRET_TOKEN not in shown and "123" not in shown and shown.startswith("https://xml.tedarikci.example/")
    with pytest.raises(SecretStoreError):
        read_env_secret("DATABASE_URL")   # yalnızca SUPPLIER_* okunabilir
    monkeypatch.setenv("SUPPLIER_ACME_TOKEN", "env-token-value")
    assert read_env_secret("SUPPLIER_ACME_TOKEN") == "env-token-value"
    from app.config import Settings
    with pytest.raises(SecretStoreError):
        encrypt("x", Settings(database_url="postgresql://x@y/z", app_secret="CHANGE_ME"))


def test_fetch_blocks_private_addresses_and_handles_http_errors(monkeypatch):
    monkeypatch.delenv("SUPPLIER_ALLOW_PRIVATE_URLS", raising=False)
    for url in ("http://127.0.0.1/feed.xml", "http://10.0.0.5/x", "http://localhost:8000/api/health", "ftp://a/b"):
        with pytest.raises(fetcher.FetchError):
            fetcher.check_url(url)
    seen = {}

    def handler(req: httpx.Request):
        seen["url"], seen["auth"] = str(req.url), req.headers.get("x-api-key")
        return httpx.Response(200, content=b"<r/>")
    fetcher.fetch("https://a.example/f.xml", fetcher.SourceAuth("header", param_name="X-Api-Key", secret="k1"),
                  transport=httpx.MockTransport(handler))
    assert seen["auth"] == "k1"
    fetcher.fetch("https://a.example/f.xml?x=1", fetcher.SourceAuth("query", param_name="key", secret="k2"),
                  transport=httpx.MockTransport(handler))
    assert "key=k2" in seen["url"] and "x=1" in seen["url"]
    with pytest.raises(fetcher.FetchError):
        fetcher.fetch("https://a.example/f", transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(RetryableError):
        fetcher.fetch("https://a.example/f", transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    # Sadece GET kullanılır
    methods = []
    fetcher.fetch("https://a.example/f", transport=httpx.MockTransport(
        lambda r: (methods.append(r.method), httpx.Response(200, content=b"x"))[1]))
    assert methods == ["GET"]


# ------------------------------------------------------------------ API + güvenlik
def test_supplier_connection_secrets_never_leave_backend(client, engine):
    sid = make_supplier(client, "Çanta Bayim", MAP_A, integration_type="xml", source_url=FEED_URL,
                        auth_type="bearer", secret="bearer-super-secret-777")
    for path in ("/api/suppliers", f"/api/suppliers/{sid}", "/api/dashboard", "/api/system/audit"):
        body = client.get(path).text
        assert SECRET_TOKEN not in body and "bearer-super-secret-777" not in body, path
    d = client.get(f"/api/suppliers/{sid}").json()
    assert d["supplier"]["code"] == "canta_bayim"
    assert d["connection"]["has_secret"] and d["connection"]["has_source_url"]
    assert "***" in d["connection"]["source_url_display"]
    with engine.begin() as c:
        raw = c.execute(text("SELECT source_url_enc, secret_enc FROM supplier_connections")).one()
        audit = " ".join(str(r) for r in c.execute(text("SELECT details FROM audit_logs")).scalars())
    assert SECRET_TOKEN not in raw[0] and "bearer-super-secret-777" not in raw[1]
    assert SECRET_TOKEN not in audit and "bearer-super-secret-777" not in audit
    # Boş gönderilmeyen secret korunur, "" ile temizlenir
    assert client.put(f"/api/suppliers/{sid}/connection", json={"integration_type": "xml", "auth_type": "bearer"},
                      headers=H).status_code == 200
    assert client.get(f"/api/suppliers/{sid}").json()["connection"]["has_secret"]
    client.put(f"/api/suppliers/{sid}/connection", json={"integration_type": "xml", "auth_type": "bearer", "secret": ""},
               headers=H)
    assert not client.get(f"/api/suppliers/{sid}").json()["connection"]["has_secret"]
    # Ortam değişkeni referansı SUPPLIER_ ile sınırlı
    r = client.put(f"/api/suppliers/{sid}/connection",
                   json={"integration_type": "xml", "auth_type": "bearer", "secret_env": "DATABASE_URL"}, headers=H)
    assert r.status_code == 422


def test_only_admin_manages_supplier_credentials(client, engine):
    with engine.begin() as c:
        c.execute(text("INSERT INTO users(username, password_hash, role) VALUES ('op', :h, 'operator'), ('vw', :h, 'viewer')"),
                  {"h": hash_password("Strong-Password-1")})
    sid = make_supplier(client, "Tedarikçi B", MAP_B)
    client.post("/api/auth/logout", headers=H)
    client.post("/api/auth/login", json={"username": "op", "password": "Strong-Password-1"}, headers=H)
    assert client.post("/api/suppliers", json={"name": "X"}, headers=H).status_code == 403
    assert client.put(f"/api/suppliers/{sid}/connection", json={"integration_type": "xml"}, headers=H).status_code == 403
    assert upload(client, sid, CSV_B)["created_count"] == 2      # operatör dosya yükleyebilir
    client.post("/api/auth/logout", headers=H)
    client.post("/api/auth/login", json={"username": "vw", "password": "Strong-Password-1"}, headers=H)
    assert client.get("/api/supplier-products").status_code == 200
    assert client.post(f"/api/suppliers/{sid}/upload", content=b"x", headers=H).status_code == 403


# ------------------------------------------------------------------ senkronizasyon
def test_sync_detects_new_price_stock_missing_and_never_deletes(client, engine):
    sid = make_supplier(client, "Çanta Bayim", MAP_A)
    r1 = upload(client, sid, XML_A)
    assert r1["status"] == "success" and r1["created_count"] == 3 and r1["records_total"] == 3
    # Fiyat ve stok değişti, CB-300 kaldırıldı, CB-400 eklendi
    xml2 = (XML_A.replace("<AlisFiyati>150,00</AlisFiyati>", "<AlisFiyati>155,00</AlisFiyati>")
            .replace("<Stok>5</Stok>", "<Stok>7</Stok>")
            .replace("<UrunKodu>CB-300</UrunKodu>", "<UrunKodu>CB-400</UrunKodu>"))
    r2 = upload(client, sid, xml2)
    assert (r2["created_count"], r2["price_changed"], r2["stock_changed"], r2["missing_count"]) == (1, 1, 1, 1)
    with engine.begin() as c:
        st = dict(c.execute(text("SELECT supplier_sku, status FROM supplier_products WHERE supplier_id = :s"), {"s": sid}).all())
        kinds = sorted(c.execute(text("SELECT kind FROM supplier_product_changes WHERE run_id = :r"), {"r": r2["run_id"]}).scalars())
    assert st == {"CB-100": "active", "CB-200": "active", "CB-300": "missing", "CB-400": "active"}  # silinmedi
    assert kinds == ["missing", "new", "price", "stock"]
    # Yeniden göründü
    r3 = upload(client, sid, XML_A)
    assert r3["reactivated_count"] == 1
    changes = client.get(f"/api/suppliers/{sid}/changes", params={"kind": "missing"}).json()
    # 2. senkronda CB-300, 3. senkronda (XML_A'da olmayan) CB-400 kayboldu
    assert changes["total"] == 2 and {i["supplier_sku"] for i in changes["items"]} == {"CB-300", "CB-400"}
    s = client.get("/api/suppliers").json()[0]
    assert s["last_sync_status"] == "success" and s["product_count"] == 4 and s["health"] == "ok"


def test_row_errors_are_reported_and_feed_duplicates_ignored(client):
    sid = make_supplier(client, "T", MAP_B)
    bad = CSV_B + "B-1;1;kopya;x;y;10;1;\n;;adsiz;;;abc;1;\n"
    r = upload(client, sid, bad)
    assert r["created_count"] == 2 and r["error_count"] == 2 and r["status"] == "partial"
    assert any("tekrar eden" in e for e in r["errors"]) and any("supplier_sku" in e for e in r["errors"])


def test_mass_disappearance_is_not_marked_missing(client, engine):
    sid = make_supplier(client, "T", MAP_B)
    many = "sku;title;cost;qty\n" + "".join(f"S{i};Urun {i};10;5\n" for i in range(30))
    upload(client, sid, many)
    r = upload(client, sid, "sku;title;cost;qty\nS1;Urun 1;10;5\n")
    assert r["status"] == "partial" and r["missing_count"] == 0 and "İŞARETLENMEDİ" in r["message"]
    with engine.begin() as c:
        assert c.execute(text("SELECT COUNT(*) FROM supplier_products WHERE status = 'missing'")).scalar() == 0
    ev = client.get("/api/system/events").json()
    assert any("İŞARETLENMEDİ" in e["message"] for e in (ev["items"] if isinstance(ev, dict) else ev))


def test_worker_job_downloads_feed_over_http(client, engine, monkeypatch):
    sid = make_supplier(client, "Çanta Bayim", MAP_A, integration_type="xml", source_url=FEED_URL,
                        auth_type="none")
    client.put(f"/api/suppliers/{sid}", json={"name": "Çanta Bayim", "sync_interval_minutes": 60}, headers=H)
    with engine.begin() as c:
        created = sync_service.schedule_supplier_jobs(c)
        assert len(created) == 1 and sync_service.schedule_supplier_jobs(c) == []   # idempotent
    urls = []

    def handler(req):
        urls.append(str(req.url))
        return httpx.Response(200, content=XML_A.encode())
    real = supplier_sync.download
    monkeypatch.setattr(supplier_sync, "download", lambda cfg, transport=None: real(cfg, httpx.MockTransport(handler)))
    from app.worker import Worker
    w = Worker(engine=engine)
    assert w.run_once()
    assert urls == [FEED_URL]
    with engine.begin() as c:
        job = c.execute(text("SELECT status, result FROM sync_jobs WHERE job_type = 'supplier.sync'")).one()
    assert job[0] == "succeeded" and job[1]["created_count"] == 3
    assert SECRET_TOKEN not in str(job[1])


def test_failed_download_marks_supplier_error(client, engine, monkeypatch):
    sid = make_supplier(client, "Kırık", MAP_A, integration_type="xml", source_url="https://broken.example/x.xml")
    with pytest.raises(Exception):
        supplier_sync.run_supplier_sync(engine, sid, transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    s = client.get(f"/api/suppliers/{sid}").json()
    assert s["supplier"]["last_sync_status"] == "failed" and "404" in s["supplier"]["last_sync_error"]
    assert s["runs"][0]["status"] == "failed"
    assert next(x for x in client.get("/api/dashboard").json()["suppliers"] if x["id"] == sid)["health"] == "error"


def test_preview_returns_fields_suggestion_and_samples_without_writing(client, engine):
    sid = make_supplier(client, "Önizleme", {})
    r = client.post(f"/api/suppliers/{sid}/preview", content=XML_A.encode(), headers=H)
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["total"] == 3 and p["using"] == "suggestion" and p["suggestion"]["supplier_sku"] == "UrunKodu"
    assert p["samples"][0]["values"]["purchase_price"] == "150.00"
    with engine.begin() as c:
        assert c.execute(text("SELECT COUNT(*) FROM supplier_products")).scalar() == 0


# ------------------------------------------------------------------ katalog + teklifler
def test_same_barcode_from_two_suppliers_is_one_catalog_product_with_two_offers(client, engine):
    a = make_supplier(client, "Çanta Bayim", MAP_A, priority=1)
    b = make_supplier(client, "Tedarikçi B", MAP_B, priority=2)
    upload(client, a, XML_A)
    pool = client.get("/api/supplier-products", params={"supplier_id": a}).json()
    cb100 = next(i for i in pool["items"] if i["supplier_sku"] == "CB-100")
    imp = client.post("/api/transfer/import", json={"supplier_product_ids": [cb100["id"]]}, headers=H).json()
    assert imp["created"] == 1
    pid = imp["product_ids"][0]
    # B senkronunda aynı barkod otomatik olarak aynı katalog ürününe bağlanır (duplicate ürün yok)
    rb = upload(client, b, CSV_B)
    assert rb["linked_count"] == 1
    with engine.begin() as c:
        assert c.execute(text("SELECT COUNT(*) FROM products WHERE barcode = '8690000000017'")).scalar() == 1
    # İkinci import aynı ürünü tekrar oluşturmaz
    b1 = next(i for i in client.get("/api/supplier-products", params={"supplier_id": b}).json()["items"] if i["supplier_sku"] == "B-1")
    assert b1["product_id"] == pid and b1["offer_count"] == 2
    again = client.post("/api/transfer/import", json={"supplier_product_ids": [b1["id"]]}, headers=H).json()
    assert again["created"] == 0 and again["already_in_catalog"] == 1

    offers = client.get(f"/api/products/{pid}/offers").json()
    assert [o["supplier_name"] for o in offers["offers"]] == ["Tedarikçi B", "Çanta Bayim"]   # ucuzdan pahalıya
    assert offers["by_strategy"] == {"manual": a, "cheapest": b, "highest_stock": a, "priority": a}
    assert offers["selected_supplier_id"] == a   # import eden tedarikçi tercih edilen olur
    with engine.begin() as c:
        p = c.execute(text("SELECT stock, cost FROM products WHERE id = :p"), {"p": pid}).one()
    assert p == (40, Decimal("150.00"))
    # En ucuz stratejisine geç: stok/maliyet B'den gelir, maliyet geçmişine 'supplier' kaydı düşer
    assert client.put(f"/api/products/{pid}/sourcing", json={"strategy": "cheapest"}, headers=H).status_code == 200
    with engine.begin() as c:
        p = c.execute(text("SELECT stock, cost FROM products WHERE id = :p"), {"p": pid}).one()
        src = c.execute(text("SELECT source FROM product_costs WHERE product_id = :p ORDER BY id DESC LIMIT 1"), {"p": pid}).scalar()
    assert p == (15, Decimal("142.00")) and src == "supplier"
    # Teklifi olmayan tedarikçi tercih edilemez
    assert client.put(f"/api/products/{pid}/sourcing", json={"strategy": "manual", "preferred_supplier_id": 999},
                      headers=H).status_code == 422


def test_preferred_supplier_disappearing_zeroes_catalog_stock(client, engine):
    a = make_supplier(client, "A", MAP_A)
    upload(client, a, XML_A)
    ids = [i["id"] for i in client.get("/api/supplier-products").json()["items"]]
    pid = client.post("/api/transfer/import", json={"supplier_product_ids": ids}, headers=H).json()["product_ids"][0]
    upload(client, a, XML_A.replace("<UrunKodu>CB-100</UrunKodu>", "<UrunKodu>CB-101</UrunKodu>"))
    with engine.begin() as c:
        stock = c.execute(text("SELECT stock FROM products WHERE barcode = '8690000000017'")).scalar()
    assert stock == 0 and pid


def test_stock_rules_and_strategies_domain():
    assert effective_stock(40, {"buffer": 2}) == 38
    assert effective_stock(3, {"buffer": 1, "min_stock": 3}) == 0
    assert effective_stock(500, {"max_stock": 100}) == 100
    assert effective_stock(-4, {}) == 0
    offers = [Offer(1, 1, "A", Decimal("150"), 40, 1), Offer(2, 2, "B", Decimal("142"), 15, 2),
              Offer(3, 3, "C", Decimal("160"), 200, 3), Offer(4, 4, "D", Decimal("100"), 0, 0)]
    assert select_offer(offers, "cheapest", None).supplier_id == 2       # D stoksuz
    assert select_offer(offers, "highest_stock", None).supplier_id == 3
    assert select_offer(offers, "priority", None).supplier_id == 1
    assert select_offer(offers, "manual", 3).supplier_id == 3
    assert select_offer(offers, "manual", None) is None


def test_pricing_domain():
    rule = PricingRule(commission_rate=Decimal("0.20"), markup_rate=Decimal("0.30"), shipping_cost=Decimal("30"))
    p = suggest_price(Decimal("150"), rule)
    # (150*1.3 + 30) / 0.8 = 281.25 -> 281.90
    assert p == Decimal("281.90")
    assert round_price(Decimal("100.00"), "x.99") == Decimal("100.99")
    assert round_price(Decimal("100.95"), "x.90") == Decimal("101.90")
    assert round_price(Decimal("10.01"), "integer") == Decimal("11.00")
    e = estimate_profit(p, Decimal("150"), rule, Decimal("20"))
    assert e.commission == Decimal("56.38") and e.profit < p and e.margin is not None


# ------------------------------------------------------------------ ürün aktarımı
def _catalog(client, sid):
    ids = [i["id"] for i in client.get("/api/supplier-products", params={"supplier_id": sid}).json()["items"]]
    return client.post("/api/transfer/import", json={"supplier_product_ids": ids}, headers=H).json()["product_ids"]


def test_transfer_flow_per_marketplace_validation_and_prepare(client, engine):
    a = make_supplier(client, "Çanta Bayim", MAP_A)
    upload(client, a, XML_A)
    pids = _catalog(client, a)
    assert len(pids) == 3
    r = client.post("/api/transfer/drafts", json={"product_ids": pids, "marketplaces": ["trendyol", "hepsiburada"]},
                    headers=H).json()
    assert len(r["draft_ids"]) == 6 and r["valid"] == 0            # kategori eşleşmesi yok
    # Aynı işlem tekrarlanınca yeni taslak açılmaz (ürün+pazaryeri başına tek)
    r2 = client.post("/api/transfer/drafts", json={"product_ids": pids, "marketplaces": ["trendyol", "hepsiburada"]},
                     headers=H).json()
    assert sorted(r2["draft_ids"]) == sorted(r["draft_ids"])
    # Yalnızca Trendyol için kategori eşleştir
    for cat, tid in (("Çanta > Sırt Çantası", "1001"), ("Çanta > Omuz Çantası", "1002")):
        assert client.put("/api/category-mappings", json={"marketplace": "trendyol", "source_category": cat,
                                                          "target_category_id": tid}, headers=H).status_code == 200
    v = client.post("/api/listing-drafts/validate", json={"ids": r["draft_ids"]}, headers=H).json()
    ty = client.get("/api/listing-drafts", params={"marketplace": "trendyol"}).json()
    hb = client.get("/api/listing-drafts", params={"marketplace": "hepsiburada"}).json()
    by_sku = {d["sku"]: d for d in ty["items"]}
    assert by_sku["canta_bayim-CB-100"]["status"] == "draft" and by_sku["canta_bayim-CB-100"]["errors"] == []
    assert by_sku["canta_bayim-CB-100"]["price"] and Decimal(by_sku["canta_bayim-CB-100"]["estimated_margin"]) > 0
    assert by_sku["canta_bayim-CB-300"]["status"] == "invalid"   # barkod/stok/görsel yok
    assert all(d["status"] == "invalid" for d in hb["items"])   # HB kuralları bağımsız, kategori yok
    assert v["validated"] == 6
    # Pazaryeri kuralı bağımsız: HB'de kategori zorunluluğunu kaldır -> HB taslakları kendi kuralıyla geçer
    rules = {x["code"]: x for x in client.get("/api/marketplace-rules").json()["items"]}
    hb_rule = {k: rules["hepsiburada"][k] for k in ("markup_rate", "fixed_cost", "shipping_cost", "min_margin_rate",
                                                     "rounding", "stock_buffer", "min_stock", "max_stock", "title_max_length")}
    hb_rule["required_fields"] = ["barcode", "images"]
    hb_rule["markup_rate"] = "0.50"
    assert client.put("/api/marketplace-rules/hepsiburada", json=hb_rule, headers=H).status_code == 200
    client.post("/api/listing-drafts/validate", json={"ids": r["draft_ids"]}, headers=H)
    hb = {d["sku"]: d for d in client.get("/api/listing-drafts", params={"marketplace": "hepsiburada"}).json()["items"]}
    ty = {d["sku"]: d for d in client.get("/api/listing-drafts", params={"marketplace": "trendyol"}).json()["items"]}
    assert hb["canta_bayim-CB-100"]["status"] == "draft"
    assert Decimal(hb["canta_bayim-CB-100"]["price"]) > Decimal(ty["canta_bayim-CB-100"]["price"])   # farklı kâr oranı
    # Elle fiyat: zarar ettiren fiyat hataya düşer
    d_id = ty["canta_bayim-CB-100"]["id"]
    bad = client.patch(f"/api/listing-drafts/{d_id}", json={"price": "100"}, headers=H).json()
    assert bad["status"] == "invalid" and any("zarar" in e.lower() for e in bad["errors"])
    ok = client.patch(f"/api/listing-drafts/{d_id}", json={"auto_price": True}, headers=H).json()
    assert ok["status"] == "draft"
    # Yayına hazırla: yalnızca hatasızlar 'ready'
    prep = client.post("/api/listing-drafts/prepare", json={"ids": r["draft_ids"]}, headers=H).json()
    assert prep["ready"] >= 2 and prep["invalid"] >= 1
    csv_text = client.get("/api/listing-drafts/export.csv", params={"marketplace": "trendyol"}).text
    assert "8690000000017" in csv_text and "CB-300" not in csv_text
    lst = client.get("/api/listing-drafts", params={"marketplace": "trendyol"}).json()
    assert lst["publishing_enabled"] is False and lst["write_enabled"] is False


def test_existing_marketplace_listing_blocks_duplicate_draft(client, engine):
    a = make_supplier(client, "A", MAP_A)
    upload(client, a, XML_A)
    pids = _catalog(client, a)
    with engine.begin() as c:
        from app.services.orders_sync import ensure_store
        store = ensure_store(c, "trendyol", "1", "Trendyol")
        c.execute(text("""INSERT INTO marketplace_listings(store_id, external_product_id, barcode, title)
                          VALUES (:s, 'TY-1', '8690000000017', 'Mevcut ilan')"""), {"s": store})
    client.post("/api/transfer/drafts", json={"product_ids": pids, "marketplaces": ["trendyol"]}, headers=H)
    d = next(x for x in client.get("/api/listing-drafts").json()["items"] if x["barcode"] == "8690000000017")
    assert d["existing_listing_id"] and any("zaten ilanda" in e for e in d["errors"])


def test_unknown_marketplace_rejected_and_new_marketplace_supported(client, engine):
    a = make_supplier(client, "A", MAP_A)
    upload(client, a, XML_A)
    pids = _catalog(client, a)
    assert client.post("/api/transfer/drafts", json={"product_ids": pids, "marketplaces": ["n11"]},
                       headers=H).status_code == 422
    # Yeni pazaryeri eklemek yalnızca bir satırdır; kural yoksa varsayılanlar kullanılır
    with engine.begin() as c:
        c.execute(text("INSERT INTO marketplaces(code, name, enabled) VALUES ('n11', 'N11', FALSE)"))
    try:
        r = client.post("/api/transfer/drafts", json={"product_ids": pids, "marketplaces": ["n11"]}, headers=H)
        assert r.status_code == 200 and len(r.json()["draft_ids"]) == 3
    finally:
        with engine.begin() as c:
            c.execute(text("UPDATE listing_drafts SET status = 'cancelled'"))
            # test DB'si: sonraki testler 3 pazaryeri bekliyor
            c.execute(text("TRUNCATE listing_drafts"))
            c.execute(text("DELETE FROM marketplaces WHERE code = 'n11'"))


def test_dashboard_shows_supplier_health(client):
    a = make_supplier(client, "Çanta Bayim", MAP_A)
    upload(client, a, XML_A)
    s = client.get("/api/dashboard").json()["suppliers"][0]
    assert (s["name"], s["product_count"], s["in_stock_count"], s["out_of_stock_count"]) == ("Çanta Bayim", 3, 2, 1)
    assert s["last_sync_at"] and s["health"] == "ok"
