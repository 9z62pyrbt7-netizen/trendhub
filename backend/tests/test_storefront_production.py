"""Web mağazası üretim özellikleri: toplu yayın (gerçek katalog biçimi), hero, kart ödeme (PayTR/iyzico, sahte HTTP),
bildirimler, tedarikçiye aktarım, e-fatura katmanı, müşteri hesabı. Dış servislere hiçbir gerçek istek yapılmaz."""
import base64
import hashlib
import hmac
import json
import re
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text

from tests.test_storefront import CHECKOUT, H, PANEL, add_product, configure_store, fresh, marketplace_order

pytestmark = pytest.mark.usefixtures("engine")


@pytest.fixture
def conn(engine):
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        yield c


@pytest.fixture
def store(engine):
    from fastapi.testclient import TestClient

    from app.storefront import catalog
    from app.storefront.main import _hits, app
    catalog.invalidate()
    _hits.clear()
    with TestClient(app) as c:
        yield c
    catalog.invalidate()


@pytest.fixture
def settings(monkeypatch):
    from app.config import get_settings
    s = get_settings()

    def set_(**kw):
        for k, v in kw.items():
            monkeypatch.setattr(s, k, v)
    return set_


def supplier_catalog(c):
    """Çanta Bayim XML içe aktarımındaki biçim: ürünün kendi görseli yok, görseller/renk/ana ürün kodu tedarikçide."""
    sid = c.execute(text("INSERT INTO suppliers(code, name) VALUES ('canta_bayim', 'Çanta Bayim') RETURNING id")).scalar()
    ids = {}
    for sku, color, stock, imgs in (("CB-1042-SYH", "Siyah", 4, [{"url": "https://cdn.cantabayim.example/1042-syh-1.jpg"},
                                                                    {"url": "https://cdn.cantabayim.example/1042-syh-2.jpg"}]),
                                    ("CB-1042-TB", "Taba", 0, ["https://cdn.cantabayim.example/1042-tb-1.jpg"]),
                                    ("CB-2001-KRM", "Krem", 2, [])):
        pid = c.execute(text("""
            INSERT INTO products(sku, barcode, name, category, images, sale_price, stock, vat_rate, is_active, preferred_supplier_id,
                                 stock_updated_at, created_at, updated_at)
            VALUES (:sku, :bc, 'Kapitone Zincir Askılı Omuz Çantası', 'Kadın > Çanta > Omuz Çantası', '[]'::jsonb, 899.90, :st, 20,
                    TRUE, :s, NOW() - INTERVAL '2 hours', NOW(), NOW()) RETURNING id"""),
            {"sku": sku, "bc": "8690" + sku[-7:].replace("-", "0"), "st": stock, "s": sid}).scalar()
        c.execute(text("""INSERT INTO supplier_products(supplier_id, product_id, supplier_sku, color, parent_code, stock, status, images, cost)
                          VALUES (:s, :p, :ssku, :color, :parent, :st, 'active', CAST(:imgs AS JSONB), 350)"""),
                  {"s": sid, "p": pid, "ssku": sku.replace("CB-", ""), "color": color, "parent": sku[:7], "st": stock,
                   "imgs": json.dumps(imgs)})
        ids[sku] = pid
    return sid, ids


def admin(client_factory):
    c, login = client_factory
    login("admin", "Admin-Password-123")
    return c


# ------------------------------------------------------------------ ürün yayını
def test_supplier_catalog_publishes_with_images_colors_and_variants(conn, store):
    from app.storefront import catalog
    _, ids = supplier_catalog(conn)
    snap = catalog.snapshot(conn, fresh=True)
    g = next(g for g in snap.groups if g.key == "CB-1042")
    assert {v.id for v in g.variants} == {ids["CB-1042-SYH"], ids["CB-1042-TB"]}
    assert [c["name"] for c in g.colors] == ["Siyah", "Taba"] and g.colors[1]["in_stock"] is False
    syh = next(v for v in g.variants if v.color == "Siyah")
    assert syh.images == ["https://cdn.cantabayim.example/1042-syh-1.jpg", "https://cdn.cantabayim.example/1042-syh-2.jpg"]
    # Görseli hiç olmayan ürün yayına uygun değil
    assert ids["CB-2001-KRM"] not in {v.id for g in snap.groups for v in g.variants}
    page = store.get(f"/urun/x-p{ids['CB-1042-SYH']}", follow_redirects=True)
    assert page.status_code == 200 and "Siyah" in page.text and "Taba" in page.text and "899,90 ₺" in page.text


def test_bulk_publish_and_overrides(client_factory, conn, store):
    c = admin(client_factory)
    _, ids = supplier_catalog(conn)
    other = add_product(conn, sku="TEK-1", name="Hasır Plaj Çantası")
    conn.execute(text("INSERT INTO app_settings(key, value) VALUES ('storefront.auto_publish', 'false') "
                      "ON CONFLICT (key) DO UPDATE SET value = 'false'"))
    lst = c.get("/api/storefront/products", params={"status": "eligible_unpublished"}).json()
    assert {i["id"] for i in lst["items"]} == {ids["CB-1042-SYH"], ids["CB-1042-TB"], other}
    item = next(i for i in lst["items"] if i["id"] == ids["CB-1042-SYH"])
    assert item["effective_color"] == "Siyah" and item["variant_count"] == 2 and item["image_count"] == 2
    assert item["thumbnail"].endswith("1042-syh-1.jpg") and item["group_key"] == "CB-1042"
    assert lst["counts"]["not_eligible"] == 1
    krem = c.get("/api/storefront/products", params={"q": "CB-2001"}).json()["items"][0]
    assert krem["hidden_reasons"][:1] == ["Görsel yok"]

    # Seçili ürünler: yayına uygun olmayan atlanır
    r = c.post("/api/storefront/products/bulk", json={"action": "publish", "product_ids": [ids["CB-1042-SYH"], ids["CB-2001-KRM"], 999999]},
               headers=PANEL).json()
    assert r["changed"] == 1 and r["skipped_not_eligible"] == [ids["CB-2001-KRM"]] and r["not_found"] == [999999]
    # Filtreyle: kalan tüm yayına uygun ürünler
    r = c.post("/api/storefront/products/bulk", json={"action": "publish", "status": "eligible_unpublished"}, headers=PANEL).json()
    assert r["changed"] == 2
    assert c.get("/api/storefront/products", params={"status": "visible"}).json()["total"] == 3
    assert c.post("/api/storefront/products/bulk", json={"action": "sil", "product_ids": [other]}, headers=PANEL).status_code == 422
    assert c.post("/api/storefront/products/bulk", json={"action": "publish"}, headers=PANEL).status_code == 422

    # Tek ürün: renk / varyant grubu / SEO düzeltmesi
    assert c.patch(f"/api/storefront/products/{other}", json={"color": "Hasır Bej", "group_code": "CB-1042",
                                                               "seo_title": "Hasır Plaj Çantası Bej", "seo_description": "Yazlık hasır çanta."},
                   headers=PANEL).status_code == 200
    assert c.patch(f"/api/storefront/products/{other}", json={"seo_title": "x" * 71}, headers=PANEL).status_code == 422
    fresh()
    page = store.get(f"/urun/x-p{other}", follow_redirects=True).text
    assert "<title>Hasır Plaj Çantası Bej | Trendçantanız</title>" in page
    assert '<meta name="description" content="Yazlık hasır çanta.">' in page and "Hasır Bej" in page
    audit = {r[0] for r in conn.execute(text("SELECT action FROM audit_logs"))}
    assert "storefront.products_bulk_publish" in audit

    r = c.post("/api/storefront/products/bulk", json={"action": "unpublish", "product_ids": [other]}, headers=PANEL).json()
    assert r["changed"] == 1
    fresh()
    assert store.get(f"/urun/x-p{other}").status_code == 404


def test_bulk_publish_rbac(client_factory, conn):
    from app.security import hash_password
    c, login = client_factory
    conn.execute(text("INSERT INTO users(username, password_hash, role) VALUES ('izleyici', :h, 'viewer')"),
                 {"h": hash_password("Viewer-Password-123")})
    login("izleyici", "Viewer-Password-123")
    assert c.post("/api/storefront/products/bulk", json={"action": "publish", "product_ids": [1]}, headers=PANEL).status_code == 403
    assert c.get("/api/storefront/integrations").status_code == 200
    assert c.get("/api/storefront/supplier-orders").status_code == 403


# ------------------------------------------------------------------ hero
def test_home_without_products_keeps_empty_design(store):
    r = store.get("/")
    assert r.status_code == 200 and "TRENDÇANTANIZ" in r.text and "data-hero" in r.text
    assert "Ürünü İncele" not in r.text and 'rel="preload" as="image"' not in r.text


def test_hero_uses_published_real_product(conn, store):
    _, ids = supplier_catalog(conn)
    marketplace_order(conn, ids["CB-1042-SYH"], 3, hours_ago=5)
    fresh()
    r = store.get("/")
    assert "Ürünü İncele" in r.text and f"-p{ids['CB-1042-SYH']}" in r.text and 'rel="preload" as="image"' in r.text


# ------------------------------------------------------------------ kart ödeme
def _card_checkout(store, pid):
    store.post("/api/store/cart/add", json={"product_id": pid, "quantity": 1}, headers=H)
    r = store.post("/api/store/checkout", json={**CHECKOUT, "payment_method": "card"}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def test_card_hidden_without_keys(conn, store, settings):
    configure_store(conn)
    settings(storefront_payment_provider="paytr", paytr_merchant_id="", paytr_merchant_key="", paytr_merchant_salt="")
    pid = add_product(conn, sku="NK1")
    fresh()
    store.post("/api/store/cart/add", json={"product_id": pid, "quantity": 1}, headers=H)
    assert "Kredi / Banka Kartı" not in store.get("/odeme").text
    r = store.post("/api/store/checkout", json={**CHECKOUT, "payment_method": "card"}, headers=H)
    assert r.status_code == 422
    assert store.post("/odeme/geri-donus/paytr", data={"merchant_oid": "x"}).status_code == 404


def _paytr_hash(key, salt, oid, status, total):
    return base64.b64encode(hmac.new(key.encode(), f"{oid}{salt}{status}{total}".encode(), hashlib.sha256).digest()).decode()


def test_paytr_flow_verifies_signature_and_amount(conn, store, settings, monkeypatch):
    from app.storefront import payments
    configure_store(conn)
    settings(storefront_payment_provider="paytr", paytr_merchant_id="123456", paytr_merchant_key="test-key",
             paytr_merchant_salt="test-salt", paytr_test_mode=True)
    pid = add_product(conn, sku="PT1", price="1299.90", stock=1)
    fresh()
    sent = {}

    def fake_post(url, **kw):
        sent.update(url=url, data=kw.get("data"))
        return httpx.Response(200, json={"status": "success", "token": "iframe-token-1"})

    monkeypatch.setattr(payments, "_post", fake_post)
    d = _card_checkout(store, pid)
    assert d["redirect"].startswith(f"/odeme/kart/{d['public_code']}?t=")
    # Ödeme alınmadan TrendHub siparişi yok; stok 30 dk ayrıldı
    assert conn.execute(text("SELECT COUNT(*) FROM orders")).scalar() == 0
    page = store.get(d["redirect"])
    assert page.status_code == 200 and "https://www.paytr.com/odeme/guvenli/iframe-token-1" in page.text
    assert "frame-src https://www.paytr.com" in page.headers["content-security-policy"]
    data = sent["data"]
    assert sent["url"] == "https://www.paytr.com/odeme/api/get-token" and data["payment_amount"] == "129990"
    assert data["merchant_oid"] == d["public_code"] and data["test_mode"] == "1" and "test-key" not in json.dumps(data)
    basket = json.loads(base64.b64decode(data["user_basket"]))
    assert basket[0][1] == "1299.90" and basket[0][2] == 1
    expected = base64.b64encode(hmac.new(b"test-key", (data["merchant_id"] + data["user_ip"] + data["merchant_oid"] + data["email"]
                                                      + data["payment_amount"] + data["user_basket"] + "1" + "0" + "TL" + "1"
                                                      + "test-salt").encode(), hashlib.sha256).digest()).decode()
    assert data["paytr_token"] == expected

    oid = d["public_code"]
    # Sahte imza → reddedilir
    bad = store.post("/odeme/geri-donus/paytr", data={"merchant_oid": oid, "status": "success", "total_amount": "129990",
                                                      "payment_amount": "129990", "hash": "sahte", "test_mode": "1"})
    assert bad.status_code == 400 and bad.text == "FAIL"
    # Doğru imza ama tutar farklı → reddedilir
    h = _paytr_hash("test-key", "test-salt", oid, "success", "100")
    assert store.post("/odeme/geri-donus/paytr", data={"merchant_oid": oid, "status": "success", "total_amount": "100",
                                                       "payment_amount": "100", "hash": h, "test_mode": "1"}).status_code == 400
    assert conn.execute(text("SELECT status FROM storefront_orders")).scalar() == "pending_payment"
    # Doğru imza ve tutar → sipariş oluşur, 'OK'
    h = _paytr_hash("test-key", "test-salt", oid, "success", "129990")
    ok = store.post("/odeme/geri-donus/paytr", data={"merchant_oid": oid, "status": "success", "total_amount": "129990",
                                                     "payment_amount": "129990", "hash": h, "test_mode": "1"})
    assert ok.status_code == 200 and ok.text == "OK"
    so = conn.execute(text("SELECT status, payment_provider, payment_reference, order_id FROM storefront_orders")).mappings().one()
    assert so["status"] == "paid" and so["payment_provider"] == "paytr" and so["order_id"]
    assert conn.execute(text("SELECT source FROM orders WHERE id = :o"), {"o": so["order_id"]}).scalar() == "storefront"
    # Tekrarlanan bildirim idempotent
    assert store.post("/odeme/geri-donus/paytr", data={"merchant_oid": oid, "status": "success", "total_amount": "129990",
                                                       "payment_amount": "129990", "hash": h, "test_mode": "1"}).text == "OK"
    assert conn.execute(text("SELECT COUNT(*) FROM orders")).scalar() == 1
    events = conn.execute(text("SELECT kind, verified FROM storefront_payment_events ORDER BY id")).all()
    assert [e.kind for e in events].count("callback") == 4 and events[-1].verified


def test_paytr_live_mode_rejects_test_payment(conn, store, settings, monkeypatch):
    from app.storefront import payments
    configure_store(conn)
    settings(storefront_payment_provider="paytr", paytr_merchant_id="1", paytr_merchant_key="k", paytr_merchant_salt="s",
             paytr_test_mode=False)
    monkeypatch.setattr(payments, "_post", lambda url, **kw: httpx.Response(200, json={"status": "success", "token": "t"}))
    pid = add_product(conn, sku="PT2", price="100")
    fresh()
    oid = _card_checkout(store, pid)["public_code"]
    h = _paytr_hash("k", "s", oid, "success", "10000")
    r = store.post("/odeme/geri-donus/paytr", data={"merchant_oid": oid, "status": "success", "total_amount": "10000",
                                                    "payment_amount": "10000", "hash": h, "test_mode": "1"})
    assert r.status_code == 400 and conn.execute(text("SELECT status FROM storefront_orders")).scalar() == "pending_payment"


def test_iyzico_flow(conn, store, settings, monkeypatch):
    from app.storefront import payments
    configure_store(conn)
    settings(storefront_payment_provider="iyzico", iyzico_api_key="api-key", iyzico_secret_key="secret-key",
             iyzico_base_url="https://sandbox-api.iyzipay.com")
    pid = add_product(conn, sku="IY1", price="750")
    fresh()
    calls = []

    def fake_post(url, **kw):
        body = json.loads(kw["content"])
        calls.append((url, body, kw["headers"]))
        if url.endswith("/initialize/auth/ecom"):
            return httpx.Response(200, json={"status": "success", "token": "iyz-token", "paymentPageUrl": "https://sandbox.iyzico.example/pay"})
        return httpx.Response(200, json={"status": "success", "paymentStatus": "SUCCESS", "fraudStatus": 1, "price": 750.0,
                                         "paidPrice": 750.0, "currency": "TRY", "basketId": body["conversationId"],
                                         "conversationId": body["conversationId"], "paymentId": "P-1"})

    monkeypatch.setattr(payments, "_post", fake_post)
    d = _card_checkout(store, pid)
    r = store.get(d["redirect"], follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "https://sandbox.iyzico.example/pay"
    url, body, headers = calls[0]
    assert body["price"] == "750.0" and body["paidPrice"] == "750.0" and body["basketId"] == d["public_code"]
    assert body["buyer"]["gsmNumber"] == "+905321112233" and sum(Decimal(i["price"]) for i in body["basketItems"]) == Decimal("750")
    assert headers["Authorization"].startswith("IYZWSv2 ") and "secret-key" not in headers["Authorization"]
    cb = re.search(r"callbackUrl", json.dumps(body)) and body["callbackUrl"]
    assert cb.startswith("http://testserver/odeme/geri-donus/iyzico?c=")
    back = store.post(cb.replace("http://testserver", ""), data={"token": "iyz-token"}, follow_redirects=False)
    assert back.status_code == 303 and back.headers["location"].startswith(f"/siparis/{d['public_code']}?t=")
    assert "basarisiz" not in back.headers["location"]
    assert conn.execute(text("SELECT status FROM storefront_orders")).scalar() == "paid"


def test_iyzico_failed_payment_keeps_order_unpaid(conn, store, settings, monkeypatch):
    from app.storefront import payments
    configure_store(conn)
    settings(storefront_payment_provider="iyzico", iyzico_api_key="a", iyzico_secret_key="b",
             iyzico_base_url="https://sandbox-api.iyzipay.com")

    def fake_post(url, **kw):
        body = json.loads(kw["content"])
        if url.endswith("/initialize/auth/ecom"):
            return httpx.Response(200, json={"status": "success", "token": "t", "paymentPageUrl": "https://x.example/p"})
        return httpx.Response(200, json={"status": "success", "paymentStatus": "FAILURE", "price": 100, "basketId": body["conversationId"],
                                         "errorMessage": "Kart limiti yetersiz"})

    monkeypatch.setattr(payments, "_post", fake_post)
    pid = add_product(conn, sku="IY2", price="100")
    fresh()
    d = _card_checkout(store, pid)
    store.get(d["redirect"], follow_redirects=False)
    code, t = d["public_code"], d["redirect"].split("t=")[1]
    back = store.post(f"/odeme/geri-donus/iyzico?c={code}&t={t}", data={"token": "t"}, follow_redirects=False)
    assert back.headers["location"].endswith("&odeme=basarisiz")
    assert conn.execute(text("SELECT status FROM storefront_orders")).scalar() == "pending_payment"
    page = store.get(back.headers["location"]).text
    assert "Ödeme gerçekleşmedi" in page and "Ödemeye devam et" in page


# ------------------------------------------------------------------ bildirimler
def test_notifications_disabled_without_provider(conn, store):
    configure_store(conn)
    pid = add_product(conn, sku="N0")
    fresh()
    store.post("/api/store/cart/add", json={"product_id": pid, "quantity": 1}, headers=H)
    assert store.post("/api/store/checkout", json={**CHECKOUT, "payment_method": "cash_on_delivery"}, headers=H).status_code == 200
    assert conn.execute(text("SELECT COUNT(*) FROM notification_outbox")).scalar() == 0
    assert conn.execute(text("SELECT COUNT(*) FROM sync_jobs WHERE job_type = 'storefront.notify'")).scalar() == 0


def test_order_notifications_email_and_sms(engine, conn, store, settings, monkeypatch):
    from app.services import order_notifications as n
    from app.services import sync_service
    configure_store(conn, notify_sms=True)
    settings(smtp_host="smtp.example.com", smtp_from="siparis@trendcantaniz.com", sms_provider="netgsm",
             netgsm_usercode="u", netgsm_password="p", netgsm_header="TRENDCANTA", storefront_order_alert_emails="sahip@example.com")
    pid = add_product(conn, sku="N1", name="Kroko Çanta")
    fresh()
    store.post("/api/store/cart/add", json={"product_id": pid, "quantity": 1}, headers=H)
    d = store.post("/api/store/checkout", json={**CHECKOUT, "payment_method": "bank_transfer"}, headers=H).json()
    rows = conn.execute(text("SELECT channel, recipient, template, body FROM notification_outbox ORDER BY id")).mappings().all()
    assert [(r["channel"], r["template"]) for r in rows] == [("email", "order_received"), ("sms", "order_received"),
                                                             ("email", "owner_alert")]
    assert rows[0]["recipient"] == "ayse@example.com" and d["public_code"] in rows[0]["body"] and "/siparis/" in rows[0]["body"]
    assert "Havale/EFT" in rows[0]["body"] and "Kroko Çanta" in rows[0]["body"]
    sent = []
    monkeypatch.setattr(n, "send_email", lambda to, subject, body: sent.append(("email", to, subject)) or "smtp")
    monkeypatch.setattr(n, "send_sms", lambda phone, msg: sent.append(("sms", phone, msg)) or "netgsm")
    res = sync_service.execute(engine, {"job_type": "storefront.notify", "payload": {}})
    assert res == {"sent": 3, "retry_or_failed": 0, "skipped": 0} and len(sent) == 3
    assert conn.execute(text("SELECT COUNT(*) FROM notification_outbox WHERE status = 'sent'")).scalar() == 3

    # Ödeme onayı + kargo bildirimi, her olay bir kez
    sfo_id = conn.execute(text("SELECT id FROM storefront_orders")).scalar()
    from app.storefront import checkout
    with engine.begin() as c:
        order_id = checkout.confirm_payment(c, sfo_id, reference="EFT-1", provider="bank_transfer")
    conn.execute(text("UPDATE orders SET internal_status = 'shipped' WHERE id = :o"), {"o": order_id})
    with engine.begin() as c:
        assert n.scan_shipped(c) == 1
    with engine.begin() as c:
        assert n.scan_shipped(c) == 0
    templates = [r[0] for r in conn.execute(text("SELECT template FROM notification_outbox WHERE channel = 'email' ORDER BY id"))]
    assert templates == ["order_received", "owner_alert", "payment_confirmed", "shipped"]

    # Geçici SMTP hatası: kuyrukta kalır, deneme sayılır
    def boom(*a):
        raise OSError("bağlantı reddedildi")
    monkeypatch.setattr(n, "send_email", boom)
    res = n.deliver_pending(engine.begin)
    assert res["retry_or_failed"] >= 1
    assert conn.execute(text("SELECT last_error FROM notification_outbox WHERE status = 'queued' LIMIT 1")).scalar().startswith("OSError")


def test_sms_number_normalization():
    from app.services.order_notifications import sms_number
    assert sms_number("0532 111 22 33") == "5321112233" and sms_number("+90 (532) 111 22 33") == "5321112233"
    assert sms_number("0212 111 22 33") is None and sms_number("") is None


# ------------------------------------------------------------------ tedarikçiye aktarım
def test_supplier_forwarding_only_for_web_orders(engine, conn, store, client_factory):
    from app.services import supplier_forwarding as sf
    configure_store(conn)
    _, ids = supplier_catalog(conn)
    fresh()
    store.post("/api/store/cart/add", json={"product_id": ids["CB-1042-SYH"], "quantity": 2}, headers=H)
    assert store.post("/api/store/checkout", json={**CHECKOUT, "payment_method": "cash_on_delivery"}, headers=H).status_code == 200
    web_order = conn.execute(text("SELECT order_id FROM storefront_orders")).scalar()
    trendyol_order = marketplace_order(conn, ids["CB-1042-SYH"], 1)

    with engine.begin() as c:
        with pytest.raises(sf.ForwardingError):
            sf.prepare(c, trendyol_order)
    # Mod 'manual' (varsayılan): bakım işi taslak hazırlar, göndermez
    with engine.begin() as c:
        assert sf.run_auto(c) == {"mode": "manual", "prepared": 1, "sent": 0}
    with engine.begin() as c:
        assert sf.run_auto(c)["prepared"] == 0  # idempotent
    so = conn.execute(text("SELECT * FROM supplier_orders")).mappings().one()
    assert so["order_id"] == web_order and so["channel"] == "storefront" and so["status"] == "draft"
    assert so["payload"]["lines"] == [{"sku": "1042-SYH", "barcode": so["payload"]["lines"][0]["barcode"],
                                       "name": "Kapitone Zincir Askılı Omuz Çantası · Siyah", "quantity": 2}]
    assert so["payload"]["ship_to"]["city"] == "İstanbul" and Decimal(so["cost"]) == Decimal("700.00")
    assert conn.execute(text("SELECT COUNT(*) FROM supplier_orders WHERE order_id = :o"), {"o": trendyol_order}).scalar() == 0

    c = admin(client_factory)
    lst = c.get("/api/storefront/supplier-orders").json()
    assert lst["total"] == 1 and lst["items"][0]["can_send"] is False and lst["mode"] == "manual"
    r = c.post(f"/api/storefront/supplier-orders/{so['id']}/send", headers=PANEL)
    assert r.status_code == 409 and "manuel" in r.json()["detail"]
    assert c.post(f"/api/storefront/supplier-orders/{so['id']}/mark-sent", json={"external_id": "CB-99"}, headers=PANEL).status_code == 200
    assert conn.execute(text("SELECT status, external_supplier_order_id FROM supplier_orders")).one() == ("manual_sent", "CB-99")
    assert c.post(f"/api/storefront/supplier-orders/{so['id']}/cancel", headers=PANEL).status_code == 409
    detail = c.get(f"/api/orders/{web_order}").json()
    assert detail["storefront"]["supplier_orders"][0]["status_label"] == "Manuel iletildi"

    # Mod 'off': hiçbir şey hazırlanmaz
    configure_store(conn, supplier_forwarding_mode="off")
    with engine.begin() as cc:
        assert sf.run_auto(cc) == {"mode": "off", "prepared": 0, "sent": 0}


def test_supplier_connector_auto_mode(engine, conn, store, monkeypatch):
    from app.services import supplier_forwarding as sf
    configure_store(conn, supplier_forwarding_mode="auto")
    _, ids = supplier_catalog(conn)
    fresh()
    received = []

    class FakeConnector(sf.SupplierOrderConnector):
        supplier_code, name = "canta_bayim", "Test bağlantısı"

        def is_configured(self):
            return True

        def submit(self, payload):
            received.append(payload)
            return "TED-1"

    monkeypatch.setitem(sf.CONNECTORS, "canta_bayim", FakeConnector)
    store.post("/api/store/cart/add", json={"product_id": ids["CB-1042-SYH"], "quantity": 1}, headers=H)
    store.post("/api/store/checkout", json={**CHECKOUT, "payment_method": "cash_on_delivery"}, headers=H)
    with engine.begin() as c:
        assert sf.run_auto(c) == {"mode": "auto", "prepared": 1, "sent": 1}
    assert received[0]["channel"] == "storefront"
    assert conn.execute(text("SELECT status, external_supplier_order_id FROM supplier_orders")).one() == ("sent", "TED-1")


# ------------------------------------------------------------------ e-fatura
def test_einvoice_off_by_default_and_provider_layer(engine, conn, store, settings, monkeypatch):
    from app.services import einvoice
    configure_store(conn)
    pid = add_product(conn, sku="EF1", price="500")
    fresh()
    store.post("/api/store/cart/add", json={"product_id": pid, "quantity": 1}, headers=H)
    store.post("/api/store/checkout", json={**CHECKOUT, "payment_method": "cash_on_delivery"}, headers=H)
    assert conn.execute(text("SELECT COUNT(*) FROM einvoice_records")).scalar() == 0
    order_id = conn.execute(text("SELECT order_id FROM storefront_orders")).scalar()

    class FakeProvider(einvoice.EInvoiceProvider):
        code, name = "fake", "Test entegratörü"

        def is_configured(self):
            return True

        def issue(self, invoice):
            assert invoice["order_code"].startswith("TC") and invoice["lines"][0]["vat_rate"] == 20
            return einvoice.InvoiceResult(external_id="uuid-1", document_number="TCE2026000000001", document_type="e-arsiv")

    monkeypatch.setitem(einvoice.PROVIDERS, "fake", FakeProvider)
    settings(einvoice_provider="fake")
    with engine.begin() as c:
        assert einvoice.queue(c, order_id) is False  # panel anahtarı kapalı
    configure_store(conn, einvoice_enabled=True)
    with engine.begin() as c:
        assert einvoice.queue(c, order_id) is True
        assert einvoice.process(c) == {"issued": 1, "failed": 0}
    assert conn.execute(text("SELECT status, document_number FROM einvoice_records")).one() == ("issued", "TCE2026000000001")


def test_integrations_status_has_no_secrets(client_factory, settings):
    settings(storefront_payment_provider="paytr", paytr_merchant_id="123", paytr_merchant_key="GIZLI-KEY", paytr_merchant_salt="GIZLI-SALT",
             smtp_host="smtp.example.com", smtp_from="a@b.co", smtp_password="GIZLI-SMTP")
    c = admin(client_factory)
    r = c.get("/api/storefront/integrations")
    assert r.status_code == 200 and "GIZLI" not in r.text
    d = r.json()
    assert d["payment"]["active"] == "PayTR" and d["email"]["configured"] is True and d["einvoice"]["active"] is False
    assert d["sms"]["configured"] is False and d["supplier_forwarding"]["mode"] == "manual"


# ------------------------------------------------------------------ müşteri hesabı
REG = {"full_name": "Zeynep Kaya", "email": "Zeynep@Example.com", "phone": "0533 444 55 66", "password": "guvenli123",
       "accept_kvkk": True}


def test_account_register_login_orders_addresses(conn, store):
    configure_store(conn)
    pid = add_product(conn, sku="AC1")
    fresh()
    assert store.get("/hesabim", follow_redirects=False).headers["location"] == "/hesap/giris?sonra=/hesabim"
    r = store.post("/api/store/account/register", json={**REG, "accept_kvkk": False}, headers=H)
    assert r.status_code == 422 and "accept_kvkk" in r.json()["fields"]
    assert store.post("/api/store/account/register", json={**REG, "password": "kisa"}, headers=H).status_code == 422
    assert store.post("/api/store/account/register", json={**REG, "password": "sadeceharf"}, headers=H).status_code == 422
    r = store.post("/api/store/account/register?sonra=//evil.example", json=REG, headers=H)
    assert r.status_code == 200 and r.json()["redirect"] == "/hesabim"
    cookie = r.headers["set-cookie"]
    assert "tc_hesap=" in cookie and "httponly" in cookie.lower() and "samesite=lax" in cookie.lower()
    row = conn.execute(text("SELECT email, password_hash FROM storefront_customers")).one()
    assert row.email == "zeynep@example.com" and row.password_hash.startswith("$argon2")
    assert store.post("/api/store/account/register", json=REG, headers=H).status_code == 422  # aynı e-posta
    # CSRF: özel başlık zorunlu
    assert store.post("/api/store/account/addresses", json={}).status_code == 403

    page = store.get("/hesabim")
    assert page.status_code == 200 and "Merhaba, Zeynep" in page.text and "Henüz siparişiniz yok" in page.text
    addr = {"title": "Ev", "full_name": "Zeynep Kaya", "phone": "05334445566", "city": "İzmir", "district": "Karşıyaka",
            "address": "Bostanlı Mah. 1234 Sok. No:5 D:2", "postal_code": "35590"}
    assert store.post("/api/store/account/addresses", json={**addr, "city": "Atlantis"}, headers=H).status_code == 422
    assert store.post("/api/store/account/addresses", json=addr, headers=H).status_code == 200
    assert store.post("/api/store/account/addresses", json={**addr, "title": "İş", "city": "Ankara", "is_default": True},
                      headers=H).status_code == 200
    addrs = conn.execute(text("SELECT id, title, is_default FROM storefront_customer_addresses ORDER BY id")).all()
    assert [(a.title, a.is_default) for a in addrs] == [("Ev", False), ("İş", True)]
    assert store.post(f"/api/store/account/addresses/{addrs[1].id}/delete", json={}, headers=H).status_code == 200
    assert conn.execute(text("SELECT title FROM storefront_customer_addresses WHERE is_default")).scalar() == "Ev"

    # Ödeme formu kayıtlı bilgilerle dolu gelir; sipariş hesaba bağlanır
    store.post("/api/store/cart/add", json={"product_id": pid, "quantity": 1}, headers=H)
    checkout_page = store.get("/odeme").text
    assert 'value="zeynep@example.com"' in checkout_page and "Bostanlı Mah." in checkout_page and "<option selected>İzmir</option>" in checkout_page
    d = store.post("/api/store/checkout", json={**CHECKOUT, "email": "zeynep@example.com", "payment_method": "cash_on_delivery"},
                   headers=H).json()
    assert conn.execute(text("SELECT customer_id FROM storefront_orders")).scalar() is not None
    page = store.get("/hesabim").text
    assert d["public_code"] in page
    detail = store.get(f"/hesabim/siparis/{d['public_code']}")
    assert detail.status_code == 200 and "Siparişlerim" in detail.text

    # Çıkış → oturum geçersiz
    assert store.post("/api/store/account/logout", json={}, headers=H).json()["redirect"] == "/"
    assert store.get("/hesabim", follow_redirects=False).status_code == 303
    assert store.get(f"/hesabim/siparis/{d['public_code']}", follow_redirects=False).status_code == 303

    # Başka müşteri bu siparişi göremez
    store.post("/api/store/account/register", json={**REG, "email": "baska@example.com"}, headers=H)
    assert store.get(f"/hesabim/siparis/{d['public_code']}").status_code == 404


def test_account_login_lockout(conn, store):
    store.post("/api/store/account/register", json=REG, headers=H)
    store.post("/api/store/account/logout", json={}, headers=H)
    for _ in range(5):
        r = store.post("/api/store/account/login", json={"email": "zeynep@example.com", "password": "yanlis1234"}, headers=H)
        assert r.status_code == 401
    r = store.post("/api/store/account/login", json={"email": "zeynep@example.com", "password": "guvenli123"}, headers=H)
    assert r.status_code == 401 and "Çok fazla" in r.json()["detail"]
    conn.execute(text("UPDATE storefront_customers SET locked_until = NOW() - INTERVAL '1 second'"))
    r = store.post("/api/store/account/login?sonra=/hesabim%23adresler", json={"email": "ZEYNEP@example.com", "password": "guvenli123"},
                   headers=H)
    assert r.status_code == 200 and r.json()["redirect"] == "/hesabim#adresler"
    # Bilinmeyen e-posta ile aynı mesaj (hesap varlığı sızdırılmaz)
    r = store.post("/api/store/account/login", json={"email": "yok@example.com", "password": "guvenli123"}, headers=H)
    assert r.json()["detail"] == "E-posta veya şifre hatalı."


def test_password_reset_flow(conn, store, settings):
    # E-posta sağlayıcısı yok: sıfırlama kapalı, iletişim yönlendirmesi
    assert store.post("/api/store/account/password/forgot", json={"email": "a@b.co"}, headers=H).status_code == 503
    assert "iletişim sayfasındaki" in store.get("/hesap/sifremi-unuttum").text

    settings(smtp_host="smtp.example.com", smtp_from="destek@trendcantaniz.com", storefront_base_url="https://www.trendcantaniz.com")
    store.post("/api/store/account/register", json=REG, headers=H)
    r = store.post("/api/store/account/password/forgot", json={"email": "zeynep@example.com"}, headers=H)
    unknown = store.post("/api/store/account/password/forgot", json={"email": "yok@example.com"}, headers=H)
    assert r.json()["message"] == unknown.json()["message"]
    body = conn.execute(text("SELECT body FROM notification_outbox WHERE template = 'password_reset'")).scalar()
    token = re.search(r"https://www\.trendcantaniz\.com/hesap/sifre-yenile\?anahtar=([\w-]+)", body).group(1)
    assert conn.execute(text("SELECT COUNT(*) FROM storefront_password_resets WHERE token_hash = :t"), {"t": token}).scalar() == 0
    assert store.post("/api/store/account/password/reset", json={"token": token, "password": "zayif"}, headers=H).status_code == 422
    r = store.post("/api/store/account/password/reset", json={"token": token, "password": "yeniSifre2026"}, headers=H)
    assert r.status_code == 200 and r.json()["redirect"] == "/hesap/giris?yenilendi=1"
    # Tüm oturumlar kapandı, anahtar tek kullanımlık
    assert store.get("/hesabim", follow_redirects=False).status_code == 303
    assert store.post("/api/store/account/password/reset", json={"token": token, "password": "yeniSifre2027"}, headers=H).status_code == 422
    assert store.post("/api/store/account/login", json={"email": "zeynep@example.com", "password": "yeniSifre2026"}, headers=H).status_code == 200
    # Şifre değiştirme: mevcut şifre doğrulanır
    assert store.post("/api/store/account/password/change", json={"current_password": "yanlis", "password": "baskaSifre1"},
                      headers=H).status_code == 422
    assert store.post("/api/store/account/password/change", json={"current_password": "yeniSifre2026", "password": "baskaSifre1"},
                      headers=H).status_code == 200
    assert store.get("/hesabim").status_code == 200  # mevcut oturum korunur


def test_account_pages_not_indexed(store):
    robots = store.get("/robots.txt").text
    assert "Disallow: /hesap/" in robots and "Disallow: /hesabim" in robots
    r = store.get("/hesap/giris")
    assert r.status_code == 200 and 'name="robots" content="noindex' in r.text and r.headers["cache-control"] == "no-store"
