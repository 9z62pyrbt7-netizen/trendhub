"""CEO niyet/orkestrasyon kabul ölçütleri: büyüme stratejisi, doğal dil reklam bütçesi, varlık-farkındalıklı para ayrıştırma,
zorunlu Kâr Koruması + Bütçe Yöneticisi zinciri, belirsiz "bu ürün" → NEEDS_CLARIFICATION. Hiçbir platform yazması yapılmaz."""
from decimal import Decimal

import pytest
from sqlalchemy import text

from tests.test_agent_runtime import store_with_sales
from tests.test_ai import H, cash, product, sell

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


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    from app.services.ai import orchestrator, tools
    monkeypatch.setattr(tools, "BACKOFF_BASE", 0)
    monkeypatch.setattr(orchestrator, "RETRY_BACKOFF", 0)


def no_platform_write(conn):
    assert conn.execute(text("SELECT COUNT(*) FROM ai_actions WHERE status = 'EXECUTED'")).scalar() == 0
    assert conn.execute(text("SELECT COUNT(*) FROM ai_proposals WHERE status IN ('approved', 'executed')")).scalar() == 0
    assert conn.execute(text("SELECT COUNT(*) FROM ai_budget_ledger")).scalar() == 0


# ======================================================================== para ayrıştırıcı
@pytest.mark.parametrize("text_, amount", [
    ("7000 TL", "7000"), ("7.000 TL", "7000"), ("7,000 TL", "7000"), ("7 bin TL", "7000"), ("7 bin lira", "7000"),
    ("7000₺", "7000"), ("7000TL", "7000"), ("7 000 TL", "7000"), ("7.000,50 TL", "7000.50"), ("7,50 TL", "7.50"),
    ("7,5 bin TL", "7500"), ("SKU-X'e 7 bin liralık reklam ver", "7000"), ("PRE-1 ürününe 7000 TL reklam aç", "7000"),
    ("ABC123 için 2500 lira", "2500"), ("%5 indirim ve 300 TL bütçe", "300"),
    ("8690000000200 barkodlu ürüne 7.000 TL", "7000"), ("TC-100 kodlu ürüne 1.250 TL", "1250"),
])
def test_money_parser(text_, amount):
    from app.services.ai.money import parse_money
    assert parse_money(text_).amount == Decimal(amount)


@pytest.mark.parametrize("text_", ["PRE-1 ürününe reklam aç", "TC-100 7000", "SKU-12 için reklam", "%20 indirim", "sipariş 7000 adet"])
def test_money_parser_never_takes_bare_or_code_numbers(text_):
    from app.services.ai.money import parse_money
    assert parse_money(text_).amount is None


def test_money_parser_ambiguous_amounts_are_not_guessed():
    from app.services.ai.money import parse_money
    r = parse_money("1.000 TL mi yoksa 2.000 TL mi koyalım")
    assert r.amount is None and r.ambiguous


def test_money_parser_excludes_entity_tokens():
    from app.services.ai.money import parse_money
    assert parse_money("7 TL ürününe 500 TL", exclude=("7 TL ürün",)).amount == Decimal("500")


# ======================================================================== niyetler
GROWTH = ["Satışları artırmak için ne yapmalıyız?", "Satışları nasıl artırabiliriz?", "Satışları artır.", "Mağazayı nasıl büyütürüz?",
          "Daha fazla satış istiyorum.", "Ne yaparsak daha çok satarız?", "Bugün satışları artırmak için ne yapabiliriz?"]


@pytest.mark.parametrize("msg", GROWTH)
def test_growth_phrases_are_strategic_growth_not_general_chat(engine, msg):
    from app.services.ai import orchestrator
    for mode in ("api", "chat"):
        p = orchestrator.plan(engine, msg, mode)
        assert p["intent"] == "growth.strategy", (msg, mode)
        assert [t[:2] for t in p["tasks"]][:4] == [("analytics", "store_health"), ("finance", "profitability"),
                                                     ("product_trend", "portfolio"), ("marketing", "growth_plan")]


@pytest.mark.parametrize("msg", ["SKU-X ürününe 7.000 TL reklam aç.", "SKU-X için 7000 TL reklam bütçesi ver.",
                                 "SKU-X'e 7 bin liralık reklam ver.", "SKU-X ürününe 7000 TL reklam bütçesi ayır.",
                                 "SKU-X için reklam için 7000 TL koy.", "SKU-X ürününe 7.000 TL reklam açalım."])
def test_ad_budget_phrases_normalize_to_same_action(engine, conn, msg):
    from app.services.ai import orchestrator
    pid = product(conn, "SKU-X", cost="150", price="600")
    p = orchestrator.plan(engine, msg, "chat")
    assert p["intent"] == "action.ads" and p["product"]["id"] == pid and p["amount"] == Decimal("7000"), (msg, p)
    assert [t[1] for t in p["tasks"]] == ["product_check", "portfolio", "budget_check", "create_campaign"]
    create = p["tasks"][-1][2]
    assert Decimal(create["daily_budget"]) * create["days"] == Decimal("7000.00")


@pytest.mark.parametrize("msg", ["Bu ürüne 7000 TL reklam bütçesi ayır.", "Bu ürüne 7.000 TL reklam açalım.",
                                 "Bu ürüne reklam için 7000 TL koy.", "Bu ürüne 7000 TL reklam ver."])
def test_this_product_without_context_needs_clarification(engine, msg):
    from app.services.ai import orchestrator
    p = orchestrator.plan(engine, msg, "chat")
    assert p["intent"] == "action.needs_clarification" and p["decision"] == "NEEDS_CLARIFICATION" and not p["tasks"]


def test_questions_about_ads_still_go_to_existing_chat(engine):
    from app.services.ai import orchestrator
    for m in ("Şu anda reklama 10.000 TL daha koymalı mıyım?", "5000 TL reklam bütçesini nasıl kullanmalıyız?"):
        assert orchestrator.plan(engine, m, "chat")["intent"] == "other"


# ======================================================================== büyüme E2E
def test_growth_e2e_delegates_verifies_and_returns_max_5_evidence_actions(engine, conn, api):
    store_with_sales(conn)
    r = api.post("/api/ai/chat", json={"message": "Satışları artırmak için ne yapmalıyız?"}, headers=H).json()
    assert r["engine"] == "orchestrator" and r["intent"] == "growth.strategy", r.get("engine")
    rid = r["request_id"]
    tasks = conn.execute(text("SELECT agent_code, task_type, status FROM ai_agent_tasks WHERE request_id = :r ORDER BY id"),
                         {"r": rid}).all()
    agents_ = [t[0] for t in tasks]
    assert agents_[:4] == ["analytics", "finance", "product_trend", "marketing"] and "advertising" in agents_
    assert agents_[-1] == "reality_checker" and all(t[2] == "completed" for t in tasks)
    v = r["verification"]
    assert v["claims"] > 0 and v["counts"]["FAILED"] == 0 and v["reruns"] > 0
    acts = r["actions"]
    assert 1 <= len(acts) <= 5
    fields = ("action", "why", "evidence_ids", "benefit", "profit_effect", "cost", "risk", "feasibility", "confidence", "approval",
              "verification")
    for a in acts:
        assert all(a.get(f) not in (None, "") for f in fields), a
        assert a["evidence_ids"] and a["risk"] in ("LOW", "MEDIUM", "HIGH", "CRITICAL") and 0 <= a["confidence"] <= 1
    kinds = [a["kind"] for a in acts]
    assert kinds[0] == "stop_loss"                               # önce zararı durdur
    assert "Zarar Eden Çanta" in acts[0]["action"]
    assert any(k == "stop_ad_loss" for k in kinds)
    promote = next(a for a in acts if a["kind"] == "promote")
    assert "Hipotez" in promote["profit_effect"] and "garanti değil" in promote["profit_effect"]   # kesin iddia yok
    ans = r["answer"]
    assert "**1. " in ans and "- Kanıt: #" in ans and "Hiçbir aksiyon uygulanmadı" in ans and "**6. " not in ans
    assert conn.execute(text("SELECT COUNT(*) FROM ai_tool_calls WHERE request_id = :r AND access = 'WRITE'"), {"r": rid}).scalar() == 0
    assert conn.execute(text("SELECT COUNT(*) FROM ai_proposals")).scalar() == 0
    no_platform_write(conn)


def test_growth_unverified_evidence_hides_numbers(engine, conn, monkeypatch):
    from app.services.ai import orchestrator, reality
    store_with_sales(conn)
    real = reality.Checker.verify

    def distrust(self, ev, by_id):
        st, val, note = real(self, ev, by_id)
        return ("FAILED", val, "test: güvenilmedi") if (ev["metric"] or "").startswith("growth.") else (st, val, note)
    monkeypatch.setattr(reality.Checker, "verify", distrust)
    out = orchestrator.handle(engine, "Satışları nasıl artırabiliriz?", source="api")
    assert out["actions"]
    for a in out["actions"]:
        assert a["verification"] == "FAILED" and a["profit_effect"].startswith("Bilinmiyor") and a["confidence"] < 0.3
        assert "₺" not in a["why"]


def test_growth_on_empty_store_does_not_invent(engine, conn):
    from app.services.ai import orchestrator
    out = orchestrator.handle(engine, "Mağazayı nasıl büyütürüz?", source="api")
    assert out["intent"] == "growth.strategy" and out["actions"] == []
    assert "Kanıta dayalı aksiyon çıkarılamadı" in out["answer"]


# ======================================================================== reklam bütçesi: zorunlu güvenlik zinciri
def _tools(conn, rid):
    return [r[0] for r in conn.execute(text("SELECT tool FROM ai_tool_calls WHERE request_id = :r AND agent_code <> 'reality_checker' ORDER BY id"),
                                       {"r": rid})]


def test_sku_ad_budget_runs_profit_guard_then_budget_governor_no_write(engine, conn, api):
    cash(conn)
    pid = product(conn, "SKU-X", cost="150", price="600", stock=80, name="Kahve Çanta")
    sell(conn, pid, price="600", n=12, days_ago=3)
    r = api.post("/api/agents/requests", json={"message": "SKU-X ürününe 7.000 TL reklam bütçesi ver."}, headers=H).json()
    assert r["intent"] == "action.ads"
    g = r["guards"]
    assert g["product"] == {"id": pid, "sku": "SKU-X"} and Decimal(g["amount"]) == Decimal("7000")
    assert g["profit_guard"]["called"] and g["profit_guard"]["state"] == "SAFE"
    assert g["budget_governor"]["called"]
    t = _tools(conn, r["request_id"])
    assert t.index("get_profit_guard") < t.index("get_budget_status") < t.index("create_campaign")
    # Bütçe Yöneticisi: ürün başına 7 günlük 5.000 TL sınırı → engel; CEO karşı çıkar
    assert g["budget_governor"]["verdict"] == "BLOCKED" and "governor_campaign" in g["budget_governor"]["checks"]
    assert r["status"] == "blocked" and r["answer"].startswith("**KARŞIYIM.**") and "**Güvenlik zinciri:**" in r["answer"]
    p = conn.execute(text("SELECT status, required_capital FROM ai_proposals")).one()
    assert p == ("blocked", Decimal("7000.00"))
    no_platform_write(conn)


def test_pre1_regression_amount_is_7000_never_1(engine, conn, api):
    pid = product(conn, "PRE-1", cost="150", price="600", stock=50)
    sell(conn, pid, price="600", n=8, days_ago=2)
    r = api.post("/api/agents/requests", json={"message": "PRE-1 ürününe 7000 TL reklam aç."}, headers=H).json()
    assert r["guards"]["product"]["sku"] == "PRE-1" and Decimal(r["guards"]["amount"]) == Decimal("7000")
    cap = conn.execute(text("SELECT required_capital, params FROM ai_proposals")).one()
    assert cap[0] == Decimal("7000.00") and Decimal(cap[1]["daily_budget"]) == Decimal("1000.00")
    assert cap[0] != Decimal("1") and Decimal(cap[1]["daily_budget"]) > 1


def test_this_product_without_context_creates_nothing(engine, conn, api):
    product(conn, "SKU-X", cost="150", price="600")
    r = api.post("/api/ai/chat", json={"message": "Bu ürüne 7000 TL reklam ver."}, headers=H).json()
    assert r["decision"] == "NEEDS_CLARIFICATION" and r["status"] == "blocked" and "SKU" in r["answer"]
    assert conn.execute(text("SELECT COUNT(*) FROM ai_proposals")).scalar() == 0
    assert conn.execute(text("SELECT COUNT(*) FROM ai_tool_calls WHERE access = 'WRITE'")).scalar() == 0
    assert conn.execute(text("SELECT COUNT(*) FROM ai_agent_tasks WHERE request_id = :r"), {"r": r["request_id"]}).scalar() == 0


def test_this_product_uses_explicit_context_and_runs_guards(engine, conn, api):
    cash(conn)
    pid = product(conn, "SKU-X", cost="150", price="600", stock=80)
    sell(conn, pid, price="600", n=12, days_ago=3)
    r = api.post("/api/agents/requests", json={"message": "Bu ürüne 7000 TL reklam ver.", "context_product_id": pid}, headers=H).json()
    assert r["intent"] == "action.ads" and r["guards"]["product"]["id"] == pid
    assert r["guards"]["profit_guard"]["called"] and r["guards"]["budget_governor"]["called"]
    no_platform_write(conn)


def test_this_product_uses_conversation_context(engine, conn, api):
    cash(conn)
    pid = product(conn, "SKU-X", cost="150", price="600", stock=80)
    sell(conn, pid, price="600", n=12, days_ago=3)
    first = api.post("/api/ai/chat", json={"message": "SKU-X ürününe 700 TL reklam aç"}, headers=H).json()
    assert first["guards"]["product"]["id"] == pid
    r = api.post("/api/ai/chat", json={"message": "Bu ürüne 7000 TL reklam ver.", "conversation_id": first["conversation_id"]},
                 headers=H).json()
    assert r["intent"] == "action.ads" and r["guards"]["product"]["id"] == pid and Decimal(r["guards"]["amount"]) == 7000
    # başka konuşmada bağlam yok → tahmin edilmez
    other = api.post("/api/ai/chat", json={"message": "Bu ürüne 7000 TL reklam ver."}, headers=H).json()
    assert other["decision"] == "NEEDS_CLARIFICATION"


def test_loss_making_product_7000_is_blocked_ceo_opposes(engine, conn, api):
    ids = store_with_sales(conn)
    api.put("/api/agents/budget", json={"total_budget": 50000, "per_campaign_limit": 10000, "max_single_action_amount": 10000},
            headers=H)
    r = api.post("/api/agents/requests", json={"message": "RT-LOSS için 7000 TL reklam bütçesi ver."}, headers=H).json()
    assert r["guards"]["product"]["id"] == ids["bad"] and r["guards"]["profit_guard"]["state"] == "DANGER"
    assert "profit_guard_danger" in r["guards"]["profit_guard"]["checks"]
    assert r["status"] == "blocked" and r["answer"].startswith("**KARŞIYIM.**")
    assert "Kâr Koruması: DANGER" in r["answer"] and "net zarar" in r["answer"]
    no_platform_write(conn)


def test_unknown_profitability_7000_blocked_by_profit_guard_limit(engine, conn, api):
    ids = store_with_sales(conn)
    api.put("/api/agents/budget", json={"total_budget": 50000, "per_campaign_limit": 10000, "max_single_action_amount": 10000},
            headers=H)
    r = api.post("/api/agents/requests", json={"message": "RT-NOCOST ürününe 7.000 TL reklam aç"}, headers=H).json()
    g = r["guards"]
    assert g["product"]["id"] == ids["nocost"] and g["profit_guard"]["state"] == "UNKNOWN"
    assert {"profit_guard_unknown", "needs_data"} & set(g["profit_guard"]["checks"]) and r["status"] == "blocked"
    # küçük test (≤ 1.000 TL) güvenlik limitleri içinde ama maliyet yok → yine NEEDS_DATA ile engellenir
    small = api.post("/api/agents/requests", json={"message": "RT-NOCOST ürününe 700 TL reklam aç"}, headers=H).json()
    assert small["status"] == "blocked" and "needs_data" in small["guards"]["profit_guard"]["checks"]
    no_platform_write(conn)


def test_unknown_low_sample_small_test_needs_approval_with_warning(engine, conn, api):
    cash(conn)
    pid = product(conn, "FEW-1", cost="150", price="600", stock=40)
    sell(conn, pid, price="600", n=1, days_ago=2)
    api.put("/api/ai/settings", json={"thresholds": {"daily_ad_budget_limit": 5000}}, headers=H)
    r = api.post("/api/agents/requests", json={"message": "FEW-1 ürününe 700 TL reklam aç"}, headers=H).json()
    assert r["guards"]["profit_guard"]["state"] == "UNKNOWN" and r["guards"]["approval_required"] is True
    assert r["answer"].startswith("**Şartlı destekliyorum.**") and "henüz hiçbir şey uygulanmadı" in r["answer"]
    big = api.post("/api/agents/requests", json={"message": "FEW-1 ürününe 3.000 TL reklam aç"}, headers=H).json()
    assert big["status"] == "blocked" and "profit_guard_unknown" in big["guards"]["profit_guard"]["checks"]
    no_platform_write(conn)


def test_write_is_not_opened_if_guard_task_fails(engine, conn, monkeypatch):
    from app.services.ai import orchestrator
    pid = product(conn, "SKU-X", cost="150", price="600")

    def boom(rt, inp):
        raise ValueError("kâr verisi okunamadı")
    monkeypatch.setitem(orchestrator.HANDLERS, ("product_trend", "product_check"), boom)
    out = orchestrator.handle(engine, "SKU-X ürününe 700 TL reklam aç", source="api")
    w = next(t for t in out["tasks"] if t["task_type"] == "create_campaign")
    assert w["status"] == "blocked" and "Zorunlu güvenlik kontrolü" in w["error"] and not w["tool_calls"]
    assert conn.execute(text("SELECT COUNT(*) FROM ai_proposals WHERE entity_id = :p"), {"p": pid}).scalar() == 0
    assert out["status"] in ("blocked", "failed", "partial")
