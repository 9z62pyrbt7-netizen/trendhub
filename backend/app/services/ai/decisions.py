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
from .config import Window, d, tl, today

HORIZONS = (1, 3, 7, 30)
NOTE = ("Sonuç = karardan sonraki N gün ile önceki N günün farkı. Bu bir korelasyondur; mevsimsellik ve başka "
        "değişiklikler de etkiler. Tek bir sonuçtan değil, tekrarlanan örüntülerden ders çıkarın.")


def _json(v) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def record(conn: Connection, *, proposal: dict | None, actor: str, decision: str, user_id: int | None = None,
           snapshot: dict | None = None, executed: bool = False, reason: str | None = None,
           decision_type: str | None = None, entity_type: str | None = None, entity_id: int | None = None,
           situation: str | None = None, ceo_stance: str | None = None, override: bool = False,
           override_reason: str | None = None) -> int:
    p = proposal or {}
    return conn.execute(text("""
        INSERT INTO ai_decisions(proposal_id, actor, agent_code, user_id, decision, decision_type, entity_type, entity_id,
                                 context_snapshot, reason, expected_result, risk_level, confidence, requires_approval,
                                 approved_by, executed_at, situation, ceo_stance, override, override_reason)
        VALUES (:pid, :actor, :agent, :uid, :dec, :dtype, :et, :eid, CAST(:snap AS JSONB), :reason, CAST(:exp AS JSONB),
                :risk, :conf, :req, :appr, CASE WHEN :executed THEN NOW() END, :sit, :stance, :ovr, :ovr_reason)
        RETURNING id"""), {"sit": situation or p.get("situation"), "stance": ceo_stance or p.get("ceo_stance"),
                          "ovr": override, "ovr_reason": override_reason,
        "pid": p.get("id"), "actor": actor, "agent": p.get("agent_code"), "uid": user_id, "dec": decision,
        "dtype": decision_type or p.get("action_type"), "et": entity_type or p.get("entity_type"),
        "eid": entity_id if entity_id is not None else p.get("entity_id"), "snap": _json(snapshot or {}),
        "reason": reason or p.get("reason"), "exp": _json(p.get("expected_result") or {}), "risk": p.get("risk_level"),
        "conf": p.get("confidence"), "req": p.get("requires_approval", True),
        "appr": user_id if decision in ("approved", "owner_action") else None, "executed": executed}).scalar()


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
        SELECT id, decision, entity_type, entity_id, proposal_id, COALESCE(executed_at, created_at) AS ref
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
            from .proposals import activity
            res_tr = {"improved": "iyileşti", "worsened": "kötüleşti", "neutral": "nötr", "insufficient_data": "veri yetersiz"}[result]
            activity(conn, f"Sonuç ölçümü ({h} gün): {res_tr}; net kâr değişimi {tl(pc) if pc is not None else '—'}",
                     agent="ceo", kind="outcome", level="warning" if result == "worsened" else "info",
                     proposal_id=dec["proposal_id"])
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
               COUNT(*) FILTER (WHERE x.decision = 'owner_action') AS initiated,
               COUNT(*) FILTER (WHERE x.decision = 'approved') AS approved,
               COUNT(*) FILTER (WHERE x.decision = 'rejected') AS rejected,
               COUNT(*) FILTER (WHERE x.decision IN ('approved', 'owner_action') AND {_best_outcome_sql()} = 'improved') AS improved,
               COUNT(*) FILTER (WHERE x.decision IN ('approved', 'owner_action') AND {_best_outcome_sql()} = 'worsened') AS worsened,
               COUNT(*) FILTER (WHERE x.decision IN ('approved', 'owner_action') AND {_best_outcome_sql()} IN ('improved','worsened','neutral')) AS evaluated
          FROM ai_decisions x WHERE x.actor = 'owner' GROUP BY x.decision_type ORDER BY 2 DESC""")
    findings = []
    for b in behaviour:
        total = b["approved"] + b["rejected"] + b["initiated"]
        b["acceptance_rate"] = finance_view.ratio(b["approved"], total) if total else None
        b["success_rate"] = finance_view.ratio(b["improved"], b["evaluated"]) if b["evaluated"] else None
        if b["evaluated"] >= 3 and b["worsened"] / b["evaluated"] >= 0.6:
            findings.append({"type": "repeated_mistake", "decision_type": b["decision_type"],
                             "message": f"'{b['decision_type']}' kararlarının {b['evaluated']} ölçülmüş sonucunun {b['worsened']} tanesinde net kâr düştü. Tercihin bu yönde olsa da kanıt aleyhine."})
        if total >= 5 and b["rejected"] / total >= 0.8:  # noqa: SIM102
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
    owner_ok = [i for i in items if i["decision"] in ("approved", "owner_action") and i["result"] == "improved"]
    owner_bad = [i for i in items if i["decision"] in ("approved", "owner_action") and i["result"] == "worsened"]
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
    out["before"] = metrics(before_w)
    # Yetersiz süre: yarım günü tam günle kıyaslamak yanıltıcı rakam üretir; karşılaştırma gösterilmez.
    out["after"] = metrics(after_w) if after_days >= 7 else None
    r = row(conn, f"""SELECT COUNT(*) FILTER (WHERE res = 'improved') AS improved, COUNT(*) FILTER (WHERE res = 'worsened') AS worsened,
                             COUNT(*) FILTER (WHERE res = 'neutral') AS neutral
                        FROM (SELECT {_best_outcome_sql()} AS res FROM ai_decisions x WHERE x.decision IN ('approved', 'owner_action')
                                 AND x.executed_at IS NOT NULL) y""")
    evaluated = (r["improved"] or 0) + (r["worsened"] or 0) + (r["neutral"] or 0)
    out["decision_success_rate"] = finance_view.ratio(r["improved"], evaluated) if evaluated else None
    out["decisions_evaluated"] = evaluated
    if out["before"]["orders"] == 0:
        out["message"] = out.get("message") or "AI öncesi döneme ait sipariş verisi yok; karşılaştırma sınırlı."
    return out


# ------------------------------------------------------------------ öğrenme: tercih ≠ kanıt
def situation_for(conn: Connection, decision_type: str | None, entity_type: str | None, entity_id: int | None) -> str | None:
    """'Benzer durum' anahtarı: kararın verildiği andaki koşul (ör. reklamın erken/olgun aşamada olması).
    Öğrenme yalnızca benzer durumlardaki geçmiş sonuçlara bakar."""
    from .config import thresholds
    th = thresholds(conn)
    if entity_type == "campaign" and entity_id:
        r = row(conn, """SELECT COUNT(DISTINCT spend_date) AS days,
                                (SELECT COALESCE(SUM(clicks), 0) FROM ad_performance WHERE campaign_id = :c
                                  AND perf_date > CURRENT_DATE - :w) AS clicks
                           FROM ad_spend WHERE campaign_id = :c AND spend_date > CURRENT_DATE - :w""",
                c=entity_id, w=int(th["ads_window_days"]))
        early = (r["days"] or 0) < th["ads_window_days"] or (r["clicks"] or 0) < th["ads_min_clicks"] * 2
        return "ads:early" if early else "ads:mature"
    if entity_type == "product" and entity_id:
        from .agents import classify
        from .data import product_economics
        e = next(iter(product_economics(conn, Window(th["analysis_days"]), [entity_id])), None)
        return f"product:{classify(e, th)[0].lower()}" if e else "product:no_data"
    return None


# Sahibin açık tercihlerinin hangi aksiyonları desteklediği
PREFERENCE_FAVORS = {
    ("ad_aggressiveness", "conservative"): {"ads.pause", "ads.decrease_budget"},
    ("ad_aggressiveness", "aggressive"): {"ads.increase_budget"},
    ("stock_aggressiveness", "aggressive"): {"inventory.restock"},
}
SITUATION_TR = {"ads:early": "reklam erken aşamadayken (az veri)", "ads:mature": "reklam yeterli veriyle",
                "product:loss": "ürün zarar ederken", "product:star": "ürün yıldızken"}


def _evidence(conn: Connection, decision_type: str, situation: str | None, *, origin: str) -> dict:
    """Uygulanmış kararların ölçülmüş sonuçları. origin: 'owner' (sahibin kendi kararı) | 'ai' (ajan önerisi) | 'all'."""
    cond = {"owner": "x.proposal_id IS NULL AND x.actor = 'owner'", "ai": "x.proposal_id IS NOT NULL AND x.agent_code IS NOT NULL",
            "all": "TRUE"}[origin]
    sit = "AND x.situation = :sit" if situation else ""
    r = row(conn, f"""
        SELECT COUNT(*) FILTER (WHERE res IN ('improved', 'worsened', 'neutral')) AS evaluated,
               COUNT(*) FILTER (WHERE res = 'improved') AS improved, COUNT(*) FILTER (WHERE res = 'worsened') AS worsened,
               COALESCE(SUM(pc) FILTER (WHERE res IN ('improved', 'worsened', 'neutral')), 0) AS profit_effect
          FROM (SELECT {_best_outcome_sql()} AS res,
                       (SELECT (o.metrics->>'profit_change')::numeric FROM ai_decision_outcomes o WHERE o.decision_id = x.id
                         ORDER BY o.horizon_days DESC LIMIT 1) AS pc
                  FROM ai_decisions x
                 WHERE x.decision_type = :t AND x.decision IN ('approved', 'owner_action') AND x.executed_at IS NOT NULL
                   AND {cond} {sit}) y""", t=decision_type, sit=situation)
    return {"evaluated": int(r["evaluated"] or 0), "improved": int(r["improved"] or 0), "worsened": int(r["worsened"] or 0),
            "profit_effect": finance_view.q2(d(r["profit_effect"]))}


def owner_preference(conn: Connection, decision_type: str, situation: str | None) -> dict:
    """OWNER_PREFERENCE: sahibin ne yapmayı TERCİH ETTİĞİ (sonuçtan bağımsız)."""
    sit = "AND situation = :sit" if situation else ""
    r = row(conn, f"""SELECT COUNT(*) FILTER (WHERE proposal_id IS NULL AND decision = 'owner_action') AS initiated,
                             COUNT(*) FILTER (WHERE proposal_id IS NOT NULL AND decision = 'approved') AS approved,
                             COUNT(*) FILTER (WHERE proposal_id IS NOT NULL AND decision = 'rejected') AS rejected
                        FROM ai_decisions WHERE actor = 'owner' AND decision_type = :t {sit}""", t=decision_type, sit=situation)
    initiated, approved, rejected = (int(r[k] or 0) for k in ("initiated", "approved", "rejected"))
    explicit = [f"{k}={json.loads(json.dumps(v))}" for (k, v), acts in PREFERENCE_FAVORS.items() if decision_type in acts
                and conn.execute(text("SELECT 1 FROM ai_owner_preferences WHERE key = :k AND value = CAST(:v AS JSONB)"),
                                 {"k": k, "v": json.dumps(v)}).first()]
    votes = approved + rejected
    prefers = initiated >= 3 or (votes >= 3 and approved / votes >= 0.7) or bool(explicit)
    dislikes = votes >= 3 and rejected / votes >= 0.7 and initiated == 0 and not explicit
    return {"initiated": initiated, "approved": approved, "rejected": rejected, "explicit": explicit,
            "prefers": prefers, "dislikes": dislikes}


def assess(conn: Connection, decision_type: str, situation: str | None) -> dict:
    """CEO değerlendirmesi: tercih ile kanıt AYRI hesaplanır; duruş (stance) kanıta göre belirlenir.
    Benzer durumda en az 3 ölçülmüş sonuç yoksa aynı karar türünün tüm sonuçlarına bakılır (kapsam not edilir)."""
    pref = owner_preference(conn, decision_type, situation)
    scope = situation
    ev = _evidence(conn, decision_type, situation, origin="all")
    if ev["evaluated"] < 3 and situation:
        scope, ev = None, _evidence(conn, decision_type, None, origin="all")
    own = _evidence(conn, decision_type, scope, origin="owner")
    ai = _evidence(conn, decision_type, scope, origin="ai")
    where = SITUATION_TR.get(scope or "", "benzer") if scope else "bu türdeki"
    notes, stance, conf_adj = [], "neutral", 0.0
    # Duruş hem sıklığa hem toplam etkiye bakar: çoğu kötü ama toplamda kârlıysa (birkaç büyük kazanç) itiraz edilmez.
    if ev["evaluated"] >= 3 and ev["worsened"] / ev["evaluated"] >= 0.6 and ev["profit_effect"] < 0:
        stance, conf_adj = "oppose", -0.3
        base = (f"geçmiş sonuçlar bunun net kârı düşürdüğünü gösteriyor: {where} {ev['evaluated']} uygulamanın "
                f"{ev['worsened']} tanesinde net kâr düştü (toplam net kâr etkisi {tl(ev['profit_effect'])}).")
        if pref["prefers"]:
            why = []
            if pref["initiated"]:
                why.append(f"{pref['initiated']} kez kendin uyguladın")
            if pref["approved"]:
                why.append(f"bu tür önerilerin {pref['approved']}/{pref['approved'] + pref['rejected']}'ini onayladın")
            if pref["explicit"]:
                why.append("tercihlerinde böyle belirttin")
            notes.append(f"Normalde bunu tercih ettiğini biliyorum ({', '.join(why)}) fakat {base} Bu sefer aynı kararı önermiyorum.")
        else:
            notes.append(f"Bu karara karşıyım: {base}")
    elif ev["evaluated"] >= 3 and ev["improved"] / ev["evaluated"] >= 0.6 and ev["profit_effect"] > 0:
        stance, conf_adj = "support", 0.05
        if pref["dislikes"]:
            notes.append(f"Bu tür önerileri genelde reddediyorsun ({pref['rejected']} ret) ama uygulandığında {where} "
                         f"{ev['evaluated']} durumun {ev['improved']} tanesinde net kâr arttı. Kanıt bu kararı destekliyor.")
        else:
            notes.append(f"Geçmiş kanıt destekliyor: {where} {ev['evaluated']} uygulamanın {ev['improved']} tanesinde net kâr arttı.")
    elif ev["evaluated"]:
        notes.append(f"{where} {ev['evaluated']} ölçülmüş sonuç var ({ev['improved']} iyileşme, {ev['worsened']} kötüleşme); henüz net bir örüntü yok.")
    if ai["evaluated"] >= 3:
        acc = ai["improved"] / ai["evaluated"]
        if ai["worsened"] / ai["evaluated"] >= 0.6:
            conf_adj -= 0.2
            notes.append(f"Kendi hatam: benim bu tür önerilerim {ai['evaluated']} kez uygulandı, {ai['worsened']} tanesinde net kâr düştü "
                         f"(isabet %{acc * 100:.0f}). Bu öneriye güvenimi düşürüyorum.")
        else:
            notes.append(f"Bu tür önerilerimin isabeti %{acc * 100:.0f} ({ai['evaluated']} ölçülmüş sonuç).")
    return {"stance": stance, "note": " ".join(notes) or None, "confidence_adjustment": conf_adj, "situation": situation,
            "evidence_scope": scope, "owner_preference": pref, "business_evidence": ev, "owner_evidence": own, "ai_evidence": ai}


def ai_accuracy(conn: Connection) -> list[dict]:
    """Ajan önerilerinin isabeti (önerildi → uygulandı → ölçüldü). CEO kendini de ölçer."""
    return rows(conn, f"""
        SELECT x.agent_code, x.decision_type, COUNT(*) FILTER (WHERE res IN ('improved','worsened','neutral')) AS evaluated,
               COUNT(*) FILTER (WHERE res = 'improved') AS improved, COUNT(*) FILTER (WHERE res = 'worsened') AS worsened,
               COUNT(*) FILTER (WHERE x.decision = 'rejected') AS rejected_by_owner
          FROM (SELECT x.*, {_best_outcome_sql()} AS res FROM ai_decisions x WHERE x.agent_code IS NOT NULL) x
         GROUP BY 1, 2 ORDER BY 1, 2""")


class DecisionError(ValueError):
    def __init__(self, message: str, status: int = 422, assessment: dict | None = None):
        super().__init__(message)
        self.status = status
        self.assessment = assessment


def record_owner_action(conn: Connection, user, *, decision_type: str, entity_type: str, entity_id: int,
                        note: str | None, override_reason: str | None, ip: str | None) -> dict:
    """Sahibin kendi verdiği (platformda uyguladığı) kararı günlüğe yazar. CEO önce değerlendirir; CEO karşıysa
    sahip ancak gerekçe yazarak kaydedebilir (override). Bu bir platform aksiyonu değildir, kayıttır."""
    from ..audit import log_audit
    from .proposals import ACTION_TR, activity
    if decision_type not in ACTION_TR:
        raise DecisionError("Bilinmeyen karar türü")
    table = {"campaign": "ad_campaigns", "product": "products"}.get(entity_type)
    if table is None or not conn.execute(text(f"SELECT 1 FROM {table} WHERE id = :i"), {"i": entity_id}).first():
        raise DecisionError("Karar verilen kayıt bulunamadı", 404)
    situation = situation_for(conn, decision_type, entity_type, entity_id)
    a = assess(conn, decision_type, situation)
    override = a["stance"] == "oppose"
    if override and len((override_reason or "").strip()) < 10:
        raise DecisionError("CEO bu karara karşı: " + (a["note"] or "") + " Yine de kaydetmek için gerekçe yaz.", 409, a)
    w = Window(7)
    did = record(conn, proposal=None, actor="owner", decision="owner_action", user_id=user.id, executed=True,
                 snapshot={"window": w.as_dict(), "metrics": entity_metrics(conn, entity_type, entity_id, w), "ceo": a},
                 reason=note, decision_type=decision_type, entity_type=entity_type, entity_id=entity_id,
                 situation=situation, ceo_stance=a["stance"], override=override, override_reason=override_reason if override else None)
    activity(conn, f"Sahip kararı kaydedildi: {ACTION_TR[decision_type]} ({entity_type} #{entity_id})"
             + (f" — CEO itirazına rağmen: {override_reason}" if override else ""), agent="ceo", kind="owner_decision",
             level="warning" if override else "info", user_id=user.id)
    log_audit(conn, actor=user.username, user_id=user.id, action="ai.owner_decision" + ("_override" if override else ""),
              entity_type="ai_decision", entity_id=did, ip=ip,
              details={"decision_type": decision_type, "entity": f"{entity_type}:{entity_id}", "ceo_stance": a["stance"]})
    return {"decision_id": did, "assessment": a, "override": override}
