"""Trendçantanız web mağazası: katalog, kanallar arası stok, sepet, sipariş akışı, panel API'si, SEO ve güvenlik."""
import hashlib
import io
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text

H = {"X-Requested-With": "Storefront"}
PANEL = {"X-Requested-With": "TrendHub"}


# ------------------------------------------------------------------ yardımcılar
def add_product(c, *, sku, name="Kapitone Omuz Çantası", price="1299.90", stock=5, images=2, model=None, color=None,
                category="Kadın > Çanta > Omuz Çantası", active=True, cost="400", stock_updated_hours_ago=3):
    imgs = [f"https://cdn.example.com/{sku}-{i}.jpg" for i in range(images)]
    pid = c.execute(text("""
        INSERT INTO products(sku, barcode, name, category, model_code, images, cost, sale_price, stock, vat_rate, is_active,
                             stock_updated_at, created_at, updated_at)
        VALUES (:sku, :bc, :name, :cat, :model, CAST(:imgs AS JSONB), :cost, :price, :stock, 20, :active,
                NOW() - make_interval(hours => :h), NOW(), NOW()) RETURNING id"""),
        # Barkod DETERMİNİSTİK olmalı: hash() süreç başına rastgeledir (PYTHONHASHSEED); rastgele barkod sayfadaki gtin'de
        # görünüp sızıntı testinde ("987") yanlış alarm veriyordu.
        {"sku": sku, "bc": "869" + str(int(hashlib.sha256(sku.encode()).hexdigest(), 16))[:10], "name": name, "cat": category, "model": model,
         "imgs": json.dumps(imgs), "cost": cost, "price": price, "stock": stock, "active": active,
         "h": stock_updated_hours_ago}).scalar()
    if color:
        sid = c.execute(text("""INSERT INTO suppliers(code, name) VALUES ('canta_bayim', 'Çanta Bayim')
                                ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name RETURNING id""")).scalar()
        c.execute(text("""INSERT INTO supplier_products(supplier_id, product_id, supplier_sku, color, parent_code, stock, status)
                          VALUES (:s, :p, :sku, :color, :parent, :stock, 'active')"""),
                  {"s": sid, "p": pid, "sku": f"S-{sku}", "color": color, "parent": model, "stock": stock})
    return pid


def marketplace_order(c, pid, qty=1, *, hours_ago=1, status="new", code="trendyol", ext=None):
    mp = c.execute(text("SELECT id FROM marketplaces WHERE code = :c"), {"c": code}).scalar()
    store = c.execute(text("SELECT id FROM stores WHERE marketplace_id = :m LIMIT 1"), {"m": mp}).scalar() or \
        c.execute(text("INSERT INTO stores(marketplace_id, name, external_id) VALUES (:m, 'Mağaza', 'x') RETURNING id"), {"m": mp}).scalar()
    ext = ext or f"O{pid}-{hours_ago}-{qty}-{status}"
    oid = c.execute(text("""INSERT INTO orders(store_id, external_order_id, status, internal_status, gross_revenue, order_date)
                            VALUES (:s, :e, 'Created', :st, 100, NOW() - make_interval(hours => :h)) RETURNING id"""),
                    {"s": store, "e": ext, "st": status, "h": hours_ago}).scalar()
    c.execute(text("""INSERT INTO order_items(order_id, product_id, external_line_id, sku, product_name, quantity, unit_price)
                      VALUES (:o, :p, :l, 'x', 'x', :q, 100)"""), {"o": oid, "p": pid, "l": f"L{oid}", "q": qty})
    return oid


def configure_store(c, **extra):
    values = {"bank_transfer_enabled": True, "bank_transfer_iban": "TR330006100519786457841326",
              "bank_transfer_account_name": "Trendçantanız Ltd.", "cash_on_delivery_enabled": True,
              "seller": {"title": "Trendçantanız Ltd.", "address": "İstanbul", "phone": "0850 000 00 00", "email": "destek@example.com"},
              "legal": {s: "metin" for s in ("mesafeli-satis-sozlesmesi", "on-bilgilendirme-formu", "kvkk-aydinlatma-metni", "iade-ve-degisim")}}
    values.update(extra)
    for k, v in values.items():
        c.execute(text("INSERT INTO app_settings(key, value) VALUES (:k, CAST(:v AS JSONB)) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"),
                  {"k": f"storefront.{k}", "v": json.dumps(v)})


CHECKOUT = {"full_name": "Ayşe Yılmaz", "email": "Ayse@Example.com", "phone": "0532 111 22 33", "city": "İstanbul",
            "district": "Kadıköy", "address": "Caferağa Mah. Moda Cad. No:10 D:3", "payment_method": "bank_transfer",
            "accept_terms": True, "accept_kvkk": True}


@pytest.fixture
def conn(engine):
    """Bu modülde veriler hemen görünür olmalı (TestClient ayrı bağlantı kullanır): autocommit bağlantı."""
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        yield c


@pytest.fixture
def store(engine):
    from fastapi.testclient import TestClient

    from app.storefront import catalog
    from app.storefront.main import app
    catalog.invalidate()
    with TestClient(app) as c:
        yield c
    catalog.invalidate()


def fresh():
    from app.storefront import catalog
    catalog.invalidate()


def available(conn, pid):
    from app.services.stock_availability import available_map
    return available_map(conn, [pid])[pid]


# ------------------------------------------------------------------ katalog
def test_visibility_rules(conn):
    from app.storefront import catalog
    visible = add_product(conn, sku="A1")
    no_img = add_product(conn, sku="A2", images=0)
    no_price = add_product(conn, sku="A3", price="0")
    inactive = add_product(conn, sku="A4", active=False)
    hidden = add_product(conn, sku="A5")
    conn.execute(text("INSERT INTO storefront_products(product_id, published) VALUES (:p, FALSE)"), {"p": hidden})
    ids = {v.id for g in catalog.snapshot(conn, fresh=True).groups for v in g.variants}
    assert visible in ids and not ids & {no_img, no_price, inactive, hidden}
    # Otomatik yayın kapalıyken yalnızca açıkça yayınlananlar
    conn.execute(text("INSERT INTO app_settings(key, value) VALUES ('storefront.auto_publish', 'false') ON CONFLICT (key) DO UPDATE SET value = 'false'"))
    conn.execute(text("INSERT INTO storefront_products(product_id, published) VALUES (:p, TRUE)"), {"p": no_img})
    assert {v.id for g in catalog.snapshot(conn, fresh=True).groups for v in g.variants} == set()


def test_variants_grouped_by_model_and_supplier_color(conn):
    from app.storefront import catalog
    a = add_product(conn, sku="M1-S", model="M1", color="Siyah")
    b = add_product(conn, sku="M1-T", model="M1", color="Taba", stock=0)
    add_product(conn, sku="X", name="Başka Çanta")
    snap = catalog.snapshot(conn, fresh=True)
    g = next(g for g in snap.groups if g.key == "M1")
    assert {v.id for v in g.variants} == {a, b}
    assert [c["name"] for c in g.colors] == ["Siyah", "Taba"] and g.colors[1]["in_stock"] is False
    assert g.rep.id == a  # stokta olan varyant temsilci
    assert len(snap.groups) == 2


def test_hero_prefers_real_bestseller_then_images_then_override(conn):
    from app.storefront import catalog
    few = add_product(conn, sku="H1", images=1, price="500")
    many = add_product(conn, sku="H2", images=6, price="400")
    g, reason = catalog.hero(conn, catalog.snapshot(conn, fresh=True))
    assert g.id == many and "Satış verisi yok" in reason
    assert catalog.bestsellers(catalog.snapshot(conn, fresh=True)) == []  # sahte "çok satan" yok
    marketplace_order(conn, few, 3, hours_ago=24 * 10)
    g, reason = catalog.hero(conn, catalog.snapshot(conn, fresh=True))
    assert g.id == few and "en çok satan" in reason
    conn.execute(text("INSERT INTO app_settings(key, value) VALUES ('storefront.hero_product_id', :v) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"),
                 {"v": str(many)})
    assert catalog.hero(conn, catalog.snapshot(conn, fresh=True))[0].id == many


# ------------------------------------------------------------------ kanallar arası stok
def test_available_stock_subtracts_all_channel_orders_since_last_stock_update(conn):
    pid = add_product(conn, sku="S1", stock=5, stock_updated_hours_ago=3)
    assert available(conn, pid) == 5
    marketplace_order(conn, pid, 2, hours_ago=1)                          # stok güncellemesinden SONRA Trendyol satışı
    marketplace_order(conn, pid, 1, hours_ago=5, ext="old")               # önceki satış zaten stoğa yansımış
    marketplace_order(conn, pid, 1, hours_ago=1, status="cancelled", ext="c")
    assert available(conn, pid) == 3
    conn.execute(text("INSERT INTO stock_reservations(product_id, quantity, expires_at) VALUES (:p, 1, NOW() + INTERVAL '1 hour')"), {"p": pid})
    conn.execute(text("INSERT INTO stock_reservations(product_id, quantity, expires_at) VALUES (:p, 5, NOW() - INTERVAL '1 minute')"), {"p": pid})
    assert available(conn, pid) == 2
    conn.execute(text("INSERT INTO app_settings(key, value) VALUES ('storefront.stock_buffer', '1') ON CONFLICT (key) DO UPDATE SET value = '1'"))
    assert available(conn, pid) == 1
    # Tedarikçi stoğu güncellenince (stock_updated_at yeni) eski siparişler artık düşülmez
    conn.execute(text("UPDATE products SET stock = 3, stock_updated_at = NOW() WHERE id = :p"), {"p": pid})
    assert available(conn, pid) == 1  # 3 - rezervasyon 1 - pay 1


def test_committed_window_limits_how_long_orders_are_subtracted(conn):
    pid = add_product(conn, sku="S2", stock=4, stock_updated_hours_ago=24 * 10)
    marketplace_order(conn, pid, 2, hours_ago=24 * 5)
    assert available(conn, pid) == 4  # pencere (48 saat) dışında
    marketplace_order(conn, pid, 1, hours_ago=2, ext="n")
    assert available(conn, pid) == 3


def test_panel_product_list_shows_channel_available_stock(client_factory, conn):
    pid = add_product(conn, sku="S3", stock=5)
    marketplace_order(conn, pid, 2)
    c, login = client_factory
    login("admin", "Admin-Password-123")
    item = next(i for i in c.get("/api/products").json()["items"] if i["id"] == pid)
    assert item["stock"] == 5 and item["available_stock"] == 3


# ------------------------------------------------------------------ sepet
def test_cart_caps_to_available_and_requires_custom_header(store, conn):
    pid = add_product(conn, sku="C1", stock=2)
    marketplace_order(conn, pid, 1)
    fresh()
    assert store.post("/api/store/cart/add", json={"product_id": pid, "quantity": 1}).status_code == 403
    r = store.post("/api/store/cart/add", json={"product_id": pid, "quantity": 5}, headers=H)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["count"] == 1 and "notice" in d and d["lines"][0]["price_fmt"] == "1.299,90 ₺"
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    assert store.post("/api/store/cart/add", json={"product_id": 999999}, headers=H).status_code == 422
    assert store.post("/api/store/cart/add", json={"product_id": pid}, headers={**H, "Origin": "https://evil.example"}).status_code == 403
    # Vekil (nginx) Host başlığından portu düşürse de aynı site kabul edilir
    assert store.post("/api/store/cart/add", json={"product_id": pid}, headers={**H, "Origin": "http://testserver:8090"}).status_code == 200


def test_cart_json_never_leaks_cost_or_supplier(store, conn):
    pid = add_product(conn, sku="C2", cost="987.65")
    fresh()
    store.post("/api/store/cart/add", json={"product_id": pid}, headers=H)
    body = store.get("/api/store/cart").text + store.get(f"/urun/x-p{pid}", follow_redirects=True).text
    assert not any(v in body for v in ("987.65", "987,65", "987.6", "987,6", "98765")), "maliyet sızdı"
    assert "canta_bayim" not in body.lower() and "supplier" not in body.lower()


# ------------------------------------------------------------------ sipariş akışı
def _checkout(store, pid, qty=1, **overrides):
    store.post("/api/store/cart/add", json={"product_id": pid, "quantity": qty}, headers=H)
    return store.post("/api/store/checkout", json={**CHECKOUT, **overrides}, headers=H)


def test_checkout_refused_until_store_is_configured(store, conn):
    pid = add_product(conn, sku="K0")
    fresh()
    r = _checkout(store, pid)
    assert r.status_code == 422 and "sipariş kabul etmiyor" in r.json()["detail"]
    assert conn.execute(text("SELECT COUNT(*) FROM storefront_orders")).scalar() == 0


def test_checkout_validation_messages(store, conn):
    configure_store(conn)
    pid = add_product(conn, sku="K1")
    fresh()
    r = _checkout(store, pid, phone="123", city="Paris", full_name="Ayşe", accept_terms=False)
    assert r.status_code == 422
    f = r.json()["fields"]
    assert {"phone", "city", "full_name"} <= set(f)
    r = _checkout(store, pid, accept_terms=False)
    assert r.status_code == 422 and "accept_terms" in r.json()["fields"]


def test_bank_transfer_reserves_stock_and_creates_trendhub_order_only_after_payment(store, client_factory, conn):
    configure_store(conn)
    pid = add_product(conn, sku="K2", stock=3, price="1000")
    fresh()
    r = _checkout(store, pid, qty=2)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "awaiting_payment" and re.match(r"^/siparis/TC\d{6}[A-Z0-9]{5}\?t=", d["redirect"])
    sfo = conn.execute(text("SELECT * FROM storefront_orders")).mappings().one()
    assert sfo["order_id"] is None and sfo["email"] == "ayse@example.com" and sfo["phone"] == "0532 111 22 33"
    assert Decimal(sfo["items_total"]) == Decimal("2000") and sfo["consents"]["terms"]
    assert conn.execute(text("SELECT COUNT(*) FROM orders")).scalar() == 0  # ödenmemiş sipariş ciroya girmez
    assert available(conn, pid) == 1  # ayrılan stok diğer kanallara karşı korunur
    # Sipariş sayfası yalnızca erişim anahtarıyla açılır
    page = store.get(d["redirect"])
    assert page.status_code == 200 and "TR330006100519786457841326" in page.text
    assert store.get(d["redirect"].split("?")[0]).status_code == 404
    assert store.get(d["redirect"].split("?")[0] + "?t=yanlis").status_code == 404
    # Sepet boşaldı
    assert store.get("/api/store/cart").json()["count"] == 0

    c, login = client_factory
    login("admin", "Admin-Password-123")
    pending = c.get("/api/storefront/orders", params={"status": "pending"}).json()["items"]
    assert [p["public_code"] for p in pending] == [sfo["public_code"]] and pending[0]["item_count"] == 2
    r = c.post(f"/api/storefront/orders/{sfo['id']}/confirm-payment", json={"reference": "EFT-1"}, headers=PANEL)
    assert r.status_code == 200, r.text
    order_id = r.json()["order_id"]
    o = conn.execute(text("""SELECT o.*, m.code FROM orders o JOIN stores s ON s.id = o.store_id
                             JOIN marketplaces m ON m.id = s.marketplace_id WHERE o.id = :i"""), {"i": order_id}).mappings().one()
    assert o["code"] == "storefront" and o["source"] == "storefront" and o["internal_status"] == "new"
    assert o["external_order_id"] == sfo["public_code"] and Decimal(o["gross_revenue"]) == Decimal("2000")
    items = conn.execute(text("SELECT product_id, quantity, unit_price, unit_cost FROM order_items WHERE order_id = :o"), {"o": order_id}).all()
    assert [(i.product_id, i.quantity) for i in items] == [(pid, 2)] and Decimal(items[0].unit_cost) == Decimal("400")
    # Web kanalında pazaryeri hizmet bedeli yok (tahmini değil, gerçek 0)
    assert conn.execute(text("SELECT amount FROM financial_transactions WHERE order_id = :o AND kind = 'service_fee'"), {"o": order_id}).scalar() == 0
    # Rezervasyon bırakıldı, stok artık sipariş olarak düşülüyor (çift sayım yok)
    assert conn.execute(text("SELECT COUNT(*) FROM stock_reservations WHERE released_at IS NULL")).scalar() == 0
    assert available(conn, pid) == 1
    # Panel sipariş detayı web bilgisini gösterir; siparişler kanal filtresiyle ayrılır
    detail = c.get(f"/api/orders/{order_id}").json()
    assert detail["marketplace"] == "storefront" and detail["storefront"]["address"].startswith("Caferağa")
    listed = c.get("/api/orders", params={"marketplace": "storefront"}).json()
    assert listed["total"] == 1 and listed["items"][0]["marketplace_name"] == "Trendçantanız Web"
    assert c.get("/api/orders", params={"marketplace": "trendyol"}).json()["total"] == 0
    # İkinci onay reddedilir (idempotent)
    assert c.post(f"/api/storefront/orders/{sfo['id']}/confirm-payment", json={}, headers=PANEL).status_code == 409


def test_cash_on_delivery_creates_order_immediately_with_fee(store, conn):
    configure_store(conn, cash_on_delivery_fee="39.90", shipping_fee="49.90", free_shipping_threshold="2000")
    pid = add_product(conn, sku="K3", price="1000")
    fresh()
    r = _checkout(store, pid, payment_method="cash_on_delivery")
    assert r.status_code == 200, r.text
    sfo = conn.execute(text("SELECT * FROM storefront_orders")).mappings().one()
    assert sfo["status"] == "cash_on_delivery" and Decimal(sfo["shipping_fee"]) == Decimal("89.80")
    assert Decimal(sfo["total"]) == Decimal("1089.80") and sfo["order_id"]
    assert conn.execute(text("SELECT status FROM orders WHERE id = :i"), {"i": sfo["order_id"]}).scalar() == "CashOnDelivery"


def test_no_overselling_across_web_and_marketplace(store, conn):
    configure_store(conn)
    pid = add_product(conn, sku="K4", stock=2)
    fresh()
    from fastapi.testclient import TestClient

    from app.storefront.main import app
    with TestClient(app) as other:
        other.post("/api/store/cart/add", json={"product_id": pid, "quantity": 1}, headers=H)
        store.post("/api/store/cart/add", json={"product_id": pid, "quantity": 1}, headers=H)
        marketplace_order(conn, pid, 1)  # bu arada Trendyol'da da satıldı
        assert store.post("/api/store/checkout", json=CHECKOUT, headers=H).status_code == 200
        r = other.post("/api/store/checkout", json=CHECKOUT, headers=H)
        assert r.status_code == 422 and "tükendi" in r.json()["detail"]
    assert available(conn, pid) == 0


def test_expired_transfer_releases_stock(store, conn):
    configure_store(conn)
    pid = add_product(conn, sku="K5", stock=1)
    fresh()
    assert _checkout(store, pid).status_code == 200
    assert available(conn, pid) == 0
    conn.execute(text("UPDATE stock_reservations SET expires_at = NOW() - INTERVAL '1 minute'"))
    from app.storefront.checkout import expire_stale
    assert expire_stale(conn)["expired_orders"] == 1
    assert conn.execute(text("SELECT status FROM storefront_orders")).scalar() == "expired"
    assert available(conn, pid) == 1


def test_storefront_maintenance_job_runs_in_worker(engine, conn):
    from app.services import sync_service
    job_id = sync_service.schedule_storefront_maintenance(conn)
    assert job_id and sync_service.schedule_storefront_maintenance(conn) is None
    res = sync_service.execute(engine, {"job_type": sync_service.STOREFRONT_MAINTENANCE, "payload": {}})
    assert res["reservations"] == {"expired_orders": 0, "deleted_carts": 0}
    # Sağlayıcı yok / ayar kapalı: bildirim, e-fatura ve tedarikçi adımları hiçbir şey yapmaz
    assert res["shipped_notifications"] == 0 and res["einvoice"] == {"issued": 0, "failed": 0}
    assert res["supplier_forwarding"] == {"mode": "manual", "prepared": 0, "sent": 0} and res["notify_retry"] is None


# ------------------------------------------------------------------ panel API
def test_panel_settings_products_and_rbac(client_factory, conn):
    c, login = client_factory
    login("admin", "Admin-Password-123")
    pid = add_product(conn, sku="P1", price="1000")
    ov = c.get("/api/storefront/overview").json()
    assert ov["checkout_ready"] is False and any("ödeme yöntemi" in b for b in ov["blockers"])
    assert ov["hero"]["product_id"] == pid and ov["card_provider_configured"] is False
    assert c.put("/api/storefront/settings", json={"values": {"bank_transfer_iban": "TR12"}}, headers=PANEL).status_code == 422
    assert c.put("/api/storefront/settings", json={"values": {"nope": 1}}, headers=PANEL).status_code == 422
    r = c.put("/api/storefront/settings", json={"values": {"bank_transfer_enabled": True, "bank_transfer_iban": "TR33 0006 1005 1978 6457 8413 26",
                                                             "bank_transfer_account_name": "Trendçantanız Ltd."}}, headers=PANEL)
    assert r.status_code == 200 and set(r.json()["changed"]) == {"bank_transfer_enabled", "bank_transfer_iban", "bank_transfer_account_name"}
    assert c.get("/api/storefront/settings").json()["values"]["bank_transfer_iban"] == "TR330006100519786457841326"
    # Gerçek olmayan indirim engellenir
    assert c.patch(f"/api/storefront/products/{pid}", json={"compare_at_price": "900"}, headers=PANEL).status_code == 422
    assert c.patch(f"/api/storefront/products/{pid}", json={"compare_at_price": "1200", "featured": True, "title": "Kapitone Çanta"}, headers=PANEL).status_code == 200
    item = c.get("/api/storefront/products").json()["items"][0]
    assert item["featured"] and item["visible"] and item["url"].startswith("/urun/kapitone-canta-p")
    assert c.patch(f"/api/storefront/products/{pid}", json={"published": False}, headers=PANEL).status_code == 200
    assert c.get("/api/storefront/products", params={"status": "hidden"}).json()["items"][0]["hidden_reasons"] == ["Web'de gizlendi"]
    audit = [r[0] for r in conn.execute(text("SELECT action FROM audit_logs ORDER BY id"))]
    assert "storefront.settings_updated" in audit and "storefront.product_updated" in audit

    conn.execute(text("""INSERT INTO users(username, password_hash, role) VALUES ('izleyici', :h, 'viewer')"""),
                 {"h": __import__("app.security", fromlist=["hash_password"]).hash_password("Viewer-Password-123")})
    c.post("/api/auth/logout", headers=PANEL)
    login("izleyici", "Viewer-Password-123")
    assert c.get("/api/storefront/overview").status_code == 200
    assert c.get("/api/storefront/settings").status_code == 403
    assert c.get("/api/storefront/orders").status_code == 403
    assert c.patch(f"/api/storefront/products/{pid}", json={"featured": False}, headers=PANEL).status_code == 403


# ------------------------------------------------------------------ sayfalar / SEO
def test_pages_seo_and_structured_data(store, conn):
    configure_store(conn)
    pid = add_product(conn, sku="SEO1", name="Kroko Desenli Omuz Çantası", price="1499.90")
    fresh()
    home = store.get("/")
    assert home.status_code == 200 and '<html lang="tr">' in home.text and "Kroko Desenli Omuz Çantası" in home.text
    assert 'fetchpriority="high"' in home.text
    r = store.get(f"/urun/yanlis-slug-p{pid}", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == f"/urun/kroko-desenli-omuz-cantasi-p{pid}"
    page = store.get(r.headers["location"])
    assert page.status_code == 200
    assert f'<link rel="canonical" href="http://testserver/urun/kroko-desenli-omuz-cantasi-p{pid}">' in page.text
    ld = [json.loads(m) for m in re.findall(r'<script type="application/ld\+json">(.*?)</script>', page.text, re.S)]
    product = next(x for x in ld if x["@type"] == "Product")
    assert product["offers"][0]["priceCurrency"] == "TRY" and product["offers"][0]["price"] == "1499.90"
    assert product["offers"][0]["availability"].endswith("InStock")
    assert any(x["@type"] == "BreadcrumbList" for x in ld) and any(x["@type"] == "Organization" for x in ld)
    assert 'property="og:locale" content="tr_TR"' in page.text and "1.499,90 ₺" in page.text
    assert "Content-Security-Policy" in page.headers and page.headers["X-Frame-Options"] == "DENY"
    sm = store.get("/sitemap.xml")
    assert sm.status_code == 200 and f"/urun/kroko-desenli-omuz-cantasi-p{pid}" in sm.text and "/kategori/omuz-cantasi" in sm.text
    assert "/sayfa/mesafeli-satis-sozlesmesi" in sm.text
    robots = store.get("/robots.txt").text
    assert "Disallow: /odeme" in robots and "Sitemap: http://testserver/sitemap.xml" in robots
    assert store.get("/kategori/omuz-cantasi").status_code == 200
    assert "Kroko" in store.get("/ara", params={"q": "kroko"}).text
    assert store.get("/api/store/search", params={"q": "omuz"}).json()["products"][0]["id"] == pid
    assert store.get("/kategori/yok").status_code == 404 and store.get("/sayfa/gizlilik-politikasi").status_code == 404
    assert store.get("/sayfa/mesafeli-satis-sozlesmesi").status_code == 200
    # Yönetim API'si mağaza sürecinde yoktur
    assert store.get("/api/orders").status_code == 404 and store.get("/api/settings").status_code == 404


def test_description_html_is_rendered_as_text(store, conn):
    pid = add_product(conn, sku="XSS")
    conn.execute(text("UPDATE products SET description = :d WHERE id = :p"),
                 {"d": '<p>Güzel çanta</p><script>alert(1)</script><img src=x onerror=alert(2)>', "p": pid})
    fresh()
    page = store.get(f"/urun/x-p{pid}", follow_redirects=True).text
    assert "Güzel çanta" in page and "<script>alert" not in page and "onerror" not in page


def test_image_proxy_is_signed_resizes_and_caches(store, conn, tmp_path, monkeypatch):
    from PIL import Image

    from app.config import get_settings
    from app.storefront import images
    monkeypatch.setattr(get_settings(), "storefront_image_cache_dir", str(tmp_path))
    buf = io.BytesIO()
    Image.new("RGB", (2000, 2500), (255, 255, 255)).save(buf, "JPEG")
    calls = []

    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(200, content=buf.getvalue(), headers={"content-type": "image/jpeg"})

    monkeypatch.setattr(images, "_transport", httpx.MockTransport(handler))
    url = images.img_url("https://cdn.example.com/a.jpg", 600)
    assert url.startswith("/img/640/")
    r = store.get(url)
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    assert "immutable" in r.headers["cache-control"]
    with Image.open(io.BytesIO(r.content)) as im:
        assert im.size == (640, 800)
    assert store.get(url).status_code == 200 and len(calls) == 1  # diskten
    tampered = url.replace("/img/640/", "/img/640/0")
    assert store.get(tampered).status_code == 404
    assert store.get(url.replace("/img/640/", "/img/999/")).status_code == 404


def test_money_format_turkish():
    from app.storefront.store_config import fmt_try
    assert fmt_try(Decimal("1299.9")) == "1.299,90 ₺" and fmt_try(0) == "0,00 ₺" and fmt_try("123456.789") == "123.456,79 ₺"


def test_slugify_turkish():
    from app.storefront.catalog import category_label, slugify
    assert slugify("Çapraz Çanta Şık & Özel İğne") == "capraz-canta-sik-ozel-igne"
    assert category_label("Kadın > Çanta > Omuz Çantası") == "Omuz Çantası"
