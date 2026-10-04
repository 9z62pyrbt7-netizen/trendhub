"""V3 — ajan zinciri uçtan uca: INPUT → ANALYSIS → DECISION → PROPOSAL/ACTION → RESULT → FEEDBACK.

Gerçek PostgreSQL + gerçek kod yolları (risk motoru, CEO hakemliği, executor, zamanlayıcı). Dış platformlar (Trendyol yazma,
reklam API) yalnızca hata/başarı davranışını kanıtlamak için sahte nesneyle değiştirilir.
"""
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import text

from tests.test_ai import H, campaign, cash, cycle, product, sell

pytestmark = pytest.mark.usefixtures("engine")


@pytest.fixture
def conn(engine):
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        yield c


@pytest.fixture
def api(client_factory):
    c, login = client_factory
    login("admin", "Admin-Password-123")
    return c


def propose(engine, **kw):
    from app.services.ai import proposals
    base = dict(title="test", reason="test", evidence={})
    with engine.begin() as c:
        pid = proposals.propose(c, **{**base, **kw})
        return c.execute(text("SELECT id, status, risk_checks FROM ai_proposals WHERE id = :i"), {"i": pid}).one()


def codes(p) -> set:
    return {c["code"] for c in p.risk_checks if c["severity"] == "block"}


def action_row(conn, proposal_id):
    return conn.execute(text("SELECT status, reason_code, reason FROM ai_actions WHERE dedupe_key = :k"),
                        {"k": f"proposal:{proposal_id}"}).one()


# ======================================================================== KÖK NEDEN: sınıflandırma neden boştu?
def test_root_cause_unmatched_sales_and_late_cost_are_repaired(engine, conn):
    """Satışlar ürüne bağlanmamış (SKU harf/boşluk farkı + yalnızca ilan barkodu) ve maliyet sonradan girilmiş:
    eski kod bu ürünleri hiç / NO_DATA görür. Normalizasyon eşik değiştirmeden düzeltir."""
    from app.services.ai import agents, reconcile
    from app.services.finance_service import recalculate_order
    store = conn.execute(text("INSERT INTO stores(marketplace_id, name, external_id) SELECT id, 'TY', '1' FROM marketplaces WHERE code = 'trendyol' RETURNING id")).scalar()
    a = product(conn, "TC-100", cost="0", price="600", name="Siyah Çanta")
    b = product(conn, "TC-200", cost="150", price="600", name="Bej Çanta")
    conn.execute(text("UPDATE products SET barcode = 'OTHER-BC' WHERE id = :i"), {"i": b})
    conn.execute(text("""INSERT INTO marketplace_listings(product_id, store_id, external_product_id, barcode, sku, title)
                         VALUES (:p, :s, 'X1', '8690000000200', 'ty-200', 'Bej')"""), {"p": b, "s": store})
    for k in range(8):
        for sku, bc in (("  tc-100 ", None), ("ty-200", "8690000000200")):
            oid = conn.execute(text("""INSERT INTO orders(store_id, external_order_id, status, internal_status, order_date)
                                       VALUES (:s, :e, 'Delivered', 'delivered', NOW() - INTERVAL '3 days') RETURNING id"""),
                               {"s": store, "e": f"RC-{sku.strip()}-{k}"}).scalar()
            conn.execute(text("""INSERT INTO order_items(order_id, external_line_id, sku, barcode, product_name, quantity, unit_price, vat_rate)
                                 VALUES (:o, :l, :s, :b, 'x', 1, 600, 20)"""), {"o": oid, "l": f"L{oid}", "s": sku, "b": bc})
            recalculate_order(conn, oid)
    before = {p["product_id"]: p["class"] for p in agents.classified_products(conn)}
    assert a not in before and b not in before                     # satışlar ürün analizinde HİÇ görünmüyor
    cov = reconcile.coverage(conn)
    assert cov["unmatched_lines"] == 16 and "eşleşmiyor" in cov["issues"][0]
    conn.execute(text("UPDATE products SET cost = 150 WHERE id = :i"), {"i": a})     # maliyet SONRADAN girildi
    out = reconcile.normalize(conn)
    assert out["linked_total"] == 16 and out["recalculated_orders"] == 16
    after = {p["product_id"]: p["class"] for p in agents.classified_products(conn)}
    assert after[a] in ("STAR", "PROFITABLE") and after[b] in ("STAR", "PROFITABLE")
    assert reconcile.coverage(conn)["unmatched_lines"] == 0
    assert reconcile.normalize(conn) == {"linked_items": {}, "linked_total": 0, "recalculated_orders": 0}   # idempotent


# ======================================================================== 1. stok 0 → reklam BLOCKED
def test_1_zero_stock_blocks_ads(engine, conn):
    from app.services.ai.growth import ad_candidates
    cash(conn)
    pid = product(conn, "Z-STOCK", cost="100", price="600", stock=0)
    sell(conn, pid, price="600", n=10, days_ago=3, tag="z")
    cand = next(c for c in ad_candidates(conn) if c["product_id"] == pid)
    assert cand["blocked"] == "stok güvenli değil"
    cycle(engine)
    assert conn.execute(text("SELECT COUNT(*) FROM ai_proposals WHERE action_type = 'ads.create_campaign' AND status <> 'blocked'")).scalar() == 0
    blk = conn.execute(text("SELECT status, reason_code FROM ai_actions WHERE agent_code = 'advertising' AND entity_id = :p"), {"p": pid}).one()
    assert blk == ("BLOCKED", "stock_risk")
    p = propose(engine, agent="advertising", action_type="ads.create_campaign", entity_type="product", entity_id=pid, channel="meta",
                params={"daily_budget": 50}, required_capital=350, capital_category="advertising")
    assert p.status == "blocked" and "stock_risk" in codes(p) and action_row(conn, p.id)[0] == "BLOCKED"


# ======================================================================== 2. zarar eden ürün → reklam ölçekleme BLOCKED
def test_2_loss_product_ads_scale_blocked(engine, conn):
    cash(conn)
    pid = product(conn, "LOSS-AD", cost="580", price="600")
    sell(conn, pid, price="600", n=10, days_ago=3, tag="l")
    p = propose(engine, agent="advertising", action_type="ads.create_campaign", entity_type="product", entity_id=pid, channel="meta",
                params={"daily_budget": 50}, required_capital=350, capital_category="advertising")
    assert p.status == "blocked" and "negative_margin" in codes(p)
    cid = campaign(conn, "Zararlı ürün", [pid], budget="100", spend_per_day="100", revenue_per_day="3000", clicks=150)
    q = propose(engine, agent="advertising", action_type="ads.increase_budget", entity_type="campaign", entity_id=cid,
                params={"current_daily_budget": "100", "new_daily_budget": "130", "delta_per_day": "30"}, required_capital=420,
                capital_category="advertising")
    assert q.status == "blocked" and codes(q) & {"negative_margin", "low_margin"}


# ======================================================================== 3. kampanya zarara sokuyor → BLOCKED
def test_3_campaign_that_causes_loss_is_blocked(engine, conn, api):
    from app.services.ai.growth import check_discount
    pid = product(conn, "CMP-1", cost="300", price="600")
    sell(conn, pid, price="600", n=10, days_ago=3, tag="c")
    bad = check_discount(conn, pid, Decimal("0.40"))
    assert bad["status"] == "BLOCKED" and bad["reason_code"] == "campaign_loss"
    safe = bad["max_safe_discount"]
    assert safe and Decimal("0") < safe < Decimal("0.40")
    ok = check_discount(conn, pid, safe)
    assert ok["status"] == "ALLOWED" and ok["unit_profit_after"] >= Decimal("20")
    p = propose(engine, agent="campaign", action_type="campaign.discount", entity_type="product", entity_id=pid,
                params={"discount_rate": "0.40", "discount_pct": 40, "min_price": "360"})
    assert p.status == "blocked" and "campaign_loss" in codes(p)
    r = api.post("/api/ai/campaign/check", json={"product_id": pid, "discount_rate": 0.4}, headers=H).json()
    assert r["status"] == "BLOCKED"


# ======================================================================== 4. maliyet eksik → otomatik fiyat BLOCKED
def test_4_missing_cost_blocks_price_change(engine, conn):
    pid = product(conn, "NOCOST", cost="0", price="300")
    sell(conn, pid, price="300", n=8, days_ago=3, tag="n")
    cycle(engine)
    assert conn.execute(text("SELECT COUNT(*) FROM ai_proposals WHERE action_type = 'pricing.change_price' AND entity_id = :p"),
                        {"p": pid}).scalar() == 0
    a = conn.execute(text("SELECT status, reason_code FROM ai_actions WHERE agent_code = 'pricing' AND entity_id = :p"), {"p": pid}).one()
    assert a == ("BLOCKED", "needs_data")
    p = propose(engine, agent="pricing", action_type="pricing.change_price", entity_type="product", entity_id=pid,
                params={"old_price": "300", "new_price": "320"})
    assert p.status == "blocked" and "needs_data" in codes(p)


# ======================================================================== 5 + 11. API hatası → FAILED, asla EXECUTED
def _approved_price_proposal(engine, conn, api, sku="PR-1"):
    pid = product(conn, sku, cost="300", price="400")
    sell(conn, pid, price="400", n=8, days_ago=3, tag=sku)
    cycle(engine)
    prop = conn.execute(text("SELECT id, params FROM ai_proposals WHERE action_type = 'pricing.change_price' AND entity_id = :p"),
                        {"p": pid}).one()
    assert api.post(f"/api/ai/proposals/{prop.id}/approve", json={"note": "fiyatı düzelt"}, headers=H).status_code == 200
    return pid, prop


def _fake_connector(monkeypatch, *, write=True, fail=None):
    calls = []

    def update_price(barcode, price):
        calls.append((barcode, price))
        if fail:
            raise fail
    from app.connectors import registry
    monkeypatch.setattr(registry, "get_connector", lambda code, settings=None: NS(settings=NS(connector_write_enabled=write),
                                                                                    update_price=update_price))
    return calls


def test_5_and_11_trendyol_api_failure_is_failed_never_executed(engine, conn, api, monkeypatch):
    from app.connectors.base import ConnectorError
    pid, prop = _approved_price_proposal(engine, conn, api)
    old_price = conn.execute(text("SELECT sale_price FROM products WHERE id = :i"), {"i": pid}).scalar()
    calls = _fake_connector(monkeypatch, fail=ConnectorError("HTTP 400: invalid barcode"))
    r = api.post(f"/api/ai/proposals/{prop.id}/execute", headers=H).json()
    assert r["status"] == "FAILED" and "HTTP 400" in r["error"] and calls
    assert conn.execute(text("SELECT status FROM ai_proposals WHERE id = :i"), {"i": prop.id}).scalar() == "failed"
    assert conn.execute(text("SELECT sale_price FROM products WHERE id = :i"), {"i": pid}).scalar() == old_price
    assert conn.execute(text("SELECT COUNT(*) FROM ai_actions WHERE status = 'EXECUTED'")).scalar() == 0
    f = conn.execute(text("SELECT error, result FROM ai_actions WHERE status = 'FAILED' AND proposal_id = :p AND dedupe_key IS NULL"),
                     {"p": prop.id}).one()
    assert "HTTP 400" in f.error and f.result["old_price"] and f.result["new_price"]
    assert conn.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action = 'ai.execute.failed'")).scalar() == 1


def test_11b_write_disabled_or_unconnected_is_skipped_and_success_is_executed(engine, conn, api, monkeypatch):
    pid, prop = _approved_price_proposal(engine, conn, api, "PR-2")
    _fake_connector(monkeypatch, write=False)
    assert api.post(f"/api/ai/proposals/{prop.id}/execute", headers=H).json()["status"] == "SKIPPED"
    assert conn.execute(text("SELECT status FROM ai_proposals WHERE id = :i"), {"i": prop.id}).scalar() == "approved"
    calls = _fake_connector(monkeypatch, write=True)
    r = api.post(f"/api/ai/proposals/{prop.id}/execute", headers=H).json()
    assert r["status"] == "EXECUTED" and calls == [("PR-2", Decimal(prop.params["new_price"]))]
    assert conn.execute(text("SELECT sale_price FROM products WHERE id = :i"), {"i": pid}).scalar() == Decimal(prop.params["new_price"])
    audit = conn.execute(text("SELECT details FROM audit_logs WHERE action = 'price.changed'")).scalar()
    assert audit["old_price"] and audit["new_price"] and audit["expected_unit_profit_new"]
    assert api.post(f"/api/ai/proposals/{prop.id}/execute", headers=H).status_code == 409     # ikinci kez uygulanmaz
    # Reklam: platform bağlı değil → SKIPPED (sahte EXECUTED yok)
    cash(conn)
    p2 = product(conn, "AD-OK", cost="100", price="600")
    sell(conn, p2, price="600", n=10, days_ago=3, tag="ad")
    ads = propose(engine, agent="advertising", action_type="ads.create_campaign", entity_type="product", entity_id=p2, channel="meta",
                  params={"daily_budget": 50}, required_capital=350, capital_category="advertising")
    assert ads.status == "pending_approval"
    api.post(f"/api/ai/proposals/{ads.id}/approve", json={"note": "test kampanyası"}, headers=H)
    r = api.post(f"/api/ai/proposals/{ads.id}/execute", headers=H).json()
    assert r["status"] == "SKIPPED" and "Meta Marketing API bağlı değil" in r["reason"]
    assert conn.execute(text("SELECT COUNT(*) FROM ai_actions WHERE status = 'EXECUTED' AND agent_code = 'advertising'")).scalar() == 0


# ======================================================================== 6. bütçe limiti → BLOCKED
def test_6_budget_and_spend_authority_limits(engine, conn, api):
    cash(conn)
    pid = product(conn, "BUD-1", cost="100", price="600")
    sell(conn, pid, price="600", n=10, days_ago=3, tag="b")
    cid = campaign(conn, "Bütçe", [pid], budget="1400", spend_per_day="1400", revenue_per_day="20000", clicks=150)
    p = propose(engine, agent="advertising", action_type="ads.increase_budget", entity_type="campaign", entity_id=cid,
                params={"current_daily_budget": "1400", "new_daily_budget": "1550", "delta_per_day": "150"}, required_capital=2100,
                capital_category="advertising")
    assert p.status == "blocked" and "budget_limit" in codes(p) and action_row(conn, p.id)[1] == "budget_limit"
    x = propose(engine, agent="pricing", action_type="pricing.change_price", entity_type="product", entity_id=pid,
                params={"old_price": "600", "new_price": "620"}, required_capital=100)
    assert "unauthorized_spend" in codes(x)
    api.put("/api/ai/settings", json={"thresholds": {"daily_ad_budget_limit": 100000}}, headers=H)
    y = propose(engine, agent="advertising", action_type="ads.create_campaign", entity_type="product", entity_id=pid, channel="meta",
                params={"daily_budget": 900}, required_capital=6300, capital_category="advertising")
    assert "agent_spend_limit" in codes(y)


# ======================================================================== 7. kârlı ürün → doğru öneri zinciri
def test_7_profitable_product_gets_correct_proposals(engine, conn):
    cash(conn)
    star = product(conn, "STAR-1", cost="150", price="600", name="Kapitone Omuz Çantası")
    sell(conn, star, price="600", n=20, days_ago=3, tag="s")
    thin = product(conn, "THIN-1", cost="300", price="400", name="İnce Marjlı Sırt Çantası")
    sell(conn, thin, price="400", n=8, days_ago=3, tag="t")
    out = cycle(engine)
    ad = conn.execute(text("SELECT status, params, required_capital FROM ai_proposals WHERE action_type = 'ads.create_campaign' AND entity_id = :p"),
                      {"p": star}).one()
    assert ad.status == "pending_approval" and Decimal(ad.params["daily_budget"]) > 0
    assert Decimal(ad.params["max_cpa"]) > Decimal("100")                       # birim net kâr = sipariş başına reklam tavanı
    mk = conn.execute(text("SELECT params FROM ai_proposals WHERE action_type = 'marketing.plan' AND entity_id = :p"), {"p": star}).scalar()
    assert "Çalışan 25–45" in mk["audience"] and mk["offer"]
    so = conn.execute(text("SELECT params FROM ai_proposals WHERE action_type = 'social.content_plan' AND entity_id = :p"), {"p": star}).scalar()
    assert so["hook"] and so["cta"] and so["creative_brief"] and "YAYINLANMADI" in so["status_note"]
    pr = conn.execute(text("SELECT params, status FROM ai_proposals WHERE action_type = 'pricing.change_price' AND entity_id = :p"),
                      {"p": thin}).one()
    old, new = Decimal(pr.params["old_price"]), Decimal(pr.params["new_price"])
    assert pr.status == "pending_approval" and old < new <= old * Decimal("1.10") + Decimal("0.01")
    assert Decimal(pr.params["unit_profit_new"]) > Decimal(pr.params["unit_profit_old"])
    assert conn.execute(text("SELECT COUNT(*) FROM ai_proposals WHERE action_type = 'ads.create_campaign' AND entity_id = :p"),
                        {"p": thin}).scalar() == 0                              # marjı ince ürüne reklam önerilmez
    s = out["summary"]["sections"]
    assert any("Kapitone" in x for x in s["HANGİ ÜRÜNLER İYİ?"]) and any("Kapitone" in x for x in s["REKLAM İÇİN EN İYİ ÜRÜNLER?"])
    assert len(s) == 11 and s["SONRAKİ EN ÖNEMLİ 3 İŞ?"]


# ======================================================================== 8. CEO çelişen kararlar → güvenli karar
def test_8_ceo_resolves_conflicting_agent_decisions(engine, conn, api):
    from app.services.ai.ceo_review import resolve_conflicts
    cash(conn)
    pid = product(conn, "CONF-1", cost="150", price="600")
    sell(conn, pid, price="600", n=12, days_ago=3, tag="c")
    disc = propose(engine, agent="campaign", action_type="campaign.discount", entity_type="product", entity_id=pid,
                   params={"discount_rate": "0.05", "discount_pct": 5, "min_price": "570"})
    raise_ = propose(engine, agent="pricing", action_type="pricing.change_price", entity_type="product", entity_id=pid,
                     params={"old_price": "600", "new_price": "640"})
    assert disc.status == "pending_approval" and raise_.status == "pending_approval"
    cid = campaign(conn, "Stoklu", [pid], budget="200", spend_per_day="200", revenue_per_day="3000", clicks=150)
    inc = propose(engine, agent="advertising", action_type="ads.increase_budget", entity_type="campaign", entity_id=cid,
                  params={"current_daily_budget": "200", "new_daily_budget": "260", "delta_per_day": "60"}, required_capital=840,
                  capital_category="advertising")
    assert inc.status == "pending_approval"
    assert api.post(f"/api/ai/proposals/{inc.id}/approve", json={"note": "artıralım"}, headers=H).status_code == 200
    with engine.begin() as c:
        out = resolve_conflicts(c)
    assert {o["proposal_id"] for o in out} == {disc.id}
    assert conn.execute(text("SELECT status FROM ai_proposals WHERE id = :i"), {"i": disc.id}).scalar() == "blocked"
    # Stok ajanı "kritik" der → onaylanmış reklam artışı bile uygulanmadan CEO tarafından engellenir
    conn.execute(text("UPDATE products SET stock = 1 WHERE id = :i"), {"i": pid})
    with engine.begin() as c:
        out = resolve_conflicts(c)
    assert inc.id in {o["proposal_id"] for o in out}
    assert conn.execute(text("SELECT status FROM ai_proposals WHERE id = :i"), {"i": inc.id}).scalar() == "blocked"
    ceo = conn.execute(text("SELECT status, reason_code FROM ai_actions WHERE dedupe_key = :k"), {"k": f"conflict:{inc.id}"}).one()
    assert ceo == ("BLOCKED", "stock_risk")
    assert api.post(f"/api/ai/proposals/{inc.id}/execute", headers=H).status_code == 409


# ======================================================================== 9. tekrar eden döngü → tekrar eden aksiyon yok
def test_9_duplicate_cycle_creates_no_duplicate_actions(engine, conn):
    from app.services.ai import ceo
    cash(conn)
    pid = product(conn, "DUP-1", cost="150", price="600")
    sell(conn, pid, price="600", n=20, days_ago=3, tag="d")
    cycle(engine)
    n_prop = conn.execute(text("SELECT COUNT(*) FROM ai_proposals WHERE status IN ('pending_approval', 'blocked')")).scalar()
    n_act = conn.execute(text("SELECT COUNT(*) FROM ai_actions")).scalar()
    cycle(engine)
    assert conn.execute(text("SELECT COUNT(*) FROM ai_proposals WHERE status IN ('pending_approval', 'blocked')")).scalar() == n_prop
    assert conn.execute(text("SELECT COUNT(*) FROM ai_actions")).scalar() == n_act
    with engine.connect() as other:                      # eşzamanlı döngü: kilit tutuluyor
        other.execute(text("SELECT pg_advisory_lock(:k)"), {"k": ceo.CYCLE_LOCK})
        other.commit()
        assert "Başka bir AI döngüsü" in cycle(engine)["skipped"]
        other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": ceo.CYCLE_LOCK})
        other.commit()
    assert "skipped" not in cycle(engine)


# ======================================================================== 10. yeniden başlatma → zamanlayıcı devam
def test_10_scheduler_survives_restart_without_duplicates(engine, conn):
    from app.services import sync_service
    from app.services.ai.config import TZ
    from app.worker import Worker
    morning = datetime(2026, 10, 5, 9, 0, tzinfo=TZ)            # pazartesi
    with engine.begin() as c:
        assert len(sync_service.schedule_ai_reviews(c, morning)) == 2     # günlük + haftalık
        assert sync_service.schedule_ai_reviews(c, morning) == []
    w1 = Worker(engine)                                          # "yeniden başlatılmış" worker
    while w1.run_once():
        pass
    st = dict(conn.execute(text("SELECT job_type, status FROM sync_jobs WHERE job_type LIKE 'ai.%review'")).all())
    assert st == {"ai.daily_review": "succeeded", "ai.weekly_review": "succeeded"}
    w2 = Worker(engine)
    with engine.begin() as c:
        assert sync_service.schedule_ai_reviews(c, morning) == []   # aynı gün tekrar yok (restart sonrası da)
        assert sync_service.schedule_ai_reviews(c, datetime(2026, 10, 5, 6, 0, tzinfo=TZ)) == []   # 08:00 öncesi yok
        assert len(sync_service.schedule_ai_reviews(c, datetime(2026, 10, 6, 9, 0, tzinfo=TZ))) == 1   # ertesi gün (salı)
    assert w2.run_once() is True
    assert conn.execute(text("SELECT summary IS NOT NULL FROM ai_briefs ORDER BY brief_date DESC LIMIT 1")).scalar()


# ======================================================================== 12. net kâr hesabı matematik
def test_12_net_profit_math_is_exact(engine, conn):
    from app.services.ai.data import product_economics
    from app.services.ai.economics import simulate, unit_economics
    from app.services.ai.config import Window
    conn.execute(text("UPDATE app_settings SET value = '50' WHERE key = 'finance.default_shipping_cost'"))
    conn.execute(text("UPDATE app_settings SET value = '10' WHERE key = 'finance.service_fee_per_order'"))
    pid = product(conn, "MATH-1", cost="400", price="1000")
    sell(conn, pid, price="1000", n=1, days_ago=2, tag="m")
    o = conn.execute(text("SELECT gross_revenue, product_cost, commission, shipping_cost, service_fee, net_profit FROM orders")).one()
    # 1000 − 400 − 200 (%20 komisyon) − 50 kargo − 10 hizmet = 340 (KDV öncesi)
    assert o == (Decimal("1000.00"), Decimal("400.00"), Decimal("200.00"), Decimal("50.00"), Decimal("10.00"), Decimal("340.00"))
    e = next(iter(product_economics(conn, Window(30), [pid])))
    # tahmini KDV = (1000 − 400) × 20/120 = 100 → net kâr 240
    assert e["vat_estimate"] == Decimal("100.00") and e["net_profit"] == Decimal("240.00")
    u = unit_economics(conn, pid)
    assert u["unit_profit"] == Decimal("240.00") and u["commission_rate"] == Decimal("0.2000")
    # 1100 TL: 1100 − 400 − 220 − 50 − 10 − (700 × 20/120 = 116,67) = 303,33
    assert simulate(u, Decimal("1100"))["unit_profit"] == Decimal("303.33")


# ======================================================================== CEO özeti + komuta merkezi
def test_command_center_and_summary(engine, conn, api):
    cash(conn)
    pid = product(conn, "CMD-1", cost="150", price="600")
    sell(conn, pid, price="600", n=10, days_ago=0, tag="cmd")
    assert api.post("/api/ai/run", headers=H).status_code == 200
    d = api.get("/api/ai/command").json()
    assert d["ceo"]["last_cycle"]["status"] in ("ok", "degraded")
    assert {a["code"] for a in d["agents"]} >= {"pricing", "campaign", "product_tracking", "marketing", "social_media"}
    assert set(d["actions"]["counts_24h"]) == {"EXECUTED", "PROPOSED", "BLOCKED", "FAILED", "SKIPPED"}
    assert d["budget"]["limit"] == 1500 and "coverage" in d and d["ad_candidates"]
    assert "BUGÜN NE OLDU?" in d["summary"]["sections"]
    assert any(p["agent"] == "pricing" for p in d["performance"])
    assert {x["code"]: x["connected"] for x in d["ad_platforms"]} == {"meta": False, "trendyol_ads": False}
    assert api.get("/api/ai/actions", params={"status": "PROPOSED"}).json()["items"]


def test_ceo_chat_gives_executive_summary_in_turkish(engine, conn):
    from app.services.ai.chat import rules_answer
    cash(conn)
    pid = product(conn, "CHAT-1", cost="150", price="600", name="Sohbet Çantası")
    sell(conn, pid, price="600", n=10, days_ago=1, tag="ch")
    cycle(engine)
    ans, used = rules_answer(conn, "Yönetici özeti ver")
    assert used == ["get_executive_summary"]
    for h in ("BUGÜN NE OLDU?", "NET KÂR", "HANGİ ÜRÜNLER İYİ?", "BENDEN ONAY BEKLEYENLER?", "SONRAKİ EN ÖNEMLİ 3 İŞ?"):
        assert h in ans
    assert "TAHMİNİ" in ans and "Sohbet Çantası" in ans


def test_summary_order_survives_jsonb_roundtrip(engine, conn, api):
    """Regresyon (Docker çalışma anında görüldü): JSONB anahtar sırasını karıştırır; özet her zaman aynı sırada gelmeli."""
    from app.services.ai.ceo_review import ordered_sections
    cash(conn)
    pid = product(conn, "ORD-1", cost="150", price="600")
    sell(conn, pid, price="600", n=5, days_ago=1, tag="o")
    cycle(engine)
    stored = api.get("/api/ai/command").json()["summary"]
    titles = [h for h, _ in ordered_sections(stored)]
    assert titles[0] == "BUGÜN NE OLDU?" and titles[-1] == "SONRAKİ EN ÖNEMLİ 3 İŞ?" and len(titles) == 11


# ======================================================================== production reklam atıf düzeltmesi (korunmalı)
def _attribution_schema(conn):
    """Production şemasını taklit eder (0013_ad_attribution): ad_performance + doğrudan/dolaylı kolonlar."""
    for col in ("direct_orders INTEGER", "indirect_orders INTEGER", "direct_revenue NUMERIC(14,2)", "indirect_revenue NUMERIC(14,2)"):
        conn.execute(text(f"ALTER TABLE ad_performance ADD COLUMN IF NOT EXISTS {col}"))


@pytest.fixture
def attribution_schema(engine):
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        _attribution_schema(c)
    yield
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        for col in ("direct_orders", "indirect_orders", "direct_revenue", "indirect_revenue"):
            c.execute(text(f"ALTER TABLE ad_performance DROP COLUMN IF EXISTS {col}"))


def _real_campaign(conn, pid, *, direct_orders, indirect_orders, direct_revenue, indirect_revenue):
    """Canlıdaki gerçek kampanya: 'Ürün-30.09.2026 20:42' — harcama 2270.76, atfedilen ciro 5795, ROAS 2.5520."""
    acc = conn.execute(text("INSERT INTO ad_accounts(channel, name) VALUES ('trendyol_ads', 'TY') RETURNING id")).scalar()
    cid = conn.execute(text("INSERT INTO ad_campaigns(account_id, name, daily_budget) VALUES (:a, 'Ürün-30.09.2026 20:42', 400) RETURNING id"),
                       {"a": acc}).scalar()
    conn.execute(text("INSERT INTO ad_campaign_products(campaign_id, product_id) VALUES (:c, :p)"), {"c": cid, "p": pid})
    conn.execute(text("INSERT INTO ad_spend(campaign_id, spend_date, amount) VALUES (:c, CURRENT_DATE - 1, 2270.76)"), {"c": cid})
    conn.execute(text("""INSERT INTO ad_performance(campaign_id, perf_date, impressions, clicks, attributed_orders, attributed_revenue,
                                                    direct_orders, indirect_orders, direct_revenue, indirect_revenue, source)
                         VALUES (:c, CURRENT_DATE - 1, 40000, 900, :n, 5795, :do, :io, :dr, :ir, 'csv')"""),
                 {"c": cid, "n": direct_orders + indirect_orders, "do": direct_orders, "io": indirect_orders, "dr": direct_revenue,
                  "ir": indirect_revenue})
    return cid


def test_all_indirect_sales_campaign_is_insufficient_data_not_pause(engine, conn, attribution_schema):
    from app.services.ai import agents
    cash(conn)
    pid = product(conn, "ADV-REAL", cost="500", price="1159")
    sell(conn, pid, price="1159", n=5, days_ago=2, tag="adv")
    cid = _real_campaign(conn, pid, direct_orders=0, indirect_orders=5, direct_revenue=0, indirect_revenue=5795)
    c = next(x for x in agents.advertising_analysis(conn) if x["id"] == cid)
    assert c["roas"] == Decimal("2.5520") and c["direct_orders"] == 0 and c["indirect_orders"] == 5
    assert c["verdict"] == "INSUFFICIENT_DATA" and "Dolaylı satış ürün kârlılığının kanıtı" in c["verdict_reason"]
    cycle(engine)
    assert conn.execute(text("SELECT COUNT(*) FROM ai_proposals WHERE entity_type = 'campaign' AND entity_id = :c"
                             " AND action_type IN ('ads.pause', 'ads.increase_budget', 'ads.decrease_budget')"), {"c": cid}).scalar() == 0


def test_mixed_attribution_uses_only_direct_revenue_as_profit_evidence(engine, conn, attribution_schema):
    from app.services.ai import agents
    pid = product(conn, "ADV-MIX", cost="500", price="1159")
    sell(conn, pid, price="1159", n=5, days_ago=2, tag="mix")
    cid = _real_campaign(conn, pid, direct_orders=1, indirect_orders=4, direct_revenue=1159, indirect_revenue=4636)
    c = next(x for x in agents.advertising_analysis(conn) if x["id"] == cid)
    assert c["roas"] == Decimal("2.5520")                                    # platform ROAS toplam atıfla gösterilir
    assert c["contribution_before_ads"] < Decimal("1159")                    # kâr kanıtı yalnızca doğrudan 1159 TL üzerinden
    assert c["ad_net_profit"] == c["contribution_before_ads"] - Decimal("2270.76")


def test_without_attribution_columns_behaviour_is_unchanged(engine, conn):
    from app.services.ai.data import has_attribution_columns
    assert has_attribution_columns(conn) is False
