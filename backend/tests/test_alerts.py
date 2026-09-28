"""Uyarı merkezi: gerçek veriden tespit, tekilleştirme, otomatik çözülme, olay uyarıları, yetki, zil özeti."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.security import hash_password
from app.services import alerts, sync_service
from tests.test_shipping_plan import _seed as seed_order
from tests.test_suppliers import MAP_A, XML_A, _catalog, make_supplier, upload

H = {"X-Requested-With": "TrendHub"}


@pytest.fixture
def client(engine):
    from app.main import app
    with TestClient(app) as c:
        assert c.post("/api/auth/login", json={"username": "admin", "password": "Admin-Password-123"},
                      headers=H).status_code == 200
        yield c


def _open(engine, code=None):
    with engine.begin() as c:
        q = "SELECT code, severity, fingerprint, occurrences, description FROM alerts WHERE status = 'open'"
        return [dict(r._mapping) for r in c.execute(text(q + (" AND code = :c" if code else "") + " ORDER BY id"),
                                                     {"c": code})]


def test_stock_zero_detected_deduplicated_and_auto_resolved(client, engine):
    sid = make_supplier(client, "Çanta Bayim", MAP_A)
    upload(client, sid, XML_A.replace("<Stok>40</Stok>", "<Stok>0</Stok>"))
    # Yalnızca havuzdaki (kataloğa alınmamış) ürünler uyarı üretmez
    assert client.post("/api/alerts/scan", headers=H).json()["detected"] == 0
    _catalog(client, sid)
    first = client.post("/api/alerts/scan", headers=H).json()
    zero = _open(engine, "supplier.stock_zero")
    # CB-100 (stok 0) ve CB-300 (stok 0)
    assert len(zero) == 2 and "Stok 40 → 0" not in zero[0]["description"]
    # CB-200 stoğu 5 > eşik 2: kritik stok uyarısı YOK
    assert _open(engine, "supplier.stock_critical") == []
    # İkinci tarama: yeni satır açılmaz, sayaç artar
    second = client.post("/api/alerts/scan", headers=H).json()
    assert second["new"] == 0 and first["new"] >= 1
    again = _open(engine, "supplier.stock_zero")
    assert len(again) == 2 and again[0]["occurrences"] == 2
    # CB-100 stoğu geri geldi -> sistem otomatik çözer; CB-300 hâlâ 0
    upload(client, sid, XML_A)
    client.post("/api/alerts/scan", headers=H)
    still = _open(engine, "supplier.stock_zero")
    assert len(still) == 1 and "Barkodsuz" not in still[0]["description"]
    with engine.begin() as c:
        assert c.execute(text("SELECT resolution FROM alerts WHERE status = 'resolved'")).scalar() == "system"


def test_stock_drop_to_zero_shows_previous_value_and_critical_threshold(client, engine):
    sid = make_supplier(client, "Çanta Bayim", MAP_A)
    upload(client, sid, XML_A)
    _catalog(client, sid)
    upload(client, sid, XML_A.replace("<Stok>40</Stok>", "<Stok>0</Stok>").replace("<Stok>5</Stok>", "<Stok>2</Stok>"))
    client.post("/api/alerts/scan", headers=H)
    assert any("Stok 40 → 0" in a["description"] for a in _open(engine, "supplier.stock_zero"))
    crit = _open(engine, "supplier.stock_critical")
    assert len(crit) == 1 and crit[0]["severity"] == "warning" and "eşik 2" in crit[0]["description"]


def test_price_change_over_threshold_and_barcode_change_raise_event_alerts(client, engine):
    sid = make_supplier(client, "Çanta Bayim", MAP_A)
    upload(client, sid, XML_A)
    # CB-100 150 -> 155 (%3.3): uyarı YOK; CB-200 1250,50 -> 1600 (%28): uyarı VAR
    upload(client, sid, XML_A.replace("<AlisFiyati>150,00</AlisFiyati>", "<AlisFiyati>155,00</AlisFiyati>")
           .replace("<AlisFiyati>1.250,50</AlisFiyati>", "<AlisFiyati>1.600,00</AlisFiyati>")
           .replace("<Barkod>8690000000017</Barkod>", "<Barkod>8690000000555</Barkod>"))
    price = _open(engine, "supplier.price_change")
    assert len(price) == 1 and "1250.50 → 1600.00" in price[0]["description"] and "%27.9" in price[0]["description"]
    bc = _open(engine, "supplier.barcode_change")
    assert len(bc) == 1 and "8690000000017 → 8690000000555" in bc[0]["description"]
    # Olay uyarıları tarama ile otomatik kapanmaz (kullanıcı çözer)
    client.post("/api/alerts/scan", headers=H)
    assert len(_open(engine, "supplier.price_change")) == 1


def test_missing_product_and_feed_failure(client, engine):
    sid = make_supplier(client, "Çanta Bayim", MAP_A)
    upload(client, sid, XML_A)
    _catalog(client, sid)
    upload(client, sid, XML_A.replace("<UrunKodu>CB-100</UrunKodu>", "<UrunKodu>CB-101</UrunKodu>"))
    with engine.begin() as c:
        c.execute(text("UPDATE suppliers SET last_sync_status = 'failed', last_sync_error = 'HTTP 503' WHERE id = :s"),
                  {"s": sid})
    client.post("/api/alerts/scan", headers=H)
    missing = _open(engine, "supplier.missing")
    assert len(missing) == 1 and "bulunamadı" in missing[0]["description"]
    feed = _open(engine, "supplier.feed_failed")
    assert feed[0]["severity"] == "critical" and "HTTP 503" in feed[0]["description"]


def test_marketplace_sync_failure_and_connection_failure(client, engine):
    with engine.begin() as c:
        c.execute(text("""INSERT INTO sync_jobs(marketplace, job_type, status, last_error, finished_at)
                          VALUES ('trendyol', 'orders.sync', 'dead', 'Yetkisiz erişim (HTTP 401)', NOW())"""))
        c.execute(text("UPDATE marketplaces SET last_check_ok = FALSE, last_check_message = 'HTTP 401' WHERE code = 'trendyol'"))
    client.post("/api/alerts/scan", headers=H)
    sync = _open(engine, "marketplace.sync_failed")
    assert len(sync) == 1 and "Siparişler çekilemedi" in sync[0]["description"] and sync[0]["severity"] == "critical"
    assert len(_open(engine, "marketplace.connection_failed")) == 1
    # Sonraki iş başarılı -> otomatik çözülür
    with engine.begin() as c:
        c.execute(text("""INSERT INTO sync_jobs(marketplace, job_type, status, finished_at)
                          VALUES ('trendyol', 'orders.sync', 'succeeded', NOW())"""))
        c.execute(text("UPDATE marketplaces SET last_check_ok = TRUE WHERE code = 'trendyol'"))
    client.post("/api/alerts/scan", headers=H)
    assert _open(engine, "marketplace.sync_failed") == [] and _open(engine, "marketplace.connection_failed") == []


def test_overdue_shipping_only_for_pending_orders(client, engine):
    old = datetime.now(timezone.utc) - timedelta(days=4)
    seed_order(engine, "TY-OLD", old)
    seed_order(engine, "TY-OLD-SHIPPED", old, status="shipped")
    seed_order(engine, "TY-NEW", datetime.now(timezone.utc))
    client.post("/api/alerts/scan", headers=H)
    overdue = _open(engine, "shipping.overdue")
    assert len(overdue) == 1 and "tahmini" in overdue[0]["description"]
    items = client.get("/api/alerts", params={"category": "shipping"}).json()["items"]
    assert items[0]["title"] == "Sipariş TY-OLD" and items[0]["external_order_id"] == "TY-OLD"
    assert items[0]["link"].startswith("#/orders?open=")


def test_resolve_requires_operator_is_audited_and_reopens_if_still_present(client, engine):
    with engine.begin() as c:
        c.execute(text("""INSERT INTO sync_jobs(marketplace, job_type, status, last_error, finished_at)
                          VALUES ('trendyol', 'listings.sync', 'failed', 'Zaman aşımı', NOW())"""))
        c.execute(text("INSERT INTO users(username, password_hash, role) VALUES ('izle', :h, 'viewer')"),
                  {"h": hash_password("Viewer-Password-123")})
    client.post("/api/alerts/scan", headers=H)
    summary = client.get("/api/alerts/summary").json()
    assert summary["critical"] == 1 and summary["total"] == 1 and summary["by_category"] == {"marketplace": 1}
    aid = client.get("/api/alerts").json()["items"][0]["id"]
    from app.main import app
    with TestClient(app) as v:
        v.post("/api/auth/login", json={"username": "izle", "password": "Viewer-Password-123"}, headers=H)
        assert v.get("/api/alerts/summary").status_code == 200
        assert v.post(f"/api/alerts/{aid}/resolve", json={}, headers=H).status_code == 403
        assert v.post("/api/alerts/scan", headers=H).status_code == 403
    r = client.post(f"/api/alerts/{aid}/resolve", json={"note": "kontrol edildi"}, headers=H)
    assert r.status_code == 200 and client.get("/api/alerts/summary").json()["total"] == 0
    with engine.begin() as c:
        audit = c.execute(text("SELECT action FROM audit_logs WHERE action = 'alert.resolved'")).scalars().all()
    assert audit == ["alert.resolved"]
    resolved = client.get("/api/alerts", params={"status": "resolved"}).json()["items"][0]
    assert resolved["resolved_by_name"] == "admin" and resolved["resolution"] == "user: kontrol edildi"
    # Sorun sürüyorsa bir sonraki taramada YENİ uyarı açılır
    client.post("/api/alerts/scan", headers=H)
    assert client.get("/api/alerts/summary").json()["total"] == 1
    assert client.get("/api/alerts", params={"status": "all"}).json()["total"] == 2


def test_scan_runs_as_worker_job_and_after_syncs(engine):
    with engine.begin() as c:
        job_id = sync_service.schedule_alerts_scan(c)
        assert job_id and sync_service.schedule_alerts_scan(c) is None   # tekrar kuyruğa girmez
        c.execute(text("""INSERT INTO sync_jobs(marketplace, job_type, status, last_error, finished_at)
                          VALUES ('hepsiburada', 'orders.sync', 'failed', 'x', NOW())"""))
    res = sync_service.execute(engine, {"id": job_id, "job_type": "alerts.scan", "payload": {}})
    assert res["new"] == 1
    assert _open(engine, "marketplace.sync_failed")[0]["fingerprint"].endswith(":orders.sync")


def test_no_alerts_without_data(client, engine):
    r = client.post("/api/alerts/scan", headers=H).json()
    assert r == {"detected": 0, "new": 0, "auto_resolved": 0}
    assert client.get("/api/alerts/summary").json()["total"] == 0


def test_upsert_unique_open_fingerprint(engine):
    a = {"category": "system", "severity": "info", "code": "x.test", "title": "t", "fingerprint": "fp-1"}
    with engine.begin() as c:
        i1, new1 = alerts.upsert(c, a)
        i2, new2 = alerts.upsert(c, {**a, "severity": "warning"})
        assert i1 == i2 and new1 and not new2
        assert alerts.resolve(c, i1, None)
        i3, new3 = alerts.upsert(c, a)
        assert new3 and i3 != i1
