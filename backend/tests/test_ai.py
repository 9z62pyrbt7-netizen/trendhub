"""AI Control Center: gerçek finans formülleriyle (finance_service.recalculate_order + finance_view) ajanlar, risk motoru,
onay akışı, acil durdurma, sermaye, karar günlüğü / sonuç ölçümü, CEO sohbeti ve yetkiler. Dış servise istek yapılmaz."""
import json
from decimal import Decimal

import pytest
from sqlalchemy import text

H = {"X-Requested-With": "TrendHub"}


@pytest.fixture
def conn(engine):
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        yield c


def product(c, sku, *, cost="200", price="500", stock=50, name=None):
    return c.execute(text("""
        INSERT INTO products(sku, barcode, name, category, images, cost, sale_price, stock, vat_rate, is_active,
                             stock_updated_at, created_at, updated_at)
        VALUES (:s, :s, :n, 'Kadın > Çanta', '[]'::jsonb, :c, :p, :st, 20, TRUE, NOW() - INTERVAL '120 days',
                NOW() - INTERVAL '120 days', NOW()) RETURNING id"""),
        {"s": sku, "n": name or f"Çanta {sku}", "c": cost, "p": price, "st": stock}).scalar()


def sell(c, pid, *, qty=1, price="500", days_ago=1, status="delivered", n=1, tag=""):
    """Trendyol siparişi + kalem; finans alanları gerçek formülle (recalculate_order) hesaplanır."""
    from app.services.finance_service import recalculate_order
    store = c.execute(text("SELECT s.id FROM stores s JOIN marketplaces m ON m.id = s.marketplace_id WHERE m.code = 'trendyol' LIMIT 1")).scalar() \
        or c.execute(text("INSERT INTO stores(marketplace_id, name, external_id) SELECT id, 'TY', '1' FROM marketplaces WHERE code = 'trendyol' RETURNING id")).scalar()
    for k in range(n):
        oid = c.execute(text("""INSERT INTO orders(store_id, external_order_id, status, internal_status, gross_revenue, order_date)
                                VALUES (:s, :e, 'Delivered', :st, 0, NOW() - make_interval(days => :d) - INTERVAL '2 hours') RETURNING id"""),
                        {"s": store, "e": f"O{pid}-{days_ago}-{k}-{tag}", "st": status, "d": days_ago}).scalar()
        c.execute(text("""INSERT INTO order_items(order_id, product_id, external_line_id, sku, product_name, quantity, unit_price, vat_rate)
                          VALUES (:o, :p, :l, (SELECT sku FROM products WHERE id = :p), (SELECT name FROM products WHERE id = :p), :q, :pr, 20)"""),
                  {"o": oid, "p": pid, "l": f"L{oid}", "q": qty, "pr": price})
        recalculate_order(c, oid)


def campaign(c, name, pids, *, budget="100", spend_per_day="100", days=7, clicks=60, revenue_per_day=None, perf=True):
    acc = c.execute(text("""INSERT INTO ad_accounts(channel, name) VALUES ('trendyol_ads', 'Ana') ON CONFLICT (channel, name)
                            DO UPDATE SET name = EXCLUDED.name RETURNING id""")).scalar()
    cid = c.execute(text("INSERT INTO ad_campaigns(account_id, name, daily_budget) VALUES (:a, :n, :b) RETURNING id"),
                    {"a": acc, "n": name, "b": budget}).scalar()
    for p in pids:
        c.execute(text("INSERT INTO ad_campaign_products(campaign_id, product_id) VALUES (:c, :p)"), {"c": cid, "p": p})
    for i in range(days):
        c.execute(text("INSERT INTO ad_spend(campaign_id, spend_date, amount) VALUES (:c, CURRENT_DATE - :i, :a)"),
                  {"c": cid, "i": i, "a": spend_per_day})
        if perf:
            c.execute(text("""INSERT INTO ad_performance(campaign_id, perf_date, impressions, clicks, attributed_orders, attributed_revenue)
                              VALUES (:c, CURRENT_DATE - :i, 5000, :cl, 3, :r)"""),
                      {"c": cid, "i": i, "cl": clicks, "r": revenue_per_day})
    return cid


def cycle(engine):
    from app.services.ai.ceo import run_cycle
    return run_cycle(engine, trigger="test")


def admin(client_factory):
    c, login = client_factory
    login("admin", "Admin-Password-123")
    return c


# ------------------------------------------------------------------ ürün & kâr
def test_product_classification_uses_real_finance(engine, conn):
    from app.services.ai import agents
    star = product(conn, "STAR", cost="150", price="600")
    loss = product(conn, "LOSS", cost="95", price="100")
    few = product(conn, "FEW")
    nocost = product(conn, "NOCOST", cost=None)
    sell(conn, star, price="600", n=12, tag="a")
    sell(conn, loss, price="100", n=5, tag="b")
    sell(conn, few, n=1, tag="c")
    sell(conn, nocost, n=4, tag="d")
    items = {i["product_id"]: i for i in agents.classified_products(conn)}
    assert items[star]["class"] == "STAR" and items[star]["net_profit"] > 1000
    assert items[loss]["class"] == "LOSS" and items[loss]["net_profit"] < 0
    assert items[few]["class"] == "NO_DATA" and items[nocost]["class"] == "NO_DATA" and items[nocost]["missing_cost"]
    # Ürün net kârı = sipariş ekranıyla aynı formül (kalem KDV sonrası kâr), reklam yokken birebir
    from app.services import finance_view
    cfg = finance_view.load(conn)
    direct = conn.execute(text(f"""SELECT SUM(fi.profit_before_vat - fi.vat_estimate) FROM order_items i JOIN orders o ON o.id = i.order_id
                                   LEFT JOIN products p ON p.id = i.product_id CROSS JOIN LATERAL (SELECT {finance_view.item_columns(cfg)}) fi
                                   WHERE i.product_id = :p"""), {"p": star, **cfg.params()}).scalar()
    assert items[star]["net_profit"] == finance_view.q2(direct)

    out = cycle(engine)
    assert out["product_profit"]["classes"]["LOSS"] == 1
    props = {(r.action_type, r.entity_id): r for r in conn.execute(text("SELECT * FROM ai_proposals"))}
    assert ("product.review_loss", loss) in props and ("product.fix_missing_cost", nocost) in props
    assert props[("product.review_loss", loss)].requires_approval is False
    # Tekrar çalıştırma aynı öneriyi çoğaltmaz
    cycle(engine)
    assert conn.execute(text("SELECT COUNT(*) FROM ai_proposals WHERE action_type = 'product.review_loss'")).scalar() == 1


# ------------------------------------------------------------------ reklam
def test_ads_decisions_use_net_profit_not_roas(engine, conn):
    from app.services.ai import agents
    good = product(conn, "AD-GOOD", cost="150", price="600")
    thin = product(conn, "AD-THIN", cost="276", price="600")   # reklam öncesi net marj ≈ %25
    sell(conn, good, price="600", n=10, tag="g")
    sell(conn, thin, price="600", n=10, tag="t")
    c_good = campaign(conn, "İyi", [good], revenue_per_day="1500", clicks=60)
    c_thin = campaign(conn, "İnce marj", [thin], revenue_per_day="480", clicks=60)   # ROAS 4.8 ama marj düşük
    c_loss = campaign(conn, "Zarar", [thin], spend_per_day="300", revenue_per_day="300", clicks=60)
    c_nodata = campaign(conn, "Verisiz", [good], perf=False)
    v = {c["id"]: c for c in agents.advertising_analysis(conn)}
    assert v[c_good]["verdict"] == "INCREASE_BUDGET" and v[c_good]["ad_net_profit"] > 0
    assert v[c_thin]["roas"] == Decimal("4.8000") and v[c_thin]["verdict"] == "CONTINUE"
    assert "Bütçe artırılmasını önermiyorum" in v[c_thin]["verdict_reason"]
    assert v[c_loss]["verdict"] == "PAUSE" and v[c_loss]["ad_net_profit"] < 0
    assert v[c_nodata]["verdict"] == "INSUFFICIENT_DATA"

    cycle(engine)
    p = conn.execute(text("SELECT * FROM ai_proposals WHERE action_type = 'ads.increase_budget'")).mappings().one()
    assert p["status"] == "pending_approval" and p["params"]["new_daily_budget"] == "130.00"
    assert Decimal(p["required_capital"]) == Decimal("420.00") and p["capital_category"] == "advertising"
    assert conn.execute(text("SELECT status FROM ai_proposals WHERE action_type = 'ads.pause'")).scalar() == "pending_approval"


def test_stock_risk_stops_ad_scaling(engine, conn):
    from app.services.ai import agents
    pid = product(conn, "LOWSTOCK", cost="150", price="600", stock=3)
    sell(conn, pid, price="600", n=14, days_ago=2, tag="s")
    cid = campaign(conn, "Stok riskli", [pid], revenue_per_day="1500", clicks=60)
    c = next(c for c in agents.advertising_analysis(conn) if c["id"] == cid)
    assert c["stock_risk"] and c["verdict"] == "CONTINUE" and "stok" in c["verdict_reason"]
    out = cycle(engine)
    assert pid in out["inventory"]["do_not_scale_ads"]
    assert conn.execute(text("SELECT COUNT(*) FROM ai_proposals WHERE action_type = 'ads.increase_budget'")).scalar() == 0
    assert conn.execute(text("SELECT COUNT(*) FROM ai_proposals WHERE action_type = 'inventory.supplier_stock_risk'")).scalar() == 1
    assert conn.execute(text("SELECT COUNT(*) FROM ai_inventory_snapshots")).scalar() >= 1


# ------------------------------------------------------------------ risk motoru + onay
def _ads_setup(engine, conn):
    pid = product(conn, "APP", cost="150", price="600")
    sell(conn, pid, price="600", n=10, tag="x")
    cid = campaign(conn, "Onaylık", [pid], revenue_per_day="1500", clicks=60)
    cycle(engine)
    return pid, cid, conn.execute(text("SELECT id FROM ai_proposals WHERE action_type = 'ads.increase_budget'")).scalar()


def test_approval_flow_manual_execution_and_journal(engine, conn, client_factory):
    pid, cid, prop = _ads_setup(engine, conn)
    c = admin(client_factory)
    r = c.post(f"/api/ai/proposals/{prop}/approve", json={"note": "deneyelim"}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved" and r.json()["execution"]["mode"] == "manual"
    assert "130.00 TL" in r.json()["execution"]["instructions"]
    dec = conn.execute(text("SELECT * FROM ai_decisions WHERE proposal_id = :p"), {"p": prop}).mappings().one()
    assert dec["actor"] == "owner" and dec["decision"] == "approved" and dec["executed_at"] is None
    assert dec["context_snapshot"]["metrics"]["ad_spend"] is not None
    assert c.post(f"/api/ai/proposals/{prop}/approve", json={}, headers=H).status_code == 409   # iki kez onay yok
    assert c.post(f"/api/ai/proposals/{prop}/executed", json={"note": "panelde yaptım"}, headers=H).status_code == 200
    assert conn.execute(text("SELECT daily_budget FROM ad_campaigns WHERE id = :c"), {"c": cid}).scalar() == Decimal("130.00")
    assert conn.execute(text("SELECT executed_at IS NOT NULL FROM ai_decisions WHERE proposal_id = :p"), {"p": prop}).scalar()
    audit = {r[0] for r in conn.execute(text("SELECT action FROM audit_logs"))}
    assert {"ai.proposal_approved", "ai.proposal_executed"} <= audit
    feed = [r[0] for r in conn.execute(text("SELECT kind FROM ai_activity WHERE proposal_id = :p ORDER BY id"), {"p": prop})]
    assert feed[:3] == ["proposal", "risk", "approval"] and "action" in feed


def test_risk_recheck_at_approval_blocks_and_persists(engine, conn, client_factory):
    pid, cid, prop = _ads_setup(engine, conn)
    conn.execute(text("DELETE FROM ad_performance WHERE campaign_id = :c"), {"c": cid})  # veri artık kârı desteklemiyor
    c = admin(client_factory)
    r = c.post(f"/api/ai/proposals/{prop}/approve", json={}, headers=H)
    assert r.status_code == 409 and "risk" in r.json()["detail"].lower()
    assert conn.execute(text("SELECT status FROM ai_proposals WHERE id = :p"), {"p": prop}).scalar() == "blocked"
    assert c.post(f"/api/ai/proposals/{prop}/approve", json={}, headers=H).status_code == 409   # bloke: kimse geçemez
    assert conn.execute(text("SELECT COUNT(*) FROM ai_risk_events WHERE proposal_id = :p AND severity = 'block'"), {"p": prop}).scalar() >= 1


def test_risk_engine_blocks_unauthorized_and_oversized(engine, conn):
    from app.services.ai import proposals
    pid = product(conn, "RISK", cost="150", price="600")
    sell(conn, pid, price="600", n=10, tag="r")
    cid = campaign(conn, "Risk", [pid], revenue_per_day="1500", clicks=60)
    with engine.begin() as c:
        a = proposals.propose(c, agent="inventory", action_type="ads.pause", entity_type="campaign", entity_id=cid,
                              title="yetkisiz", reason="x", evidence={})
        b = proposals.propose(c, agent="advertising", action_type="ads.increase_budget", entity_type="campaign", entity_id=cid,
                              title="büyük adım", reason="x", evidence={},
                              params={"current_daily_budget": "100", "new_daily_budget": "300", "delta_per_day": "200"})
        ghost = proposals.propose(c, agent="advertising", action_type="ads.pause", entity_type="campaign", entity_id=999999,
                                  title="olmayan kampanya", reason="x", evidence={})
    st = {r.id: (r.status, r.risk_checks) for r in conn.execute(text("SELECT id, status, risk_checks FROM ai_proposals"))}
    assert st[a][0] == "blocked" and st[a][1][0]["code"] == "unauthorized_action"
    assert st[b][0] == "blocked" and "budget_step" in {x["code"] for x in st[b][1]}
    assert st[ghost][0] == "blocked" and st[ghost][1][0]["code"] == "entity_missing"


def test_emergency_stop(engine, conn, client_factory):
    from app.security import hash_password
    pid, cid, prop = _ads_setup(engine, conn)
    conn.execute(text("INSERT INTO users(username, password_hash, role) VALUES ('op', :h, 'operator')"),
                 {"h": hash_password("Operator-Pass-123")})
    c, login = client_factory
    login("op", "Operator-Pass-123")
    assert c.post("/api/ai/emergency-stop", json={"active": True, "reason": "test"}, headers=H).status_code == 200
    assert c.post("/api/ai/emergency-stop", json={"active": False}, headers=H).status_code == 403   # yalnızca yönetici kaldırır
    assert c.post(f"/api/ai/proposals/{prop}/approve", json={}, headers=H).status_code == 403      # operatör onaylayamaz
    c.post("/api/auth/logout", headers=H)
    login("admin", "Admin-Password-123")
    r = c.post(f"/api/ai/proposals/{prop}/approve", json={}, headers=H)
    assert r.status_code == 409 and "Acil durdurma" in r.json()["detail"]
    # Pazaryeri yayını ve tedarikçiye gönderim de durur; analiz sürer
    from app.services import publishing
    with engine.begin() as cc:
        assert next(g for g in publishing.gates(cc, "trendyol") if g["code"] == "emergency_stop")["ok"] is False
    assert "error" not in json.dumps(cycle(engine)["advertising"])
    ov = c.get("/api/ai/overview").json()
    assert ov["emergency_stop"] is True and ov["brief"]["items"][0]["kind"] == "emergency"
    assert c.post("/api/ai/emergency-stop", json={"active": False}, headers=H).status_code == 200
    assert c.post(f"/api/ai/proposals/{prop}/approve", json={}, headers=H).status_code == 200


# ------------------------------------------------------------------ sermaye
def test_capital_cash_is_not_profit(engine, conn, client_factory):
    c = admin(client_factory)
    cap = c.get("/api/ai/capital").json()
    assert cap["usable"] is None and "Kasa bilgisi girilmedi" in cap["recommendation"]
    for kind, name, amount in (("cash", "Banka", "60000"), ("supplier_liability", "Çanta Bayim", "5000"),
                               ("pending_payout", "Trendyol", "12000"), ("investable_limit", "AI", "50000")):
        assert c.put("/api/ai/capital/accounts", json={"kind": kind, "name": name, "amount": amount}, headers=H).status_code == 200
    conn.execute(text("INSERT INTO expenses(category, description, amount, expense_date) VALUES ('rent', 'Kira', 9000, CURRENT_DATE - 10)"))
    cap = c.get("/api/ai/capital").json()
    # 60000 − 5000 borç − rezerv (90 günlük gider 9000/3 = 3000 × 1 ay) = 52000 → üst limit 50000; hakediş sayılmaz
    assert Decimal(cap["reserve_required"]) == Decimal("3000.00") and Decimal(cap["usable"]) == Decimal("50000.00")
    assert Decimal(cap["pending_payout_not_counted"]) == Decimal("12000.00")
    assert Decimal(cap["justified"]) == 0 and "ek sermaye kullanmayı önermiyorum" in cap["recommendation"]
    pid, cid, prop = _ads_setup(engine, conn)
    cap = c.get("/api/ai/capital").json()
    assert Decimal(cap["justified"]) == Decimal("420.00") and Decimal(cap["unused"]) == Decimal("49580.00")
    assert c.put("/api/ai/capital/accounts", json={"kind": "bogus", "name": "x", "amount": "1"}, headers=H).status_code == 422


# ------------------------------------------------------------------ karar günlüğü / sonuç / CEO itirazı
def test_outcomes_and_ceo_pushback(engine, conn):
    from app.services.ai import decisions, proposals
    pid = product(conn, "OUT", cost="150", price="600")
    sell(conn, pid, price="600", n=6, days_ago=12, tag="before")
    sell(conn, pid, price="600", n=1, days_ago=5, tag="after")
    cid = campaign(conn, "Geçmiş", [pid], revenue_per_day="1500", clicks=60)
    for _ in range(3):
        did = conn.execute(text("""INSERT INTO ai_decisions(actor, decision, decision_type, entity_type, entity_id, executed_at, created_at)
                                   VALUES ('owner', 'approved', 'ads.increase_budget', 'product', :p, NOW() - INTERVAL '8 days',
                                           NOW() - INTERVAL '8 days') RETURNING id"""), {"p": pid}).scalar()
    with engine.begin() as c:
        assert decisions.evaluate_outcomes(c) == 9          # 3 karar × (1, 3, 7 gün); 30 gün henüz dolmadı
    res = conn.execute(text("SELECT final_result FROM ai_decision_outcomes WHERE horizon_days = 7 AND decision_id = :d"), {"d": did}).scalar()
    assert res == "worsened"
    with engine.begin() as c:
        assert decisions.track_record(c, "ads.increase_budget") == {"evaluated": 3, "improved": 0, "worsened": 3}
        new = proposals.propose(c, agent="advertising", action_type="ads.increase_budget", entity_type="campaign", entity_id=cid,
                                title="tekrar artır", reason="x", evidence={}, confidence=0.8,
                                params={"current_daily_budget": "100", "new_daily_budget": "130", "delta_per_day": "30"})
    p = conn.execute(text("SELECT ceo_note, confidence FROM ai_proposals WHERE id = :i"), {"i": new}).mappings().one()
    assert "3 kez uyguladın; 3 durumda net kâr düştü" in p["ceo_note"] and p["confidence"] == Decimal("0.600")
    with engine.begin() as c:
        rep = decisions.quality_report(c, 30)
        pat = decisions.owner_patterns(c)
    assert len(rep["owner_failures"]) == 3 and any(f["type"] == "repeated_mistake" for f in pat["findings"])


# ------------------------------------------------------------------ CEO sohbeti
def test_chat_rules_engine_answers_from_data(engine, conn, client_factory):
    loss = product(conn, "CH-LOSS", cost="95", price="100", name="Hasır Plaj Çantası")
    good = product(conn, "CH-GOOD", cost="150", price="600", name="Kapitone Omuz Çantası")
    sell(conn, loss, price="100", n=5, tag="l")
    sell(conn, good, price="600", n=12, tag="g")
    campaign(conn, "Kapitone", [good], revenue_per_day="1500", clicks=60)
    campaign(conn, "Hasır", [loss], spend_per_day="300", revenue_per_day="300", clicks=60)
    c = admin(client_factory)
    r = c.post("/api/ai/chat", json={"message": "Zarar eden ürünleri bul."}, headers=H).json()
    assert r["engine"] == "rules" and "Hasır Plaj Çantası" in r["answer"] and r["tools_used"] == ["get_products"]
    conv = r["conversation_id"]
    r = c.post("/api/ai/chat", json={"message": "5000 TL reklam bütçesini nasıl kullanmalıyız?", "conversation_id": conv}, headers=H).json()
    assert "kanıtla desteklenen kısmı" in r["answer"] and "Zarar eden kampanyalara pay verilmedi: Hasır" in r["answer"]
    r = c.post("/api/ai/chat", json={"message": "Şu anda sisteme 50.000 TL koyarsam ne kadarını kullanmak mantıklı?"}, headers=H).json()
    assert "50.000,00 ₺ eklesen bile" in r["answer"]
    r = c.post("/api/ai/chat", json={"message": "Bugün mağazada ne oldu?"}, headers=H).json()
    assert r["answer"].startswith("Bugün bilmen gerekenler")
    hist = c.get(f"/api/ai/chat/{conv}").json()
    assert [m["role"] for m in hist] == ["user", "assistant", "user", "assistant"]


def test_chat_claude_tool_loop_is_read_only(engine, conn, client_factory, monkeypatch):
    """Claude motoru: sahte istemciyle araç döngüsü (gerçek API çağrısı yok)."""
    from types import SimpleNamespace as NS

    import anthropic

    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "anthropic_api_key", "test-key")
    loss = product(conn, "CL-LOSS", cost="95", price="100", name="Zararlı Çanta")
    sell(conn, loss, price="100", n=5, tag="cl")
    calls = []

    class FakeMessages:
        def create(self, **kw):
            calls.append(kw)
            usage = NS(input_tokens=100, output_tokens=20)
            if len(calls) == 1:
                return NS(stop_reason="tool_use", usage=usage, content=[
                    NS(type="tool_use", id="t1", name="get_products", input={"product_class": "LOSS"}),
                    NS(type="tool_use", id="t2", name="approve_everything", input={})])
            results = kw["messages"][-1]["content"]
            assert "Zararlı Çanta" in results[0]["content"] and results[1]["is_error"] is True
            return NS(stop_reason="end_turn", usage=usage, content=[NS(type="text", text="Zararlı Çanta zarar ediyor.")])

    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: NS(beta=NS(messages=FakeMessages())))
    c = admin(client_factory)
    r = c.post("/api/ai/chat", json={"message": "Zarar eden ürünler?"}, headers=H).json()
    assert r["engine"] == "claude" and r["answer"] == "Zararlı Çanta zarar ediyor." and r["usage"]["requests"] == 2
    assert calls[0]["model"] == "claude-opus-5-5" and calls[0]["fallbacks"] == "default"
    assert {t["name"] for t in calls[0]["tools"]} >= {"get_products", "get_capital_position"}
    assert not any("approve" in t["name"] for t in calls[0]["tools"])
    # Model hata verirse kural motoru cevap verir
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: (_ for _ in ()).throw(RuntimeError("ağ yok")))
    assert c.post("/api/ai/chat", json={"message": "Zarar eden ürünler?"}, headers=H).json()["engine"] == "rules"


# ------------------------------------------------------------------ worker / gözlemlenebilirlik / yetkiler
def test_worker_cycle_and_agent_failure_is_visible(engine, conn, monkeypatch):
    from app.services import sync_service
    from app.services.ai import agents, ceo
    with engine.begin() as c:
        assert sync_service.schedule_ai_cycle(c) and sync_service.schedule_ai_cycle(c) is None

    def boom(conn, ctx):
        raise RuntimeError("tedarikçi verisi okunamadı")
    monkeypatch.setattr(ceo, "AGENT_ORDER", (("inventory", boom), ("product_profit", agents.run_product_profit)))
    out = sync_service.execute(engine, {"job_type": "ai.cycle", "payload": {}})
    assert "error" in out["inventory"] and "classes" in out["product_profit"]
    st = dict(conn.execute(text("SELECT agent_code, status FROM ai_agent_runs WHERE agent_code IN ('inventory', 'product_profit', 'ceo')")).all())
    assert st["inventory"] == "error" and st["product_profit"] == "ok" and st["ceo"] == "degraded"
    assert conn.execute(text("SELECT last_error FROM ai_agents WHERE code = 'inventory'")).scalar().startswith("RuntimeError")
    assert conn.execute(text("SELECT COUNT(*) FROM ai_activity WHERE level = 'error'")).scalar() == 1
    with engine.begin() as c:
        c.execute(text("UPDATE app_settings SET value = 'false' WHERE key = 'ai.enabled'"))
        c.execute(text("INSERT INTO app_settings(key, value) VALUES ('ai.enabled', 'false') ON CONFLICT DO NOTHING"))
        c.execute(text("DELETE FROM sync_jobs"))
        assert sync_service.schedule_ai_cycle(c) is None


def test_rbac_and_screens(engine, conn, client_factory):
    from app.security import hash_password
    conn.execute(text("INSERT INTO users(username, password_hash, role) VALUES ('izleyici', :h, 'viewer')"),
                 {"h": hash_password("Viewer-Password-123")})
    c, login = client_factory
    login("izleyici", "Viewer-Password-123")
    for path in ("/api/ai/overview", "/api/ai/agents", "/api/ai/proposals", "/api/ai/profit", "/api/ai/ads", "/api/ai/inventory",
                 "/api/ai/capital", "/api/ai/decisions", "/api/ai/quality-report", "/api/ai/scorecard", "/api/ai/owner",
                 "/api/ai/activity", "/api/ai/risk", "/api/ai/settings", "/api/ai/runs"):
        assert c.get(path).status_code == 200, path
    assert c.post("/api/ai/run", headers=H).status_code == 403
    assert c.post("/api/ai/emergency-stop", json={"active": True}, headers=H).status_code == 403
    assert c.put("/api/ai/settings", json={"enabled": False}, headers=H).status_code == 403
    assert c.post("/api/ai/chat", json={"message": "özet"}, headers=H).status_code == 200
    ag = {a["code"]: a for a in c.get("/api/ai/agents").json()}
    assert ag["social_media"]["health"] == "UNAVAILABLE" and "Meta" in ag["social_media"]["unavailable_reason"]
    assert c.get("/api/ai/scorecard").json()["go_live"] is None
    c.post("/api/auth/logout", headers=H)
    login("admin", "Admin-Password-123")
    assert c.patch("/api/ai/agents/social_media", json={"enabled": True}, headers=H).status_code == 409
    assert c.patch("/api/ai/agents/risk", json={"enabled": False}, headers=H).status_code == 409
    assert c.put("/api/ai/settings", json={"thresholds": {"star_margin": 3}}, headers=H).status_code == 422
    assert c.put("/api/ai/settings", json={"thresholds": {"star_margin": 0.25}, "inventory_model": "own_stock"}, headers=H).status_code == 200
    s = c.get("/api/ai/settings").json()
    assert s["thresholds"]["star_margin"] == 0.25 and s["inventory_model"] == "own_stock" and s["llm_configured"] is False
    assert c.put("/api/ai/owner/preferences", json={"key": "risk_tolerance", "value": "low"}, headers=H).status_code == 200
    assert c.put("/api/ai/owner/preferences", json={"key": "min_margin", "value": 5}, headers=H).status_code == 422
    assert c.post("/api/ai/run", headers=H).status_code == 200
    sc = c.get("/api/ai/scorecard").json()
    assert sc["go_live"] is not None and "en az 7 gün" in sc["message"]


def test_stale_proposals_are_retired_when_condition_disappears(engine, conn):
    from app.services.finance_service import recalculate_order
    pid = product(conn, "LATECOST", cost=None)
    sell(conn, pid, n=4, tag="lc")
    cycle(engine)
    q = "SELECT status FROM ai_proposals WHERE action_type = 'product.fix_missing_cost' AND entity_id = :p"
    assert conn.execute(text(q), {"p": pid}).scalar() == "pending_approval"
    assert conn.execute(text("SELECT risk_level FROM ai_proposals WHERE entity_id = :p"), {"p": pid}).scalar() == "low"
    conn.execute(text("UPDATE products SET cost = 200 WHERE id = :p"), {"p": pid})   # sahip maliyeti girdi
    conn.execute(text("UPDATE order_items SET unit_cost = NULL WHERE product_id = :p"), {"p": pid})
    for (oid,) in conn.execute(text("SELECT order_id FROM order_items WHERE product_id = :p"), {"p": pid}).all():
        recalculate_order(conn, oid)
    out = cycle(engine)
    assert out["product_profit"]["retired"] == 1
    assert conn.execute(text(q), {"p": pid}).scalar() == "superseded"
