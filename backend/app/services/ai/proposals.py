"""Öneri yaşam döngüsü: oluşturma → risk motoru → (CEO notu) → onay/ret → uygulama → sonuç ölçümü.

Kurallar:
  * Ajanlar yalnızca kendi izinli aksiyon türlerini önerebilir (yetkisiz aksiyon BLOKE edilir).
  * Risk motoru her öneriyi veritabanından YENİDEN hesaplayarak doğrular; ajanın yazdığı rakamlara güvenmez.
  * Bloke edilen öneri kimse tarafından (CEO, yönetici) onaylanamaz; veri değişince yeni öneri oluşur.
  * Acil durdurma aktifken onay ve uygulama yapılamaz.
  * V1'de platforma yazan executor YOKTUR: onaylanan öneri "manuel uygulanacak" olur; sahip uygulayınca işaretler.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ...db import row, rows
from ..audit import log_audit
from . import config
from .config import Window, d, thresholds, tl

RISK_ORDER = ["low", "medium", "high", "critical"]
RISK_TR = {"low": "Düşük", "medium": "Orta", "high": "Yüksek", "critical": "Kritik"}
STATUS_TR = {"pending_approval": "Onay bekliyor", "blocked": "Risk motoru bloke etti", "approved": "Onaylandı — uygulanacak",
             "rejected": "Reddedildi", "executed": "Uygulandı", "failed": "Uygulama başarısız", "expired": "Süresi doldu",
             "superseded": "Yenisiyle değişti", "info": "Bilgi"}

# Ajan → izinli aksiyonlar (en az yetki). Listede olmayan aksiyon risk motorunda BLOKE edilir.
ALLOWED_ACTIONS = {
    "advertising": {"ads.increase_budget", "ads.decrease_budget", "ads.pause"},
    "product_profit": {"product.review_loss", "product.fix_missing_cost"},
    "inventory": {"inventory.restock", "inventory.supplier_stock_risk", "inventory.dead_stock"},
}
ACTION_TR = {
    "ads.increase_budget": "Reklam bütçesini artır", "ads.decrease_budget": "Reklam bütçesini azalt",
    "ads.pause": "Reklamı durdur", "product.review_loss": "Zarar eden ürünü gözden geçir",
    "product.fix_missing_cost": "Ürün maliyetini gir", "inventory.restock": "Stok al",
    "inventory.supplier_stock_risk": "Tedarikçi stoğu tükeniyor", "inventory.dead_stock": "Satmayan stok",
}


def _json(v) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def activity(conn: Connection, message: str, *, agent: str | None = None, kind: str = "info", level: str = "info",
             proposal_id: int | None = None, run_id: int | None = None, user_id: int | None = None) -> None:
    conn.execute(text("""INSERT INTO ai_activity(agent_code, kind, level, message, proposal_id, run_id, user_id)
                         VALUES (:a, :k, :l, :m, :p, :r, :u)"""),
                 {"a": agent, "k": kind, "l": level, "m": message[:1000], "p": proposal_id, "r": run_id, "u": user_id})


# ------------------------------------------------------------------ risk motoru
def _check(code: str, severity: str, message: str, **details) -> dict:
    return {"code": code, "severity": severity, "message": message, "details": details}


def evaluate(conn: Connection, p: dict, *, at_approval: bool = False) -> tuple[list[dict], str]:
    """Öneri için risk kontrolleri. (kontroller, risk seviyesi). severity='block' olan varsa öneri onaylanamaz."""
    th = thresholds(conn)
    checks: list[dict] = []
    level = p.get("risk_level") or "medium"
    action = p["action_type"]
    if action not in ALLOWED_ACTIONS.get(p["agent_code"], set()):
        checks.append(_check("unauthorized_action", "block", f"{p['agent_code']} ajanının '{action}' önermeye yetkisi yok."))
        return checks, "critical"
    if at_approval and config.emergency_stop(conn):
        checks.append(_check("emergency_stop", "block", "Acil durdurma aktif: hiçbir yazma işlemi onaylanamaz."))

    if action.startswith("ads."):
        from .data import campaign_performance, inventory_status
        camp = next((c for c in campaign_performance(conn, Window(th["ads_window_days"])) if c["id"] == p["entity_id"]), None)
        if camp is None:
            checks.append(_check("entity_missing", "block", "Kampanya bulunamadı (öneri geçersiz)."))
            return checks, "critical"
        ev = p.get("evidence") or {}
        # Halüsinasyon / tutarsızlık koruması: kanıttaki net kâr işareti güncel hesapla aynı olmalı
        if camp["ad_net_profit"] is None:
            checks.append(_check("insufficient_data", "block", "Güncel veride kampanyanın net kâr etkisi hesaplanamıyor."))
        elif ev.get("ad_net_profit") is not None and (d(ev["ad_net_profit"]) >= 0) != (camp["ad_net_profit"] >= 0):
            checks.append(_check("data_mismatch", "block", "Önerideki kâr verisi güncel hesapla çelişiyor; öneri yenilenmeli.",
                                 proposal=ev.get("ad_net_profit"), current=camp["ad_net_profit"]))
        last = camp["last_perf_date"]
        stale = last is None or last < config.today() - timedelta(days=int(th["ads_data_stale_days"]))
        if stale:
            sev = "block" if action == "ads.increase_budget" else "warning"
            checks.append(_check("stale_data", sev, f"Reklam performans verisi bayat (son: {last or 'yok'})."))
        params = p.get("params") or {}
        if action in ("ads.increase_budget", "ads.decrease_budget"):
            cur, new = params.get("current_daily_budget"), params.get("new_daily_budget")
            if cur in (None, 0) or new is None:
                checks.append(_check("no_budget", "block", "Kampanyanın günlük bütçesi tanımlı değil."))
            else:
                step = abs(d(new) - d(cur)) / d(cur)
                if step > d(th["ads_max_budget_step"]) + Decimal("0.0001"):
                    checks.append(_check("budget_step", "block",
                                         f"Bütçe değişimi %{step * 100:.0f}; tek adımda en fazla %{th['ads_max_budget_step'] * 100:.0f}."))
                if d(new) < 0:
                    checks.append(_check("negative_budget", "block", "Negatif bütçe."))
        if action == "ads.increase_budget":
            if camp["ad_net_profit"] is not None and camp["ad_net_profit"] < 0:
                checks.append(_check("negative_margin", "block", "Reklam net zarar ederken bütçe artırılamaz."))
            nm = camp["net_margin_after_ads"]
            if nm is not None and nm < d(th["ads_target_margin_after_ads"]):
                checks.append(_check("low_margin", "block",
                                     f"Reklam sonrası net marj %{nm * 100:.1f}; hedef en az %{th['ads_target_margin_after_ads'] * 100:.0f}."))
            risky = [i for i in inventory_status(conn, camp["product_ids"])
                     if i["stockout_risk"] or i["out_of_stock"]] if camp["product_ids"] else []
            if risky:
                checks.append(_check("stock_risk", "block", "DO_NOT_SCALE_ADS: kampanya ürünlerinde stok yok veya tükenmek üzere.",
                                     products=[r["sku"] or r["product_id"] for r in risky]))
            spike = row(conn, """SELECT (SELECT COALESCE(MAX(amount), 0) FROM ad_spend WHERE campaign_id = :c
                                          AND spend_date >= CURRENT_DATE - 1) AS recent,
                                        (SELECT COALESCE(SUM(amount), 0) / 7 FROM ad_spend WHERE campaign_id = :c
                                          AND spend_date BETWEEN CURRENT_DATE - 8 AND CURRENT_DATE - 2) AS avg""", c=camp["id"])
            if d(spike["avg"]) > 0 and d(spike["recent"]) > d(spike["avg"]) * 2 and d(spike["recent"]) >= 100:
                checks.append(_check("spend_anomaly", "block",
                                     f"Anormal reklam harcaması: son gün {tl(spike['recent'])}, önceki ortalama {tl(spike['avg'])}. "
                                     "Neden anlaşılmadan bütçe artırılmaz.", recent=spike["recent"], avg=spike["avg"]))
            if camp["missing_cost"]:
                checks.append(_check("missing_cost", "block", "Kampanya ürünlerinde maliyet eksik; kâr olduğundan yüksek görünür."))
            level = "high" if d(params.get("delta_per_day")) >= 200 else "medium"
        elif action == "ads.pause":
            level = "medium"
        else:
            level = "low"
    elif action.startswith("product.") or action.startswith("inventory."):
        exists = conn.execute(text("SELECT 1 FROM products WHERE id = :i"), {"i": p["entity_id"]}).first()
        if not exists:
            checks.append(_check("entity_missing", "block", "Ürün bulunamadı (öneri geçersiz)."))
            return checks, "critical"
        level = "medium" if action == "inventory.restock" else "low"

    if action in ("ads.increase_budget", "inventory.restock"):
        from .data import data_quality
        stale_orders = [q for q in data_quality(conn) if q["code"] == "orders_stale"]
        if stale_orders:
            checks.append(_check("stale_orders", "block", stale_orders[0]["message"] + " Para harcayan öneri bayat veriyle onaylanmaz."))
    cap = d(p.get("required_capital"))
    if action in ("ads.increase_budget", "inventory.restock") or cap > 0:
        from ..platform.sources import finance_status
        fs = finance_status(conn)
        # Trendyol finans verisi bir kez alınmışsa güncelliği zorunludur; bayatsa para harcayan öneri onaylanmaz.
        if fs["connected"] and fs["freshness"] in ("STALE", "ERROR"):
            checks.append(_check("finance_stale", "block",
                                 f"Finans verisi güncel olmadığı için ek reklam bütçesi / harcama önermiyorum "
                                 f"(son başarılı Trendyol finans senkronu {fs['age_hours']} saat önce; sınır {fs['stale_hours']} saat).",
                                 age_hours=fs["age_hours"]))
        elif fs["connected"] and fs["freshness"] == "DEGRADED":
            checks.append(_check("finance_degraded", "warning",
                                 "Son Trendyol finans senkronu hata verdi; nakit/hakediş bir önceki başarılı veriye dayanıyor."))
    if cap > 0:
        from .capital import position
        pos = position(conn, exclude_proposal=p.get("id"))
        if pos["usable"] is None:
            checks.append(_check("no_cash_data", "block", "Kasa bilgisi girilmemiş: kasada olduğu doğrulanmayan para harcanamaz."))
        elif cap > pos["unused"]:
            checks.append(_check("capital_exceeded", "block",
                                 f"Gereken sermaye {tl(cap)}, kullanılabilir ve ayrılmamış sermaye {tl(pos['unused'])}."))
        elif pos["usable"] and cap > pos["usable"] * Decimal("0.5"):
            level = "high"
    if any(c["severity"] == "block" for c in checks):
        level = max(level, "high", key=RISK_ORDER.index)
    return checks, level


# ------------------------------------------------------------------ oluşturma
def propose(conn: Connection, *, agent: str, action_type: str, entity_type: str, entity_id: int, title: str, reason: str,
            evidence: dict, params: dict | None = None, expected_result: dict | None = None, confidence: float | None = None,
            required_capital: Decimal | int = 0, capital_category: str | None = None, requires_approval: bool = True,
            channel: str | None = None, run_id: int | None = None) -> int | None:
    """Öneriyi kaydeder (tekilleştirilmiş). Açık aynı öneri varsa kanıtı tazelenir. Öneri id'si döner."""
    dedupe = f"{agent}:{entity_type}:{entity_id}:{action_type}"
    p = {"agent_code": agent, "action_type": action_type, "entity_type": entity_type, "entity_id": entity_id,
         "evidence": evidence, "params": params or {}, "required_capital": required_capital}
    existing = row(conn, "SELECT id, status FROM ai_proposals WHERE dedupe_key = :k AND status IN ('pending_approval', 'blocked', 'approved')",
                   k=dedupe)
    if existing and existing["status"] == "approved":
        return existing["id"]  # onaylanmış, uygulanmayı bekliyor: aynısını tekrar önerme
    p["id"] = existing["id"] if existing else None
    checks, level = evaluate(conn, p)
    blocked = any(c["severity"] == "block" for c in checks)
    status = "blocked" if blocked else "pending_approval"
    from .decisions import assess, situation_for
    situation = situation_for(conn, action_type, entity_type, entity_id)
    ceo = assess(conn, action_type, situation)
    note = ceo["note"]
    conf = None if confidence is None else round(max(0.0, min(1.0, confidence + ceo["confidence_adjustment"])), 3)
    values = {"agent": agent, "run": run_id, "ch": channel, "act": action_type, "et": entity_type, "eid": entity_id,
              "title": title[:300], "reason": reason[:2000], "ev": _json(evidence), "params": _json(params or {}),
              "exp": _json(expected_result or {}), "cap": d(required_capital), "capcat": capital_category,
              "risk": level, "conf": conf, "req": requires_approval, "status": status, "checks": _json(checks),
              "note": note, "k": dedupe, "sit": situation, "stance": ceo["stance"], "assess": _json(ceo)}
    # Aynı varlık için ajanın açık başka aksiyonu varsa (ör. önce artır, şimdi durdur) eskisi geçersiz olur
    superseded = [r["id"] for r in rows(conn, """
        UPDATE ai_proposals SET status = 'superseded', updated_at = NOW()
         WHERE agent_code = :a AND entity_type = :et AND entity_id = :eid AND dedupe_key <> :k
           AND status IN ('pending_approval', 'blocked') RETURNING id""", a=agent, et=entity_type, eid=entity_id, k=dedupe)]
    if existing:
        pid = existing["id"]
        conn.execute(text("""UPDATE ai_proposals SET run_id = :run, title = :title, reason = :reason, evidence = CAST(:ev AS JSONB),
                             params = CAST(:params AS JSONB), expected_result = CAST(:exp AS JSONB), required_capital = :cap,
                             risk_level = :risk, confidence = :conf, status = :status, risk_checks = CAST(:checks AS JSONB),
                             ceo_note = :note, situation = :sit, ceo_stance = :stance, ceo_assessment = CAST(:assess AS JSONB),
                             updated_at = NOW() WHERE id = :id"""), {**values, "id": pid})
    else:
        pid = conn.execute(text("""
            INSERT INTO ai_proposals(agent_code, run_id, channel, action_type, entity_type, entity_id, title, reason, evidence,
                                     params, expected_result, required_capital, capital_category, risk_level, confidence,
                                     requires_approval, status, risk_checks, ceo_note, dedupe_key, expires_at, situation,
                                     ceo_stance, ceo_assessment)
            VALUES (:agent, :run, :ch, :act, :et, :eid, :title, :reason, CAST(:ev AS JSONB), CAST(:params AS JSONB),
                    CAST(:exp AS JSONB), :cap, :capcat, :risk, :conf, :req, :status, CAST(:checks AS JSONB), :note, :k,
                    NOW() + INTERVAL '7 days', :sit, :stance, CAST(:assess AS JSONB)) RETURNING id"""), values).scalar()
        activity(conn, f"{title} önerildi", agent=agent, kind="proposal", proposal_id=pid, run_id=run_id)
        pv = _profit_validation(conn, p)
        if pv:
            activity(conn, pv, agent="product_profit", kind="validation", proposal_id=pid, run_id=run_id)
        stance_tr = {"oppose": "KARŞI", "support": "destekliyor", "neutral": "nötr"}[ceo["stance"]]
        activity(conn, f"CEO incelemesi ({stance_tr}): " + (note or "geçmişte benzer karar yok; kanıt yok."), agent="ceo",
                 kind="ceo_review", level="warning" if ceo["stance"] == "oppose" else "info", proposal_id=pid, run_id=run_id)
        for c in checks:
            conn.execute(text("""INSERT INTO ai_risk_events(proposal_id, code, severity, message, details)
                                 VALUES (:p, :c, :s, :m, CAST(:d AS JSONB))"""),
                         {"p": pid, "c": c["code"], "s": c["severity"], "m": c["message"], "d": _json(c["details"])})
        if blocked:
            activity(conn, "Risk motoru bloke etti: " + "; ".join(c["message"] for c in checks if c["severity"] == "block"),
                     agent="risk", kind="risk", level="warning", proposal_id=pid, run_id=run_id)
        else:
            activity(conn, f"Risk motoru onayladı ({RISK_TR[level]} risk); sahibin onayı bekleniyor", agent="risk",
                     kind="risk", level="success", proposal_id=pid, run_id=run_id)
    for sid in superseded:
        activity(conn, "Önceki öneri yenisiyle değişti", agent=agent, kind="proposal", proposal_id=sid, run_id=run_id)
    return pid


def _profit_validation(conn: Connection, p: dict) -> str | None:
    """Önerinin dayandığı kâr rakamını tek kaynaktan (finance_view → ai.data) yeniden hesaplayıp aktiviteye yazar."""
    from .data import campaign_performance, product_economics
    th = thresholds(conn)
    if p["entity_type"] == "campaign":
        c = next((c for c in campaign_performance(conn, Window(th["ads_window_days"])) if c["id"] == p["entity_id"]), None)
        if c is None or c["ad_net_profit"] is None:
            return "Kâr doğrulaması: güncel veriyle reklamın net kâr etkisi hesaplanamadı."
        return (f"Kâr doğrulaması (finance_view): ürün marjı %{(c['product_margin_before_ads'] or 0) * 100:.1f}, "
                f"reklam sonrası net {tl(c['ad_net_profit'])}.")
    if p["entity_type"] == "product":
        e = next(iter(product_economics(conn, Window(th["analysis_days"]), [p["entity_id"]])), None)
        return f"Kâr doğrulaması (finance_view): {th['analysis_days']} gün net kâr {tl(e['net_profit'])}." if e else None
    return None


# ------------------------------------------------------------------ onay / ret / uygulama
class ProposalError(ValueError):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def _get(conn: Connection, pid: int) -> dict:
    p = row(conn, "SELECT * FROM ai_proposals WHERE id = :id FOR UPDATE", id=pid)
    if p is None:
        raise ProposalError("Öneri bulunamadı", 404)
    return p


def _snapshot(conn: Connection, p: dict) -> dict:
    """Karar anındaki bağlam: varlığın güncel metrikleri (sonuç ölçümünde temel)."""
    from .decisions import entity_metrics
    w = Window(7)
    return {"window": w.as_dict(), "metrics": entity_metrics(conn, p["entity_type"], p["entity_id"], w),
            "evidence": p["evidence"], "params": p["params"]}


def approve(conn: Connection, pid: int, user, note: str | None, ip: str | None) -> dict:
    p = _get(conn, pid)
    if p["status"] == "blocked":
        raise ProposalError("Risk motorunun bloke ettiği öneri onaylanamaz (kimse bu kontrolü aşamaz).")
    if p["status"] != "pending_approval":
        raise ProposalError(f"Bu öneri onaylanamaz (durum: {STATUS_TR.get(p['status'], p['status'])}).")
    checks, level = evaluate(conn, p, at_approval=True)
    if any(c["severity"] == "block" for c in checks):
        conn.execute(text("UPDATE ai_proposals SET status = 'blocked', risk_checks = CAST(:c AS JSONB), risk_level = :l, updated_at = NOW() WHERE id = :id"),
                     {"c": _json(checks), "l": level, "id": pid})
        for c in checks:
            if c["severity"] == "block":
                conn.execute(text("INSERT INTO ai_risk_events(proposal_id, code, severity, message) VALUES (:p, :c, 'block', :m)"),
                             {"p": pid, "c": c["code"], "m": c["message"]})
        activity(conn, "Onay anında risk motoru bloke etti: " + "; ".join(c["message"] for c in checks if c["severity"] == "block"),
                 agent="risk", kind="risk", level="warning", proposal_id=pid, user_id=user.id)
        log_audit(conn, actor=user.username, user_id=user.id, action="ai.proposal_blocked_at_approval", entity_type="ai_proposal",
                  entity_id=pid, ip=ip, details={"checks": [c["code"] for c in checks if c["severity"] == "block"]})
        raise ProposalError("Onay anında risk kontrolü başarısız: " + "; ".join(c["message"] for c in checks if c["severity"] == "block"))
    # Owner override ≠ güvenlik bypass: CEO itiraz ediyorsa sahip gerekçeyle devam edebilir (yalnızca LOW/MEDIUM risk).
    # Risk motorunun bloğu (yukarıda) hiçbir koşulda aşılamaz.
    override = p.get("ceo_stance") == "oppose"
    if override and level in ("high", "critical"):
        raise ProposalError(f"CEO bu karara karşı ve risk {RISK_TR[level].upper()}: sahip onayıyla bile uygulanamaz.")
    if override and len((note or "").strip()) < 10:
        raise ProposalError("CEO bu karara karşı. Yine de devam etmek için gerekçeni yaz (en az 10 karakter); "
                            "karar 'sahip override' olarak kaydedilir.", 422)
    result = execute_action(conn, p)
    status = "executed" if result.get("mode") == "automatic" and result.get("ok") else "approved"
    conn.execute(text("""UPDATE ai_proposals SET status = :s, decided_by = :u, decided_at = NOW(), decision_note = :n,
                         execution_result = CAST(:r AS JSONB), risk_checks = CAST(:c AS JSONB), risk_level = :l,
                         executed_at = CASE WHEN :s = 'executed' THEN NOW() END, updated_at = NOW() WHERE id = :id"""),
                 {"s": status, "u": user.id, "n": note, "r": _json(result), "c": _json(checks), "l": level, "id": pid})
    from .decisions import record
    record(conn, proposal=p, actor="owner", decision="approved", user_id=user.id, snapshot=_snapshot(conn, p),
           executed=status == "executed", reason=note, override=override, override_reason=note if override else None)
    if override:
        conn.execute(text("UPDATE ai_proposals SET owner_override = TRUE WHERE id = :id"), {"id": pid})
        log_audit(conn, actor=user.username, user_id=user.id, action="ai.owner_override", entity_type="ai_proposal",
                  entity_id=pid, ip=ip, details={"ceo_note": p.get("ceo_note"), "reason": note, "risk_level": level})
    activity(conn, (f"Sahip CEO itirazına rağmen onayladı (override): {note}" if override else f"Sahip onayladı: {p['title']}"),
             agent="ceo", kind="approval", level="warning" if override else "success", proposal_id=pid, user_id=user.id)
    if status == "approved":
        activity(conn, "Uygulama bağlantısı yok: öneri sahibin manuel uygulamasını bekliyor", kind="action", proposal_id=pid)
    log_audit(conn, actor=user.username, user_id=user.id, action="ai.proposal_approved", entity_type="ai_proposal",
              entity_id=pid, ip=ip, details={"action_type": p["action_type"], "entity": f"{p['entity_type']}:{p['entity_id']}",
                                             "params": p["params"], "mode": result.get("mode")})
    return {"status": status, "execution": result}


def reject(conn: Connection, pid: int, user, note: str | None, ip: str | None) -> None:
    p = _get(conn, pid)
    if p["status"] not in ("pending_approval", "blocked"):
        raise ProposalError(f"Bu öneri reddedilemez (durum: {STATUS_TR.get(p['status'], p['status'])}).")
    conn.execute(text("""UPDATE ai_proposals SET status = 'rejected', decided_by = :u, decided_at = NOW(), decision_note = :n,
                         updated_at = NOW() WHERE id = :id"""), {"u": user.id, "n": note, "id": pid})
    from .decisions import record
    record(conn, proposal=p, actor="owner", decision="rejected", user_id=user.id, snapshot=_snapshot(conn, p), reason=note)
    activity(conn, f"Sahip reddetti: {p['title']}", agent="ceo", kind="approval", level="warning", proposal_id=pid, user_id=user.id)
    log_audit(conn, actor=user.username, user_id=user.id, action="ai.proposal_rejected", entity_type="ai_proposal",
              entity_id=pid, ip=ip, details={"action_type": p["action_type"], "note": note})


def mark_executed(conn: Connection, pid: int, user, note: str | None, ip: str | None) -> None:
    """Sahip öneriyi platformda elle uyguladığında. Sonuç ölçümü bu andan başlar."""
    if config.emergency_stop(conn):
        raise ProposalError("Acil durdurma aktif: uygulama kaydı yapılamaz.")
    p = _get(conn, pid)
    if p["status"] != "approved":
        raise ProposalError("Yalnızca onaylanmış öneri uygulandı olarak işaretlenebilir.")
    conn.execute(text("""UPDATE ai_proposals SET status = 'executed', executed_by = :u, executed_at = NOW(),
                         execution_result = COALESCE(execution_result, '{}'::jsonb) || CAST(:r AS JSONB), updated_at = NOW()
                         WHERE id = :id"""), {"u": user.id, "r": _json({"manual_note": note}), "id": pid})
    conn.execute(text("UPDATE ai_decisions SET executed_at = NOW() WHERE proposal_id = :p AND decision = 'approved'"), {"p": pid})
    if p["action_type"] in ("ads.increase_budget", "ads.decrease_budget") and p["params"].get("new_daily_budget") is not None:
        # Platformda elle değiştirilen bütçe TrendHub kaydına da yansır (bir sonraki öneri doğru temelden başlasın)
        conn.execute(text("UPDATE ad_campaigns SET daily_budget = :b WHERE id = :id"),
                     {"b": d(p["params"]["new_daily_budget"]), "id": p["entity_id"]})
    elif p["action_type"] == "ads.pause":
        conn.execute(text("UPDATE ad_campaigns SET status = 'paused' WHERE id = :id"), {"id": p["entity_id"]})
    activity(conn, f"Uygulandı (manuel): {p['title']}", kind="action", level="success", proposal_id=pid, user_id=user.id)
    log_audit(conn, actor=user.username, user_id=user.id, action="ai.proposal_executed", entity_type="ai_proposal",
              entity_id=pid, ip=ip, details={"mode": "manual", "note": note})


# Action Engine: aksiyon türü → platform executor'ı. V1'de pazaryerine/reklam platformuna yazan executor yok
# (Trendyol reklam API'si bağlı değil, pazaryeri yazma bilinçli olarak kapalı). Yeni executor eklemek: fonksiyon
# yazıp buraya kaydetmek; çekirdek değişmez.
EXECUTORS: dict = {}

MANUAL_STEPS = {
    "ads.increase_budget": "Reklam panelinde kampanyanın günlük bütçesini {new_daily_budget} TL yapın, sonra 'Uyguladım' deyin.",
    "ads.decrease_budget": "Reklam panelinde kampanyanın günlük bütçesini {new_daily_budget} TL yapın, sonra 'Uyguladım' deyin.",
    "ads.pause": "Reklam panelinde kampanyayı durdurun, sonra 'Uyguladım' deyin.",
}


def execute_action(conn: Connection, p: dict) -> dict:
    if not config.write_allowed(conn):
        return {"mode": "blocked", "ok": False, "message": "Acil durdurma aktif"}
    fn = EXECUTORS.get(p["action_type"])
    if fn is None:
        tpl = MANUAL_STEPS.get(p["action_type"], "Öneriyi uygulayın, sonra 'Uyguladım' deyin.")
        try:
            steps = tpl.format(**(p["params"] or {}))
        except (KeyError, IndexError):
            steps = tpl
        return {"mode": "manual", "ok": None, "instructions": steps}
    try:
        res = fn(conn, p)
        return {"mode": "automatic", "ok": True, **(res or {})}
    except Exception as exc:  # noqa: BLE001
        return {"mode": "automatic", "ok": False, "error": f"{exc.__class__.__name__}: {str(exc)[:300]}"}


def retire_stale(conn: Connection, agent: str, run_id: int) -> int:
    """Ajanın bu çalışmada yeniden üretmediği açık (onaylanmamış) önerileri kapatır: koşul ortadan kalktı
    (ör. maliyet girildi, reklam artık zarar etmiyor). Onaylanmış ve uygulanmayı bekleyenlere dokunulmaz."""
    ids = [r[0] for r in conn.execute(text("""
        UPDATE ai_proposals SET status = 'superseded', updated_at = NOW()
         WHERE agent_code = :a AND status IN ('pending_approval', 'blocked') AND (run_id IS NULL OR run_id <> :r)
        RETURNING id"""), {"a": agent, "r": run_id})]
    for pid in ids:
        activity(conn, "Koşul ortadan kalktı; öneri kapatıldı", agent=agent, kind="proposal", proposal_id=pid, run_id=run_id)
    return len(ids)


def expire_old(conn: Connection) -> int:
    return conn.execute(text("""UPDATE ai_proposals SET status = 'expired', updated_at = NOW()
                                 WHERE status IN ('pending_approval', 'blocked') AND expires_at < NOW()""")).rowcount


def decorate(p: dict) -> dict:
    p["status_label"] = STATUS_TR.get(p["status"], p["status"])
    p["risk_label"] = RISK_TR.get(p["risk_level"], p["risk_level"])
    p["action_label"] = ACTION_TR.get(p["action_type"], p["action_type"])
    return p


def now() -> datetime:
    return datetime.now(timezone.utc)
