"""CEO ile Konuş.

İki motor, AYNI salt okunur araçlar:
  * `rules`  (varsayılan; API anahtarı gerekmez): soru niyetini anahtar kelimelerle bulur, araçları çağırır, cevabı
             şablonla Türkçe yazar. Her rakam araçtan gelir.
  * `claude` (ANTHROPIC_API_KEY tanımlı ve `ai.llm_enabled` açıksa): Claude araçları kendisi seçer ve sonucu
             doğal dille anlatır. Araçlar salt okunurdur; model hiçbir aksiyon alamaz/onaylayamaz.
Model hatası veya kapalıysa kural motoruna düşülür (sohbet hiç cevapsız kalmaz).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import timedelta
from decimal import Decimal

from sqlalchemy.engine import Connection

from ...config import get_settings
from ...db import rows
from .. import app_settings, finance_view
from . import agents
from .config import Window, d, thresholds, today

log = logging.getLogger("trendhub.ai.chat")


def _money(v) -> str:
    from ...storefront.store_config import fmt_try
    return fmt_try(v)


def _pct(v) -> str:
    return "—" if v is None else f"%{Decimal(v) * 100:.1f}"


# ------------------------------------------------------------------ araçlar (salt okunur)
def tool_period(conn: Connection, days: int = 7) -> dict:
    from .data import period_summary
    days = max(1, min(int(days or 7), 365))
    cur = Window(days, end_date=today() - timedelta(days=1)) if days > 1 else Window(1, end_date=today() - timedelta(days=1))
    return {"current": period_summary(conn, cur), "previous": period_summary(conn, cur.previous())}


def tool_profit_drop(conn: Connection, days: int = 7) -> dict:
    from .data import product_economics
    p = tool_period(conn, days)
    c, b = p["current"], p["previous"]
    comps = {"ciro (net satış)": (d(c["net_sales"]) - d(b["net_sales"])),
             "ürün maliyeti": -(d(c["product_cost"]) - d(b["product_cost"])),
             "komisyon": -(d(c["commission"]) - d(b["commission"])), "kargo": -(d(c["shipping"]) - d(b["shipping"])),
             "reklam harcaması": -(d(c["ad_spend"]) - d(b["ad_spend"])), "dönem giderleri": -(d(c["expenses"]) - d(b["expenses"])),
             "tahmini KDV": -(d(c["vat_estimate"]) - d(b["vat_estimate"]))}
    cw = Window(int(c["window"]["days"]), end_date=today() - timedelta(days=1))
    now_p = {e["product_id"]: e for e in product_economics(conn, cw)}
    prev_p = {e["product_id"]: e for e in product_economics(conn, cw.previous())}
    changes = []
    for pid in set(now_p) | set(prev_p):
        a, z = now_p.get(pid), prev_p.get(pid)
        delta = (a["net_profit"] if a else Decimal("0")) - (z["net_profit"] if z else Decimal("0"))
        changes.append({"product_id": pid, "name": (a or z)["name"], "profit_change": finance_view.q2(delta),
                        "units_now": a["units"] if a else 0, "units_before": z["units"] if z else 0})
    changes.sort(key=lambda x: x["profit_change"])
    return {"current_profit": c["net_profit"], "previous_profit": b["net_profit"],
            "profit_change": finance_view.q2(d(c["net_profit"]) - d(b["net_profit"])),
            "drivers": sorted(({"component": k, "profit_effect": finance_view.q2(v)} for k, v in comps.items()),
                              key=lambda x: x["profit_effect"]),
            "worst_products": changes[:5], "window_days": c["window"]["days"]}


def tool_products(conn: Connection, product_class: str | None = None, limit: int = 10) -> dict:
    items = agents.classified_products(conn)
    if product_class:
        items = [i for i in items if i["class"] == product_class.upper()]
    key = (lambda x: x["net_profit"]) if (product_class or "").upper() == "LOSS" else (lambda x: -x["net_profit"])
    items.sort(key=key)
    keep = ("product_id", "sku", "name", "class", "class_reason", "units", "net_sales", "profit_before_ads",
            "ad_spend_allocated", "net_profit", "net_margin", "return_rate", "missing_cost")
    return {"window_days": thresholds(conn)["analysis_days"], "count": len(items),
            "products": [{k: i[k] for k in keep} for i in items[:max(1, min(int(limit or 10), 50))]]}


def tool_ads(conn: Connection) -> dict:
    camps = agents.advertising_analysis(conn)
    keep = ("id", "name", "channel", "daily_budget", "spend", "clicks", "attributed_orders", "attributed_revenue", "roas",
            "conversion_rate", "product_margin_before_ads", "ad_net_profit", "net_margin_after_ads", "verdict", "verdict_reason",
            "stock_risk")
    return {"window_days": thresholds(conn)["ads_window_days"], "campaigns": [{k: c[k] for k in keep} for c in camps]}


def tool_ad_budget_plan(conn: Connection, amount: float) -> dict:
    """Verilen reklam bütçesinin (30 gün) ne kadarının kanıtla desteklendiği. Kalan kullanılmaz."""
    th = thresholds(conn)
    amount = d(amount)
    camps = agents.advertising_analysis(conn)
    plan, used = [], Decimal("0")
    for c in sorted(camps, key=lambda x: -(x["ad_net_profit"] or Decimal("-1e9"))):
        if c["verdict"] not in ("INCREASE_BUDGET", "CONTINUE") or c["ad_net_profit"] is None or c["ad_net_profit"] < 0:
            continue
        daily = d(c["daily_budget"]) if c["daily_budget"] else c["spend"] / max(1, th["ads_window_days"])
        factor = 1 + d(th["ads_max_budget_step"]) if c["verdict"] == "INCREASE_BUDGET" else Decimal("1")
        need = finance_view.q2(daily * factor * 30)
        give = min(need, amount - used)
        if give <= 0:
            break
        plan.append({"campaign": c["name"], "verdict": c["verdict"], "monthly": give, "reason": c["verdict_reason"]})
        used += give
    test = Decimal("0")
    if not plan:
        stars = [p for p in agents.classified_products(conn) if p["class"] == "STAR"]
        if stars:
            test = min(amount, d(th["ads_min_spend"]) * 2)
            plan.append({"campaign": f"Yeni test: {stars[0]['name']}", "verdict": "TEST", "monthly": test,
                         "reason": ("Kanıtlanmış kârlı reklam yok. En kârlı ürün için yalnızca karar verecek kadar veri "
                                    f"toplayan küçük bir test ({test} TL) öneriyorum; sonuç ölçülmeden fazlası harcanmamalı.")})
            used = test
    return {"requested": amount, "justified": finance_view.q2(used), "unused": finance_view.q2(amount - used), "plan": plan,
            "excluded_losing": [c["name"] for c in camps if c["ad_net_profit"] is not None and c["ad_net_profit"] < 0]}


def tool_inventory(conn: Connection, limit: int = 10) -> dict:
    from .data import inventory_status
    items = inventory_status(conn)
    keep = ("product_id", "sku", "name", "available", "supplier_stock", "daily_velocity", "days_of_inventory", "stockout_risk",
            "dead_stock")
    risky = [i for i in items if i["stockout_risk"]]
    return {"stockout_risk_count": len(risky), "products": [{k: i[k] for k in keep} for i in risky[:limit]]}


def tool_capital(conn: Connection, hypothetical_amount: float | None = None) -> dict:
    from .capital import position
    p = position(conn)
    out = {k: p[k] for k in ("usable", "justified", "unused", "reserve_required", "liabilities", "pending_payout_not_counted",
                             "recommendation", "justified_by_category", "efficiency")}
    if hypothetical_amount:
        amt = d(hypothetical_amount)
        out["hypothetical"] = {"amount": amt, "justified": min(amt, p["justified"]),
                               "unused": max(Decimal("0"), amt - p["justified"]),
                               "message": (f"{_money(amt)} eklesen bile şu an kanıtla desteklenen kullanım {_money(min(amt, p['justified']))}; "
                                           f"kalan {_money(max(Decimal('0'), amt - p['justified']))} kasada beklemeli.")}
    return out


def tool_approvals(conn: Connection) -> dict:
    items = rows(conn, """SELECT id, agent_code, action_type, title, reason, risk_level, status, ceo_note, required_capital
                            FROM ai_proposals WHERE status IN ('pending_approval', 'approved', 'blocked')
                           ORDER BY CASE status WHEN 'pending_approval' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END, id DESC LIMIT 20""")
    return {"items": items}


def tool_decision_quality(conn: Connection, days: int = 30) -> dict:
    from .decisions import quality_report
    r = quality_report(conn, days)
    slim = lambda xs: [{"title": i["title"] or i["decision_type"], "decision": i["decision"], "result": i["result"],  # noqa: E731
                        "profit_change": i["profit_change"]} for i in xs[:10]]
    return {"owner_successes": slim(r["owner_successes"]), "owner_failures": slim(r["owner_failures"]),
            "ai_mistakes": slim(r["ai_mistakes"]), "missed_opportunities": slim(r["missed_opportunities"]),
            "lessons": r["lessons"], "awaiting_measurement": r["awaiting_measurement"], "note": r["note"]}


def tool_brief(conn: Connection) -> dict:
    from .ceo import build_brief
    return build_brief(conn)


TOOLS = {
    "get_daily_brief": (tool_brief, "Bugün bilinmesi gereken en önemli konular (önceliklendirilmiş).", {}),
    "get_period_summary": (tool_period, "Son N günün sipariş, ciro, gider ve tahmini net kâr özeti + önceki eş dönem.",
                           {"days": {"type": "integer", "description": "Gün sayısı (1 = dün)"}}),
    "get_profit_drop_analysis": (tool_profit_drop, "Net kârın önceki döneme göre neden değiştiği: gider kalemleri ve en çok kâr kaybeden ürünler.",
                                 {"days": {"type": "integer"}}),
    "get_products": (tool_products, "Ürün birim ekonomisi ve sınıfı (STAR, PROFITABLE, WATCH, LOSS, NO_DATA).",
                     {"product_class": {"type": "string", "enum": ["STAR", "PROFITABLE", "WATCH", "LOSS", "NO_DATA"]},
                      "limit": {"type": "integer"}}),
    "get_ads_analysis": (tool_ads, "Reklam kampanyalarının net kâr etkisi ve ajan kararı.", {}),
    "get_ad_budget_plan": (tool_ad_budget_plan, "Verilen aylık reklam bütçesinin kanıtla desteklenen kullanımı.",
                           {"amount": {"type": "number"}}),
    "get_inventory_risks": (tool_inventory, "Stok tükenme riski olan ürünler (satış hızı, kalan gün).", {"limit": {"type": "integer"}}),
    "get_capital_position": (tool_capital, "Nakit/sermaye pozisyonu; isteğe bağlı varsayımsal ek sermaye için kullanım.",
                             {"hypothetical_amount": {"type": "number"}}),
    "get_pending_approvals": (tool_approvals, "Onay bekleyen, uygulanmayı bekleyen ve bloke edilen öneriler.", {}),
    "get_decision_quality": (tool_decision_quality, "Sahip ve AI kararlarının ölçülmüş sonuçları, tekrarlanan hatalar.",
                             {"days": {"type": "integer"}}),
}


def call_tool(conn: Connection, name: str, args: dict) -> dict:
    fn, _desc, schema = TOOLS[name]
    clean = {k: v for k, v in (args or {}).items() if k in schema}
    return fn(conn, **clean)


# ------------------------------------------------------------------ kural motoru
def _fold(s: str) -> str:
    return s.replace("İ", "i").replace("I", "ı").lower()


def parse_amount(q: str) -> Decimal | None:
    m = re.search(r"(\d{1,3}(?:[.\s]\d{3})+|\d+(?:[.,]\d+)?)\s*(bin|k)?\s*(tl|₺|lira)?", _fold(q))
    if not m:
        return None
    num = m.group(1).replace(" ", "")
    num = num.replace(".", "") if re.match(r"^\d{1,3}(\.\d{3})+$", num) else num.replace(",", ".")
    v = Decimal(num)
    if m.group(2):
        v *= 1000
    return v if v > 0 else None


def rules_answer(conn: Connection, question: str) -> tuple[str, list[str]]:
    q = _fold(question)
    used: list[str] = []

    def use(name, **kw):
        used.append(name)
        return call_tool(conn, name, kw)

    amount = parse_amount(question)
    if any(w in q for w in ("sermaye", "koyarsam", "yatırır", "yatırsam", "kasaya", "nakit")):
        c = use("get_capital_position", hypothetical_amount=float(amount) if amount else None)
        lines = [c["recommendation"]]
        if c.get("hypothetical"):
            lines.append(c["hypothetical"]["message"])
        if c["justified_by_category"]:
            lines.append("Gerekçelendirilmiş kullanım: " + ", ".join(f"{x['label']} {_money(x['amount'])}" for x in c["justified_by_category"]) + ".")
        if c["pending_payout_not_counted"]:
            lines.append(f"Bekleyen hakediş {_money(c['pending_payout_not_counted'])} henüz kasada olmadığı için kullanılabilir sayılmadı.")
        return "\n".join(lines), used
    if "bütçe" in q or ("reklam" in q and amount):
        if amount:
            p = use("get_ad_budget_plan", amount=float(amount))
            lines = [f"{_money(p['requested'])} reklam bütçesinin kanıtla desteklenen kısmı {_money(p['justified'])}; "
                     f"{_money(p['unused'])} kullanılmamalı."]
            lines += [f"• {x['campaign']}: {_money(x['monthly'])}/ay — {x['reason']}" for x in p["plan"]]
            if p["excluded_losing"]:
                lines.append("Zarar eden kampanyalara pay verilmedi: " + ", ".join(p["excluded_losing"]) + ".")
            if not p["plan"]:
                lines.append("Kârlılığı kanıtlanmış reklam ya da yıldız ürün yok. Bu bütçeyi şimdilik kullanmamayı öneriyorum.")
            return "\n".join(lines), used
    if "reklam" in q:
        a = use("get_ads_analysis")
        if not a["campaigns"]:
            prods = use("get_products", product_class="STAR", limit=5)["products"]
            txt = "Aktif reklam kampanyası kaydı yok (Reklam merkezine harcama ve performans girilmeli)."
            if prods:
                txt += " Reklam verilecek aday (yıldız) ürünler: " + ", ".join(f"{p['name']} (net marj {_pct(p['net_margin'])})" for p in prods) + "."
            return txt, used
        lines = [f"Son {a['window_days']} gün reklam değerlendirmesi (karar ölçütü ROAS değil, reklam sonrası net kâr):"]
        for c in a["campaigns"]:
            net = f"net {_money(c['ad_net_profit'])}" if c["ad_net_profit"] is not None else "net etki ölçülemiyor"
            lines.append(f"• {c['name']}: {agents.AD_VERDICTS[c['verdict']]} — {net}. {c['verdict_reason']}")
        stars = use("get_products", product_class="STAR", limit=5)["products"]
        if stars:
            lines.append("Reklama en uygun (yıldız) ürünler: " + ", ".join(p["name"] for p in stars) + ".")
        return "\n".join(lines), used
    if "zarar" in q or "kaybett" in q:
        p = use("get_products", product_class="LOSS", limit=10)
        if not p["products"]:
            return f"Son {p['window_days']} günde zarar eden ürün yok (maliyeti eksik ürünler hariç; onlar 'veri yok' sınıfında).", used
        lines = [f"Son {p['window_days']} günde {p['count']} ürün zarar etti:"]
        lines += [f"• {x['name']}: {x['units']} adet, net {_money(x['net_profit'])} (reklam öncesi {_money(x['profit_before_ads'])}, "
                  f"reklam payı {_money(x['ad_spend_allocated'])})" for x in p["products"]]
        return "\n".join(lines), used
    if re.search(r"k[âa]r\b|kârı|karı|düş", q):
        r = use("get_profit_drop_analysis", days=7)
        lines = [f"Son {r['window_days']} günün tahmini net kârı {_money(r['current_profit'])}; önceki eş dönem {_money(r['previous_profit'])} "
                 f"(fark {_money(r['profit_change'])})."]
        neg = [x for x in r["drivers"] if x["profit_effect"] < 0][:3]
        if neg:
            lines.append("Kârı en çok aşağı çeken kalemler: " + ", ".join(f"{x['component']} ({_money(x['profit_effect'])})" for x in neg) + ".")
        bad = [x for x in r["worst_products"] if x["profit_change"] < 0][:3]
        if bad:
            lines.append("En çok kâr kaybeden ürünler: " + ", ".join(f"{x['name']} ({_money(x['profit_change'])})" for x in bad) + ".")
        return "\n".join(lines), used
    if "stok" in q:
        r = use("get_inventory_risks")
        if not r["products"]:
            return "Satış hızına göre tükenme riski olan ürün yok.", used
        return "\n".join([f"{r['stockout_risk_count']} üründe tükenme riski var:"] + [
            f"• {p['name']}: {p['available']} adet, günde {p['daily_velocity']} satış → "
            + (f"~{p['days_of_inventory']} gün" if p["days_of_inventory"] is not None else "stok yok") for p in r["products"]]), used
    if any(w in q for w in ("yanlış", "hata", "karar", "ne öğren")):
        r = use("get_decision_quality", days=30)
        lines = []
        if r["owner_failures"]:
            lines.append("Net kârı düşüren kararların: " + "; ".join(f"{x['title']} ({_money(x['profit_change'])})" for x in r["owner_failures"]) + ".")
        if r["ai_mistakes"]:
            lines.append("Benim (AI) önerip senin uyguladığın ve kâr düşüren kararlar: " + "; ".join(x["title"] for x in r["ai_mistakes"]) + ".")
        if r["owner_successes"]:
            lines.append("İşe yarayan kararlar: " + "; ".join(x["title"] for x in r["owner_successes"]) + ".")
        lines += r["lessons"]
        if r["awaiting_measurement"]:
            lines.append(f"{r['awaiting_measurement']} kararın sonucu henüz ölçülmedi.")
        lines.append(r["note"])
        return "\n".join(lines), used
    if any(w in q for w in ("onay", "bekleyen")):
        r = use("get_pending_approvals")
        if not r["items"]:
            return "Bekleyen öneri yok.", used
        return "\n".join(f"• [{x['status']}] {x['title']} — {x['reason'][:160]}" for x in r["items"][:10]), used
    b = use("get_daily_brief")
    lines = ["Bugün bilmen gerekenler:"] + [f"{i + 1}. {it['text']}" for i, it in enumerate(b["items"])]
    if "bugün" not in q and "ne oldu" not in q and "özet" not in q:
        lines.append("\nŞunları da sorabilirsin: neden kâr düştü, zarar eden ürünler, hangi ürünlere reklam vermeliyiz, "
                     "5000 TL reklam bütçesi, 50.000 TL sermaye, stok riski, bu ay neyi yanlış yaptım.")
    return "\n".join(lines), used


# ------------------------------------------------------------------ Claude motoru
SYSTEM_PROMPT = """Sen Trendçantanız'ın (Trendyol ve kendi web sitesinde kadın çantası satan bir e-ticaret işletmesi) CEO ajanısın.
Tek hedefin işletmenin sürdürülebilir NET KÂRINI artırmak; ciroyu tek başına başarı sayma.

Kurallar:
- Her rakamı araçlardan al. Araçta olmayan veriyi tahmin etme, uydurma; "bu veri sistemde yok" de ve hangi verinin girilmesi gerektiğini söyle.
- Finans rakamları tahminidir (hakediş verisi bağlı değil); bunu gerektiğinde belirt. Kâr ile nakit aynı şey değildir.
- Reklamda ROAS tek başına karar ölçütü değildir; reklam sonrası net kâra ve net marja bak.
- Mevcut sermayenin tamamını kullanmayı önermek zorunda değilsin; kanıt yoksa "şu anda ek sermaye kullanmayı önermiyorum" de.
- İşletme sahibini memnun etmeye çalışma. Kanıt sahibin tercihinin aleyhineyse bunu açıkça ve saygılı biçimde söyle.
- Hiçbir aksiyon alamazsın ve hiçbir öneriyi onaylayamazsın; onaylar paneldeki Onaylar ekranından sahip tarafından yapılır.
- Türkçe, kısa ve net yaz. Önce cevabı ver, sonra en fazla 3-5 maddelik gerekçe. Para birimi TL."""


def _claude_tools() -> list[dict]:
    return [{"name": n, "description": desc, "input_schema": {"type": "object", "properties": schema, "additionalProperties": False}}
            for n, (_fn, desc, schema) in TOOLS.items()]


def llm_available(conn: Connection) -> bool:
    s = get_settings()
    return bool(s.anthropic_api_key.strip()) and bool(app_settings.get(conn, "ai.llm_enabled", True))


def claude_answer(conn: Connection, question: str, history: list[dict]) -> tuple[str, list[str], dict]:
    import anthropic

    s = get_settings()
    client = anthropic.Anthropic(api_key=s.anthropic_api_key.strip(), timeout=60.0, max_retries=2)
    messages = [*history[-10:], {"role": "user", "content": question}]
    used: list[str] = []
    usage = {"input_tokens": 0, "output_tokens": 0, "requests": 0}
    for _ in range(6):
        resp = client.beta.messages.create(
            model=s.ai_model, max_tokens=16000, system=SYSTEM_PROMPT, tools=_claude_tools(), messages=messages,
            output_config={"effort": "medium"}, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        usage["requests"] += 1
        usage["input_tokens"] += resp.usage.input_tokens
        usage["output_tokens"] += resp.usage.output_tokens
        if resp.stop_reason == "refusal":
            raise RuntimeError("Model isteği reddetti")
        if resp.stop_reason != "tool_use":
            text_out = "\n".join(b.text for b in resp.content if b.type == "text").strip()
            return text_out or "Cevap üretilemedi.", used, usage
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for b in resp.content:
            if b.type != "tool_use":
                continue
            used.append(b.name)
            try:
                if b.name not in TOOLS:
                    raise KeyError(b.name)
                content = json.dumps(call_tool(conn, b.name, dict(b.input or {})), ensure_ascii=False, default=str)[:30000]
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": content})
            except Exception as exc:  # noqa: BLE001
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": f"Hata: {exc}", "is_error": True})
        messages.append({"role": "user", "content": results})
    return "Soru çok sayıda adım gerektirdi; lütfen daha dar sorun.", used, usage


def answer(conn: Connection, question: str, history: list[dict]) -> dict:
    if llm_available(conn):
        try:
            text_out, used, usage = claude_answer(conn, question, history)
            return {"answer": text_out, "engine": "claude", "tools_used": used, "usage": usage}
        except Exception as exc:  # noqa: BLE001 - model hatasında kural motoru
            log.warning("CEO sohbeti Claude hatası, kural motoruna geçildi: %s", exc.__class__.__name__)
    text_out, used = rules_answer(conn, question)
    return {"answer": text_out, "engine": "rules", "tools_used": used, "usage": {}}
