"""Uzman ajanlar (deterministik, eşikler panelden): Ürün & Kâr, Stok/Tedarikçi, Reklam, Sermaye.

Her çalışma `ai_agent_runs`'a yazılır (başlangıç, bitiş, süre, girdi kaynakları, çıktı, uyarı, hata). Hata sessizce
kaybolmaz: ajanın durumu ERROR olur ve aktivite akışında görünür.
"""
from __future__ import annotations

import json
import math
import time
from contextlib import contextmanager
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ...db import row, rows
from .. import finance_view
from . import config
from .config import Window, d, thresholds, tl
from .proposals import activity, propose, retire_stale

CLASSES = {"STAR": "Yıldız", "PROFITABLE": "Kârlı", "WATCH": "İzle", "LOSS": "Zarar", "NO_DATA": "Veri yok"}
AD_VERDICTS = {"INCREASE_BUDGET": "Bütçeyi artır", "DECREASE_BUDGET": "Bütçeyi azalt", "PAUSE": "Durdur",
               "CONTINUE": "Devam", "TEST": "Teste devam", "INSUFFICIENT_DATA": "Veri yetersiz"}


def _json(v) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


class RunContext:
    def __init__(self, run_id: int):
        self.run_id = run_id
        self.sources: list[str] = []
        self.warnings: list[str] = []
        self.output: dict = {}
        self.usage: dict = {}


@contextmanager
def agent_run(engine: Engine, code: str, trigger: str = "schedule"):
    """Ajan çalışması: kendi işleminde kayıt açar; gövde ayrı işlemde çalışır, hata durumunda kayıt ERROR olur."""
    with engine.begin() as conn:
        run_id = conn.execute(text("INSERT INTO ai_agent_runs(agent_code, trigger, status) VALUES (:a, :t, 'running') RETURNING id"),
                              {"a": code, "t": trigger}).scalar()
    ctx = RunContext(run_id)
    t0 = time.monotonic()
    status, error = "ok", None
    try:
        yield ctx
        status = "degraded" if ctx.warnings else "ok"
    except Exception as exc:
        status, error = "error", f"{exc.__class__.__name__}: {str(exc)[:500]}"
        raise
    finally:
        with engine.begin() as conn:
            conn.execute(text("""UPDATE ai_agent_runs SET status = :s, finished_at = NOW(), duration_ms = :ms,
                                 input_sources = CAST(:src AS JSONB), output = CAST(:out AS JSONB), warnings = CAST(:w AS JSONB),
                                 error = :e, usage = CAST(:u AS JSONB) WHERE id = :id"""),
                         {"s": status, "ms": int((time.monotonic() - t0) * 1000), "src": _json(ctx.sources),
                          "out": _json(ctx.output), "w": _json(ctx.warnings), "e": error, "u": _json(ctx.usage), "id": run_id})
            conn.execute(text("""UPDATE ai_agents SET last_run_at = NOW(), last_status = :s, last_error = :e, updated_at = NOW()
                                 WHERE code = :c"""), {"s": status, "e": error, "c": code})
            if error:
                activity(conn, f"Ajan hata verdi: {error}", agent=code, kind="run", level="error", run_id=run_id)


def agent_enabled(conn: Connection, code: str) -> bool:
    r = row(conn, "SELECT enabled, available FROM ai_agents WHERE code = :c", c=code)
    return bool(r and r["enabled"] and r["available"])


# ------------------------------------------------------------------ Ürün & Kâr
def classify(e: dict, th: dict) -> tuple[str, str]:
    if e["units"] < th["min_units_for_data"]:
        return "NO_DATA", f"{e['units']} adet satış; karar için en az {th['min_units_for_data']} gerekli"
    if e["missing_cost"]:
        return "NO_DATA", "Ürün maliyeti eksik; kâr hesaplanamaz"
    m = e["net_margin"]
    if e["net_profit"] < 0:
        return "LOSS", f"Net zarar {tl(e['net_profit'])}"
    if m is not None and m >= d(th["star_margin"]) and e["net_profit"] >= d(th["star_min_profit"]):
        return "STAR", f"Net marj %{m * 100:.1f}, net kâr {tl(e['net_profit'])}"
    if m is not None and m >= d(th["profitable_margin"]):
        return "PROFITABLE", f"Net marj %{m * 100:.1f}"
    return "WATCH", f"Net marj %{(m or 0) * 100:.1f} (hedefin altında)"


def classified_products(conn: Connection, window: Window | None = None) -> list[dict]:
    from .data import product_economics
    th = thresholds(conn)
    window = window or Window(th["analysis_days"])
    out = []
    for e in product_economics(conn, window):
        cls, why = classify(e, th)
        out.append({**e, "class": cls, "class_label": CLASSES[cls], "class_reason": why})
    return out


def _cost_driver(e: dict) -> str:
    parts = {"ürün maliyeti": e["product_cost"], "komisyon": e["commission"], "kargo": e["shipping"],
             "reklam": e["ad_spend_allocated"] + e["order_level_ads"], "iade": e["refund"], "hizmet bedeli": e["service_fee"]}
    name, val = max(parts.items(), key=lambda kv: kv[1])
    share = finance_view.ratio(val, e["net_sales"])
    return f"en büyük gider kalemi {name} ({tl(val)}{f', satışın %{share * 100:.0f}' if share else ''})"


def run_product_profit(conn: Connection, ctx: RunContext) -> dict:
    th = thresholds(conn)
    window = Window(th["analysis_days"])
    ctx.sources += ["orders/order_items (finance_view)", "ad_spend + ad_campaign_products", "expenses (SKU reklam)"]
    items = classified_products(conn, window)
    counts = {k: 0 for k in CLASSES}
    for i in items:
        counts[i["class"]] += 1
    losses = sorted((i for i in items if i["class"] == "LOSS"), key=lambda x: x["net_profit"])[:10]
    for e in losses:
        ads_eat = e["profit_before_ads"] > 0 > e["net_profit"]
        reason = (f"Son {window.days} günde {e['units']} adet satıldı, net zarar {tl(e['net_profit'])} "
                  f"(reklam öncesi {tl(e['profit_before_ads'])}); {_cost_driver(e)}. ")
        reason += ("Reklam payı kârı siliyor: reklamı azaltmadan/durdurmadan önce ürün kendi başına kârlı."
                   if ads_eat else "Ürün reklamsız da zarar ediyor: fiyat, maliyet veya kargo kurgusu gözden geçirilmeli.")
        propose(conn, agent="product_profit", action_type="product.review_loss", entity_type="product", entity_id=e["product_id"],
                title=f"Zarar eden ürün: {e['name']}", reason=reason, run_id=ctx.run_id, requires_approval=False,
                evidence={k: e[k] for k in ("units", "net_sales", "product_cost", "commission", "shipping", "refund",
                                             "ad_spend_allocated", "profit_before_ads", "net_profit", "net_margin", "return_rate")},
                expected_result={"goal": "Ürünün net kârını sıfırın üstüne çıkarmak veya satışını durdurmak"},
                confidence=min(0.9, 0.4 + e["units"] / 50))
    missing = [i for i in items if i["missing_cost"] and i["units"] > 0]
    for e in missing[:20]:
        propose(conn, agent="product_profit", action_type="product.fix_missing_cost", entity_type="product",
                entity_id=e["product_id"], title=f"Maliyet eksik: {e['name']}", run_id=ctx.run_id, requires_approval=False,
                reason=(f"Son {window.days} günde {e['units']} adet satıldı ama bazı satışlarda ürün maliyeti yok. "
                        "Maliyet girilmeden bu ürün için kâr, reklam veya stok kararı verilemez."),
                evidence={"units": e["units"], "net_sales": e["net_sales"]}, confidence=1.0)
    if missing:
        ctx.warnings.append(f"{len(missing)} üründe maliyet eksik")
    total_loss = sum((i["net_profit"] for i in items if i["class"] == "LOSS"), Decimal("0"))
    retired = retire_stale(conn, "product_profit", ctx.run_id)
    ctx.output = {"window": window.as_dict(), "products": len(items), "classes": counts, "loss_total": total_loss,
                  "proposals": len(losses) + min(len(missing), 20), "retired": retired}
    activity(conn, f"{len(items)} ürün analiz edildi: {counts['STAR']} yıldız, {counts['LOSS']} zarar, {counts['NO_DATA']} veri yok",
             agent="product_profit", kind="run", run_id=ctx.run_id)
    return ctx.output


# ------------------------------------------------------------------ Stok / Tedarikçi
def run_inventory(conn: Connection, ctx: RunContext) -> dict:
    from .data import inventory_status
    th = thresholds(conn)
    model = config.inventory_model(conn)
    ctx.sources += ["products.stock", "supplier_products.stock", "order_items (tüm kanallar)", "stock_reservations"]
    items = inventory_status(conn)
    risky = [i for i in items if i["stockout_risk"]]
    econ = {e["product_id"]: e for e in classified_products(conn)} if risky else {}
    # Günlük anlık görüntü (stoksuz kalma ölçümü / AI karnesi için)
    for i in items:
        conn.execute(text("""INSERT INTO ai_inventory_snapshots(product_id, snap_date, stock, available, supplier_stock, units_7d)
                             VALUES (:p, :d, :s, :a, :ss, :u) ON CONFLICT (product_id, snap_date) DO UPDATE
                             SET stock = EXCLUDED.stock, available = EXCLUDED.available,
                                 supplier_stock = EXCLUDED.supplier_stock, units_7d = EXCLUDED.units_7d"""),
                     {"p": i["product_id"], "d": config.today(), "s": i["stock"], "a": i["available"],
                      "ss": i["supplier_stock"], "u": i["units_7d"]})
    n = 0
    for i in risky[:15]:
        e = econ.get(i["product_id"])
        cls = e["class"] if e else "NO_DATA"
        days_txt = f"yaklaşık {i['days_of_inventory']} günlük" if i["days_of_inventory"] is not None else "hiç"
        base = (f"Son {i['velocity_basis']} satış hızına göre (günde {i['daily_velocity']} adet) {days_txt} stok kaldı "
                f"(kullanılabilir {i['available']}). Reklam ölçeklenmemeli (DO_NOT_SCALE_ADS).")
        if model == "own_stock" and cls in ("STAR", "PROFITABLE") and e:
            qty = max(0, math.ceil(float(i["daily_velocity"]) * 30) - i["available"])
            unit_cost = d(e["product_cost"]) / e["units"] if e["units"] else Decimal("0")
            cap = finance_view.q2(unit_cost * qty)
            if qty and cap > 0:
                propose(conn, agent="inventory", action_type="inventory.restock", entity_type="product", entity_id=i["product_id"],
                        title=f"Stok al: {i['name']} ({qty} adet)", run_id=ctx.run_id, required_capital=cap,
                        capital_category="inventory", params={"quantity": qty, "unit_cost": finance_view.q2(unit_cost)},
                        reason=base + f" Ürün {CLASSES[cls].lower()} sınıfında; 30 günlük ihtiyaç için {qty} adet öneriyorum.",
                        evidence={**{k: i[k] for k in ("available", "daily_velocity", "days_of_inventory", "units_7d")},
                                  "net_margin": e["net_margin"]},
                        expected_result={"avoid_stockout_days": 30}, confidence=0.6)
                n += 1
                continue
        if cls in ("STAR", "PROFITABLE", "WATCH"):
            propose(conn, agent="inventory", action_type="inventory.supplier_stock_risk", entity_type="product",
                    entity_id=i["product_id"], title=f"Stok tükeniyor: {i['name']}", run_id=ctx.run_id, requires_approval=False,
                    reason=base + (f" Tedarikçi stoğu: {i['supplier_stock']}." if i["supplier_stock"] is not None else "")
                    + " Tedarikçiyle stok teyidi yapın; kârlı bir ürünün tükenmesi satış kaybıdır.",
                    evidence={k: i[k] for k in ("available", "supplier_stock", "daily_velocity", "days_of_inventory", "units_7d")},
                    confidence=0.7)
            n += 1
    dead = [i for i in items if i["dead_stock"]] if model == "own_stock" else []
    for i in dead[:10]:
        propose(conn, agent="inventory", action_type="inventory.dead_stock", entity_type="product", entity_id=i["product_id"],
                title=f"Satmayan stok: {i['name']}", run_id=ctx.run_id, requires_approval=False,
                reason=(f"{th['dead_stock_days']} gündür satış yok; {i['available']} adet stokta"
                        + (f", bağlı sermaye {tl(i['capital_tied'])}." if i["capital_tied"] else ".")),
                evidence={k: i[k] for k in ("available", "capital_tied")}, confidence=0.8)
    if risky:
        activity(conn, f"Sinyal DO_NOT_SCALE_ADS: {len(risky)} üründe stok tükenme riski", agent="inventory", kind="signal",
                 level="warning", run_id=ctx.run_id)
    retired = retire_stale(conn, "inventory", ctx.run_id)
    ctx.output = {"model": model, "tracked": len(items), "stockout_risk": len(risky), "dead_stock": len(dead),
                  "do_not_scale_ads": [i["product_id"] for i in risky], "proposals": n + len(dead[:10]), "retired": retired}
    activity(conn, f"{len(items)} ürünün stoğu izlendi; {len(risky)} tükenme riski", agent="inventory", kind="run",
             run_id=ctx.run_id)
    return ctx.output


# ------------------------------------------------------------------ Reklam
def ad_verdict(c: dict, th: dict, stock_risk: bool) -> tuple[str, str]:
    if c["attributed_revenue"] is None:
        return "INSUFFICIENT_DATA", "Platform/kullanıcı bildirimli reklam satışı yok; reklamın kâr etkisi ölçülemez."
    if not c["product_ids"]:
        return "INSUFFICIENT_DATA", "Kampanyaya ürün bağlanmamış; ürün marjı bilinmeden net kâr hesaplanamaz."
    if c["missing_cost"] or c["product_margin_before_ads"] is None:
        return "INSUFFICIENT_DATA", "Kampanya ürünlerinin maliyeti/satışı eksik; reklam öncesi marj hesaplanamıyor."
    roas_txt = f"ROAS {c['roas']:.2f}" if c["roas"] is not None else "ROAS yok"
    if (c["clicks"] or 0) < th["ads_min_clicks"] or c["spend"] < d(th["ads_min_spend"]):
        if c["ad_net_profit"] is not None and c["ad_net_profit"] < 0 and -c["ad_net_profit"] >= d(th["ads_pause_loss"]):
            return "PAUSE", f"Veri az olmasına rağmen reklam {tl(-c['ad_net_profit'])} net zarar yazdı."
        return "TEST", (f"{c['clicks'] or 0} tıklama / {tl(c['spend'])} harcama ile karar için veri yetersiz; "
                        "mevcut bütçeyle teste devam.")
    net, nm = c["ad_net_profit"], c["net_margin_after_ads"]
    nm_txt = f"%{nm * 100:.1f}" if nm is not None else "—"
    if net < 0:
        if -net >= d(th["ads_pause_loss"]):
            return "PAUSE", f"{roas_txt} olsa da reklam {tl(-net)} net zarar ediyor (reklam sonrası net marj {nm_txt})."
        return "DECREASE_BUDGET", f"{roas_txt}; reklam {tl(-net)} net zarar ediyor. Bütçe azaltılmalı."
    if nm is not None and nm < d(th["ads_target_margin_after_ads"]):
        return "CONTINUE", (f"{roas_txt} olmasına rağmen reklam sonrası net marj {nm_txt}; hedef "
                            f"%{th['ads_target_margin_after_ads'] * 100:.0f}. Bütçe artırılmasını önermiyorum.")
    if stock_risk:
        return "CONTINUE", f"Reklam kârlı (net {tl(net)}, marj {nm_txt}) ama ürünlerde stok tükenme riski var: ölçekleme yok."
    return "INCREASE_BUDGET", (f"{roas_txt}, reklam sonrası net kâr {tl(net)}, net marj {nm_txt}; "
                               f"conversion {(c['conversion_rate'] or 0) * 100:.1f}%. Kontrollü artış öneriyorum.")


def advertising_analysis(conn: Connection) -> list[dict]:
    from .data import campaign_performance, inventory_status
    th = thresholds(conn)
    camps = [c for c in campaign_performance(conn, Window(th["ads_window_days"])) if c["status"] == "active"]
    pids = sorted({p for c in camps for p in c["product_ids"]})
    risky = {i["product_id"] for i in inventory_status(conn, pids) if i["stockout_risk"]} if pids else set()
    for c in camps:
        stock_risk = bool(set(c["product_ids"]) & risky)
        v, why = ad_verdict(c, th, stock_risk)
        c.update(verdict=v, verdict_label=AD_VERDICTS[v], verdict_reason=why, stock_risk=stock_risk)
    return camps


def run_advertising(conn: Connection, ctx: RunContext) -> dict:
    th = thresholds(conn)
    ctx.sources += ["ad_spend", "ad_performance (platform/kullanıcı bildirimli)", "ürün marjları (finance_view)",
                    "stok sinyali (DO_NOT_SCALE_ADS)"]
    camps = advertising_analysis(conn)
    step = Decimal(str(th["ads_max_budget_step"]))
    counts: dict[str, int] = {}
    n = 0
    for c in camps:
        counts[c["verdict"]] = counts.get(c["verdict"], 0) + 1
        activity(conn, f"Kampanya #{c['id']} {c['name']}: {c['verdict_label']}", agent="advertising", kind="analysis",
                 run_id=ctx.run_id)
        ev = {k: c[k] for k in ("spend", "clicks", "impressions", "attributed_orders", "attributed_revenue", "roas", "ctr",
                                "cpc", "conversion_rate", "product_margin_before_ads", "ad_net_profit", "net_margin_after_ads",
                                "last_perf_date")}
        conf = min(0.9, 0.3 + (c["clicks"] or 0) / (th["ads_min_clicks"] * 5))
        budget = d(c["daily_budget"]) if c["daily_budget"] is not None else None
        if c["verdict"] in ("INCREASE_BUDGET", "DECREASE_BUDGET"):
            if not budget:
                ctx.warnings.append(f"Kampanya #{c['id']} için günlük bütçe tanımlı değil; bütçe önerisi üretilemedi")
                continue
            factor = (1 + step) if c["verdict"] == "INCREASE_BUDGET" else (1 - step)
            new = finance_view.q2(budget * factor)
            delta = new - budget
            action = "ads.increase_budget" if delta > 0 else "ads.decrease_budget"
            propose(conn, agent="advertising", action_type=action, entity_type="campaign", entity_id=c["id"], channel=c["channel"],
                    title=f"{c['name']}: {tl(budget)} → {tl(new)}/gün", reason=c["verdict_reason"], run_id=ctx.run_id,
                    evidence=ev, params={"current_daily_budget": budget, "new_daily_budget": new, "delta_per_day": delta},
                    required_capital=finance_view.q2(delta * 14) if delta > 0 else 0,
                    capital_category="advertising" if delta > 0 else None,
                    expected_result={"measure_after_days": 7, "metric": "reklam sonrası net kâr",
                                     "baseline_ad_net_profit": c["ad_net_profit"]}, confidence=conf)
            n += 1
        elif c["verdict"] == "PAUSE":
            propose(conn, agent="advertising", action_type="ads.pause", entity_type="campaign", entity_id=c["id"],
                    channel=c["channel"], title=f"Reklamı durdur: {c['name']}", reason=c["verdict_reason"], run_id=ctx.run_id,
                    evidence=ev, expected_result={"saves_per_day": finance_view.q2(c["spend"] / max(1, c["spend_days"] or 1))},
                    confidence=conf)
            n += 1
    if any(c["verdict"] == "INSUFFICIENT_DATA" for c in camps):
        ctx.warnings.append("Bazı kampanyalarda veri yetersiz")
    retired = retire_stale(conn, "advertising", ctx.run_id)
    ctx.output = {"campaigns": len(camps), "verdicts": counts, "proposals": n, "retired": retired}
    activity(conn, f"{len(camps)} aktif kampanya analiz edildi; {n} öneri", agent="advertising", kind="run", run_id=ctx.run_id)
    return ctx.output


def run_capital(conn: Connection, ctx: RunContext) -> dict:
    from .capital import position
    ctx.sources += ["ai_capital_accounts (elle)", "expenses (aylık sabit gider)", "ai_proposals (gerekçelendirilmiş)"]
    p = position(conn)
    if p["usable"] is None:
        ctx.warnings.append("Kasa bilgisi girilmemiş")
    ctx.output = {k: p[k] for k in ("usable", "justified", "unused", "reserve_required", "pending_payout_not_counted")}
    ctx.output["recommendation"] = p["recommendation"]
    return ctx.output


# ------------------------------------------------------------------ anomali (risk motoru, sistem düzeyi)
def scan_anomalies(conn: Connection) -> list[dict]:
    """Sistem düzeyi risk olayları: anormal reklam harcaması, beklenmeyen sipariş artışı, stoksuz ürüne reklam."""
    found = []
    r = row(conn, """
        SELECT (SELECT COALESCE(SUM(amount), 0) FROM ad_spend WHERE spend_date = CURRENT_DATE - 1) AS y_spend,
               (SELECT COALESCE(SUM(amount), 0) / 7 FROM ad_spend WHERE spend_date BETWEEN CURRENT_DATE - 8 AND CURRENT_DATE - 2) AS avg_spend,
               (SELECT COUNT(*) FROM orders WHERE order_date >= CURRENT_DATE - 1 AND order_date < CURRENT_DATE) AS y_orders,
               (SELECT COUNT(*)::numeric / 14 FROM orders WHERE order_date >= CURRENT_DATE - 15 AND order_date < CURRENT_DATE - 1) AS avg_orders""")
    if r["avg_spend"] and d(r["y_spend"]) > d(r["avg_spend"]) * 2 and d(r["y_spend"]) > 200:
        found.append({"code": "ad_spend_spike", "severity": "warning",
                      "message": f"Dün reklam harcaması {tl(r['y_spend'])}; son 7 gün ortalaması {tl(r['avg_spend'])}."})
    if r["avg_orders"] and d(r["y_orders"]) > d(r["avg_orders"]) * 3 and r["y_orders"] >= 10:
        found.append({"code": "order_spike", "severity": "warning",
                      "message": f"Dün {r['y_orders']} sipariş geldi (ortalama {d(r['avg_orders']):.1f}). Fiyat hatası veya olağandışı kampanya kontrol edin."})
    oos_ads = rows(conn, """
        SELECT DISTINCT p.id, p.name FROM ad_campaigns c JOIN ad_campaign_products x ON x.campaign_id = c.id
          JOIN products p ON p.id = x.product_id
         WHERE c.status = 'active' AND COALESCE(p.stock, 0) <= 0
           AND EXISTS (SELECT 1 FROM ad_spend s WHERE s.campaign_id = c.id AND s.spend_date >= CURRENT_DATE - 2)""")
    for p in oos_ads:
        found.append({"code": "ads_on_zero_stock", "severity": "warning",
                      "message": f"Stoğu 0 olan ürüne reklam harcanıyor: {p['name']}"})
    for f in found:
        dup = conn.execute(text("""SELECT 1 FROM ai_risk_events WHERE code = :c AND message = :m
                                   AND created_at > NOW() - INTERVAL '20 hours'"""), {"c": f["code"], "m": f["message"]}).first()
        if not dup:
            conn.execute(text("INSERT INTO ai_risk_events(code, severity, message) VALUES (:c, :s, :m)"),
                         {"c": f["code"], "s": f["severity"], "m": f["message"]})
            activity(conn, f["message"], agent="risk", kind="risk", level="warning")
    return found
