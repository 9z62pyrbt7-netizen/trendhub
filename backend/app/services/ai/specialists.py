"""Uzman ajan görev işleyicileri.

Her işleyici yalnızca `TaskRuntime.call` ile araç çağırır ve her önemli rakamı `TaskRuntime.claim` ile kanıta bağlar.
İşleyicinin döndürdüğü metin AJAN SONUCUDUR; araç sonucu ayrı tutulur (ai_tool_calls). Araç çağrısı olmadan üretilen sonuç
orkestratörde `no_evidence` olarak işaretlenir ve CEO cevabında kullanılmaz.
"""
from __future__ import annotations

import json
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Engine

from . import sanitize
from .config import d, tl
from .tools import ToolContext, ToolResult, invoke

BASIS = {"ACTUAL": "ACTUAL", "ESTIMATED": "ESTIMATED", "PARTIAL": "PARTIAL", "NO_DATA": "ESTIMATED", "NO_SALES": "ACTUAL",
         "DATA_REQUIRED": "UNKNOWN"}


def get_path(data, path: str):
    cur = data
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


class TaskRuntime:
    def __init__(self, engine: Engine, *, request_id: int, task_id: int, agent_code: str, run_id: int | None, user_id: int | None = None):
        self.engine = engine
        self.ctx = ToolContext(engine, agent_code, request_id, task_id, run_id, user_id)
        self.calls: list[ToolResult] = []
        self.claim_ids: list[int] = []
        self.findings: list[str] = []
        self.recommendations: list[dict] = []

    def call(self, tool: str, **args) -> ToolResult:
        r = invoke(self.ctx, tool, args)
        self.calls.append(r)
        return r

    def claim(self, text_: str, value, *, call: ToolResult | None, path: str | None, metric: str, basis: str = "ACTUAL",
              kind: str = "metric", period: dict | None = None, baseline=None, supports: list[int] | None = None) -> int:
        """İddia kaydı. `value` ajanın söylediği değerdir; Gerçeklik Denetçisi bunu araç sonucuyla karşılaştırır."""
        payload = {"value": value, "path": path, "kind": kind, "basis": basis, "supports": supports or []}
        with self.engine.begin() as c:
            eid = c.execute(text("""INSERT INTO ai_evidence(request_id, task_id, tool_call_id, agent_code, claim, metric, value, baseline,
                                                            period_start, period_end, source)
                                    VALUES (:r, :t, :tc, :a, :cl, :m, CAST(:v AS JSONB), CAST(:b AS JSONB), :ps, :pe, :src) RETURNING id"""),
                            {"r": self.ctx.request_id, "t": self.ctx.task_id, "tc": call.call_id if call else None,
                             "a": self.ctx.agent_code, "cl": text_[:1000], "m": metric, "v": sanitize.to_json(payload),
                             "b": sanitize.to_json(baseline) if baseline is not None else None,
                             "ps": (period or {}).get("from"), "pe": (period or {}).get("to"),
                             "src": f"{call.tool}#{call.call_uid[:8]}" if call else None}).scalar()
        self.claim_ids.append(eid)
        return eid

    def recommend(self, priority: int, text_: str, **extra) -> None:
        self.recommendations.append({"priority": priority, "text": text_, "agent": self.ctx.agent_code, **extra})


def _num(v):
    return None if v is None else Decimal(str(v))


# ------------------------------------------------------------------ Finans
def finance_profitability(rt: TaskRuntime, inp: dict) -> dict:
    out = {}
    for label, days in (("today", 1), ("30d", 30)):
        r = rt.call("get_profitability", days=days)
        if not r.ok:
            rt.findings.append(f"Finans verisi alınamadı ({label}): {r.error}")
            continue
        x = r.data
        basis = BASIS.get(x["status"], "ESTIMATED")
        period = x["window"]
        if x["net_profit"] is None:
            rt.claim(f"{'Bugün' if days == 1 else 'Son 30 gün'} net kâr hesaplanamıyor: {x['note']}", None, call=r, path="net_profit",
                     metric=f"finance.{label}.net_profit", basis="UNKNOWN", period=period)
            rt.recommend(88, f"{x['missing_cost_orders']} siparişte ürün maliyeti eksik; maliyetler girilmeden kâr hesaplanamaz.",
                         kind="data")
        else:
            rt.claim(f"{'Bugün' if days == 1 else 'Son 30 gün'} net kâr {tl(x['net_profit'])}", x["net_profit"], call=r,
                     path="net_profit", metric=f"finance.{label}.net_profit", basis=basis, period=period)
        if days == 30:
            for key, lab in (("contribution_margin", "Katkı marjı (reklam öncesi)"), ("profit_per_order", "Sipariş başı net kâr"),
                             ("break_even_roas", "Başabaş ROAS"), ("break_even_cpa", "Başabaş CPA")):
                rt.claim(f"{lab}: {x[key] if x[key] is not None else 'UNKNOWN'}", x[key], call=r, path=key,
                         metric=f"finance.30d.{key}", basis=basis if x[key] is not None else "UNKNOWN", period=period)
            if x["status"] == "ESTIMATED":
                rt.findings.append("Komisyon gerçek Trendyol finans kaydından değil, oran tahmininden.")
        out[label] = {k: x.get(k) for k in ("status", "net_profit", "net_sales", "orders", "contribution_margin", "break_even_roas",
                                            "break_even_cpa", "profit_per_order", "note")}
    return {"summary": "Finans: " + "; ".join(f"{k} net kâr {v['net_profit'] if v['net_profit'] is not None else 'UNKNOWN'} ({v['status']})"
                                               for k, v in out.items()), "data": out}


# ------------------------------------------------------------------ Analitik
def analytics_store_health(rt: TaskRuntime, inp: dict) -> dict:
    today = rt.call("get_orders", days=1)
    week = rt.call("get_orders", days=7)
    data = {}
    if today.ok:
        cur = today.data["current"]
        basis = BASIS.get(today.data["provenance"], "ESTIMATED")
        rt.claim(f"Bugün {cur['orders']} sipariş", cur["orders"], call=today, path="current.orders", metric="sales.today.orders",
                 period=cur["window"])
        rt.claim(f"Bugün net satış {tl(cur['net_sales'])}", cur["net_sales"], call=today, path="current.net_sales",
                 metric="sales.today.net_sales", basis=basis, period=cur["window"], baseline=today.data["previous"]["net_sales"])
        data["today"] = cur
    if week.ok:
        ch = week.data["change"]["net_sales"]
        prev = week.data["previous"]
        if ch is not None:
            rt.claim(f"Son 7 gün net satış değişimi %{Decimal(ch) * 100:.1f} (önceki 7 gün {tl(prev['net_sales'])} → "
                     f"{tl(week.data['current']['net_sales'])})", ch, call=week, path="change.net_sales", metric="sales.7d.change",
                     kind="change", period=week.data["current"]["window"], baseline={"period": prev["window"], "value": prev["net_sales"]},
                     basis=BASIS.get(week.data["provenance"], "ESTIMATED"))
        data["week"] = week.data["current"]
    an = rt.call("get_anomalies")
    if an.ok:
        rt.claim(f"{an.data['count']} anomali tespit edildi", an.data["count"], call=an, path="count", metric="risk.anomalies")
        for a in an.data["anomalies"]:
            rt.findings.append(a["message"])
            pr = {"loss_products": 86, "stock_critical": 80, "sales_drop": 78, "roas_drop": 74, "ad_spend_up": 72,
                  "conversion_drop": 60}.get(a["code"], 50)
            rt.recommend(pr, a["message"], kind="anomaly", code=a["code"])
        for q in an.data["data_quality"]:
            if q["severity"] == "warning":
                rt.findings.append(q["message"])
        data["anomalies"] = an.data["anomalies"]
    return {"summary": f"Analitik: {len(data.get('anomalies', []))} anomali", "data": data}


# ------------------------------------------------------------------ Ürün & Trend
def product_portfolio(rt: TaskRuntime, inp: dict) -> dict:
    p = rt.call("get_products", limit=10)
    data = {}
    if p.ok:
        x = p.data
        rt.claim("Ürün sınıfları: " + (", ".join(f"{k} {v}" for k, v in sorted(x["class_counts"].items())) or "satış yok"),
                 x["class_counts"], call=p, path="class_counts", metric="products.class_counts")
        if x["best"]:
            rt.claim(f"En kârlı ürün: {x['best']['name']} ({tl(x['best']['net_profit'])}, {x['best']['trend_class']})",
                     x["best"]["net_profit"], call=p, path="best.net_profit", metric="products.best", basis="ESTIMATED")
        if x["worst"]:
            rt.claim(f"En kötü ürün: {x['worst']['name']} ({tl(x['worst']['net_profit'])}, {x['worst']['trend_class']})",
                     x["worst"]["net_profit"], call=p, path="worst.net_profit", metric="products.worst", basis="ESTIMATED")
        data = {"best": x["best"], "worst": x["worst"], "class_counts": x["class_counts"]}
    g = rt.call("get_profit_guard")
    if g.ok:
        dang = g.data["counts"].get("DANGER", 0)
        rt.claim(f"Kâr Koruması: {dang} ürün DANGER", dang, call=g, path="counts.DANGER", metric="products.danger")
        for it in [i for i in g.data["items"] if i["state"] == "DANGER"][:3]:
            rt.recommend(85, f"{it['name']} zarar ediyor ({'; '.join(it['reasons'])}); reklam/indirimle büyütme, fiyat ve maliyeti incele.",
                         kind="loss_product", product_id=it["product_id"])
        unknown = g.data["counts"].get("UNKNOWN", 0)
        if unknown:
            rt.findings.append(f"{unknown} ürünün gerçek kârı bilinmiyor (UNKNOWN); bunlara büyük bütçe açılmaz.")
        data["guard"] = g.data["counts"]
    return {"summary": "Ürün & Trend: " + ", ".join(f"{k} {v}" for k, v in sorted(data.get("class_counts", {}).items())), "data": data}


# ------------------------------------------------------------------ Operasyon
def operations_health(rt: TaskRuntime, inp: dict) -> dict:
    inv = rt.call("get_inventory", risk_only=True, limit=5)
    data = {}
    if inv.ok:
        n = inv.data["stockout_risk_count"]
        names = ", ".join(i["name"] for i in inv.data["items"][:3])
        rt.claim(f"Kritik stok: {n} ürün" + (f" ({names})" if names else ""), n, call=inv, path="stockout_risk_count",
                 metric="stock.critical")
        if n:
            rt.recommend(80, f"{n} üründe stok tükenmek üzere ({names}); tedarikçi stoğunu kontrol et, bu ürünlere reklam açma.",
                         kind="stock")
        data["stock"] = inv.data["items"][:5]
    h = rt.call("get_operations_health")
    if h.ok:
        x = h.data
        rt.claim(f"Son 24 saatte {x['failed_jobs_24h']} başarısız iş, {x['unprocessed_orders']} işlenmeyen sipariş",
                 x["failed_jobs_24h"], call=h, path="failed_jobs_24h", metric="ops.failed_jobs")
        rt.claim(f"{len(x['open_incidents'])} açık operasyon olayı", len(x["open_incidents"]), call=h, path="open_incidents",
                 metric="ops.incidents", kind="count")
        for s in x["stale_sources"]:
            rt.findings.append(f"Veri kaynağı {s['label']}: {s['freshness']}")
        for i in x["open_incidents"][:5]:
            rt.findings.append(f"Olay #{i['id']} ({i['severity']}): {i['title']}")
            if i["severity"] == "critical":
                rt.recommend(92, f"Kritik operasyon olayı: {i['title']}", kind="incident")
        if x["unprocessed_orders"]:
            rt.recommend(84, f"{x['unprocessed_orders']} sipariş işlenmeyi bekliyor; kargo gecikmesi puanı düşürür.", kind="orders")
        data["ops"] = {k: x[k] for k in ("failed_jobs_24h", "unprocessed_orders", "stuck_jobs", "queued_jobs")}
    return {"summary": "Operasyon: " + json.dumps(data.get("ops", {}), default=str), "data": data}


# ------------------------------------------------------------------ Reklam
def advertising_review(rt: TaskRuntime, inp: dict) -> dict:
    pl = rt.call("get_ad_platforms")
    data = {}
    if pl.ok:
        conn_n = sum(1 for p in pl.data["platforms"] if p["connected"])
        rt.claim(f"Bağlı reklam/sosyal platform sayısı: {conn_n}", conn_n, call=pl, path=None, metric="ads.platforms_connected",
                 kind="count_connected")
        data["platforms"] = {p["code"]: p["state"] for p in pl.data["platforms"]}
    t = rt.call("get_ad_performance", days=1)
    if t.ok:
        rt.claim(f"Bugün reklam harcaması {tl(t.data['spend'])}", t.data["spend"], call=t, path="spend", metric="ads.today.spend",
                 period=t.data["window"])
    w = rt.call("get_ad_performance", days=7)
    if w.ok:
        x = w.data
        rt.claim(f"Son 7 gün reklam harcaması {tl(x['spend'])}", x["spend"], call=w, path="spend", metric="ads.7d.spend",
                 period=x["window"])
        roas_id = rt.claim(f"Son 7 gün ROAS {x['roas'] if x['roas'] is not None else 'hesaplanamıyor (performans verisi yok)'}",
                           x["roas"], call=w, path="roas", metric="ads.7d.roas", period=x["window"],
                           basis="ACTUAL" if x["roas"] is not None else "UNKNOWN")
        for cmp in x["campaigns"]:
            if cmp["ad_net_profit"] is None:
                continue
            idx = x["campaigns"].index(cmp)
            pid = rt.claim(f"{cmp['name']}: reklam sonrası net {tl(cmp['ad_net_profit'])}", cmp["ad_net_profit"], call=w,
                           path=f"campaigns.{idx}.ad_net_profit", metric="ads.campaign.net", period=x["window"])
            good = Decimal(cmp["ad_net_profit"]) > 0
            rt.claim(f"{cmp['name']} reklamı {'kârlı' if good else 'zarar ediyor'} ({cmp['verdict_label']})", good, call=w,
                     path=None, metric="ads.campaign.assessment", kind="assessment", supports=[pid, roas_id])
            if cmp["verdict"] == "PAUSE":
                rt.recommend(82, f"{cmp['name']} reklamı zarar ediyor ({tl(cmp['ad_net_profit'])}); durdurma önerisi onaya sun.",
                             kind="ads", campaign_id=cmp["id"])
        if not x["campaigns"]:
            rt.findings.append("Reklam kampanyası kaydı yok.")
        rt.findings.append("Reklam verisi: " + x["data_source"])
        data["7d"] = {k: x[k] for k in ("spend", "roas", "cpa", "ctr", "conversion_rate", "ad_net_profit")}
    return {"summary": f"Reklam: 7 gün {data.get('7d', {})}", "data": data}


# ------------------------------------------------------------------ Pazarlama / Büyüme
def marketing_experiments(rt: TaskRuntime, inp: dict) -> dict:
    p = rt.call("get_products", limit=50)
    if not p.ok:
        return {"summary": "Ürün verisi alınamadı", "data": {}}
    picks = [x for x in p.data["products"] if x["trend_class"] in ("WINNER", "PROMISING") and not x["stockout_risk"]][:2]
    made = []
    for x in picks:
        base = Decimal(x["net_profit"])
        thr = (abs(base) * Decimal("0.10")).quantize(Decimal("0.01"))
        r = rt.call("propose_experiment", product_id=x["product_id"], title=f"Kısa video içerik testi: {x['name']}"[:200],
                    hypothesis=(f"{x['name']} ({x['trend_class']}) için gerçek müşteri sorularına cevap veren 9:16 içerik, 14 gün içinde "
                                f"ürünün net kârını en az {tl(thr)} artırır (temel: son {p.data['window_days']} gün {tl(base)})."),
                    expected_impact={"metric": "net_profit", "baseline": str(base), "min_increase": str(thr)},
                    cost=0, risk="LOW", duration_days=14, success_metric="net_profit", success_threshold=thr)
        if r.ok:
            rt.claim(f"Deney #{r.data['experiment_id']} önerildi: {x['name']}", r.data["experiment_id"], call=r, path="experiment_id",
                     metric="growth.experiment", kind="action_internal")
            made.append(r.data["experiment_id"])
    if not picks:
        rt.findings.append("Deney için uygun (WINNER/PROMISING + stoklu) ürün yok; kanıtsız deney önerilmez.")
    return {"summary": f"Büyüme: {len(made)} deney önerildi (sonuç ölçülmeden başarılı sayılmaz)", "data": {"experiments": made}}


# ------------------------------------------------------------------ Kreatif
def creative_brief(rt: TaskRuntime, inp: dict) -> dict:
    pid = inp.get("product_id")
    if not pid:
        p = rt.call("get_products", limit=50)
        cand = [x for x in (p.data.get("products", []) if p.ok else []) if x["trend_class"] in ("WINNER", "PROMISING", "NORMAL")]
        if not cand:
            rt.findings.append("Kreatif için kârlı ürün yok.")
            return {"summary": "Kreatif üretilmedi (uygun ürün yok)", "data": {}}
        pid = cand[0]["product_id"]
    r = rt.call("create_ad_draft", product_id=int(pid), variants=2)
    if r.ok:
        rt.claim(f"{len(r.data['creative_ids'])} kreatif taslağı kaydedildi (yayınlanmadı)", r.data["creative_ids"], call=r,
                 path="creative_ids", metric="creative.drafts", kind="action_internal")
        return {"summary": f"Kreatif: A/B taslakları #{r.data['creative_ids']}", "data": {"variants": r.data["variants"]}}
    return {"summary": f"Kreatif taslağı oluşturulamadı: {r.error}", "data": {}}


# ------------------------------------------------------------------ Sosyal medya
def social_calendar(rt: TaskRuntime, inp: dict) -> dict:
    from .growth import content_plan, marketing_picks
    p = rt.call("get_products", limit=50)
    pl = rt.call("get_ad_platforms")
    cal = []
    if p.ok:
        picks = [x for x in p.data["products"] if x["trend_class"] in ("WINNER", "PROMISING", "NORMAL")][:3]
        with rt.engine.begin() as c:
            mp = {m["product_id"]: m for m in marketing_picks(c, 10)}
        for i, x in enumerate(picks):
            m = mp.get(x["product_id"])
            plan = content_plan(m) if m else {"hook": x["name"], "caption": x["name"], "cta": "Trendyol'da incele"}
            for day, fmt in ((i * 2 + 1, "Reels / TikTok"), (i * 2 + 2, "Hikâye")):
                cal.append({"day": day, "product_id": x["product_id"], "format": fmt, "hook": plan["hook"], "caption": plan["caption"],
                            "cta": plan["cta"]})
        rt.claim(f"{len(cal)} içerik planlandı ({len(picks)} ürün; yayınlanmadı)", len(picks), call=p, path=None,
                 metric="social.planned", kind="count_class", period=None, baseline=["WINNER", "PROMISING", "NORMAL"])
    if inp.get("publish") and cal:
        r = rt.call("publish_social_post", platform=inp.get("platform", "instagram"), product_id=cal[0]["product_id"],
                    caption=cal[0]["caption"][:2200])
        rt.claim(f"Yayın durumu: {r.status}", r.status, call=r, path=None, metric="social.publish", kind="action")
    if pl.ok:
        rt.findings += [f"{x['name']}: {x['state']}" for x in pl.data["platforms"] if x["code"] in ("instagram_publish", "tiktok")]
    return {"summary": f"Sosyal medya: {len(cal)} içerik planı, 0 yayın", "data": {"calendar": cal}}


# ------------------------------------------------------------------ CEO hafıza
def ceo_memory(rt: TaskRuntime, inp: dict) -> dict:
    r = rt.call("get_decision_history", limit=20)
    if not r.ok:
        return {"summary": "Karar geçmişi alınamadı", "data": {}}
    measured = [x for x in r.data["decisions"] if x["actual_result"]]
    rt.claim(f"{len(r.data['decisions'])} karar kaydı, {len(measured)} tanesinin sonucu ölçüldü", len(r.data["decisions"]), call=r,
             path="decisions", metric="memory.decisions", kind="count")
    for x in measured[:5]:
        if x["lesson"]:
            rt.findings.append(x["lesson"])
    return {"summary": f"Hafıza: {len(measured)} ölçülmüş karar", "data": {"measured": measured[:10]}}


# ------------------------------------------------------------------ yazma görevleri (CEO adına, yetkili birim)
def advertising_create(rt: TaskRuntime, inp: dict) -> dict:
    r = rt.call("create_campaign", product_id=inp["product_id"], daily_budget=inp["daily_budget"], days=inp.get("days", 7),
                channel=inp.get("channel", "meta"), reason=inp.get("reason", ""))
    rt.claim(f"Reklam kampanyası isteği: {r.status}", r.status, call=r, path=None, metric="write.campaign", kind="action")
    return {"summary": r.summary, "data": r.data, "write_status": r.status}


def pricing_change(rt: TaskRuntime, inp: dict) -> dict:
    r = rt.call("update_price", product_id=inp["product_id"], new_price=inp["new_price"], reason=inp.get("reason", ""))
    rt.claim(f"Fiyat değişikliği isteği: {r.status}", r.status, call=r, path=None, metric="write.price", kind="action")
    return {"summary": r.summary, "data": r.data, "write_status": r.status}


def product_check(rt: TaskRuntime, inp: dict) -> dict:
    g = rt.call("get_profit_guard", product_id=inp["product_id"])
    cost = rt.call("get_product_cost", product_id=inp["product_id"])
    if g.ok:
        rt.claim(f"{g.data.get('name')}: kâr durumu {g.data['state']} ({'; '.join(g.data['reasons'])})", g.data["state"], call=g,
                 path="state", metric="guard.state")
    if cost.ok and cost.data.get("unit_profit") is not None:
        rt.claim(f"Birim net kâr {tl(cost.data['unit_profit'])}", cost.data["unit_profit"], call=cost, path="unit_profit",
                 metric="product.unit_profit", basis="ESTIMATED" if cost.data.get("basis") == "catalog_estimate" else "ACTUAL")
    return {"summary": f"Ürün kontrolü: {g.data.get('state') if g.ok else '?'}", "data": {"guard": g.data if g.ok else None,
                                                                                           "cost": cost.data if cost.ok else None}}


def budget_check(rt: TaskRuntime, inp: dict) -> dict:
    b = rt.call("get_budget_status")
    a = rt.call("get_ad_performance", days=30)
    if b.ok:
        rt.claim(f"Kalan bütçe {tl(b.data['available_budget']) if b.data['available_budget'] is not None else 'tanımsız'}",
                 b.data["available_budget"], call=b, path="available_budget", metric="budget.available")
    if a.ok:
        prof = [c for c in a.data["campaigns"] if c["verdict"] in ("INCREASE_BUDGET", "CONTINUE") and c["ad_net_profit"] is not None
                and Decimal(c["ad_net_profit"]) > 0]
        rt.claim(f"Kârlılığı kanıtlanmış kampanya sayısı: {len(prof)}", len(prof), call=a, path=None, metric="ads.proven",
                 kind="count_proven")
    return {"summary": "Bütçe kontrolü", "data": {"budget": b.data if b.ok else None}}


# ------------------------------------------------------------------ Büyüme stratejisi (CEO "satışları nasıl artırırız?")
def marketing_growth_plan(rt: TaskRuntime, inp: dict) -> dict:
    """Gerçek veriden büyüme fırsatları. Her fırsatın rakamları kanıt iddiasına (claim) bağlıdır; CEO yalnızca doğrulananları
    rakamla sunar. Beklenen etki HİPOTEZDİR (ölçülmeden kesin değil); kayıp önleme ile kazanç ayrı yazılır."""
    from .config import thresholds
    with rt.engine.connect() as c:
        th = thresholds(c)
    prods = rt.call("get_products", limit=200)
    guard = rt.call("get_profit_guard")
    fin = rt.call("get_profitability", days=30)
    cx = rt.call("get_customer_signals", days=30)
    ads = rt.call("get_ad_performance", days=30)
    opps: list[dict] = []

    def add(priority, action, why, evidence, benefit, profit_effect, cost, risk, feasibility, confidence, approval, kind):
        opps.append({"priority": priority, "action": action, "why": why, "evidence_ids": evidence, "benefit": benefit,
                     "profit_effect": profit_effect, "cost": cost, "risk": risk, "feasibility": feasibility,
                     "confidence": round(confidence, 2), "approval": approval, "kind": kind})

    if guard.ok:
        items = guard.data["items"]
        danger = [(i, x) for i, x in enumerate(items) if x["state"] == "DANGER"][:3]
        if danger:
            ids = [rt.claim(f"{x['name']}: son {th['analysis_days']} gün net {tl(x['net_profit'])}", x["net_profit"], call=guard,
                            path=f"items.{i}.net_profit", metric="growth.loss_product", basis="ESTIMATED") for i, x in danger]
            total = sum((Decimal(str(x["net_profit"])) for _, x in danger), Decimal("0"))
            add(95, "Zarar eden ürünleri büyütmeyi durdur; fiyat/maliyet/komisyon yapısını düzelt: "
                + ", ".join(x["name"] for _, x in danger),
                f"Bu ürünler son {th['analysis_days']} günde toplam {tl(total)} net sonuç verdi (Kâr Koruması: DANGER).", ids,
                "Satışı artırmadan önce zararlı satışı durdurmak: her ek satış zararı büyütür.",
                f"Kayıp önleme: aynı tablo sürerse {th['analysis_days']} günde yaklaşık {tl(abs(total))} zarar tekrarlanır (kazanç değil).",
                "0 TL (iç çalışma)", "LOW", "Hemen: fiyat değişikliği önerisi sahibin onayıyla; Trendyol yazma kapalı → manuel.",
                0.8, "Fiyat değişikliği HIGH risk: sahibin onayı", "stop_loss")
    if ads.ok:
        losing = [(i, c_) for i, c_ in enumerate(ads.data["campaigns"]) if c_["verdict"] == "PAUSE" and c_["ad_net_profit"] is not None][:2]
        if losing:
            ids = [rt.claim(f"{c_['name']}: 30 gün reklam sonrası net {tl(c_['ad_net_profit'])}", c_["ad_net_profit"], call=ads,
                            path=f"campaigns.{i}.ad_net_profit", metric="growth.losing_ad") for i, c_ in losing]
            loss = sum((Decimal(str(c_["ad_net_profit"])) for _, c_ in losing), Decimal("0"))
            add(92, "Zarar eden reklamı durdur: " + ", ".join(c_["name"] for _, c_ in losing),
                f"Reklam sonrası net {tl(loss)} (30 gün); bu bütçe kârlı ürüne kaydırılabilir.", ids,
                "Reklam bütçesi zarar yerine kârlı ürüne gider.",
                f"Kayıp önleme: 30 günde yaklaşık {tl(abs(loss))} reklam zararı durur.", "0 TL", "LOW",
                "Durdurma önerisi (MEDIUM) sahibin onayıyla; reklam platformu bağlı değil → manuel.", 0.75,
                "Kampanya durdurma: sahibin onayı", "stop_ad_loss")
    if fin.ok and fin.data["status"] == "DATA_REQUIRED":
        eid = rt.claim(f"{fin.data['missing_cost_orders']} siparişte ürün maliyeti yok", fin.data["missing_cost_orders"], call=fin,
                       path="missing_cost_orders", metric="growth.missing_cost")
        add(90, "Eksik ürün maliyetlerini gir", f"{fin.data['missing_cost_orders']} siparişte maliyet yok; net kâr şu an UNKNOWN.", [eid],
            "Hangi ürünün gerçekten kâr ettiği ölçülebilir hale gelir; yanlış ürünü büyütme riski kalkar.",
            "Doğrudan etki yok; karar kalitesini artırır.", "0 TL (veri girişi)", "LOW", "Hemen: Ürünler → maliyet.", 0.9,
            "Gerekmez (veri girişi)", "data")
    if prods.ok:
        plist = prods.data["products"]
        good = [(i, x) for i, x in enumerate(plist) if x["trend_class"] in ("WINNER", "PROMISING", "NORMAL") and not x["missing_cost"]
                and Decimal(str(x["net_profit"])) > 0]
        risky = [(i, x) for i, x in good if x["stockout_risk"]][:2]
        if risky:
            ids = [rt.claim(f"{x['name']}: 30 gün net kâr {tl(x['net_profit'])}, kullanılabilir stok {x['available']}", x["net_profit"],
                            call=prods, path=f"products.{i}.net_profit", metric="growth.stock_risk_winner", basis="ESTIMATED")
                   for i, x in risky]
            np_ = sum((Decimal(str(x["net_profit"])) for _, x in risky), Decimal("0"))
            add(85, "Kârlı ürünlerin stoğunu güvenceye al: " + ", ".join(x["name"] for _, x in risky),
                "Kârlı ürünlerde stok tükenmek üzere.", ids, "Stoksuzluk nedeniyle kaybedilecek satışı önler.",
                f"Kayıp önleme: bu ürünler 30 günde {tl(np_)} net kâr getirdi; stok biterse bu katkı durur.",
                "Dropship: 0 TL · kendi stok: alış maliyeti × adet (Bütçe Yöneticisi)", "LOW",
                "Tedarikçi stoğu kontrolü manuel (stok yazma TrendHub'da yok).", 0.7, "Stok alımı varsa sahibin onayı", "stock")
        winners = [(i, x) for i, x in good if not x["stockout_risk"]][:2]
        for i, x in winners:
            eid = rt.claim(f"{x['name']} ({x['trend_class']}): 30 gün net kâr {tl(x['net_profit'])}, {x['units']} adet", x["net_profit"],
                           call=prods, path=f"products.{i}.net_profit", metric="growth.winner", basis="ESTIMATED")
            base = Decimal(str(x["net_profit"]))
            small = x["units"] < int(th["min_units_for_data"]) * 2
            add(70 if x["trend_class"] == "WINNER" else 65,
                f"{x['name']} için 14 günlük organik içerik + ürün sayfası iyileştirme deneyi",
                f"{x['trend_class']} sınıfı, son 30 günde {x['units']} adet ve {tl(base)} net kâr.", [eid],
                "Kârlı üründe talebi artırma (hipotez; sonuç ölçülmeden başarılı sayılmaz).",
                f"Hipotez: net kârda en az %10 artış ≈ +{tl(base * Decimal('0.10'))} / 30 gün (temel {tl(base)}); garanti değil.",
                "0 TL (organik içerik)", "LOW", "Hemen; paylaşım manuel (sosyal medya hesabı bağlı değil).",
                0.4 if small else 0.55, "Gerekmez; deneyi başlatmak sahibin kararı", "promote")
        if guard.ok:
            safe = {x["product_id"]: (j_, x) for j_, x in enumerate(guard.data["items"]) if x["state"] == "SAFE"}
            cand = next(((i, x) for i, x in good if x["product_id"] in safe and not x["stockout_risk"]), None)
            if cand:
                gi, gx = safe[cand[1]["product_id"]]
                daily = (Decimal(str(th["ads_min_spend"])) / Decimal(th["ads_window_days"]) * 2).quantize(Decimal("1"))
                cost = daily * Decimal(th["ads_window_days"])
                eid = rt.claim(f"{gx['name']}: Kâr Koruması SAFE, birim net kâr {tl(gx.get('unit_profit'))}", gx.get("unit_profit"),
                               call=guard, path=f"items.{gi}.unit_profit", metric="growth.ad_test_unit_profit", basis="ESTIMATED")
                add(60, f"{gx['name']} için küçük reklam testi ({tl(daily)}/gün × {th['ads_window_days']} gün)",
                    "Kârı doğrulanmış (SAFE) ve stoğu güvenli ürün; reklamın kârlı olup olmadığı henüz ölçülmedi.", [eid],
                    "Reklamın bu üründe net kâr getirip getirmediğini ölçmek.",
                    f"Sipariş başına {tl(gx.get('unit_profit'))} üstü reklam maliyeti kârı sıfırlar (başabaş CPA); test zararlı da çıkabilir.",
                    f"{tl(cost)} (test bütçesi)", "MEDIUM",
                    "Reklam platformu bağlı değil → kampanya manuel açılır; öneri Bütçe Yöneticisi + Kâr Koruması'ndan geçer.", 0.35,
                    "CRITICAL: reklam kampanyası önerisi + Bütçe Yöneticisi + sahibin onayı", "ad_test")
    if cx.ok and cx.data["info_gaps"]:
        g = cx.data["info_gaps"][0]
        eid = rt.claim(f"{g['name']}: '{g['label']}' hakkında {g['count']} müşteri sorusu", g["count"], call=cx, path="info_gaps.0.count",
                       metric="growth.cx_gap")
        add(55, f"{g['name']} ürün sayfasına '{g['label']}' bilgisini ekle",
            f"Son 30 günde bu konuda {g['count']} soru geldi.", [eid],
            "Soru ve iade azalabilir, dönüşüm artabilir (ölçülmedi).", "Doğrudan ölçülemez; dolaylı etki.", "0 TL", "LOW",
            "Hemen; ürün açıklaması manuel güncellenir (TrendHub yazmaz).", 0.5, "Gerekmez", "cx")
    if not opps:
        rt.findings.append("Kanıta dayalı büyüme fırsatı çıkarılamadı (satış/maliyet verisi yetersiz).")
    opps.sort(key=lambda o: -o["priority"])
    return {"summary": f"Büyüme: {len(opps)} aday aksiyon (CEO en fazla 5'ini seçer)", "data": {"opportunities": opps}}


HANDLERS = {
    ("finance", "profitability"): finance_profitability,
    ("analytics", "store_health"): analytics_store_health,
    ("product_trend", "portfolio"): product_portfolio,
    ("product_trend", "product_check"): product_check,
    ("product_trend", "price_change"): pricing_change,
    ("operations", "ops_health"): operations_health,
    ("advertising", "ads_review"): advertising_review,
    ("advertising", "create_campaign"): advertising_create,
    ("finance", "budget_check"): budget_check,
    ("marketing", "growth_experiments"): marketing_experiments,
    ("marketing", "growth_plan"): marketing_growth_plan,
    ("creative", "creative_brief"): creative_brief,
    ("social_media", "content_calendar"): social_calendar,
    ("ceo", "memory"): ceo_memory,
}
TASK_TR = {"profitability": "kârlılık ve finans metriklerini hesapla", "store_health": "sipariş/satış ve anomali analizi",
           "portfolio": "ürün performansı ve kâr durumu", "product_check": "ürün kâr durumu kontrolü", "price_change": "fiyat değişikliği",
           "ops_health": "stok ve operasyon sağlığı", "ads_review": "reklam performansı ve platform durumu",
           "create_campaign": "reklam kampanyası isteği", "budget_check": "bütçe ve kanıtlanmış reklam kontrolü",
           "growth_experiments": "büyüme deneyleri", "growth_plan": "satış büyütme fırsatları (kanıtlı)", "creative_brief": "kreatif taslakları", "content_calendar": "içerik takvimi",
           "memory": "geçmiş kararlar ve sonuçları"}


def numeric(v) -> Decimal | None:
    try:
        return None if v is None or isinstance(v, bool) else d(v)
    except Exception:  # noqa: BLE001
        return None
