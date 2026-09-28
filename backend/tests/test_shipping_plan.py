"""Kargoya verilme günü tahmini: Çanta Bayim kesim saati kuralı, Türkiye saati, sınır değerler."""
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from app.connectors.base import NormalizedLine, NormalizedOrder
from app.domain import shipping_plan as SP

TR = ZoneInfo("Europe/Istanbul")
NOW = datetime(2026, 9, 28, 9, 30, tzinfo=TR)        # "şimdi": 28 Eylül 2026, 09:30 TR


def tr(h, m, s=0, day=28):
    return datetime(2026, 9, day, h, m, s, tzinfo=TR)


def p(order_dt, now=NOW, **kw):
    kw.setdefault("supplier_code", "canta_bayim")
    return SP.plan(order_dt, now, **kw)


@pytest.mark.parametrize("h,m,s,code,label", [
    (10, 59, 0, "today", "Bugün kargoya verilecek · 28 Eyl"),
    (10, 59, 59, "today", "Bugün kargoya verilecek · 28 Eyl"),
    (11, 0, 0, "unknown", "Kargo günü belirsiz"),      # 11:00 "11:00'dan önce" DEĞİL
    (11, 59, 0, "unknown", "Kargo günü belirsiz"),
    (11, 59, 59, "unknown", "Kargo günü belirsiz"),
    (12, 0, 0, "tomorrow", "Yarın kargoya verilecek · 29 Eyl"),
    (12, 1, 0, "tomorrow", "Yarın kargoya verilecek · 29 Eyl"),
])
def test_cutoff_boundaries(h, m, s, code, label):
    r = p(tr(h, m, s))
    assert (r["code"], r["label"]) == (code, label)
    assert r["is_estimate"] is True
    if code == "unknown":
        assert r["date"] is None and r["window"] == "undefined"
        assert "11:00–11:59 arası için doğrulanmış kural yok" in r["rule_note"]


def test_near_midnight():
    # 23:59 TR -> ertesi gün
    r = p(tr(23, 59, 59), now=tr(23, 59, 59))
    assert (r["code"], r["date"]) == ("tomorrow", "2026-09-29")
    # 00:01 TR (bir önceki gün 21:01 UTC) -> aynı TR günü
    r = p(datetime(2026, 9, 27, 21, 1, tzinfo=timezone.utc), now=tr(8, 0))
    assert (r["code"], r["date"], r["order_time_tr"]) == ("today", "2026-09-28", "00:01")


def test_utc_to_istanbul_conversion():
    # 07:59 UTC = 10:59 TR -> bugün ; 08:00 UTC = 11:00 TR -> belirsiz ; 09:00 UTC = 12:00 TR -> yarın
    assert p(datetime(2026, 9, 28, 7, 59, tzinfo=timezone.utc))["code"] == "today"
    assert p(datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc))["code"] == "unknown"
    assert p(datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc))["code"] == "tomorrow"
    # Saat dilimi olmayan değer UTC kabul edilir (DB TIMESTAMPTZ)
    assert p(datetime(2026, 9, 28, 9, 0))["order_time_tr"] == "12:00"
    # "Şimdi" UTC olarak verilse de gün Türkiye saatine göre belirlenir: 21:30 UTC = 00:30 TR (ertesi gün)
    r = p(tr(10, 0), now=datetime(2026, 9, 28, 21, 30, tzinfo=timezone.utc))
    assert (r["code"], r["label"]) == ("past", "Planlanan kargo günü geçti · 28 Eyl")


def test_relative_label_follows_current_day():
    order = tr(15, 0, day=27)                                     # 27 Eyl 15:00 -> 28 Eyl planı
    assert p(order, now=tr(9, 0))["label"] == "Bugün kargoya verilecek · 28 Eyl"
    assert p(order, now=tr(20, 0, day=27))["label"] == "Yarın kargoya verilecek · 28 Eyl"
    assert p(order, now=tr(9, 0, day=30))["code"] == "past"


def test_weekend_is_not_adjusted_but_flagged():
    sat = datetime(2026, 10, 2, 13, 0, tzinfo=TR)                 # Cuma 13:00 -> Cumartesi
    r = p(sat, now=sat)
    assert r["date"] == "2026-10-03" and r["code"] == "tomorrow"   # iş günü hesabı yok
    assert "Hafta sonu" in r["weekend_note"]
    assert p(tr(9, 0))["weekend_note"] is None


def test_not_applicable_or_no_rule():
    assert p(tr(9, 0), status="shipped")["code"] == "not_applicable"
    assert p(tr(9, 0), status="cancelled")["label"] == "—"
    assert p(tr(9, 0), status="awaiting_shipment")["code"] == "today"
    assert p(tr(9, 0), supplier_code="baska_tedarikci")["code"] == "no_rule"
    assert p(tr(9, 0), supplier_code=None)["code"] == "no_rule"
    assert p(None)["code"] == "no_date"


# ---------------------------------------------------------------- API
H = {"X-Requested-With": "TrendHub"}


def _seed(engine, number, when_utc, status="new"):
    from app.services.orders_sync import ensure_store, upsert_orders
    with engine.begin() as c:
        store = ensure_store(c, "trendyol", "1", "Trendyol")
        upsert_orders(c, store, [NormalizedOrder(
            external_order_id=number, marketplace_status="Created", internal_status=status, order_date=when_utc,
            lines=[NormalizedLine("L-" + number, "SKU-1", "B1", "Çanta", 1, Decimal("100"))])])


def test_orders_api_returns_backend_computed_plan_without_side_effects(client_factory, engine):
    client, login = client_factory
    login("admin", "Admin-Password-123")
    now = datetime.now(timezone.utc)
    today_tr = now.astimezone(TR).date()
    morning = datetime(today_tr.year, today_tr.month, today_tr.day, 9, 15, tzinfo=TR).astimezone(timezone.utc)
    noon = datetime(today_tr.year, today_tr.month, today_tr.day, 11, 30, tzinfo=TR).astimezone(timezone.utc)
    _seed(engine, "TY-MORNING", morning)
    _seed(engine, "TY-NOON", noon)
    _seed(engine, "TY-SHIPPED", morning, status="shipped")
    with engine.begin() as c:
        before = c.execute(text("SELECT external_order_id, status, internal_status FROM orders ORDER BY id")).all()
        jobs_before = c.execute(text("SELECT COUNT(*) FROM sync_jobs")).scalar()
    items = {o["external_order_id"]: o for o in client.get("/api/orders").json()["items"]}
    m = items["TY-MORNING"]["shipping_plan"]
    expected_code = "today" if now.astimezone(TR).date() == today_tr else "past"
    assert m["code"] == expected_code and m["date"] == today_tr.isoformat() and m["supplier_code"] == "canta_bayim"
    assert items["TY-NOON"]["shipping_plan"]["label"] == "Kargo günü belirsiz"
    assert items["TY-SHIPPED"]["shipping_plan"]["code"] == "not_applicable"
    detail = client.get(f"/api/orders/{items['TY-MORNING']['id']}").json()
    assert detail["shipping_plan"]["date"] == today_tr.isoformat()
    # Tahmin hiçbir şeyi değiştirmez ve iş kuyruğuna/pazaryerine istek eklemez
    with engine.begin() as c:
        after = c.execute(text("SELECT external_order_id, status, internal_status FROM orders ORDER BY id")).all()
        assert after == before and c.execute(text("SELECT COUNT(*) FROM sync_jobs")).scalar() == jobs_before
    # Varsayılan tedarikçi kapatılırsa kural uygulanmaz
    with engine.begin() as c:
        c.execute(text("""INSERT INTO app_settings(key, value) VALUES ('shipping_plan.default_supplier_code', '""')
                          ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"""))
    try:
        items = {o["external_order_id"]: o for o in client.get("/api/orders").json()["items"]}
        assert items["TY-MORNING"]["shipping_plan"]["code"] == "no_rule"
    finally:
        with engine.begin() as c:
            c.execute(text("DELETE FROM app_settings WHERE key = 'shipping_plan.default_supplier_code'"))


def test_cutoff_is_configurable_from_settings(client_factory, engine):
    client, login = client_factory
    login("admin", "Admin-Password-123")
    s = {i["key"]: i for i in client.get("/api/settings").json()["items"]}
    assert s["shipping.same_day_before"]["value"] == "11:00" and s["shipping.next_day_from"]["value"] == "12:00"
    assert s["shipping.same_day_before"]["group"] == "Kargo planı"
    now = datetime.now(timezone.utc)
    d = now.astimezone(TR).date()
    _seed(engine, "TY-1130", datetime(d.year, d.month, d.day, 11, 30, tzinfo=TR).astimezone(timezone.utc))
    get = lambda: {o["external_order_id"]: o for o in client.get("/api/orders").json()["items"]}["TY-1130"]["shipping_plan"]  # noqa: E731
    assert get()["code"] == "unknown"
    # Kesim saati 12:00 seçilirse belirsiz aralık kalmaz: 11:30 -> bugün
    r = client.put("/api/settings", json={"values": {"shipping.same_day_before": "12:00"}}, headers=H)
    assert r.status_code == 200
    p = get()
    assert p["date"] == d.isoformat() and "12:00 öncesi aynı gün" in p["rule_note"] and "doğrulanmış" not in p["rule_note"]
    # Kesim 11:00, ertesi gün 11:00 -> 11:30 yarın
    client.put("/api/settings", json={"values": {"shipping.same_day_before": "11:00", "shipping.next_day_from": "11:00"}}, headers=H)
    assert get()["window"] == "after_cutoff"
    # Geçersiz: kesim saati > ertesi gün saati; bozuk saat biçimi
    assert client.put("/api/settings", json={"values": {"shipping.same_day_before": "13:00"}}, headers=H).status_code == 422
    assert client.put("/api/settings", json={"values": {"shipping.next_day_from": "25:00"}}, headers=H).status_code == 422
    with engine.begin() as c:
        assert "shipping.same_day_before" in str(c.execute(text(
            "SELECT details FROM audit_logs WHERE action = 'settings.updated' ORDER BY id DESC LIMIT 1")).scalar())
