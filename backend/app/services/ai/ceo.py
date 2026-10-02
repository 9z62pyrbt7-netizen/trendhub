"""CEO: döngüyü yönetir, günlük "Bugün bilmen gerekenler" özetini önceliklendirir.

Hedef fonksiyon: sürdürülebilir NET KÂR. Ciro tek başına başarı sayılmaz; özet ve öneriler net kâr, nakit, risk ve
veri kalitesine göre sıralanır. Veri yoksa "veri yok" der, rakam uydurmaz.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ...db import row
from .. import finance_view
from . import agents, config
from .config import Window, d, today
from .proposals import activity, expire_old

log = logging.getLogger("trendhub.ai")
AGENT_ORDER = (("inventory", agents.run_inventory), ("product_profit", agents.run_product_profit),
               ("advertising", agents.run_advertising), ("capital", agents.run_capital))


def run_cycle(engine: Engine, trigger: str = "schedule") -> dict:
    """Tüm ajanları sırayla çalıştırır. Bir ajanın hatası diğerlerini durdurmaz (ama sessizce de kaybolmaz)."""
    out: dict = {}
    with engine.begin() as conn:
        if not config.enabled(conn):
            return {"skipped": "ai.enabled=false"}
    with agents.agent_run(engine, "ceo", trigger) as ceo:
        ceo.sources += [c for c, _ in AGENT_ORDER]
        for code, fn in AGENT_ORDER:
            with engine.begin() as conn:
                if not agents.agent_enabled(conn, code):
                    out[code] = {"skipped": "kapalı"}
                    continue
            try:
                with agents.agent_run(engine, code, trigger) as ctx:
                    with engine.begin() as conn:
                        out[code] = fn(conn, ctx)
            except Exception as exc:  # noqa: BLE001 - ajan hatası kayıtlı (agent_run), döngü sürer
                log.exception("AI ajanı başarısız: %s", code)
                out[code] = {"error": f"{exc.__class__.__name__}: {exc}"}
                ceo.warnings.append(f"{code} ajanı hata verdi")
        with engine.begin() as conn:
            out["risk_anomalies"] = len(agents.scan_anomalies(conn))
            out["expired"] = expire_old(conn)
        with engine.begin() as conn:
            from .decisions import evaluate_outcomes
            out["outcomes_written"] = evaluate_outcomes(conn)
        with engine.begin() as conn:
            brief = build_brief(conn)
            store_brief(conn, brief)
            out["brief_items"] = len(brief["items"])
            activity(conn, f"Günlük özet güncellendi ({len(brief['items'])} madde)", agent="ceo", kind="brief", run_id=ceo.run_id)
        ceo.output = out
    return out


def _money(v) -> str:
    from ...storefront.store_config import fmt_try
    return fmt_try(v)


def build_brief(conn: Connection) -> dict:
    """En önemli 3–7 konu, önem sırasıyla. Her madde kaynağını (`source`) taşır."""
    from .capital import position
    from .data import data_quality, period_summary
    items: list[dict] = []
    t = today()
    y = period_summary(conn, Window(1, end_date=t - timedelta(days=1)))
    prev = period_summary(conn, Window(7, end_date=t - timedelta(days=2)))
    avg_profit = d(prev["net_profit"]) / 7
    if y["orders"] or prev["orders"]:
        diff = d(y["net_profit"]) - avg_profit
        trend = ("ortalamanın üstünde" if diff > 0 else "ortalamanın altında") if prev["orders"] else ""
        items.append({"priority": 80 if diff < 0 else 60, "kind": "yesterday", "level": "warning" if d(y["net_profit"]) < 0 else "info",
                      "text": f"Dün {y['orders']} sipariş geldi; tahmini net kâr {_money(y['net_profit'])} "
                              f"(son 7 gün günlük ortalaması {_money(finance_view.q2(avg_profit))}{', ' + trend if trend else ''}).",
                      "source": "Finans (dashboard ile aynı formül)", "link": "#/finance"})
    else:
        items.append({"priority": 50, "kind": "yesterday", "level": "info", "text": "Dün ve önceki 7 günde sipariş kaydı yok.",
                      "source": "Siparişler", "link": "#/orders"})
    camps = agents.advertising_analysis(conn)
    losing = [c for c in camps if c["ad_net_profit"] is not None and c["ad_net_profit"] < 0]
    if losing:
        loss = sum((-c["ad_net_profit"] for c in losing), Decimal("0"))
        items.append({"priority": 90, "kind": "ads_loss", "level": "warning",
                      "text": f"{len(losing)} reklam kampanyası net zarar ediyor (son 7 gün toplam {_money(loss)}).",
                      "source": "Reklam ajanı", "link": "#/ai?tab=ads"})
    nodata = [c for c in camps if c["verdict"] == "INSUFFICIENT_DATA" and c["spend"] > 0]
    if nodata:
        items.append({"priority": 55, "kind": "ads_nodata", "level": "info",
                      "text": f"{len(nodata)} kampanyaya para harcanıyor ama kâr etkisi ölçülemiyor (performans/maliyet verisi eksik).",
                      "source": "Reklam ajanı", "link": "#/ai?tab=ads"})
    from .data import inventory_status
    risky = [i for i in inventory_status(conn) if i["stockout_risk"]]
    if risky:
        top = risky[0]
        txt = (f"{top['name']} yaklaşık {top['days_of_inventory']} günlük stoğa düştü" if top["days_of_inventory"] is not None
               else f"{top['name']} stokta yok ama satılıyordu")
        items.append({"priority": 75, "kind": "stock", "level": "warning",
                      "text": txt + (f"; toplam {len(risky)} üründe tükenme riski." if len(risky) > 1 else "."),
                      "source": "Stok ajanı", "link": "#/ai?tab=inventory"})
    r = row(conn, """SELECT COUNT(*) FILTER (WHERE status = 'pending_approval' AND requires_approval) AS pending,
                            COUNT(*) FILTER (WHERE status = 'pending_approval' AND NOT requires_approval) AS tasks,
                            COUNT(*) FILTER (WHERE status = 'approved') AS to_apply,
                            COUNT(*) FILTER (WHERE status = 'blocked' AND created_at > NOW() - INTERVAL '1 day') AS blocked
                       FROM ai_proposals""")
    if r["pending"]:
        items.append({"priority": 85, "kind": "approvals", "level": "action",
                      "text": f"Bugün onaylamanı önerdiğim {r['pending']} işlem var.", "source": "Onaylar", "link": "#/ai?tab=approvals"})
    if r["to_apply"]:
        items.append({"priority": 70, "kind": "to_apply", "level": "action",
                      "text": f"Onayladığın {r['to_apply']} işlem platformda uygulanmayı bekliyor.", "source": "Onaylar",
                      "link": "#/ai?tab=approvals"})
    if r["tasks"]:
        items.append({"priority": 45, "kind": "tasks", "level": "info",
                      "text": f"{r['tasks']} inceleme görevi var (zarar eden ürün, eksik maliyet, stok).", "source": "Ajanlar",
                      "link": "#/ai?tab=approvals"})
    loss_products = [p for p in agents.classified_products(conn) if p["class"] == "LOSS"]
    if loss_products:
        tot = sum((p["net_profit"] for p in loss_products), Decimal("0"))
        items.append({"priority": 72, "kind": "loss_products", "level": "warning",
                      "text": f"Son 30 günde {len(loss_products)} ürün zarar ettirdi (toplam {_money(tot)}).",
                      "source": "Ürün & Kâr ajanı", "link": "#/ai?tab=profit"})
    pos = position(conn)
    if pos["usable"] is not None and pos["justified"] == 0 and pos["usable"] > 0:
        items.append({"priority": 30, "kind": "capital", "level": "info", "text": pos["recommendation"],
                      "source": "Sermaye motoru", "link": "#/ai?tab=capital"})
    quality = data_quality(conn)
    for q in quality:
        if q["severity"] == "warning":
            items.append({"priority": 65, "kind": "data_quality", "level": "warning", "text": q["message"],
                          "source": "Veri kalitesi", "link": "#/ai?tab=risk"})
    if config.emergency_stop(conn):
        items.append({"priority": 100, "kind": "emergency", "level": "critical",
                      "text": "ACİL DURDURMA AKTİF: hiçbir yazma işlemi yapılmıyor; analiz devam ediyor.", "source": "Risk motoru",
                      "link": "#/ai"})
    items.sort(key=lambda x: -x["priority"])
    return {"date": t, "items": items[:7], "data_quality": quality}


def store_brief(conn: Connection, brief: dict) -> None:
    conn.execute(text("""INSERT INTO ai_briefs(brief_date, items, data_quality) VALUES (:d, CAST(:i AS JSONB), CAST(:q AS JSONB))
                         ON CONFLICT (brief_date) DO UPDATE SET items = EXCLUDED.items, data_quality = EXCLUDED.data_quality,
                         updated_at = NOW()"""),
                 {"d": brief["date"], "i": json.dumps(brief["items"], ensure_ascii=False, default=str),
                  "q": json.dumps(brief["data_quality"], ensure_ascii=False, default=str)})
