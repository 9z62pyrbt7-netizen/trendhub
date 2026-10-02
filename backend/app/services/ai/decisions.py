"""Karar günlüğü, sonuç ölçümü (1/3/7/30 gün), sahip analizi, karar kalitesi raporu, AI karnesi.

Sonuç ölçümü nedensellik İDDİA ETMEZ: kararın uygulandığı andan sonraki N gün ile önceki N gün karşılaştırılır.
Mevsimsellik ve diğer değişiklikler sonucu etkileyebilir; ekranlarda bu not gösterilir.
"""
from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ...db import row, rows
from .. import finance_view
from .config import Window, d, today

HORIZONS = (1, 3, 7, 30)
NOTE = ("Sonuç = karardan sonraki N gün ile önceki N günün farkı. Bu bir korelasyondur; mevsimsellik ve başka "
        "değişiklikler de etkiler. Tek bir sonuçtan değil, tekrarlanan örüntülerden ders çıkarın.")


def _json(v) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def record(conn: Connection, *, proposal: dict | None, actor: str, decision: str, user_id: int | None = None,
           snapshot: dict | None = None, executed: bool = False, reason: str | None = None,
           decision_type: str | None = None, entity_type: str | None = None, entity_id: int | None = None) -> int:
    p = proposal or {}
    return conn.execute(text("""
        INSERT INTO ai_decisions(proposal_id, actor, agent_code, user_id, decision, decision_type, entity_type, entity_id,
                                 context_snapshot, reason, expected_result, risk_level, confidence, requires_approval,
                                 approved_by, executed_at)
        VALUES (:pid, :actor, :agent, :uid, :dec, :dtype, :et, :eid, CAST(:snap AS JSONB), :reason, CAST(:exp AS JSONB),
                :risk, :conf, :req, :appr, CASE WHEN :executed THEN NOW() END) RETURNING id"""), {
        "pid": p.get("id"), "actor": actor, "agent": p.get("agent_code"), "uid": user_id, "dec": decision,
        "dtype": decision_type or p.get("action_type"), "et": entity_type or p.get("entity_type"),
        "eid": entity_id if entity_id is not None else p.get("entity_id"), "snap": _json(snapshot or {}),
        "reason": reason or p.get("reason"), "exp": _json(p.get("expected_result") or {}), "risk": p.get("risk_level"),
        "conf": p.get("confidence"), "req": p.get("requires_approval", True),
        "appr": user_id if decision == "approved" else None, "executed": executed}).scalar()


# ------------------------------------------------------------------ varlık metrikleri
def entity_metrics(conn: Connection, entity_type: str | None, entity_id: int | None, w: Window) -> dict:
    from .data import campaign_performance, product_economics
    if entity_type == "product" and entity_id:
        e = next(iter(product_economics(conn, w, [entity_id])), None)
        base = {"units": 0, "revenue": Decimal("0"), "net_profit": Decimal("0"), "ad_spend": Decimal("0"), "returns": 0}
        if e:
            base = {"units": e["units"], "revenue": e["net_sales"], "net_profit": e["net_profit"],
                    "ad_spend": e["ad_spend_allocated"], "returns": e["returned_orders"]}
        inv = conn.execute(text("SELECT available FROM ai_inventory_snapshots WHERE product_id = :p AND snap_date <= :e "
                                "ORDER BY snap_date DESC LIMIT 1"), {"p": entity_id, "e": w.end_date}).scalar()
        base["stock"] = inv
        return base
    if entity_type == "campaign" and entity_id:
        c = next((c for c in campaign_performance(conn, w) if c["id"] == entity_id), None)
        if c is None:
            return {}
        econ = product_economics(conn, w, c["product_ids"]) if c["product_ids"] else []
        return {"ad_spend": c["spend"], "attributed_revenue": c["attributed_revenue"], "ad_net_profit": c["ad_net_profit"],
                "clicks": c["clicks"], "conversion_rate": c["conversion_rate"],
                "units": sum(e["units"] for e in econ), "revenue": sum((e["net_sales"] for e in econ), Decimal("0")),
                "net_profit": sum((e["net_profit"] for e in econ), Decimal("0")),
                "returns": sum(e["returned_orders"] for e in econ)}
    return {}


def _change(after, before):
    if after is None or before is None:
        return None
    return finance_view.q2(d(after) - d(before)) if isinstance(after, Decimal) or isinstance(before, Decimal) else after - before


def evaluate_outcomes(conn: Connection, limit: int = 200) -> int:
    """Süresi dolmuş ufuklar için sonuç yazar (worker döngüsü). Yazılan sonuç sayısını döner."""
    n = 0
    t = today()
    for dec in rows(conn, """
        SELECT id, decision, entity_type, entity_id, COALESCE(executed_at, created_at) AS ref
          FROM ai_decisions WHERE entity_type IN ('product', 'campaign') AND entity_id IS NOT NULL
           AND created_at > NOW() - INTERVAL '45 days' ORDER BY id LIMIT :l""", l=limit):
        ref = dec["ref"].date()
        done = {r[0] for r in conn.execute(text("SELECT horizon_days FROM ai_decision_outcomes WHERE decision_id = :d"),
                                           {"d": dec["id"]})}
        for h in HORIZONS:
            if h in done or ref + timedelta(days=h) > t:
                continue
            before = entity_metrics(conn, dec["entity_type"], dec["entity_id"], Window(h, end_date=ref - timedelta(days=1)))
            after = entity_metrics(conn, dec["entity_type"], dec["entity_id"],
                                   Window(h, start_date=ref, end_date=ref + timedelta(days=h - 1)))
            metrics = {"before": before, "after": after,
                       "revenue_change": _change(after.get("revenue"), before.get("revenue")),
                       "profit_change": _change(after.get("net_profit"), before.get("net_profit")),
                       "ad_spend_change": _change(after.get("ad_spend"), before.get("ad_spend")),
                       "conversion_change": _change(after.get("conversion_rate"), before.get("conversion_rate")),
                       "return_change": _change(after.get("returns"), before.get("returns")),
                       "stock_change": _change(after.get("stock"), before.get("stock"))}
            pc = metrics["profit_change"]
            no_activity = not (before.get("units") or after.get("units") or before.get("ad_spend") or after.get("ad_spend"))
            if pc is None or no_activity:
                result = "insufficient_data"
            else:
                tol = max(Decimal("50"), abs(d(before.get("net_profit"))) * Decimal("0.05"))
                result = "improved" if pc > tol else ("worsened" if pc < -tol else "neutral")
            conn.execute(text("""INSERT INTO ai_decision_outcomes(decision_id, horizon_days, metrics, final_result)
                                 VALUES (:d, :h, CAST(:m AS JSONB), :r) ON CONFLICT DO NOTHING"""),
                         {"d": dec["id"], "h": h, "m": _json(metrics), "r": result})
            n += 1
    return n


# ------------------------------------------------------------------ sahip zekâsı
def _best_outcome_sql() -> str:
    """Her karar için en uzun ölçülmüş ufkun sonucu."""
    return """(SELECT o.final_result FROM ai_decision_outcomes o WHERE o.decision_id = x.id
                ORDER BY o.horizon_days DESC LIMIT 1)"""


def track_record(conn: Connection, decision_type: str, actor: str = "owner") -> dict:
    """Bu tür onaylanmış (uygulanmış) kararların gerçek sonuçları."""
    r = row(conn, f"""
        SELECT COUNT(*) FILTER (WHERE res IN ('improved', 'worsened', 'neutral')) AS evaluated,
               COUNT(*) FILTER (WHERE res = 'improved') AS improved, COUNT(*) FILTER (WHERE res = 'worsened') AS worsened
          FROM (SELECT {_best_outcome_sql()} AS res FROM ai_decisions x
                 WHERE x.decision_type = :t AND x.actor = :a AND x.decision = 'approved' AND x.executed_at IS NOT NULL) y""",
            t=decision_type, a=actor)
    return {k: int(v or 0) for k, v in r.items()}


def owner_patterns(conn: Connection) -> dict:
    """OWNER_PREFERENCE (açık tercihler + kabul/ret davranışı) ve BUSINESS_EVIDENCE (sonuçlar) AYRI raporlanır."""
    prefs = rows(conn, "SELECT key, value, source, note, updated_at FROM ai_owner_preferences ORDER BY key")
    behaviour = rows(conn, f"""
        SELECT x.decision_type,
               COUNT(*) FILTER (WHERE x.decision = 'approved') AS approved,
               COUNT(*) FILTER (WHERE x.decision = 'rejected') AS rejected,
               COUNT(*) FILTER (WHERE x.decision = 'approved' AND {_best_outcome_sql()} = 'improved') AS improved,
               COUNT(*) FILTER (WHERE x.decision = 'approved' AND {_best_outcome_sql()} = 'worsened') AS worsened,
               COUNT(*) FILTER (WHERE x.decision = 'approved' AND {_best_outcome_sql()} IN ('improved','worsened','neutral')) AS evaluated
          FROM ai_decisions x WHERE x.actor = 'owner' GROUP BY x.decision_type ORDER BY 2 DESC""")
    findings = []
    for b in behaviour:
        total = b["approved"] + b["rejected"]
        b["acceptance_rate"] = finance_view.ratio(b["approved"], total) if total else None
        b["success_rate"] = finance_view.ratio(b["improved"], b["evaluated"]) if b["evaluated"] else None
        if b["evaluated"] >= 3 and b["worsened"] / b["evaluated"] >= 0.6:
            findings.append({"type": "repeated_mistake", "decision_type": b["decision_type"],
                             "message": f"'{b['decision_type']}' kararlarının {b['evaluated']} ölçülmüş sonucunun {b['worsened']} tanesinde net kâr düştü. Tercihin bu yönde olsa da kanıt aleyhine."})
        if total >= 5 and b["rejected"] / total >= 0.8:
            findings.append({"type": "preference", "decision_type": b["decision_type"],
                             "message": f"'{b['decision_type']}' önerilerinin %{b['rejected'] * 100 // total}'ini reddediyorsun; ajan bu tercihi not eder ama kanıt güçlüyse önermeye devam eder."})
    return {"preferences": prefs, "behaviour": behaviour, "findings": findings, "note": NOTE}


def quality_report(conn: Connection, days: int = 7) -> dict:
    """Haftalık 'Karar Kalitesi Raporu': sahip ve AI kararlarının ölçülmüş sonuçları."""
    items = rows(conn, f"""
        SELECT x.id, x.actor, x.agent_code, x.decision, x.decision_type, x.entity_type, x.entity_id, x.reason, x.created_at,
               x.executed_at, p.title, p.status AS proposal_status, {_best_outcome_sql()} AS result,
               (SELECT o.metrics->>'profit_change' FROM ai_decision_outcomes o WHERE o.decision_id = x.id
                 ORDER BY o.horizon_days DESC LIMIT 1) AS profit_change,
               (SELECT MAX(o.horizon_days) FROM ai_decision_outcomes o WHERE o.decision_id = x.id) AS horizon
          FROM ai_decisions x LEFT JOIN ai_proposals p ON p.id = x.proposal_id
         WHERE x.created_at > NOW() - make_interval(days => :days) OR EXISTS (
               SELECT 1 FROM ai_decision_outcomes o WHERE o.decision_id = x.id AND o.evaluated_at > NOW() - make_interval(days => :days))
         ORDER BY x.created_at DESC""", days=days)
    owner_ok = [i for i in items if i["decision"] == "approved" and i["result"] == "improved"]
    owner_bad = [i for i in items if i["decision"] == "approved" and i["result"] == "worsened"]
    # AI hataları: ajanın önerdiği, sahibin onaylayıp uyguladığı ve kâr düşüren kararlar — saklanmaz
    ai_bad = [i for i in owner_bad if i["agent_code"]]
    # Olası kaçırılan fırsatlar: reddedilen bütçe artırma önerisi sonrası kampanya kârlı kaldıysa
    missed = [i for i in items if i["decision"] == "rejected" and i["decision_type"] == "ads.increase_budget"
              and i["profit_change"] is not None and d(i["profit_change"]) >= 0]
    patterns = owner_patterns(conn)
    lessons = [f["message"] for f in patterns["findings"]]
    if not items:
        lessons.append("Bu dönemde kayıtlı karar yok. Önerileri onaylayıp/reddettikçe rapor anlamlı hale gelir.")
    pending = sum(1 for i in items if i["result"] is None)
    return {"days": days, "decisions": items, "owner_successes": owner_ok, "owner_failures": owner_bad,
            "ai_mistakes": ai_bad, "missed_opportunities": missed, "repeated_mistakes": [f for f in patterns["findings"] if f["type"] == "repeated_mistake"],
            "lessons": lessons, "awaiting_measurement": pending, "note": NOTE}


def scorecard(conn: Connection) -> dict:
    """AI öncesi / sonrası iş metrikleri. AI kendi kendini başarılı ilan edemez: yalnızca gerçek iş metrikleri."""
    from .data import period_summary
    first = conn.execute(text("SELECT MIN(started_at) FROM ai_agent_runs WHERE status IN ('ok', 'degraded')")).scalar()
    if first is None:
        return {"go_live": None, "message": "AI henüz çalışmadı; karşılaştırma yapılamaz."}
    go = first.date()
    t = today()
    after_days = (t - go).days
    out = {"go_live": go, "after_days": after_days, "note": ("Önce/sonra karşılaştırması nedensellik kanıtı değildir; "
                                                            "mevsim, kampanya ve fiyat değişiklikleri de etkiler.")}
    if after_days < 7:
        out["message"] = f"AI {after_days} gündür çalışıyor; anlamlı karşılaştırma için en az 7 gün gerekir."
    n = max(1, min(30, after_days))
    before_w = Window(n, end_date=go - timedelta(days=1))
    after_w = Window(n, end_date=t - timedelta(days=1)) if after_days >= 1 else Window(1, end_date=t)

    def metrics(w: Window) -> dict:
        s = period_summary(conn, w)
        days = w.days
        stockouts = conn.execute(text("""SELECT COUNT(*) FROM ai_inventory_snapshots WHERE snap_date BETWEEN :a AND :b
                                         AND available <= 0 AND units_7d > 0"""), {"a": w.start_date, "b": w.end_date}).scalar()
        snaps = conn.execute(text("SELECT COUNT(*) FROM ai_inventory_snapshots WHERE snap_date BETWEEN :a AND :b"),
                             {"a": w.start_date, "b": w.end_date}).scalar()
        return {"window": w.as_dict(), "orders": s["orders"],
                "net_profit_per_day": finance_view.q2(d(s["net_profit"]) / days), "net_margin": s["net_margin"],
                "ad_spend": s["ad_spend"],
                "profit_per_ad_lira": finance_view.ratio(s["net_profit"], s["ad_spend"]) if s["ad_spend"] else None,
                "refund_rate": finance_view.ratio(s["refund"], s["revenue"]) if s["revenue"] else None,
                "stockout_product_days": int(stockouts) if snaps else None}
    out["before"], out["after"] = metrics(before_w), metrics(after_w)
    r = row(conn, f"""SELECT COUNT(*) FILTER (WHERE res = 'improved') AS improved, COUNT(*) FILTER (WHERE res = 'worsened') AS worsened,
                             COUNT(*) FILTER (WHERE res = 'neutral') AS neutral
                        FROM (SELECT {_best_outcome_sql()} AS res FROM ai_decisions x WHERE x.decision = 'approved'
                                 AND x.executed_at IS NOT NULL) y""")
    evaluated = (r["improved"] or 0) + (r["worsened"] or 0) + (r["neutral"] or 0)
    out["decision_success_rate"] = finance_view.ratio(r["improved"], evaluated) if evaluated else None
    out["decisions_evaluated"] = evaluated
    if out["before"]["orders"] == 0:
        out["message"] = out.get("message") or "AI öncesi döneme ait sipariş verisi yok; karşılaştırma sınırlı."
    return out
