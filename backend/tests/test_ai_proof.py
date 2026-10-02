"""AI Control Center V1 — davranış kanıtları (kontrollü test verisi + gerçek kod yolları).

Her test bir iddiayı kanıtlar: kalıcı kayıt (ai_decisions / ai_decision_outcomes / ai_owner_preferences) → geri çağırma
(assess) → karar mantığı (CEO duruşu, güven, onay kuralları). Sonuçlar gerçek sipariş + reklam verisinden
`evaluate_outcomes` ile hesaplanır; hiçbir sonuç elle yazılmaz.
"""
from decimal import Decimal

import httpx
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


def spend(c, cid, days_ago: list[int], amount="60", perf_revenue=None, clicks=40):
    for d in days_ago:
        c.execute(text("INSERT INTO ad_spend(campaign_id, spend_date, amount) VALUES (:c, CURRENT_DATE - :d, :a)"),
                  {"c": cid, "d": d, "a": amount})
        if perf_revenue is not None:
            c.execute(text("""INSERT INTO ad_performance(campaign_id, perf_date, impressions, clicks, attributed_orders, attributed_revenue)
                              VALUES (:c, CURRENT_DATE - :d, 3000, :cl, 2, :r)"""), {"c": cid, "d": d, "cl": clicks, "r": perf_revenue})


def shift_decision(c, decision_id, days=8):
    """Zamanın geçmesini simüle eder: karar 'days' gün önce verilmiş/uygulanmış olur."""
    c.execute(text("""UPDATE ai_decisions SET created_at = NOW() - make_interval(days => :d),
                      executed_at = NOW() - make_interval(days => :d) WHERE id = :i"""), {"d": days, "i": decision_id})


def evaluate(engine):
    from app.services.ai.decisions import evaluate_outcomes
    with engine.begin() as c:
        return evaluate_outcomes(c)


def bare_campaign(c, name, pid, budget="100"):
    return campaign(c, name, [pid], budget=budget, days=0)


# ======================================================================== 1. CEO LEARNING (tercih ≠ kanıt)
def test_1_ceo_learning_owner_preference_vs_business_evidence(engine, conn, api):
    from app.services.ai import decisions
    ids = []
    for i in range(10):
        pid = product(conn, f"PAUSE-{i}", cost="150", price="600")
        cid = bare_campaign(conn, f"Erken kapatılan {i}", pid)
        r = api.post("/api/ai/decisions", json={"decision_type": "ads.pause", "entity_type": "campaign", "entity_id": cid,
                                                  "note": "Erken kapatıyorum"}, headers=H)
        assert r.status_code == 200, r.text
        assert r.json()["assessment"]["situation"] == "ads:early"
        ids.append((r.json()["decision_id"], pid, cid, i < 7))
    # Kontrollü veri: kararlar 8 gün önce verilmiş; 7'sinde kapatma sonrası satış çöktü, 3'ünde zarar eden reklam durdu
    for did, pid, cid, bad in ids:
        shift_decision(conn, did)
        if bad:
            spend(conn, cid, [10, 11, 12, 13, 14], amount="60")      # önce: düşük reklam, iyi satış
            sell(conn, pid, price="600", n=6, days_ago=11, tag=f"b{did}")
            sell(conn, pid, price="600", n=1, days_ago=4, tag=f"a{did}")   # sonra: satış düştü
        else:
            spend(conn, cid, [10, 11, 12, 13, 14], amount="400")     # önce: reklam kârı yiyordu
            sell(conn, pid, price="600", n=1, days_ago=11, tag=f"b{did}")
            sell(conn, pid, price="600", n=1, days_ago=4, tag=f"a{did}")
    assert evaluate(engine) == 30                                   # 10 karar × (1, 3, 7 gün)
    res = dict(conn.execute(text("""SELECT final_result, COUNT(*) FROM ai_decision_outcomes WHERE horizon_days = 7
                                    GROUP BY 1""")).all())
    assert res == {"worsened": 7, "improved": 3}

    # Persistence: tercih (sahibin kendi 10 kararı) ve kanıt (ölçülmüş sonuçlar) AYRI hesaplanıyor
    with engine.begin() as c:
        pref = decisions.owner_preference(c, "ads.pause", "ads:early")
        ev = decisions._evidence(c, "ads.pause", "ads:early", origin="all")
    assert pref["initiated"] == 10 and pref["prefers"] is True
    assert ev == {"evaluated": 10, "improved": 3, "worsened": 7, "profit_effect": ev["profit_effect"]} and ev["profit_effect"] < 0

    # Retrieval + karar mantığı: yeni benzer durum → CEO itiraz eder
    pid = product(conn, "PAUSE-NEW", cost="150", price="600")
    cid = bare_campaign(conn, "Yeni erken kampanya", pid)
    a = api.post("/api/ai/decisions/assess", json={"decision_type": "ads.pause", "entity_type": "campaign", "entity_id": cid},
                 headers=H).json()
    assert a["stance"] == "oppose" and a["situation"] == "ads:early"
    assert "Normalde bunu tercih ettiğini biliyorum" in a["note"] and "10 kez kendin uyguladın" in a["note"]
    assert "net kârı düşürdüğünü gösteriyor" in a["note"] and "10 uygulamanın 7 tanesinde" in a["note"]
    assert "Bu sefer aynı kararı önermiyorum" in a["note"]
    # İtiraz karar mantığını değiştirir: gerekçesiz kayıt reddedilir, gerekçeyle "override" olarak kaydedilir
    r = api.post("/api/ai/decisions", json={"decision_type": "ads.pause", "entity_type": "campaign", "entity_id": cid}, headers=H)
    assert r.status_code == 409 and r.json()["detail"]["assessment"]["stance"] == "oppose"
    r = api.post("/api/ai/decisions", json={"decision_type": "ads.pause", "entity_type": "campaign", "entity_id": cid,
                                             "override_reason": "Ürün sezon sonu, stoğu bitireceğim"}, headers=H)
    assert r.status_code == 200 and r.json()["override"] is True
    d = conn.execute(text("SELECT override, override_reason, ceo_stance FROM ai_decisions WHERE id = :i"),
                     {"i": r.json()["decision_id"]}).one()
    assert d.override and d.ceo_stance == "oppose" and "sezon" in d.override_reason
    assert conn.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action = 'ai.owner_decision_override'")).scalar() == 1

    # Kanıt olmayan farklı bir karar türünde CEO tercihi taklit etmez, nötr kalır
    neutral = api.post("/api/ai/decisions/assess", json={"decision_type": "ads.decrease_budget", "entity_type": "campaign",
                                                         "entity_id": cid}, headers=H).json()
    assert neutral["stance"] == "neutral" and neutral["note"] is None

    # Açık tercih (OWNER_PREFERENCE) kaydedilir ve gerekçede ayrıca görünür
    api.put("/api/ai/owner/preferences", json={"key": "ad_aggressiveness", "value": "conservative"}, headers=H)
    a2 = api.post("/api/ai/decisions/assess", json={"decision_type": "ads.pause", "entity_type": "campaign", "entity_id": cid},
                  headers=H).json()
    assert "tercihlerinde böyle belirttin" in a2["note"] and a2["owner_preference"]["explicit"]


# ======================================================================== 2 + 7. CEO kendi hataları + owner override
def _profitable_campaign(c, name, *, before_units, after_units, budget="100", price="600"):
    pid = product(c, f"P-{name}", cost="150", price=price)
    cid = campaign(c, name, [pid], budget=budget, revenue_per_day="1500", clicks=60, days=7)   # son 7 gün: kârlı reklam
    if before_units:
        sell(c, pid, price=price, n=before_units, days_ago=11, tag="b")
    if after_units:
        sell(c, pid, price=price, n=after_units, days_ago=4, tag="a")
    return pid, cid


def test_2_ceo_measures_its_own_accuracy_and_7_owner_override(engine, conn, api):
    cash(conn, "100000")
    camps = [_profitable_campaign(conn, f"AI{i}", before_units=6 if i < 3 else 1, after_units=1 if i < 3 else 6) for i in range(4)]
    cycle(engine)
    props = {r.entity_id: r.id for r in conn.execute(text("SELECT entity_id, id FROM ai_proposals WHERE action_type = 'ads.increase_budget'"))}
    assert set(props) == {cid for _, cid in camps}
    for _, cid in camps:   # sahip ajan önerilerini onaylayıp uyguladı
        assert api.post(f"/api/ai/proposals/{props[cid]}/approve", json={}, headers=H).status_code == 200
        assert api.post(f"/api/ai/proposals/{props[cid]}/executed", json={}, headers=H).status_code == 200
    for did in [r[0] for r in conn.execute(text("SELECT id FROM ai_decisions WHERE agent_code = 'advertising'"))]:
        shift_decision(conn, did)
    evaluate(engine)
    acc = {(r["agent_code"], r["decision_type"]): r for r in api.get("/api/ai/scorecard").json()["ai_accuracy"]}
    a = acc[("advertising", "ads.increase_budget")]
    assert (a["evaluated"], a["improved"], a["worsened"]) == (4, 1, 3)

    # Benzer yeni öneri: CEO kendi geçmiş başarısız önerilerini hesaba katar
    _, new_cid = _profitable_campaign(conn, "YENI", before_units=3, after_units=3)
    cycle(engine)
    p = conn.execute(text("SELECT * FROM ai_proposals WHERE entity_id = :c AND action_type = 'ads.increase_budget'"),
                     {"c": new_cid}).mappings().one()
    assert p["ceo_stance"] == "oppose" and p["status"] == "pending_approval"
    assert "Kendi hatam: benim bu tür önerilerim 4 kez uygulandı, 3 tanesinde net kâr düştü (isabet %25)" in p["ceo_note"]
    assert p["ceo_assessment"]["ai_evidence"]["worsened"] == 3
    assert Decimal(p["confidence"]) < Decimal("0.5")   # güven düşürüldü

    # 7. OWNER OVERRIDE: CEO karşı, risk orta → gerekçe zorunlu, override olarak kayıt + audit
    r = api.post(f"/api/ai/proposals/{p['id']}/approve", json={}, headers=H)
    assert r.status_code == 422 and "gerekçeni yaz" in r.json()["detail"]
    r = api.post(f"/api/ai/proposals/{p['id']}/approve", json={"note": "Kampanya haftası, bilerek risk alıyorum"}, headers=H)
    assert r.status_code == 200
    row = conn.execute(text("SELECT owner_override FROM ai_proposals WHERE id = :i"), {"i": p["id"]}).scalar()
    dec = conn.execute(text("SELECT override, override_reason FROM ai_decisions WHERE proposal_id = :i"), {"i": p["id"]}).one()
    assert row and dec.override and "risk" in dec.override_reason
    assert conn.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action = 'ai.owner_override'")).scalar() == 1

    # CEO karşı + HIGH risk → owner override YOK
    _, big = _profitable_campaign(conn, "BUYUK", before_units=3, after_units=3, budget="1000")   # +300 TL/gün → HIGH
    cycle(engine)
    hp = conn.execute(text("SELECT id, risk_level, ceo_stance, status FROM ai_proposals WHERE entity_id = :c"), {"c": big}).one()
    assert hp.risk_level == "high" and hp.ceo_stance == "oppose" and hp.status == "pending_approval"
    r = api.post(f"/api/ai/proposals/{hp.id}/approve", json={"note": "Yine de istiyorum, sorumluluk bende"}, headers=H)
    assert r.status_code == 409 and "sahip onayıyla bile uygulanamaz" in r.json()["detail"]
    # Güvenlik bloğu (risk motoru) override ile de aşılamaz: %150 bütçe adımı her zaman bloke
    blocked, status, codes = _attack(engine, entity_id=big, params={"current_daily_budget": "100", "new_daily_budget": "250",
                                                                   "delta_per_day": "150"}, title="override ile bypass denemesi")
    assert status == "blocked" and "budget_step" in codes
    r = api.post(f"/api/ai/proposals/{blocked}/approve", json={"note": "Ben sahibim, yine de onaylıyorum"}, headers=H)
    assert r.status_code == 409 and "bloke" in r.json()["detail"]
    assert conn.execute(text("SELECT status FROM ai_proposals WHERE id = :i"), {"i": blocked}).scalar() == "blocked"


# ======================================================================== 3. PROFIT SOURCE OF TRUTH
def test_3_single_source_of_truth_for_product_profit(engine, conn, api):
    from app.services.ai import agents, chat, decisions
    from app.services.ai.config import Window
    pid = product(conn, "SOT-1", cost="180", price="650", name="Tek Kaynak Çantası")
    sell(conn, pid, price="650", n=9, days_ago=3, tag="s")
    sell(conn, pid, price="650", n=2, days_ago=3, tag="r", status="returned")
    cid = campaign(conn, "SOT", [pid], revenue_per_day="900", clicks=60, days=5)
    conn.execute(text("""INSERT INTO expenses(category, description, amount, expense_date, sku)
                         VALUES ('advertising', 'Influencer', 250, CURRENT_DATE - 2, 'SOT-1')"""))
    q = lambda v: Decimal(str(v)).quantize(Decimal("0.01"))  # noqa: E731
    ai_profit = q(next(p for p in api.get("/api/ai/profit", params={"days": 30}).json()["products"] if p["product_id"] == pid)["net_profit"])
    values = {
        "AI Control Center (Kâr Merkezi)": ai_profit,
        "Product Agent": q(next(p for p in agents.classified_products(conn) if p["product_id"] == pid)["net_profit"]),
        "SKU raporu (Raporlar)": q(next(s for s in api.get("/api/reports/sku", params={"period": "30d"}).json()["items"]
                                       if s["sku"] == "SOT-1")["net_profit_after_tax"]),
        "Reklam merkezi (ürün reklam sonrası kâr)": q(next(p for p in api.get("/api/ads/summary", params={"period": "30d"}).json()["products"]
                                                           if p["product_id"] == pid)["profit_after_ads"]),
        "CEO Chat aracı": q(next(p for p in chat.call_tool(conn, "get_products", {"limit": 50})["products"] if p["product_id"] == pid)["net_profit"]),
        "Karar günlüğü (entity_metrics)": q(decisions.entity_metrics(conn, "product", pid, Window(30))["net_profit"]),
    }
    assert len(set(values.values())) == 1, values
    # Reklam ajanı aynı ürün marjını kullanır
    camp = next(c for c in agents.advertising_analysis(conn) if c["id"] == cid)
    e = next(p for p in agents.classified_products(conn) if p["product_id"] == pid)
    assert camp["product_margin_before_ads"] == (e["profit_before_ads"] / e["net_sales"]).quantize(Decimal("0.0001"))
    # Dönem kârı: Dashboard ile AI ana ekranı aynı
    dash = api.get("/api/dashboard", params={"period": "7d"}).json()["summary"]["net_profit_after_tax"]
    kp = api.get("/api/ai/overview").json()["kpis"]["net_profit_7d"]
    assert q(dash) == q(kp)
    # CEO Chat cevabındaki rakam da aynı kaynaktan
    from app.services.ai.chat import _money
    assert _money(ai_profit) in chat.rules_answer(conn, "En kötü ürünüm hangisi?")[0]


# ======================================================================== 4. CAPITAL SAFETY
def test_4_capital_safety_does_not_deploy_available_capital(engine, conn, api):
    conn.execute(text("""INSERT INTO ai_capital_accounts(kind, name, amount) VALUES ('cash', 'Banka', 50000),
                         ('investable_limit', 'AI', 50000)"""))
    pid = product(conn, "CAP-1", cost="150", price="600")
    sell(conn, pid, price="600", n=10, days_ago=3, tag="c")
    campaign(conn, "Kanıtlı fırsat", [pid], budget="950", spend_per_day="950", revenue_per_day="9000", clicks=80)
    # Dropship + tükenmek üzere olan yıldız ürün: stok sermayesi AYRILMAMALI
    star = product(conn, "CAP-STAR", cost="150", price="600", stock=4)
    sell(conn, star, price="600", n=14, days_ago=2, tag="s")
    cycle(engine)
    cap = api.get("/api/ai/capital").json()
    assert Decimal(cap["usable"]) == Decimal("50000.00")
    assert Decimal(cap["justified"]) == Decimal("3990.00")          # 950 → 1235 TL/gün, 14 günlük test
    assert Decimal(cap["unused"]) == Decimal("46010.00")
    assert "Kalan 46.010,00 ₺ için ek sermaye kullanmayı şu anda önermiyorum" in cap["recommendation"]
    assert [x["category"] for x in cap["justified_by_category"]] == ["advertising"]
    assert conn.execute(text("SELECT COUNT(*) FROM ai_proposals WHERE action_type = 'inventory.restock'")).scalar() == 0
    risk_task = conn.execute(text("SELECT required_capital FROM ai_proposals WHERE action_type = 'inventory.supplier_stock_risk'")).scalar()
    assert risk_task == 0
    from app.services.ai.chat import rules_answer
    ans = rules_answer(conn, "Şu anda sisteme 50.000 TL koyarsam ne kadarını kullanmak mantıklı?")[0]
    assert "3.990,00 ₺" in ans and "46.010,00 ₺" in ans and "önermiyorum" in ans
    # Kendi stoğu modeline geçilirse aynı ürün için stok sermayesi gerekçelendirilir (kontrast)
    api.put("/api/ai/settings", json={"inventory_model": "own_stock"}, headers=H)
    cycle(engine)
    assert conn.execute(text("SELECT COUNT(*) FROM ai_proposals WHERE action_type = 'inventory.restock'")).scalar() == 1


# ======================================================================== 5. LOSS VS REVENUE
def test_5_high_revenue_good_roas_but_loss_is_not_success(engine, conn, api):
    from app.services.ai import agents, proposals
    from app.services.ai.chat import rules_answer
    cash(conn)
    loss = product(conn, "BIG-LOSS", cost="900", price="1000", name="Çok Satan Ama Zararlı Çanta")
    small = product(conn, "SMALL-OK", cost="100", price="400", name="Küçük Kârlı Çanta")
    sell(conn, loss, price="1000", n=25, days_ago=3, tag="l")        # 25.000 TL ciro
    sell(conn, small, price="400", n=4, days_ago=3, tag="s")
    cid = campaign(conn, "Yüksek ROAS", [loss], budget="200", spend_per_day="200", revenue_per_day="2400", clicks=90)
    c = next(c for c in agents.advertising_analysis(conn) if c["id"] == cid)
    assert c["roas"] >= Decimal("12") and c["ad_net_profit"] < 0 and c["verdict"] in ("PAUSE", "DECREASE_BUDGET")
    assert next(p for p in agents.classified_products(conn) if p["product_id"] == loss)["class"] == "LOSS"
    cycle(engine)
    assert conn.execute(text("SELECT COUNT(*) FROM ai_proposals WHERE action_type = 'ads.increase_budget'")).scalar() == 0
    with engine.begin() as cc:
        pid = proposals.propose(cc, agent="advertising", action_type="ads.increase_budget", entity_type="campaign", entity_id=cid,
                                title="zorla artır", reason="ROAS iyi", evidence={},
                                params={"current_daily_budget": "200", "new_daily_budget": "260", "delta_per_day": "60"},
                                required_capital=840, capital_category="advertising")
    st = conn.execute(text("SELECT status, risk_checks FROM ai_proposals WHERE id = :i"), {"i": pid}).one()
    assert st.status == "blocked" and "negative_margin" in {x["code"] for x in st.risk_checks}
    assert "Çok Satan Ama Zararlı Çanta" in rules_answer(conn, "En kötü ürünüm hangisi?")[0]
    brief = [i["kind"] for i in api.get("/api/ai/overview").json()["brief"]["items"]]
    assert "ads_loss" in brief and "loss_products" in brief


# ======================================================================== 6. RISK ENGINE ATTACK
def _attack(engine, **kw):
    from app.services.ai import proposals
    base = dict(agent="advertising", action_type="ads.increase_budget", entity_type="campaign", title="saldırı", reason="x",
                evidence={}, params={"current_daily_budget": "100", "new_daily_budget": "130", "delta_per_day": "30"},
                required_capital=420, capital_category="advertising")
    base.update(kw)
    with engine.begin() as c:
        pid = proposals.propose(c, **base)
        p = c.execute(text("SELECT status, risk_checks FROM ai_proposals WHERE id = :i"), {"i": pid}).one()
    return pid, p.status, {x["code"] for x in p.risk_checks if x["severity"] == "block"}


def test_6_risk_engine_attacks_are_blocked(engine, conn, api):
    from app.services import publishing, supplier_forwarding
    from app.services.ai import agents, proposals
    cash(conn)
    good = product(conn, "RA-GOOD", cost="150", price="600")
    sell(conn, good, price="600", n=10, days_ago=3, tag="g")
    ok_cid = campaign(conn, "Sağlıklı", [good], revenue_per_day="1500", clicks=60)
    results = {}
    # a) negatif marj
    neg = product(conn, "RA-NEG", cost="900", price="1000")
    sell(conn, neg, price="1000", n=8, days_ago=3, tag="n")
    results["negatif marj"] = _attack(engine, entity_id=campaign(conn, "Neg", [neg], revenue_per_day="2000", clicks=60))
    # b) stoğu 0 ürün, son 30 günde satış yok (hız 0) — önceki sürümde bu durum bloke edilmiyordu
    zero = product(conn, "RA-ZERO", cost="150", price="600", stock=0)
    sell(conn, zero, price="600", n=8, days_ago=40, tag="z")   # 30 günden eski satış: satış hızı 0
    results["stok 0"] = _attack(engine, entity_id=campaign(conn, "Stoksuz", [zero], revenue_per_day="1500", clicks=60))
    # c) sermaye limiti aşımı ve kasa bilgisi yok
    conn.execute(text("UPDATE ai_capital_accounts SET amount = 300 WHERE kind = 'cash'"))
    results["sermaye limiti"] = _attack(engine, entity_id=ok_cid)
    conn.execute(text("DELETE FROM ai_capital_accounts"))
    results["kasa bilgisi yok"] = _attack(engine, entity_id=ok_cid, agent="advertising")
    cash(conn)
    # d) bayat reklam verisi ve bayat sipariş verisi
    stale = product(conn, "RA-STALE", cost="150", price="600")
    sell(conn, stale, price="600", n=10, days_ago=3, tag="st")
    st_cid = bare_campaign(conn, "Bayat", stale)
    spend(conn, st_cid, [6, 7, 8, 9], amount="100", perf_revenue="1500", clicks=60)
    results["bayat reklam verisi"] = _attack(engine, entity_id=st_cid)
    conn.execute(text("UPDATE marketplaces SET last_sync_at = NOW() - INTERVAL '30 hours' WHERE code = 'trendyol'"))
    results["bayat sipariş verisi"] = _attack(engine, entity_id=ok_cid)
    conn.execute(text("UPDATE marketplaces SET last_sync_at = NOW() WHERE code = 'trendyol'"))
    # e) anormal reklam artışı (bugün 5 kat)
    sp = product(conn, "RA-SPIKE", cost="150", price="600")
    sell(conn, sp, price="600", n=10, days_ago=3, tag="sp")
    sp_cid = campaign(conn, "Ani artış", [sp], revenue_per_day="1500", clicks=60, days=8)
    conn.execute(text("UPDATE ad_spend SET amount = 600 WHERE campaign_id = :c AND spend_date = CURRENT_DATE"), {"c": sp_cid})
    results["anormal reklam artışı"] = _attack(engine, entity_id=sp_cid)
    # f) bütçe adımı, yetkisiz aksiyon
    results["aşırı bütçe adımı"] = _attack(engine, entity_id=ok_cid, params={"current_daily_budget": "100", "new_daily_budget": "250",
                                                                             "delta_per_day": "150"})
    results["yetkisiz aksiyon"] = _attack(engine, entity_id=ok_cid, agent="inventory")
    expected = {"negatif marj": "negative_margin", "stok 0": "stock_risk", "sermaye limiti": "capital_exceeded",
                "kasa bilgisi yok": "no_cash_data", "bayat reklam verisi": "stale_data", "bayat sipariş verisi": "stale_orders",
                "anormal reklam artışı": "spend_anomaly", "aşırı bütçe adımı": "budget_step", "yetkisiz aksiyon": "unauthorized_action"}
    for name, code in expected.items():
        _, status, codes = results[name]
        assert status == "blocked" and code in codes, (name, status, codes)
    with engine.begin() as c:
        assert any(f["code"] == "ad_spend_spike" for f in agents.scan_anomalies(c))

    # g) duplicate action: aynı öneri tek kayıt; onay iki kez yapılamaz; onaylı öneri yeniden açılmaz
    a1, s1, _ = _attack(engine, entity_id=ok_cid)
    a2, s2, _ = _attack(engine, entity_id=ok_cid)
    assert a1 == a2 and s1 == "pending_approval"
    assert conn.execute(text("SELECT COUNT(*) FROM ai_proposals WHERE entity_id = :c AND status = 'pending_approval'"),
                        {"c": ok_cid}).scalar() == 1
    assert api.post(f"/api/ai/proposals/{a1}/approve", json={}, headers=H).status_code == 200
    assert api.post(f"/api/ai/proposals/{a1}/approve", json={}, headers=H).status_code == 409
    a3, s3, _ = _attack(engine, entity_id=ok_cid)
    assert a3 == a1 and s3 == "approved"

    # h) kill switch: hiçbir yazma işlemi geçmez
    pend, _, _ = _attack(engine, entity_id=campaign(conn, "Kill", [good], revenue_per_day="1500", clicks=60))
    assert api.post("/api/ai/emergency-stop", json={"active": True}, headers=H).status_code == 200
    assert api.post(f"/api/ai/proposals/{pend}/approve", json={}, headers=H).status_code == 409
    assert api.post(f"/api/ai/proposals/{a1}/executed", json={}, headers=H).status_code == 409
    with engine.begin() as c:
        assert proposals.execute_action(c, {"action_type": "ads.pause", "params": {}})["mode"] == "blocked"
        assert next(g for g in publishing.gates(c, "trendyol") if g["code"] == "emergency_stop")["ok"] is False
        sid = c.execute(text("""INSERT INTO supplier_orders(order_id, status, channel, payload) VALUES (NULL, 'draft', 'storefront', '{}')
                                RETURNING id""")).scalar()
        with pytest.raises(supplier_forwarding.ForwardingError, match="Acil durdurma"):
            supplier_forwarding.send(c, sid)


# ======================================================================== 8. AUDITABILITY
def test_8_single_id_trail_from_agent_to_outcome(engine, conn, api):
    cash(conn)
    pid = product(conn, "TRAIL", cost="150", price="600")
    sell(conn, pid, price="600", n=6, days_ago=11, tag="b")
    sell(conn, pid, price="600", n=6, days_ago=4, tag="a")
    cid = campaign(conn, "İz sürülen", [pid], revenue_per_day="1500", clicks=60)
    cycle(engine)
    prop = conn.execute(text("SELECT id FROM ai_proposals WHERE entity_id = :c"), {"c": cid}).scalar()
    api.post(f"/api/ai/proposals/{prop}/approve", json={"note": "deneyelim"}, headers=H)
    api.post(f"/api/ai/proposals/{prop}/executed", json={"note": "panelde 130 yaptım"}, headers=H)
    shift_decision(conn, conn.execute(text("SELECT id FROM ai_decisions WHERE proposal_id = :p"), {"p": prop}).scalar())
    evaluate(engine)
    t = api.get(f"/api/ai/proposals/{prop}/trail").json()
    kinds = [a["kind"] for a in t["activity"]]
    assert kinds[:4] == ["proposal", "validation", "ceo_review", "risk"]
    chain = ["proposal", "validation", "ceo_review", "risk", "approval", "action", "action", "outcome"]
    it = iter(kinds)
    assert all(k in it for k in chain), kinds                       # sıralı alt dizi
    assert t["agent_run"]["agent_code"] == "advertising" and t["proposal"]["situation"] in ("ads:early", "ads:mature")
    assert {a["action"] for a in t["audit"]} >= {"ai.proposal_approved", "ai.proposal_executed"}
    dec = t["decisions"][0]
    assert dec["decision"] == "approved" and dec["executed_at"] and dec["context_snapshot"]["metrics"]
    assert [o["horizon_days"] for o in dec["outcomes"]] == [1, 3, 7]
    assert next(a for a in t["activity"] if a["kind"] == "validation")["message"].startswith("Kâr doğrulaması (finance_view)")


# ======================================================================== 9. FAILURE
def test_9_failures_degrade_instead_of_producing_wrong_data(engine, conn, api, monkeypatch):
    import anthropic
    from sqlalchemy.exc import OperationalError

    from app.config import Settings, get_settings
    from app.connectors import registry
    from app.connectors.trendyol import TrendyolConnector
    from app.services import jobs, sync_service
    from app.services.ai import agents, ceo
    cash(conn)
    pid = product(conn, "FAIL", cost="150", price="600")
    sell(conn, pid, price="600", n=10, days_ago=3, tag="f")
    campaign(conn, "Hata testi", [pid], revenue_per_day="1500", clicks=60)

    # a) Anthropic API kapalı → sohbet kural motoruna düşer ve bunu söyler
    monkeypatch.setattr(get_settings(), "anthropic_api_key", "test-key")

    def down(**kw):
        raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: type("C", (), {"beta": type("B", (), {"messages": type("M", (), {"create": staticmethod(down)})()})()})())
    r = api.post("/api/ai/chat", json={"message": "Zarar eden ürünler?"}, headers=H).json()
    assert r["engine"] == "rules" and "APIConnectionError" in r["fallback_reason"]
    monkeypatch.setattr(get_settings(), "anthropic_api_key", "")

    # b) Trendyol geçici hata (503): senkron başarısız, son başarılı senkron eskir → ajanlar DEGRADED, harcama bloke
    conn.execute(text("UPDATE marketplaces SET last_sync_at = NOW() - INTERVAL '30 hours' WHERE code = 'trendyol'"))
    s = Settings(database_url="postgresql://x@y/z", trendyol_seller_id="1", trendyol_api_key="k", trendyol_api_secret="s",
                 trendyol_rate_per_minute=6000)
    monkeypatch.setitem(registry.CONNECTOR_CLASSES, "trendyol", lambda settings: TrendyolConnector(
        settings, transport=httpx.MockTransport(lambda req: httpx.Response(503)), sleep=lambda x: None))
    with pytest.raises(Exception):   # noqa: B017 - connector'ın kendi geçici hata türü
        sync_service.execute(engine, {"job_type": "orders.sync", "marketplace": "trendyol", "payload": {}, "store_id": None}, s)
    cycle(engine)
    st = dict(conn.execute(text("SELECT DISTINCT ON (agent_code) agent_code, status FROM ai_agent_runs ORDER BY agent_code, id DESC")).all())
    assert st["advertising"] == "degraded" and st["product_profit"] == "degraded"
    p = conn.execute(text("SELECT status, risk_checks FROM ai_proposals WHERE action_type = 'ads.increase_budget'")).one()
    assert p.status == "blocked" and "stale_orders" in {x["code"] for x in p.risk_checks}
    assert any(i["kind"] == "data_quality" for i in api.get("/api/ai/overview").json()["brief"]["items"])
    conn.execute(text("UPDATE marketplaces SET last_sync_at = NOW() WHERE code = 'trendyol'"))

    # c) Worker yeniden başladı: yarıda kalan çalışma ve iş
    conn.execute(text("""INSERT INTO ai_agent_runs(agent_code, status, started_at) VALUES ('inventory', 'running', NOW() - INTERVAL '2 hours')"""))
    conn.execute(text("""INSERT INTO sync_jobs(job_type, status, payload, idempotency_key, run_after, max_attempts, attempts, locked_by, locked_at, started_at, created_at)
                         VALUES ('ai.cycle', 'running', '{}', 'ai.cycle', NOW(), 1, 1, 'ölü-worker', NOW() - INTERVAL '2 hours', NOW() - INTERVAL '2 hours', NOW())"""))
    with engine.begin() as c:
        assert jobs.requeue_stale(c) >= 1
    cycle(engine)
    assert conn.execute(text("SELECT COUNT(*) FROM ai_agent_runs WHERE status = 'running'")).scalar() == 0
    assert conn.execute(text("SELECT error FROM ai_agent_runs WHERE started_at < NOW() - INTERVAL '1 hour'")).scalar().startswith("Çalışma yarıda kesildi")

    # d) Veritabanı bağlantısı koptu: ajan ERROR, diğerleri çalışır, hatalı ajanın önerileri silinmez, özet uyarır
    before = conn.execute(text("SELECT COUNT(*) FROM ai_proposals WHERE agent_code = 'product_profit' AND status = 'pending_approval'")).scalar()

    def db_down(conn_, ctx):
        raise OperationalError("SELECT 1", {}, Exception("server closed the connection unexpectedly"))
    monkeypatch.setattr(ceo, "AGENT_ORDER", (("product_profit", db_down), ("advertising", agents.run_advertising)))
    out = cycle(engine)
    assert "OperationalError" in out["product_profit"]["error"] and "campaigns" in out["advertising"]
    assert conn.execute(text("SELECT COUNT(*) FROM ai_proposals WHERE agent_code = 'product_profit' AND status = 'pending_approval'")).scalar() == before
    items = api.get("/api/ai/overview").json()["brief"]["items"]
    assert items[0]["kind"] == "agent_error" and "Ürün & Kâr" in items[0]["text"]
    ag = {a["code"]: a["health"] for a in api.get("/api/ai/agents").json()}
    assert ag["product_profit"] == "ERROR"
    # Gerçek bağlantı kesintisi: tüm DB oturumları sonlandırılır; havuz yeniden bağlanır, döngü doğru çalışır
    from app.db import get_engine
    with get_engine().connect().execution_options(isolation_level="AUTOCOMMIT") as c:
        c.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()"))
    monkeypatch.setattr(ceo, "AGENT_ORDER", (("product_profit", agents.run_product_profit),))
    assert "classes" in cycle(engine)["product_profit"]


# ======================================================================== 10. CEO CHAT GROUNDING
QUESTIONS = ["Bugün ne kadar net kâr ettik?", "Neden?", "En kötü ürünüm hangisi?",
             "Şu anda reklama 10.000 TL daha koymalı mıyım?", "Ben son zamanlarda hangi hataları yapıyorum?"]


def _ask(api, conv=None):
    out = []
    for q in QUESTIONS:
        r = api.post("/api/ai/chat", json={"message": q, "conversation_id": conv}, headers=H).json()
        conv = r["conversation_id"]
        out.append(r)
    return out


def test_10a_chat_says_not_enough_data_instead_of_guessing(engine, conn, api):
    answers = _ask(api)
    for q, r in zip(QUESTIONS, answers):
        assert r["engine"] == "rules" and "Bunu söylemek için yeterli verim yok" in r["answer"], (q, r["answer"])
        assert r["tools_used"], q   # cevap bir araca (veriye) dayanıyor


def test_10b_chat_answers_are_grounded_in_real_numbers(engine, conn, api):
    from app.services.ai.chat import _money
    from app.services.ai.config import Window, today
    from app.services.ai.data import period_summary
    from app.services.finance_service import recalculate_order
    cash(conn)
    good = product(conn, "CH-GOOD", cost="150", price="600", name="İyi Çanta")
    bad = product(conn, "CH-BAD", cost="560", price="600", name="Kötü Çanta")
    sell(conn, good, price="600", n=8, days_ago=3, tag="g")
    sell(conn, bad, price="600", n=6, days_ago=3, tag="b")
    for i, p in enumerate((good, good, bad)):   # bugünün siparişleri
        oid = conn.execute(text("""INSERT INTO orders(store_id, external_order_id, status, internal_status, gross_revenue, order_date)
                                   SELECT s.id, :e, 'Created', 'new', 0, NOW() FROM stores s JOIN marketplaces m ON m.id = s.marketplace_id
                                    WHERE m.code = 'trendyol' LIMIT 1 RETURNING id"""), {"e": f"TODAY-{i}"}).scalar()
        conn.execute(text("""INSERT INTO order_items(order_id, product_id, external_line_id, sku, product_name, quantity, unit_price, vat_rate)
                             SELECT :o, id, :l, sku, name, 1, 600, 20 FROM products WHERE id = :p"""), {"o": oid, "l": f"T{i}", "p": p})
        recalculate_order(conn, oid)
    campaign(conn, "İyi reklam", [good], revenue_per_day="1500", clicks=60)
    a = _ask(api)
    today_sum = period_summary(conn, Window(1, end_date=today()))
    assert today_sum["orders"] == 3
    assert _money(today_sum["net_profit"]) in a[0]["answer"] and a[0]["tools_used"] == ["get_today_summary"]
    assert any("finance_view" in s for s in a[0]["sources"])
    assert a[1]["tools_used"] == ["get_today_summary"] and _money(today_sum["product_cost"]) in a[1]["answer"]   # "Neden?" bugüne bağlı
    assert "Kötü Çanta" in a[2]["answer"] and "net kâra göre" in a[2]["answer"]
    assert "10.000,00 ₺ reklam bütçesinin kanıtla desteklenen kısmı" in a[3]["answer"] and "önermiyorum" in a[3]["answer"]
    assert "Bunu söylemek için yeterli verim yok" in a[4]["answer"]   # henüz ölçülmüş kararı yok — uydurmaz
    hist = api.get(f"/api/ai/chat/{a[0]['conversation_id']}").json()
    assert len(hist) == 10 and all(m["tools_used"] for m in hist if m["role"] == "assistant")


def test_1b_stance_uses_total_profit_not_only_frequency(engine, conn, api):
    """Çoğu küçük kötüleşme + birkaç büyük kazanç: toplam etki pozitifse CEO itiraz ETMEZ."""
    for i in range(5):
        pid = product(conn, f"MIX-{i}", cost="150", price="600")
        cid = bare_campaign(conn, f"Karışık {i}", pid)
        did = api.post("/api/ai/decisions", json={"decision_type": "ads.pause", "entity_type": "campaign", "entity_id": cid},
                       headers=H).json()["decision_id"]
        shift_decision(conn, did)
        if i < 3:   # küçük kötüleşme: 2 adet → 1 adet
            sell(conn, pid, price="600", n=2, days_ago=11, tag=f"b{i}")
            sell(conn, pid, price="600", n=1, days_ago=4, tag=f"a{i}")
        else:       # büyük kazanç: ağır reklam zararı durdu
            spend(conn, cid, [10, 11, 12, 13, 14], amount="900")
            sell(conn, pid, price="600", n=1, days_ago=11, tag=f"b{i}")
            sell(conn, pid, price="600", n=1, days_ago=4, tag=f"a{i}")
    evaluate(engine)
    pid = product(conn, "MIX-NEW", cost="150", price="600")
    a = api.post("/api/ai/decisions/assess", json={"decision_type": "ads.pause", "entity_type": "campaign",
                                                   "entity_id": bare_campaign(conn, "Karışık yeni", pid)}, headers=H).json()
    ev = a["business_evidence"]
    assert ev["worsened"] == 3 and ev["improved"] == 2 and Decimal(str(ev["profit_effect"])) > 0
    assert a["stance"] == "neutral"
