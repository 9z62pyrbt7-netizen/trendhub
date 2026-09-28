"""Reklam merkezi, finans tablosu (gerçek/tahmini), dönemler, CSV dışa aktarım ve muhasebe rolü yetkileri."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.security import hash_password
from tests.test_shipping_plan import _seed as seed_order

H = {"X-Requested-With": "TrendHub"}
TR = ZoneInfo("Europe/Istanbul")


def _login(username, password):
    from app.main import app
    c = TestClient(app)
    c.__enter__()
    assert c.post("/api/auth/login", json={"username": username, "password": password}, headers=H).status_code == 200
    return c


@pytest.fixture
def admin(engine):
    c = _login("admin", "Admin-Password-123")
    yield c
    c.__exit__(None, None, None)


@pytest.fixture
def accountant(engine, admin):
    with engine.begin() as c:
        c.execute(text("INSERT INTO users(username, password_hash, role) VALUES ('muhasebe', :h, 'accountant')"),
                  {"h": hash_password("Muhasebe-Password-123")})
    c = _login("muhasebe", "Muhasebe-Password-123")
    yield c
    c.__exit__(None, None, None)


def _campaign(client, channel="trendyol_ads", name="Yaz kampanyası", product_ids=()):
    aid = client.post("/api/ads/accounts", json={"channel": channel, "name": f"{channel} hesabı"}, headers=H)
    aid = aid.json()["id"] if aid.status_code == 201 else next(a["id"] for a in client.get("/api/ads/accounts").json()
                                                               if a["channel"] == channel)
    r = client.post("/api/ads/campaigns", json={"account_id": aid, "name": name, "product_ids": list(product_ids)}, headers=H)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_spend_without_performance_has_no_fake_attribution(admin):
    cid = _campaign(admin)
    today = datetime.now(TR).date().isoformat()
    assert admin.post("/api/ads/spend", json={"campaign_id": cid, "spend_date": today, "amount": "250.00"}, headers=H).status_code == 201
    s = admin.get("/api/ads/summary", params={"period": "7d"}).json()
    d = s["direct"]
    assert Decimal(str(d["spend"])) == Decimal("250.00")
    assert d["has_attribution"] is False and d["attributed_revenue"] is None and d["roas"] is None and d["cpa"] is None
    assert "tahmin üretmez" in d["note"] and "anlamına gelmez" in s["general"]["note"]
    assert s["by_channel"][0]["channel_label"] == "Trendyol Reklam"


def test_performance_gives_roas_cpa_conversion(admin):
    cid = _campaign(admin, "meta", "Instagram")
    today = datetime.now(TR).date().isoformat()
    admin.post("/api/ads/spend", json={"campaign_id": cid, "spend_date": today, "amount": "100"}, headers=H)
    admin.post("/api/ads/performance", json={"campaign_id": cid, "perf_date": today, "impressions": 10000, "clicks": 200,
                                             "attributed_orders": 4, "attributed_revenue": "800"}, headers=H)
    # aynı gün tekrar girilirse güncellenir (çift sayılmaz)
    admin.post("/api/ads/performance", json={"campaign_id": cid, "perf_date": today, "impressions": 10000, "clicks": 200,
                                             "attributed_orders": 5, "attributed_revenue": "1000"}, headers=H)
    c = admin.get("/api/ads/summary", params={"period": "today"}).json()["by_campaign"][0]
    assert Decimal(str(c["roas"])) == Decimal("10") and Decimal(str(c["cpa"])) == Decimal("20.00")
    assert Decimal(str(c["conversion_rate"])) == Decimal("0.025") and Decimal(str(c["ctr"])) == Decimal("0.02")


def test_product_profit_after_ads_is_estimate(admin, engine):
    now = datetime.now(timezone.utc)
    seed_order(engine, "TY-A1", now)
    with engine.begin() as c:
        c.execute(text("INSERT INTO products(sku, barcode, name) VALUES ('SKU-1', 'B1', 'Çanta') ON CONFLICT DO NOTHING"))
        pid = c.execute(text("SELECT id FROM products WHERE sku = 'SKU-1'")).scalar()
        c.execute(text("UPDATE order_items SET product_id = :p, unit_cost = 40"), {"p": pid})
    cid = _campaign(admin, product_ids=[pid])
    admin.post("/api/ads/spend", json={"campaign_id": cid, "spend_date": datetime.now(TR).date().isoformat(), "amount": "30"}, headers=H)
    p = admin.get("/api/ads/summary", params={"period": "today"}).json()["products"][0]
    assert p["is_estimate"] and Decimal(str(p["ad_spend"])) == Decimal("30.00")
    assert Decimal(str(p["profit_after_ads"])) == Decimal(str(p["profit_before_ads"])) - Decimal("30")


def test_finance_statement_labels_and_includes_ad_spend(admin, engine):
    seed_order(engine, "TY-F1", datetime.now(timezone.utc))
    cid = _campaign(admin)
    admin.post("/api/ads/spend", json={"campaign_id": cid, "spend_date": datetime.now(TR).date().isoformat(), "amount": "20"}, headers=H)
    admin.post("/api/finance/expenses", json={"category": "rent", "amount": "10", "expense_date": datetime.now(TR).date().isoformat()}, headers=H)
    s = admin.get("/api/finance/statement", params={"period": "this_month"}).json()
    lines = {x["key"]: x for x in s["lines"]}
    assert lines["gross_sales"]["basis_label"] == "Gerçek" and Decimal(str(lines["gross_sales"]["amount"])) == Decimal("100.00")
    assert Decimal(str(lines["ad_spend"]["amount"])) == Decimal("-20.00") and lines["ad_spend"]["basis_label"] == "Girilen"
    assert Decimal(str(lines["other_expenses"]["amount"])) == Decimal("-10.00")
    assert lines["vat_estimate"]["basis_label"] == "Tahmini" and lines["net_profit"]["basis_label"] == "Tahmini"
    assert s["range"]["period"] == "this_month" and s["range"]["from"].endswith("-01")


def test_last_month_period_boundaries(admin):
    r = admin.get("/api/finance/statement", params={"period": "last_month"}).json()["range"]
    today = datetime.now(TR).date()
    first_this = today.replace(day=1)
    assert r["to"] == (first_this - timedelta(days=1)).isoformat()
    assert r["from"] == (first_this - timedelta(days=1)).replace(day=1).isoformat()
    assert admin.get("/api/finance/statement", params={"period": "bogus"}).status_code == 422


def test_csv_exports(admin, engine):
    seed_order(engine, "TY-CSV", datetime.now(timezone.utc))
    cid = _campaign(admin)
    admin.post("/api/ads/spend", json={"campaign_id": cid, "spend_date": datetime.now(TR).date().isoformat(), "amount": "5", "note": "=HACK()"}, headers=H)
    for path in ("/api/finance/statement.csv", "/api/finance/expenses.csv", "/api/finance/orders.csv", "/api/ads/spend.csv"):
        r = admin.get(path, params={"period": "this_month"})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv") and r.text.startswith("﻿"), path
    assert "TY-CSV" in admin.get("/api/finance/orders.csv", params={"period": "this_month"}).text
    assert "'=HACK()" in admin.get("/api/ads/spend.csv", params={"period": "this_month"}).text   # formül enjeksiyonu engellenir


def test_accountant_reads_finance_and_enters_spend_but_nothing_else(accountant, admin, engine):
    # Okuyabilir / dışa aktarabilir
    for path in ("/api/finance/statement", "/api/finance/summary", "/api/finance/expenses", "/api/ads/summary",
                 "/api/finance/orders.csv", "/api/reports/sku", "/api/orders", "/api/alerts"):
        assert accountant.get(path).status_code == 200, path
    # Finans kaydı girebilir (audit'li)
    cid = _campaign(admin)
    r = accountant.post("/api/ads/spend", json={"campaign_id": cid, "spend_date": date.today().isoformat(), "amount": "12"}, headers=H)
    assert r.status_code == 201
    assert accountant.post("/api/finance/expenses", json={"category": "rent", "amount": "10", "expense_date": date.today().isoformat()}, headers=H).status_code == 201
    with engine.begin() as c:
        actions = c.execute(text("SELECT action FROM audit_logs a JOIN users u ON u.id = a.user_id WHERE u.username = 'muhasebe'")).scalars().all()
    assert "ads.spend_created" in actions and "expense.created" in actions
    # Yapamaz: credential, mağaza, yayın, kullanıcı, ayar, senkron, taslak, uyarı çözme
    forbidden = [
        ("GET", "/api/integrations/trendyol/connection", None),
        ("PUT", "/api/integrations/trendyol/connection", {"values": {"trendyol_seller_id": "1"}}),
        ("DELETE", "/api/integrations/trendyol/connection", None),
        ("PUT", "/api/integrations/trendyol/write-permission", {"enabled": False}),
        ("POST", "/api/integrations/trendyol/sync", {}),
        ("POST", "/api/marketplaces", {"code": "yeni_mp", "name": "Yeni"}),
        ("POST", "/api/publish/preview", {"marketplace": "trendyol", "draft_ids": [1]}),
        ("POST", "/api/publish/confirm", {"token": "x" * 30, "confirm": True}),
        ("GET", "/api/users", None),
        ("POST", "/api/users", {"username": "hacker", "role": "admin", "password": "Password-12345"}),
        ("PUT", "/api/settings", {"values": {"stock.low_stock_threshold": 1}}),
        ("POST", "/api/suppliers", {"name": "X", "connection": {"integration_type": "manual"}}),
        ("POST", "/api/transfer/drafts", {"product_ids": [1], "marketplaces": ["trendyol"]}),
        ("POST", "/api/alerts/scan", None),
    ]
    for method, path, body in forbidden:
        r = accountant.request(method, path, json=body, headers=H)
        assert r.status_code == 403, (method, path, r.status_code, r.text)


def test_admin_can_create_accountant_user(admin):
    r = admin.post("/api/users", json={"username": "mali", "role": "accountant", "password": "Accountant-Pass-123"}, headers=H)
    assert r.status_code in (200, 201), r.text
    assert any(u["role"] == "accountant" for u in admin.get("/api/users").json())
