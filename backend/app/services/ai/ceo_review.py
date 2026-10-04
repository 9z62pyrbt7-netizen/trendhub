"""CEO: ajanlar arası hakemlik, önceliklendirme, ajan performansı ve Türkçe yönetici özeti.

Hakemlik kuralları (ajanlar aynı ürün için birbirinden habersiz karar veremez):
  * Stok ajanı "stok kritik" diyorsa: reklam bütçe artışı / yeni reklam / indirim kampanyası → CEO BLOCKED (stock_risk)
  * Ürün zarar ediyorsa (LOSS): reklam / indirim → BLOCKED (loss_product)
  * Fiyat ajanı aynı ürün için fiyat artışı öneriyorsa: indirim ve yeni reklam → BLOCKED (conflict) — önce birim kâr düzelir
Onaylanmış ama uygulanmamış öneriler de yeniden değerlendirilir (durum değiştiyse uygulanmadan engellenir).
"""
from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ...db import row, rows
from . import actions
from .config import Window, d, thresholds, tl, today
from .proposals import activity

SCALING = {"ads.increase_budget", "ads.create_campaign", "campaign.discount"}


def _products_of(conn: Connection, p: dict) -> list[int]:
    if p["entity_type"] == "product":
        return [p["entity_id"]]
    if p["entity_type"] == "campaign":
        return [r["product_id"] for r in rows(conn, "SELECT product_id FROM ad_campaign_products WHERE campaign_id = :c", c=p["entity_id"])]
    return []


def resolve_conflicts(conn: Connection, cycle_id: int | None = None) -> list[dict]:
    from .agents import classified_products
    from .data import inventory_status
    th = thresholds(conn)
    open_ = rows(conn, """SELECT * FROM ai_proposals WHERE status IN ('pending_approval', 'approved') AND requires_approval
                           AND action_type = ANY(:a) ORDER BY id""", a=list(SCALING))
    if not open_:
        return []
    pmap = {p["id"]: _products_of(conn, p) for p in open_}
    pids = sorted({x for v in pmap.values() for x in v})
    stock_risk = {i["product_id"] for i in inventory_status(conn, pids)
                  if i["stockout_risk"] or i["available"] < int(th["stock_safety_units"])} if pids else set()
    prods = classified_products(conn)
    loss = {p["product_id"] for p in prods if p["class"] == "LOSS"}
    # Reklamsız da zarar eden ürün: kampanya bütçe artışını engeller. Zararı yalnızca reklam payından geliyorsa kampanyanın
    # kendi net kâr etkisi risk motorunda (negative_margin / low_margin) değerlendirilir; burada çift sayılmaz.
    loss_before_ads = {p["product_id"] for p in prods if p["class"] == "LOSS" and p["profit_before_ads"] < 0}
    raising = {r["entity_id"] for r in rows(conn, """SELECT entity_id FROM ai_proposals WHERE action_type = 'pricing.change_price'
                                                       AND status IN ('pending_approval', 'approved')
                                                       AND (params->>'new_price')::numeric > (params->>'old_price')::numeric""")}
    out = []
    for p in open_:
        ids = set(pmap[p["id"]])
        found = []
        if ids & stock_risk:
            found.append(("stock_risk", "Stok ajanı: ürün stoğu kritik/tükeniyor → reklam veya kampanya ölçeklenmez."))
        if ids & (loss_before_ads if p["entity_type"] == "campaign" else loss):
            found.append(("loss_product", "Ürün & Kâr ajanı: ürün zarar ediyor → reklam/indirim zararı büyütür."))
        if p["action_type"] in ("campaign.discount", "ads.create_campaign") and ids & raising:
            found.append(("conflict", "Fiyat ajanı aynı ürün için fiyat artışı öneriyor; birim kâr düzelmeden indirim/reklam yapılmaz."))
        if not found:
            continue
        msg = "CEO hakemliği: " + " ".join(m for _c, m in found)
        checks = (p["risk_checks"] or []) + [{"code": f"ceo_{c}", "severity": "block", "message": m, "details": {}} for c, m in found]
        conn.execute(text("""UPDATE ai_proposals SET status = 'blocked', risk_checks = CAST(:c AS JSONB), updated_at = NOW()
                             WHERE id = :id"""), {"c": json.dumps(checks, ensure_ascii=False, default=str), "id": p["id"]})
        activity(conn, msg, agent="ceo", kind="ceo_review", level="warning", proposal_id=p["id"])
        actions.sync_proposal(conn, p["id"])
        actions.log(conn, agent="ceo", action_type=f"arbitrate:{p['action_type']}", status="BLOCKED", reason=msg,
                    reason_code=found[0][0], entity_type=p["entity_type"], entity_id=p["entity_id"], proposal_id=p["id"],
                    input_data={"products": sorted(ids)}, cycle_id=cycle_id, dedupe_key=f"conflict:{p['id']}")
        out.append({"proposal_id": p["id"], "title": p["title"], "reasons": [c for c, _m in found]})
    return out


# ------------------------------------------------------------------ önceliklendirme
def _impact(p: dict) -> Decimal:
    ex, ev = p["expected_result"] or {}, p["evidence"] or {}
    for v in (ex.get("expected_profit_change_same_volume"), ev.get("net_profit"), (d(ex.get("saves_per_day")) * 30) or None):
        if v not in (None, "", 0):
            return abs(d(v))
    return d(p["required_capital"]) / 10


def next_tasks(conn: Connection, coverage: dict | None = None, limit: int = 3) -> list[dict]:
    items = []
    if coverage and coverage["unmatched_lines"]:
        items.append({"impact": d(coverage["unmatched_revenue"]), "text":
                      f"{coverage['unmatched_lines']} satış kalemini ürünle eşleştir (ciro {tl(coverage['unmatched_revenue'])} kâr analizinde görünmüyor)."})
    if coverage and coverage["missing_cost_lines"]:
        items.append({"impact": Decimal("1000000"), "text":
                      f"{coverage['missing_cost_lines']} satış kalemindeki ürünlerin maliyetini gir (maliyetsiz ürüne fiyat/reklam kararı verilmez)."})
    for p in rows(conn, """SELECT * FROM ai_proposals WHERE status IN ('pending_approval', 'approved') ORDER BY id DESC LIMIT 200"""):
        verb = "Onayla/değerlendir" if p["status"] == "pending_approval" and p["requires_approval"] else (
            "Uygula" if p["status"] == "approved" else "İncele")
        items.append({"impact": _impact(p), "text": f"{verb}: {p['title']}", "proposal_id": p["id"]})
    items.sort(key=lambda x: -x["impact"])
    return [{k: v for k, v in i.items() if k != "impact"} | {"impact": i["impact"]} for i in items[:limit]]


# ------------------------------------------------------------------ ajan performansı
def agent_performance(conn: Connection, days: int = 30) -> list[dict]:
    perf = {r["agent_code"]: dict(r) for r in rows(conn, """
        SELECT agent_code, COUNT(*) AS proposals,
               COUNT(*) FILTER (WHERE status = 'blocked') AS blocked,
               COUNT(*) FILTER (WHERE status IN ('approved', 'executed')) AS approved,
               COUNT(*) FILTER (WHERE status = 'rejected') AS rejected,
               COUNT(*) FILTER (WHERE status = 'executed') AS executed
          FROM ai_proposals WHERE created_at > NOW() - make_interval(days => :d) AND requires_approval GROUP BY 1""", d=days)}
    for r in rows(conn, """
        SELECT p.agent_code, COUNT(*) FILTER (WHERE o.final_result = 'improved') AS improved,
               COUNT(*) FILTER (WHERE o.final_result = 'worsened') AS worsened,
               COUNT(*) FILTER (WHERE o.final_result IN ('improved', 'worsened', 'neutral')) AS measured
          FROM ai_decision_outcomes o JOIN ai_decisions x ON x.id = o.decision_id JOIN ai_proposals p ON p.id = x.proposal_id
         WHERE o.horizon_days = 7 GROUP BY 1"""):
        perf.setdefault(r["agent_code"], {"agent_code": r["agent_code"], "proposals": 0, "blocked": 0, "approved": 0, "rejected": 0,
                                          "executed": 0}).update(improved=r["improved"], worsened=r["worsened"], measured=r["measured"])
    runs = {r["agent_code"]: r for r in rows(conn, """
        SELECT agent_code, COUNT(*) AS runs, COUNT(*) FILTER (WHERE status = 'error') AS errors
          FROM ai_agent_runs WHERE started_at > NOW() - make_interval(days => :d) GROUP BY 1""", d=days)}
    out = []
    for a in rows(conn, "SELECT code, name, available, enabled, last_status FROM ai_agents WHERE available ORDER BY code"):
        p = perf.get(a["code"], {})
        decided = int(p.get("approved") or 0) + int(p.get("rejected") or 0)
        measured = int(p.get("measured") or 0)
        out.append({"agent": a["code"], "name": a["name"], "last_status": a["last_status"],
                    "runs": int((runs.get(a["code"]) or {}).get("runs") or 0), "errors": int((runs.get(a["code"]) or {}).get("errors") or 0),
                    "proposals": int(p.get("proposals") or 0), "blocked": int(p.get("blocked") or 0),
                    "approval_rate": round(int(p.get("approved") or 0) / decided, 3) if decided else None,
                    "measured": measured, "success_rate": round(int(p.get("improved") or 0) / measured, 3) if measured else None})
    return out


# ------------------------------------------------------------------ yönetici özeti
def executive_summary(conn: Connection, coverage: dict | None = None) -> dict:
    from ..platform.finance import profit_provenance
    from .agents import classified_products
    from .data import inventory_status, period_summary
    from .growth import ad_candidates
    t = today()
    td = period_summary(conn, Window(1, end_date=t))
    yd = period_summary(conn, Window(1, end_date=t - timedelta(days=1)))
    wk = period_summary(conn, Window(7, end_date=t))
    w7 = Window(7, end_date=t)
    pv = profit_provenance(conn, w7.start, w7.end)
    kind = {"ACTUAL": "GERÇEK", "PARTIAL": "KISMEN GERÇEK", "ESTIMATED": "TAHMİNİ", "NO_DATA": "VERİ YOK"}[pv["status"]]
    prods = classified_products(conn)
    good = sorted((p for p in prods if p["class"] in ("STAR", "PROFITABLE")), key=lambda x: -x["net_profit"])[:3]
    bad = sorted((p for p in prods if p["class"] == "LOSS"), key=lambda x: x["net_profit"])[:3]
    risky = [i for i in inventory_status(conn) if i["stockout_risk"]][:3]
    ads = [c for c in ad_candidates(conn) if not c["blocked"]][:3]
    done = rows(conn, """SELECT agent_code, action_type, actual_action, reason FROM ai_actions WHERE status = 'EXECUTED'
                          AND created_at >= CURRENT_DATE ORDER BY id DESC LIMIT 5""")
    blocked = rows(conn, """SELECT agent_code, action_type, reason_code, reason FROM ai_actions WHERE status = 'BLOCKED'
                             AND created_at >= CURRENT_DATE ORDER BY id DESC LIMIT 50""")
    waiting = rows(conn, """SELECT id, title, risk_level FROM ai_proposals WHERE status = 'pending_approval' AND requires_approval
                             ORDER BY id DESC LIMIT 10""")
    cls_counts: dict[str, int] = {}
    for p in prods:
        cls_counts[p["class"]] = cls_counts.get(p["class"], 0) + 1
    happened = [f"Bugün {td['orders']} sipariş (dün {yd['orders']})."]
    if coverage and coverage["issues"]:
        happened.append("Veri sorunu: " + coverage["issues"][0])
    sections = {
        "BUGÜN NE OLDU?": happened,
        "NE KADAR SATIŞ?": [f"Bugün net satış {tl(td['net_sales'])}, dün {tl(yd['net_sales'])}, son 7 gün {tl(wk['net_sales'])}."
                            if wk["orders"] else "Son 7 günde sipariş kaydı yok (veri yok)."],
        "NET KÂR": [f"Son 7 gün net kâr {tl(wk['net_profit'])} ({kind}); bugün {tl(td['net_profit'])}." if wk["orders"]
                    else "Veri yok: son 7 günde sipariş yok."]
                   + ([f"Komisyonun {pv['actual_commission_items']}/{pv['items']} kalemi gerçek Trendyol kesintisi."]
                      if pv["status"] == "PARTIAL" else []),
        "HANGİ ÜRÜNLER İYİ?": [f"{p['name']}: net kâr {tl(p['net_profit'])}, marj %{d(p['net_margin'] or 0) * 100:.1f}" for p in good]
                              or [f"Kârlı/yıldız ürün yok. Sınıflar: {cls_counts or 'veri yok'}."],
        "HANGİLERİ ZARARDA?": [f"{p['name']}: {tl(p['net_profit'])}" for p in bad] or ["Zarar eden ürün yok."],
        "STOK RİSKİ?": [f"{i['name']}: {i['available']} adet, " + (f"~{i['days_of_inventory']} gün" if i["days_of_inventory"] else "stokta yok")
                        for i in risky] or ["Tükenme riski yok."],
        "REKLAM İÇİN EN İYİ ÜRÜNLER?": [f"{c['name']}: birim net kâr {tl(c['unit_profit'])}, stok {c['available']}" for c in ads]
                                       or ["Reklama uygun (kârlı + stoklu + maliyeti bilinen) ürün yok."],
        "BUGÜN YAPILAN AKSİYONLAR?": [f"{a['actual_action'] or a['action_type']}" for a in done]
                                     or ["Sistem bugün platformda hiçbir aksiyon uygulamadı (yalnızca öneri)."],
        "ENGELLENEN RİSKLİ AKSİYONLAR?": ([f"{len(blocked)} aksiyon engellendi."] + [f"{b['agent_code']}: {b['reason'][:140]}" for b in blocked[:3]])
                                         if blocked else ["Bugün engellenen aksiyon yok."],
        "BENDEN ONAY BEKLEYENLER?": [f"#{w['id']} {w['title']} (risk {w['risk_level']})" for w in waiting] or ["Onay bekleyen öneri yok."],
        "SONRAKİ EN ÖNEMLİ 3 İŞ?": [x["text"] for x in next_tasks(conn, coverage)] or ["Bekleyen iş yok."],
    }
    # JSONB anahtar sırasını korumaz: okuma tarafı "order" listesine göre sıralar
    return {"date": t.isoformat(), "sections": sections, "order": list(sections), "classes": cls_counts,
            "profit_basis": pv["status"]}


def ordered_sections(summary: dict) -> list[tuple[str, list[str]]]:
    order = summary.get("order") or list(summary["sections"])
    return [(h, summary["sections"][h]) for h in order if h in summary["sections"]]


def daily_review(engine) -> dict:
    """Günlük performans değerlendirmesi: geçmiş kararların sonuçları + yönetici özeti (LLM yok)."""
    from .decisions import evaluate_outcomes
    from .reconcile import coverage
    with engine.begin() as conn:
        written = evaluate_outcomes(conn)
        summ = executive_summary(conn, coverage(conn))
        conn.execute(text("""INSERT INTO ai_briefs(brief_date, items, data_quality, summary) VALUES (:d, '[]'::jsonb, '[]'::jsonb, CAST(:s AS JSONB))
                             ON CONFLICT (brief_date) DO UPDATE SET summary = EXCLUDED.summary, updated_at = NOW()"""),
                     {"d": today(), "s": json.dumps(summ, ensure_ascii=False, default=str)})
        activity(conn, f"Günlük değerlendirme: {written} sonuç ölçümü yazıldı; yönetici özeti güncellendi", agent="ceo", kind="review")
    return {"outcomes_written": written, "summary_sections": len(summ["sections"])}


def weekly_review(engine) -> dict:
    """Haftalık strateji değerlendirmesi: ajan performansı + bu hafta / geçen hafta."""
    from .data import period_summary
    with engine.begin() as conn:
        perf = agent_performance(conn, 7)
        cur, prev = period_summary(conn, Window(7)), period_summary(conn, Window(7).previous())
        weak = [p for p in perf if p["success_rate"] is not None and p["success_rate"] < 0.5 and p["measured"] >= 3]
        msg = (f"Haftalık değerlendirme: net kâr {tl(cur['net_profit'])} (önceki hafta {tl(prev['net_profit'])}). "
               + (("Başarı oranı düşük ajanlar: " + ", ".join(f"{p['name']} %{p['success_rate'] * 100:.0f}" for p in weak) + ".")
                  if weak else "Ölçülmüş sonucu kötü olan ajan yok."))
        activity(conn, msg, agent="ceo", kind="review")
    return {"message": msg, "agents": len(perf)}


def latest_summary(conn: Connection) -> dict | None:
    r = row(conn, "SELECT summary FROM ai_briefs WHERE summary IS NOT NULL ORDER BY brief_date DESC LIMIT 1")
    return r["summary"] if r else None
