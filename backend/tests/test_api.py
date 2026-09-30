from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.connectors.base import NormalizedLine, NormalizedOrder, NormalizedShipment
from app.security import hash_password
from app.services.orders_sync import ensure_store, upsert_orders

H = {"X-Requested-With": "TrendHub"}


@pytest.fixture
def client(engine):
    from app.main import app
    with TestClient(app) as c:   # lifespan -> admin bootstrap
        yield c


def login(client, username="admin", password="Admin-Password-123"):
    r = client.post("/api/auth/login", json={"username": username, "password": password}, headers=H)
    assert r.status_code == 200, r.text
    return r


def add_user(engine, username, role):
    with engine.begin() as c:
        c.execute(text("INSERT INTO users(username, password_hash, role) VALUES (:u, :h, :r)"),
                  {"u": username, "h": hash_password("Strong-Password-1"), "r": role})


def seed_order(engine, number="TY-9", status="new", price="200"):
    with engine.begin() as c:
        store = ensure_store(c, "trendyol", "1", "Trendyol")
        upsert_orders(c, store, [NormalizedOrder(
            external_order_id=number, marketplace_status="Created", internal_status=status,
            order_date=datetime.now(timezone.utc),
            lines=[NormalizedLine("L-" + number, "SKU-9", "B9", "Sırt Çantası", 1, Decimal(price))],
            shipments=[NormalizedShipment("P-" + number, "Aras", "T1")])])
        return c.execute(text("SELECT id FROM orders WHERE external_order_id = :n"), {"n": number}).scalar()


def test_health_is_public_and_minimal(client):
    r = client.get("/api/health")
    assert r.json() == {"status": "healthy", "database": "connected"}


@pytest.mark.parametrize("path", ["/api/dashboard", "/api/orders", "/api/products", "/api/integrations",
                                  "/api/finance/summary", "/api/reports/sku", "/api/system/health",
                                  "/api/settings", "/api/marketplaces", "/api/shipments", "/api/suppliers"])
def test_endpoints_require_auth(client, path):
    assert client.get(path).status_code == 401


def test_login_logout_and_bootstrap_admin(client, engine):
    r = login(client)
    assert r.json()["user"]["role"] == "admin"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/api/auth/me").json()["username"] == "admin"
    assert client.post("/api/auth/logout", headers=H).status_code == 200
    assert client.get("/api/auth/me").status_code == 401
    with engine.connect() as c:
        h = c.execute(text("SELECT token_hash FROM user_sessions LIMIT 1")).scalar()
        assert len(h) == 64  # yalnızca özet saklanır
        actions = [r[0] for r in c.execute(text("SELECT action FROM audit_logs ORDER BY id"))]
    assert actions == ["auth.login", "auth.logout"]


def test_csrf_header_required(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "Admin-Password-123"})
    assert r.status_code == 403


def test_account_lockout_and_failed_login_is_audited(client, engine):
    for _ in range(5):
        assert client.post("/api/auth/login", json={"username": "admin", "password": "yanlis-parola"},
                           headers=H).status_code == 401
    r = client.post("/api/auth/login", json={"username": "admin", "password": "Admin-Password-123"}, headers=H)
    assert r.status_code == 423
    with engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action = 'auth.login_failed'")).scalar() == 6


def test_dashboard_empty_state_shows_no_fake_data(client):
    login(client)
    d = client.get("/api/dashboard?period=7d").json()
    assert d["summary"]["orders"]["orders"] == 0 and d["summary"]["orders"]["revenue"] == 0
    assert len(d["statuses"]) == 9 and all(s["count"] == 0 for s in d["statuses"])
    assert len(d["daily"]) == 7
    states = {i["code"]: i["state"] for i in d["integrations"]}
    assert states == {"trendyol": "not_connected", "hepsiburada": "not_connected", "amazon_tr": "not_connected"}


def test_integrations_never_return_secret_values(client, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "trendyol_api_secret", "SUPER-SECRET-VALUE")
    login(client)
    body = client.get("/api/integrations").text
    assert "SUPER-SECRET-VALUE" not in body
    ty = next(i for i in client.get("/api/integrations").json()["items"] if i["code"] == "trendyol")
    assert {c["env"]: c["is_set"] for c in ty["credentials"]} == {
        "TRENDYOL_SELLER_ID": False, "TRENDYOL_API_KEY": False, "TRENDYOL_API_SECRET": True}
    r = client.post("/api/integrations/trendyol/sync", headers=H)
    assert r.status_code == 409 and "bağlı değil" in r.json()["detail"]


def test_orders_list_detail_and_status_change(client, engine):
    oid = seed_order(engine)
    login(client)
    lst = client.get("/api/orders?q=SKU-9").json()
    assert lst["total"] == 1 and lst["items"][0]["status_label"] == "Yeni"
    detail = client.get(f"/api/orders/{oid}").json()
    assert detail["items"][0]["sku"] == "SKU-9" and "payload_hash" not in detail
    assert {t["code"] for t in detail["allowed_transitions"]} >= {"sent_to_supplier", "cancelled"}

    r = client.post(f"/api/orders/{oid}/status", json={"status": "delivered"}, headers=H)
    assert r.status_code == 409
    r = client.post(f"/api/orders/{oid}/status", json={"status": "sent_to_supplier", "note": "Çanta Bayim"}, headers=H)
    assert r.status_code == 200
    hist = client.get(f"/api/orders/{oid}").json()["history"]
    assert [h["to_status"] for h in hist] == ["new", "sent_to_supplier"] and hist[-1]["username"] == "admin"


def test_manual_adjustment_recalculates_profit(client, engine):
    oid = seed_order(engine)
    login(client)
    before = client.get(f"/api/orders/{oid}").json()["net_profit"]
    r = client.post(f"/api/orders/{oid}/adjustments", json={"kind": "advertising", "amount": "30"}, headers=H)
    assert r.status_code == 200
    after = client.get(f"/api/orders/{oid}").json()
    assert Decimal(str(after["advertising_cost"])) == Decimal("30")
    assert Decimal(str(before)) - Decimal(str(after["net_profit"])) == Decimal("30")


def test_viewer_cannot_mutate_and_operator_cannot_change_settings(client, engine):
    oid = seed_order(engine)
    add_user(engine, "izleyici", "viewer")
    add_user(engine, "operator1", "operator")
    login(client, "izleyici", "Strong-Password-1")
    assert client.get("/api/orders").status_code == 200
    assert client.post(f"/api/orders/{oid}/status", json={"status": "preparing"}, headers=H).status_code == 403
    login(client, "operator1", "Strong-Password-1")
    assert client.post(f"/api/orders/{oid}/status", json={"status": "preparing"}, headers=H).status_code == 200
    assert client.put("/api/settings", json={"values": {"finance.default_shipping_cost": 10}}, headers=H).status_code == 403
    assert client.get("/api/users").status_code == 403


def test_settings_validation_and_audit(client, engine):
    login(client)
    assert client.put("/api/settings", json={"values": {"finance.commission_rate.trendyol": 5}}, headers=H).status_code == 422
    assert client.put("/api/settings", json={"values": {"bilinmeyen": 1}}, headers=H).status_code == 422
    r = client.put("/api/settings", json={"values": {"finance.commission_rate.trendyol": 0.18}}, headers=H)
    assert r.json()["changed"] == ["finance.commission_rate.trendyol"]
    with engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action = 'settings.updated'")).scalar() == 1


def test_products_cost_history_and_recalculation(client, engine):
    oid = seed_order(engine)
    login(client)
    r = client.post("/api/products", json={"sku": "SKU-9", "name": "Sırt Çantası", "stock": 2}, headers=H)
    assert r.status_code == 201
    pid = r.json()["id"]
    assert client.post("/api/products", json={"sku": "SKU-9", "name": "x"}, headers=H).status_code == 409
    r = client.post(f"/api/products/{pid}/cost", json={"cost": "80"}, headers=H)
    assert r.json()["recalculated_orders"] == 1
    assert Decimal(str(client.get(f"/api/orders/{oid}").json()["product_cost"])) == Decimal("80")
    prods = client.get("/api/products?low_stock=true").json()
    assert prods["total"] == 1 and prods["items"][0]["is_low_stock"] is True
    assert len(client.get(f"/api/products/{pid}/costs").json()) == 1


def test_finance_reports_and_csv(client, engine):
    seed_order(engine, "A", price="100")
    seed_order(engine, "B", price="300")
    seed_order(engine, "C", status="cancelled", price="999")
    login(client)
    assert client.post("/api/finance/expenses", json={"category": "advertising", "amount": "50",
                                                      "expense_date": datetime.now().date().isoformat(),
                                                      "sku": "SKU-9"}, headers=H).status_code == 201
    f = client.get("/api/finance/summary?period=30d").json()
    assert f["orders"]["orders"] == 2 and f["orders"]["revenue"] == 400
    assert f["expenses"]["total"] == 50
    assert f["net_profit_after_expenses"] == pytest.approx(f["orders"]["net_profit"] - 50)
    sku = client.get("/api/reports/sku?period=30d").json()["items"]
    assert len(sku) == 1 and sku[0]["quantity"] == 2 and sku[0]["advertising"] == 50
    csv = client.get("/api/reports/sku.csv?period=30d")
    assert csv.status_code == 200 and "Net Kâr" in csv.text and "SKU-9" in csv.text


def test_system_health_jobs_and_events(client, engine):
    login(client)
    h = client.get("/api/system/health").json()
    assert h["database"]["migration"] == "0009_storefront_production"
    assert h["worker_alive"] is False and h["status"] == "degraded"
    assert h["config"]["connector_write_enabled"] is False
    from app.services import jobs
    with engine.begin() as c:
        jobs.enqueue(c, "orders.sync", marketplace="trendyol", idempotency_key="z", max_attempts=1)
        j = jobs.claim(c, "w")
        jobs.fail(c, j, "patladı")
    assert client.get("/api/system/jobs?status=dead").json()["total"] == 1
    ev = client.get("/api/system/events").json()["items"]
    assert len(ev) == 1
    assert client.post(f"/api/system/jobs/{j['id']}/retry", headers=H).status_code == 200
    assert client.post(f"/api/system/events/{ev[0]['id']}/resolve", headers=H).status_code == 200
    assert client.get("/api/system/audit").json()["total"] >= 3


def test_user_management(client):
    login(client)
    assert client.post("/api/users", json={"username": "ali", "role": "operator", "password": "kisa"},
                       headers=H).status_code == 422
    r = client.post("/api/users", json={"username": "ali", "role": "operator", "password": "Uzun-Parola-123"}, headers=H)
    assert r.status_code == 201
    me = client.get("/api/auth/me").json()["id"]
    assert client.patch(f"/api/users/{me}", json={"is_active": False}, headers=H).status_code == 409


def test_create_product_with_barcode_links_existing_order_items(client, engine):
    oid = seed_order(engine)   # kalem: sku SKU-9, barkod B9
    login(client)
    r = client.post("/api/products", json={"sku": "BASKA-SKU", "barcode": "B9", "name": "Barkodla eşleşen"}, headers=H)
    assert r.status_code == 201, r.text
    with engine.connect() as c:
        assert c.execute(text("SELECT product_id FROM order_items WHERE order_id = :o"), {"o": oid}).scalar() == r.json()["id"]


def test_dashboard_and_finance_expose_estimated_tax_today_top_products(client, engine):
    with engine.begin() as c:
        c.execute(text("INSERT INTO products(sku, name, cost, vat_rate) VALUES ('SKU-9', 'Sırt Çantası', 60, 20)"))
    oid = seed_order(engine, "T1", price="240")
    seed_order(engine, "T2", status="returned", price="120")
    login(client)
    d = client.get("/api/dashboard?period=7d").json()
    assert d["today"]["orders"] == 2 and d["pending_orders"] == 1
    assert d["returns"]["period"] == 1
    assert d["top_products"][0]["sku"] == "SKU-9" and float(d["top_products"][0]["revenue"]) == 240
    assert {m["code"] for m in d["by_marketplace"]} == {"trendyol", "hepsiburada", "amazon_tr", "storefront"}
    s = d["summary"]
    assert s["is_estimate"] is True
    # T1: satış 240 (KDV 40) - maliyet 60 (KDV 10) = 30; iade edilen T2'de KDV 0
    assert float(s["tax_estimate"]) == 30.0
    assert float(s["net_profit_after_tax"]) == pytest.approx(float(s["net_profit_after_expenses"]) - 30.0)
    assert float(s["orders"]["average_order_value"]) == 180.0
    detail = client.get(f"/api/orders/{oid}").json()
    assert float(detail["tax_estimate"]) == 30.0
    sku = client.get("/api/reports/sku?period=7d").json()["items"][0]
    # iade edilen T2'nin ürünü stoğa döner -> maliyet yalnızca T1'den (60)
    assert float(sku["product_cost"]) == 60.0 and float(sku["tax_estimate"]) == 30.0


def test_system_health_reports_connectors_and_heartbeat(client, engine):
    from app.services import jobs
    with engine.begin() as c:
        c.execute(text("INSERT INTO worker_heartbeats(worker_id, hostname) VALUES ('w1', 'h')"))
        jobs.enqueue(c, "orders.sync", marketplace="trendyol", idempotency_key="k", max_attempts=1)
        j = jobs.claim(c, "w1")
        jobs.fail(c, j, "hata")
    login(client)
    h = client.get("/api/system/health").json()
    assert h["worker_alive"] is True and h["last_heartbeat_at"]
    by = {c["code"]: c for c in h["connectors"]}
    assert set(by) == {"trendyol", "hepsiburada", "amazon_tr"}
    assert by["trendyol"]["state"] == "not_connected" and by["trendyol"]["failed_24h"] == 1
    assert "credentials" not in by["trendyol"]
