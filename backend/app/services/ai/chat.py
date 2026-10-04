"""CEO ile Konuş.

İki motor, AYNI salt okunur araçlar:
  * `rules`  (varsayılan; API anahtarı gerekmez): soru niyetini anahtar kelimelerle bulur, araçları çağırır, cevabı
             şablonla Türkçe yazar. Her rakam araçtan gelir.
  * `claude` (ANTHROPIC_API_KEY tanımlı ve `ai.llm_enabled` açıksa): Claude araçları kendisi seçer ve sonucu
             doğal dille anlatır. Araçlar salt okunurdur; model hiçbir aksiyon alamaz/onaylayamaz.
Model hatası veya kapalıysa kural motoruna düşülür (sohbet hiç cevapsız kalmaz).
"""
from __future__ import annotations

import contextvars
import functools
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


NO_DATA = "Bunu söylemek için yeterli verim yok"


def tool_today(conn: Connection) -> dict:
    """Bugünün (şu ana kadar) sonucu + son 7 günün günlük ortalaması + veri kalitesi uyarıları."""
    from .data import data_quality, period_summary
    t = today()
    cur = period_summary(conn, Window(1, end_date=t))
    base = period_summary(conn, Window(7, end_date=t - timedelta(days=1)))
    avg = {k: finance_view.q2(d(base[k]) / 7) for k in ("orders", "net_sales", "product_cost", "commission", "shipping",
                                                        "service_fee", "ad_spend", "expenses", "vat_estimate", "net_profit", "refund")}
    return {"today": cur, "daily_average_7d": avg, "data_quality": data_quality(conn)}


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
                             "recommendation", "justified_by_category", "efficiency", "cash_structure")}
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


def tool_customer_signals(conn: Connection, days: int = 30) -> dict:
    """Gerçek müşteri soruları ve iadelerinden sinyaller. Müşteri adı/id içermez; metinler maskelenmiştir."""
    from ..platform.cx import analyze
    r = analyze(conn, max(7, min(int(days or 30), 90)))
    return {k: r[k] for k in ("days", "min_sample", "total_questions", "categories", "total_returns", "info_gaps", "complaints",
                              "has_data", "note")} | {"products": [
        {k: p[k] for k in ("name", "questions", "unanswered", "categories", "returns", "units_sold", "return_rate",
                           "return_rate_status", "return_reasons")} for p in r["products"][:10]]}


def tool_data_sources(conn: Connection) -> dict:
    """Her veri kaynağının bağlantı durumu ve tazeliği (FRESH / STALE / DEGRADED / ERROR)."""
    from ..platform.sources import data_sources
    keep = ("code", "label", "kind", "connection", "freshness", "last_successful_sync", "age_hours", "error_count")
    return {"sources": [{k: x.get(k) for k in keep} for x in data_sources(conn)]}


def tool_executive_summary(conn: Connection) -> dict:
    """CEO yönetici özeti: bugün, satış, net kâr (kaynağıyla), iyi/zararlı ürünler, stok, reklam adayları, aksiyonlar, onaylar."""
    from .ceo_review import executive_summary, latest_summary
    from .reconcile import coverage
    return latest_summary(conn) or executive_summary(conn, coverage(conn))


def tool_brief(conn: Connection) -> dict:
    from .ceo import build_brief
    return build_brief(conn)


TOOLS = {
    "get_daily_brief": (tool_brief, "Bugün bilinmesi gereken en önemli konular (önceliklendirilmiş).", {}),
    "get_today_summary": (tool_today, "Bugünün (şu ana kadar) sipariş, ciro, gider ve tahmini net kârı; son 7 gün günlük ortalaması.", {}),
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
    "get_customer_signals": (tool_customer_signals, "Gerçek müşteri soruları (kategori) ve iade sebepleri; ürün sayfası bilgi eksikleri.",
                             {"days": {"type": "integer"}}),
    "get_data_sources": (tool_data_sources, "Veri kaynaklarının tazeliği; finans/iade/soru verisi güncel mi.", {}),
    "get_executive_summary": (tool_executive_summary, "CEO'nun Türkçe yönetici özeti (bugün ne oldu, kâr, ürünler, stok, reklam, "
                              "yapılan/engellenen aksiyonlar, onay bekleyenler, sonraki 3 iş).", {}),
}


# Orkestratör sohbet motorunu çağırdığında her araç kullanımı ai_tool_calls'a yazılır (kanıt izi). Bağlam yoksa davranış aynı.
TOOL_LOG: contextvars.ContextVar = contextvars.ContextVar("trendhub_chat_tool_log", default=None)


def _logged(name: str, fn):
    @functools.wraps(fn)
    def wrapper(conn, *args, **kwargs):
        ctx = TOOL_LOG.get()
        if ctx is None:
            return fn(conn, *args, **kwargs)
        import time as _t
        import uuid as _u

        from sqlalchemy import text as _text

        from . import sanitize
        t0, status, err, res = _t.monotonic(), "succeeded", None, None
        try:
            res = fn(conn, *args, **kwargs)
            return res
        except Exception as exc:
            status, err = "failed", f"{exc.__class__.__name__}: {str(exc)[:300]}"
            raise
        finally:
            data = json.loads(json.dumps(res, default=str)) if res is not None else {}
            with ctx["engine"].begin() as c:
                c.execute(_text("""INSERT INTO ai_tool_calls(call_uid, request_id, run_id, agent_code, tool, access, risk_level, arguments,
                                                             status, completed_at, duration_ms, result_summary, result, result_hash, error)
                                   VALUES (:u, :r, :run, 'ceo', :t, 'READ', 'LOW', CAST(:a AS JSONB), :s, NOW(), :ms, :sum,
                                           CAST(:res AS JSONB), :h, :e)"""),
                          {"u": _u.uuid4().hex, "r": ctx["request_id"], "run": ctx["run_id"], "t": f"chat.{name}",
                           "a": sanitize.to_json({"args": list(args), **kwargs}), "s": status, "ms": int((_t.monotonic() - t0) * 1000),
                           "sum": f"sohbet aracı {name}: {status}", "res": sanitize.to_json(sanitize.bounded(sanitize.redact(data))),
                           "h": sanitize.digest(data), "e": err})
    return wrapper


for _name, (_fn, _desc, _schema) in list(TOOLS.items()):
    _w = _logged(_name, _fn)
    TOOLS[_name] = (_w, _desc, _schema)
    globals()[_fn.__name__] = _w


def call_tool(conn: Connection, name: str, args: dict) -> dict:
    fn, _desc, schema = TOOLS[name]
    clean = {k: v for k, v in (args or {}).items() if k in schema}
    return fn(conn, **clean)


def tool_payload_for_llm(conn: Connection, name: str, args: dict) -> str:
    """LLM'e giden araç çıktısı: JSON → kişisel veri maskesi → PII kontrolü (müşteri adı / telefon / e-posta / IBAN).
    Araçlar zaten müşteri alanı döndürmez; bu ikinci savunma hattıdır."""
    from ..platform.pii import assert_no_pii, scrub
    content = scrub(json.dumps(call_tool(conn, name, args), ensure_ascii=False, default=str))[:30000]
    names = [r["customer_name"] for r in rows(conn, """SELECT DISTINCT customer_name FROM orders
                                                         WHERE customer_name IS NOT NULL AND length(customer_name) >= 5
                                                         ORDER BY customer_name LIMIT 2000""")]
    return assert_no_pii(content, names)


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


SOURCES = {
    "get_today_summary": "Siparişler + kalem kârı (finance_view, dashboard ile aynı formül), bugün",
    "get_period_summary": "Siparişler + kalem kârı (finance_view), dönem giderleri, reklam harcaması",
    "get_profit_drop_analysis": "Dönem karşılaştırması (finance_view) + ürün ekonomisi",
    "get_products": "Ürün birim ekonomisi (finance_view kalem kârı − reklam payı)",
    "get_ads_analysis": "ad_spend + ad_performance + ürün marjları",
    "get_ad_budget_plan": "Reklam ajanı kararları + eşikler",
    "get_inventory_risks": "Ürün stoğu, tedarikçi stoğu, tüm kanal satışları",
    "get_capital_position": "Kasa/borç: elle girilen · hakediş/alacak: Trendyol Finance (cari hesap) · açık öneriler",
    "get_pending_approvals": "ai_proposals",
    "get_decision_quality": "Karar günlüğü + ölçülmüş sonuçlar (ai_decision_outcomes)",
    "get_daily_brief": "CEO günlük özeti",
    "get_customer_signals": "Trendyol Soru-Cevap + Trendyol İadeler (gerçek kayıtlar)",
    "get_data_sources": "Kaynak tazeliği (sync_state)",
    "get_executive_summary": "CEO yönetici özeti (ajan döngüsü)",
}


def provenance_lines(conn: Connection, start, end) -> list[str]:
    """Net kârın kaynağı: hangi bileşen gerçek pazaryeri verisi, hangisi tahmin."""
    from ..platform.finance import profit_provenance
    pv = profit_provenance(conn, start, end)
    if not pv["items"]:
        return []
    comm = ("Trendyol Finance (gerçek)" if pv["estimated_commission_items"] == 0 else
            f"Trendyol Finance {pv['actual_commission_items']}/{pv['items']} kalem gerçek, kalanı TAHMİN (oran)"
            if pv["actual_commission_items"] else "TAHMİN (komisyon oranı; gerçek finans kaydı henüz yok)")
    ship = (f"Trendyol kargo faturası ({pv['actual_shipping_orders']}/{pv['orders']} sipariş gerçek)" if pv["actual_shipping_orders"]
            else "TAHMİN (varsayılan kargo/desi)")
    return ["Kaynak:", "• satış: Trendyol Orders", f"• komisyon: {comm}", f"• kargo: {ship}",
            "• ürün maliyeti: tedarikçi / yerel maliyet kaydı", "• reklam: elle girilen reklam verisi (Trendyol reklam API'si bağlı değil)"]


def _today_answer(t: dict, conn: Connection | None = None) -> str:
    c, avg = t["today"], t["daily_average_7d"]
    if not c["orders"]:
        return f"{NO_DATA}: bugün henüz kayıtlı sipariş yok (son senkron verisine göre)."
    prov = []
    label = "tahmini net kâr"
    if conn is not None:
        w = Window(1, end_date=today())
        prov = provenance_lines(conn, w.start, w.end)
        from ..platform.finance import profit_provenance
        st = profit_provenance(conn, w.start, w.end)["status"]
        label = {"ACTUAL": "gerçekleşen net kâr", "PARTIAL": "net kâr (kısmen tahmini)"}.get(st, "tahmini net kâr")  # NO_DATA/ESTIMATED
    lines = [f"Bugün şu ana kadar {c['orders']} sipariş; {label} {_money(c['net_profit'])} "
             f"(net satış {_money(c['net_sales'])}, net marj {_pct(c['net_margin'])}). Son 7 günün günlük ortalaması {_money(avg['net_profit'])}."]
    if c["missing_cost_orders"]:
        lines.append(f"Uyarı: {c['missing_cost_orders']} siparişte ürün maliyeti eksik; kâr olduğundan yüksek görünür.")
    for q in t["data_quality"]:
        if q["code"] in ("orders_stale", "finance_stale"):
            lines.append("Uyarı: " + q["message"])
    return "\n".join(lines + prov)


def _why_today(t: dict) -> str:
    c, avg = t["today"], t["daily_average_7d"]
    if not c["orders"]:
        return f"{NO_DATA}: bugün sipariş olmadığı için açıklanacak bir kâr yok."
    parts = [("net satış", d(c["net_sales"]) - d(avg["net_sales"])), ("ürün maliyeti", -(d(c["product_cost"]) - d(avg["product_cost"]))),
             ("komisyon", -(d(c["commission"]) - d(avg["commission"]))), ("kargo", -(d(c["shipping"]) - d(avg["shipping"]))),
             ("reklam", -(d(c["ad_spend"]) - d(avg["ad_spend"]))), ("dönem giderleri", -(d(c["expenses"]) - d(avg["expenses"]))),
             ("tahmini KDV", -(d(c["vat_estimate"]) - d(avg["vat_estimate"])))]
    parts.sort(key=lambda x: x[1])
    lines = [f"Bugünün net kârı {_money(c['net_profit'])}: net satış {_money(c['net_sales'])} − ürün maliyeti {_money(c['product_cost'])} "
             f"− komisyon {_money(c['commission'])} − kargo {_money(c['shipping'])} − reklam {_money(c['ad_spend'])} "
             f"− dönem giderleri {_money(c['expenses'])} − tahmini KDV {_money(c['vat_estimate'])}."]
    neg = [f"{n} ({_money(finance_view.q2(v))})" for n, v in parts if v < 0][:3]
    pos = [f"{n} (+{_money(finance_view.q2(v))})" for n, v in parts[::-1] if v > 0][:2]
    if neg:
        lines.append("7 günlük ortalamaya göre kârı aşağı çekenler: " + ", ".join(neg) + ".")
    if pos:
        lines.append("Yukarı çekenler: " + ", ".join(pos) + ".")
    return "\n".join(lines)


def rules_answer(conn: Connection, question: str, last_tools: list[str] | None = None) -> tuple[str, list[str]]:
    q = _fold(question)
    used: list[str] = []
    last_tools = last_tools or []

    def use(name, **kw):
        used.append(name)
        return call_tool(conn, name, kw)

    amount = parse_amount(question)
    if re.match(r"^\s*(neden|niye|niçin|sebebi|nasıl yani)\b", q) and last_tools:
        if "get_today_summary" in last_tools:
            return _why_today(use("get_today_summary")), used
        r = use("get_profit_drop_analysis", days=7)
        neg = [x for x in r["drivers"] if x["profit_effect"] < 0][:3]
        if not neg:
            return f"{NO_DATA}: önceki döneme göre kârı aşağı çeken bir kalem görünmüyor.", used
        return "Önceki eş döneme göre kârı aşağı çeken kalemler: " + ", ".join(
            f"{x['component']} ({_money(x['profit_effect'])})" for x in neg) + ".", used
    if "bugün" in q and re.search(r"k[âa]r|kazan|ne kadar", q):
        return _today_answer(use("get_today_summary"), conn), used
    if re.search(r"en kötü|en zayıf|en çok zarar", q):
        p = use("get_products", limit=50)
        judged = [x for x in p["products"] if x["class"] != "NO_DATA"]
        if not judged:
            nodata = len(p["products"])
            return (f"{NO_DATA}: son {p['window_days']} günde karar verilecek kadar satışı ve maliyeti tam olan ürün yok"
                    + (f" ({nodata} ürün 'veri yok': satış az veya maliyet eksik)." if nodata else ".")), used
        w = min(judged, key=lambda x: x["net_profit"])
        return (f"Son {p['window_days']} günün en kötü ürünü: {w['name']} — {w['units']} adet, net satış {_money(w['net_sales'])}, "
                f"net kâr {_money(w['net_profit'])} (reklam payı {_money(w['ad_spend_allocated'])}, net marj {_pct(w['net_margin'])}). "
                "Sıralama ciroya değil net kâra göre."), used
    if any(w in q for w in ("sermaye", "koyarsam", "yatırır", "yatırsam", "kasaya", "nakit")) and not ("reklam" in q and amount):
        c = use("get_capital_position", hypothetical_amount=float(amount) if amount else None)
        lines = [c["recommendation"]]
        if c.get("hypothetical"):
            lines.append(c["hypothetical"]["message"])
        if c["justified_by_category"]:
            lines.append("Gerekçelendirilmiş kullanım: " + ", ".join(f"{x['label']} {_money(x['amount'])}" for x in c["justified_by_category"]) + ".")
        cs = c["cash_structure"]
        pend = cs["pending_marketplace_payout"]
        if pend["kind"] == "UNKNOWN":
            lines.append("Bekleyen Trendyol hakedişini bilmiyorum (finans verisi bağlı değil, elle de girilmedi); "
                         "bu yüzden yalnızca kasadaki nakde göre konuşuyorum.")
        elif pend["amount"]:
            src = "Trendyol cari hesap ekstresi" if pend["kind"] == "ACTUAL" else "elle girilen"
            lines.append(f"Bekleyen hakediş {_money(pend['amount'])} ({src}) henüz kasada olmadığı için harcanabilir sayılmadı.")
        if cs["marketplace_receivable"]["amount"]:
            lines.append(f"Vadesi gelmemiş pazaryeri alacağı {_money(cs['marketplace_receivable']['amount'])} de harcanabilir değil.")
        return "\n".join(lines), used
    if any(w in q for w in ("hakediş", "hakedis", "ödeme ne zaman", "trendyol ödeme")):
        c = use("get_capital_position")
        cs = c["cash_structure"]
        pend = cs["pending_marketplace_payout"]
        if pend["kind"] != "ACTUAL":
            return (f"{NO_DATA}: Trendyol finans (cari hesap) verisi henüz alınmadı"
                    + (f"; elle girilen bekleyen hakediş {_money(pend['amount'])}." if pend["amount"] else ".")), used
        nxt = f" Sonraki ödeme tarihi: {pend['next_payment_date']:%d.%m.%Y}." if pend.get("next_payment_date") else ""
        warn = "" if pend.get("freshness") == "FRESH" else f" Uyarı: finans verisi {pend.get('freshness')}."
        return (f"Trendyol'da ödenmemiş hakediş: vadesi yakın {_money(pend['amount'])}, vadesi gelmemiş {_money(cs['marketplace_receivable']['amount'])}."
                f"{nxt} Bu tutarlar kasaya girene kadar harcanabilir sermaye sayılmaz.{warn}\nKaynak: Trendyol Finance (cari hesap ekstresi)."), used
    if any(w in q for w in ("müşteri", "soru", "iade", "şikayet", "şikâyet")):
        r = use("get_customer_signals", days=30)
        if not r["has_data"]:
            return f"{NO_DATA}: son 30 günde kayıtlı müşteri sorusu veya iade yok (Trendyol soru/iade senkronu henüz veri getirmedi).", used
        lines = [f"Son {r['days']} günde {r['total_questions']} müşteri sorusu, {r['total_returns']} iade kalemi."]
        if r["categories"]:
            lines.append("Soru konuları: " + ", ".join(
                f"{x['label']} {x['count']}" + (f" (%{x['share'] * 100:.0f})" if x["share"] is not None else "") for x in r["categories"][:5])
                + ("" if any(x["share"] is not None for x in r["categories"]) else f" — yüzde için örnek yetersiz (en az {r['min_sample']})") + ".")
        lines += [f"• {g['text']}" for g in r["info_gaps"][:3]]
        lines += [f"• İade: {c['name']} — {c['count']} kez '{c['reason']}'" for c in r["complaints"][:3]]
        lines.append("Cevaplar otomatik gönderilmez; öneriler Müşteri deneyimi ekranında.")
        return "\n".join(lines), used
    if "bütçe" in q or ("reklam" in q and amount):
        if amount:
            p = use("get_ad_budget_plan", amount=float(amount))
            if not p["plan"] and not p["excluded_losing"] and not any(
                    c["ad_net_profit"] is not None for c in use("get_ads_analysis")["campaigns"]):
                return (f"{NO_DATA}: hiçbir kampanyanın ölçülmüş reklam sonrası net kârı yok (performans verisi veya ürün maliyeti eksik) "
                        f"ve kanıtlanmış yıldız ürün de yok. Bu yüzden {_money(p['requested'])} eklemeyi önermiyorum."), used
            lines = [f"{_money(p['requested'])} reklam bütçesinin kanıtla desteklenen kısmı {_money(p['justified'])}; "
                     f"{_money(p['unused'])} kullanılmamalı."]
            lines += [f"• {x['campaign']}: {_money(x['monthly'])}/ay — {x['reason']}" for x in p["plan"]]
            if p["excluded_losing"]:
                lines.append("Zarar eden kampanyalara pay verilmedi: " + ", ".join(p["excluded_losing"]) + ".")
            if not p["plan"]:
                lines.append("Kârlılığı kanıtlanmış reklam ya da yıldız ürün yok. Bu bütçeyi şimdilik kullanmamayı öneriyorum.")
            elif p["unused"] > 0:
                lines.append(f"Kalan {_money(p['unused'])} için ek reklam harcamasını şu anda önermiyorum.")
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
        w = Window(int(r["window_days"]), end_date=today() - timedelta(days=1))
        return "\n".join(lines + provenance_lines(conn, w.start, w.end)), used
    if "stok" in q:
        r = use("get_inventory_risks")
        if not r["products"]:
            return "Satış hızına göre tükenme riski olan ürün yok.", used
        return "\n".join([f"{r['stockout_risk_count']} üründe tükenme riski var:"] + [
            f"• {p['name']}: {p['available']} adet, günde {p['daily_velocity']} satış → "
            + (f"~{p['days_of_inventory']} gün" if p["days_of_inventory"] is not None else "stok yok") for p in r["products"]]), used
    if any(w in q for w in ("yanlış", "hata", "karar", "ne öğren")):
        r = use("get_decision_quality", days=30)
        measured = r["owner_successes"] or r["owner_failures"] or r["ai_mistakes"] or r["missed_opportunities"]
        if not measured:
            return (f"{NO_DATA}: son 30 günde sonucu ölçülmüş kararın yok"
                    + (f" ({r['awaiting_measurement']} karar ölçüm bekliyor)." if r["awaiting_measurement"] else ".")
                    + " Kararlarını onay/ret veya 'Kararımı kaydet' ile girdikçe hatalarını kanıta dayalı söyleyebilirim."), used
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
    if any(w in q for w in ("yönetici özeti", "durum raporu", "sonraki iş", "ne yapmalıyım")):
        from .ceo_review import ordered_sections
        sm = use("get_executive_summary")
        return "\n".join(f"{h}\n" + "\n".join(f"• {x}" for x in lines) for h, lines in ordered_sections(sm)), used
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
- Her rakamı araçlardan al. Araçta olmayan veriyi tahmin etme, uydurma; "Bunu söylemek için yeterli verim yok" de ve hangi verinin eksik olduğunu söyle.
- Kâr ile nakit aynı şey değildir. Bekleyen Trendyol hakedişi ve alacak harcanabilir sermaye DEĞİLDİR.
- Rakamın gerçek mi tahmini mi olduğunu araç çıktısından (provenance / kind: ACTUAL / ESTIMATED / MANUAL) söyle; tahmini kârı asla gerçekleşmiş kâr gibi anlatma.
- Bir veri kaynağı STALE / ERROR ise o veriye dayanarak para harcamayı önerme ve bunu açıkça söyle.
- Müşteri kişisel verisi (ad, telefon, adres) sende yok ve istenmez; müşteriye cevap gönderemezsin, yalnızca öneri yazabilirsin.
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
                content = tool_payload_for_llm(conn, b.name, dict(b.input or {}))
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": content})
            except Exception as exc:  # noqa: BLE001
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": f"Hata: {exc}", "is_error": True})
        messages.append({"role": "user", "content": results})
    return "Soru çok sayıda adım gerektirdi; lütfen daha dar sorun.", used, usage


def answer(conn: Connection, question: str, history: list[dict]) -> dict:
    fallback = None
    if llm_available(conn):
        try:
            text_out, used, usage = claude_answer(conn, question, [{"role": h["role"], "content": h["content"]} for h in history])
            return {"answer": text_out, "engine": "claude", "tools_used": used, "usage": usage,
                    "sources": [SOURCES[t] for t in dict.fromkeys(used) if t in SOURCES]}
        except Exception as exc:  # noqa: BLE001 - model hatasında kural motoru
            fallback = f"Claude kullanılamadı ({exc.__class__.__name__}); kural motoru cevapladı"
            log.warning("CEO sohbeti Claude hatası, kural motoruna geçildi: %s", exc.__class__.__name__)
    last_tools = next((h.get("tools_used") or [] for h in reversed(history) if h["role"] == "assistant"), [])
    text_out, used = rules_answer(conn, question, last_tools)
    return {"answer": text_out, "engine": "rules", "tools_used": used, "usage": {}, "fallback_reason": fallback,
            "sources": [SOURCES[t] for t in dict.fromkeys(used) if t in SOURCES]}
