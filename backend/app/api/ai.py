"""AI Control Center API.

İzinler (services/ai/config.PERMISSIONS): READ = tüm roller, PROPOSE = operatör+, APPROVE/EXECUTE/ADMIN = yönetici.
Acil durdurmayı operatör AÇABİLİR (güvenli yön), yalnızca yönetici kaldırabilir.
"""
from __future__ import annotations

import json
import secrets
import time
from collections import defaultdict, deque
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..db import get_conn, get_engine, row, rows
from ..deps import CurrentUser, admin, client_ip, operator, viewer
from ..services import app_settings
from ..services.ai import agents, capital, ceo, chat, config, decisions, proposals
from datetime import timedelta

from ..services.ai.config import Window, thresholds
from ..services.audit import log_audit
from .common import Page, paged

router = APIRouter(prefix="/api/ai", tags=["ai"])


def _health(a: dict, stop: bool, running: set[str]) -> str:
    if not a["available"]:
        return "UNAVAILABLE"
    if not a["enabled"] or (stop and a["code"] in ("advertising", "inventory", "product_profit")):
        return "STOPPED" if not a["enabled"] else "RUNNING_READ_ONLY"
    if a["code"] in running:
        return "RUNNING"
    return {"error": "ERROR", "degraded": "DEGRADED"}.get(a["last_status"] or "", "IDLE")


def agent_list(conn: Connection) -> list[dict]:
    stop = config.emergency_stop(conn)
    running = {r[0] for r in conn.execute(text("""SELECT DISTINCT agent_code FROM ai_agent_runs
                                                  WHERE status = 'running' AND started_at > NOW() - INTERVAL '30 minutes'"""))}
    items = rows(conn, """
        SELECT a.*, (SELECT COUNT(*) FROM ai_agent_runs r WHERE r.agent_code = a.code AND r.started_at > NOW() - INTERVAL '7 days') AS runs_7d,
               (SELECT COUNT(*) FROM ai_agent_runs r WHERE r.agent_code = a.code AND r.status = 'error'
                 AND r.started_at > NOW() - INTERVAL '7 days') AS errors_7d,
               (SELECT AVG(duration_ms)::int FROM ai_agent_runs r WHERE r.agent_code = a.code AND r.finished_at IS NOT NULL
                 AND r.started_at > NOW() - INTERVAL '7 days') AS avg_ms,
               (SELECT COUNT(*) FROM ai_proposals p WHERE p.agent_code = a.code AND p.status = 'pending_approval') AS pending
          FROM ai_agents a ORDER BY a.available DESC, a.code""")
    for a in items:
        a["health"] = _health(a, stop, running)
    return items


# ------------------------------------------------------------------ genel bakış
@router.get("/overview")
def overview(_: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    b = row(conn, "SELECT brief_date, items, data_quality, updated_at FROM ai_briefs ORDER BY brief_date DESC LIMIT 1")
    if b is None or b["brief_date"] != config.today():
        live = ceo.build_brief(conn)
        b = {"brief_date": live["date"], "items": live["items"], "data_quality": live["data_quality"], "updated_at": None,
             "live": True}
    counts = row(conn, """SELECT COUNT(*) FILTER (WHERE status = 'pending_approval' AND requires_approval) AS pending,
                                 COUNT(*) FILTER (WHERE status = 'pending_approval' AND NOT requires_approval) AS tasks,
                                 COUNT(*) FILTER (WHERE status = 'approved') AS to_apply,
                                 COUNT(*) FILTER (WHERE status = 'blocked') AS blocked FROM ai_proposals""")
    last = row(conn, "SELECT started_at, finished_at, status FROM ai_agent_runs WHERE agent_code = 'ceo' ORDER BY id DESC LIMIT 1")
    return {"brief": b, "kpis": kpis(conn, counts), "emergency_stop": config.emergency_stop(conn), "enabled": config.enabled(conn),
            "agents": agent_list(conn), "counts": counts, "last_cycle": last,
            "llm": {"available": chat.llm_available(conn), "model": None}}


def kpis(conn: Connection, counts: dict) -> dict:
    """Ana ekranın 7 göstergesi. Hepsi diğer ekranlarla aynı kaynaktan (period_summary, capital.position)."""
    from ..services.ai.data import data_quality, period_summary
    t = config.today()
    week = period_summary(conn, Window(7, end_date=t))
    yday = period_summary(conn, Window(1, end_date=t - timedelta(days=1)))
    pos = capital.position(conn)
    used = row(conn, """SELECT COALESCE(SUM(required_capital), 0) AS v FROM ai_proposals
                         WHERE required_capital > 0 AND (status = 'approved' OR (status = 'executed' AND executed_at > NOW() - INTERVAL '30 days'))""")["v"]
    ad_today = conn.execute(text("SELECT COALESCE(SUM(amount), 0) FROM ad_spend WHERE spend_date = :d"), {"d": t}).scalar()
    risk_24h = conn.execute(text("SELECT COUNT(*) FROM ai_risk_events WHERE created_at > NOW() - INTERVAL '24 hours' AND severity <> 'info'")).scalar()
    dq = [q for q in data_quality(conn) if q["severity"] == "warning"]
    has_ads = bool(conn.execute(text("SELECT 1 FROM ad_spend LIMIT 1")).first())
    return {"orders_7d": week["orders"], "has_ad_data": has_ads,   # veri yok ≠ 0: arayüz bu bayraklarla "—" gösterir
            "net_profit_7d": week["net_profit"], "net_profit_yesterday": yday["net_profit"], "net_margin_7d": week["net_margin"],
            "cash_usable": pos["usable"], "cash_total": pos["totals"]["cash"] if pos["usable"] is not None else None,
            "capital_used": used, "capital_pending": max(pos["justified"] - used, 0) if pos["justified"] is not None else 0,
            "capital_unused": pos["unused"], "ad_spend_today": ad_today,
            "pending_payout": pos["cash_structure"]["pending_marketplace_payout"]["amount"],
            "pending_payout_kind": pos["cash_structure"]["pending_marketplace_payout"]["kind"],
            "pending_proposals": counts["pending"], "to_apply": counts["to_apply"],
            "risk_alerts": int(risk_24h) + len(dq) + int(counts["blocked"] or 0), "data_warnings": [q["message"] for q in dq]}


@router.post("/run")
def run_now(request: Request, user: CurrentUser = Depends(operator)):
    """Ajan döngüsünü şimdi çalıştırır (salt analiz + öneri; hiçbir aksiyon uygulanmaz)."""
    result = ceo.run_cycle(get_engine(), trigger=f"manual:{user.username}")
    with get_engine().begin() as conn:
        log_audit(conn, actor=user.username, user_id=user.id, action="ai.cycle_run", ip=client_ip(request),
                  details={k: v for k, v in result.items() if k in ("risk_anomalies", "outcomes_written", "brief_items")})
    return json.loads(json.dumps(result, default=str))


# ------------------------------------------------------------------ ajanlar
@router.get("/agents")
def list_agents(_: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    return agent_list(conn)


class AgentPatch(BaseModel):
    enabled: bool


@router.patch("/agents/{code}")
def patch_agent(code: str, body: AgentPatch, request: Request, user: CurrentUser = Depends(admin),
                conn: Connection = Depends(get_conn)):
    a = row(conn, "SELECT code, available FROM ai_agents WHERE code = :c", c=code)
    if a is None:
        raise HTTPException(404, "Ajan bulunamadı")
    if body.enabled and not a["available"]:
        raise HTTPException(409, "Bu ajanın veri kaynağı bağlı değil; açılamaz.")
    if code in ("ceo", "risk") and not body.enabled:
        raise HTTPException(409, "CEO ve Risk motoru kapatılamaz (Risk motoru güvenlik katmanıdır).")
    conn.execute(text("UPDATE ai_agents SET enabled = :e, updated_at = NOW() WHERE code = :c"), {"e": body.enabled, "c": code})
    proposals.activity(conn, f"Ajan {'açıldı' if body.enabled else 'kapatıldı'}: {code}", agent=code, kind="config", user_id=user.id)
    log_audit(conn, actor=user.username, user_id=user.id, action="ai.agent_toggled", entity_type="ai_agent", ip=client_ip(request),
              details={"code": code, "enabled": body.enabled})
    return {"ok": True}


@router.get("/runs")
def list_runs(page: Page = Depends(), agent: str | None = None, _: CurrentUser = Depends(viewer),
              conn: Connection = Depends(get_conn)):
    w, p = ("agent_code = :a", {"a": agent}) if agent else ("TRUE", {})
    total = conn.execute(text(f"SELECT COUNT(*) FROM ai_agent_runs WHERE {w}"), p).scalar()
    items = rows(conn, f"""SELECT id, agent_code, trigger, status, started_at, finished_at, duration_ms, input_sources, output,
                                  warnings, error, usage FROM ai_agent_runs WHERE {w} ORDER BY id DESC LIMIT :limit OFFSET :offset""",
                 **p, limit=page.page_size, offset=page.offset)
    return paged(items, total, page)


# ------------------------------------------------------------------ öneriler / onay
@router.get("/proposals")
def list_proposals(page: Page = Depends(), status: str | None = None, agent: str | None = None,
                   _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    where, params = ["TRUE"], {}
    if status == "open":
        where.append("p.status IN ('pending_approval', 'approved')")
    elif status:
        if status not in proposals.STATUS_TR:
            raise HTTPException(422, "Geçersiz durum")
        where.append("p.status = :st")
        params["st"] = status
    if agent:
        where.append("p.agent_code = :ag")
        params["ag"] = agent
    w = " AND ".join(where)
    total = conn.execute(text(f"SELECT COUNT(*) FROM ai_proposals p WHERE {w}"), params).scalar()
    items = rows(conn, f"""
        SELECT p.*, a.name AS agent_name, u.username AS decided_by_name,
               CASE p.entity_type WHEN 'product' THEN (SELECT name FROM products WHERE id = p.entity_id)
                                  WHEN 'campaign' THEN (SELECT name FROM ad_campaigns WHERE id = p.entity_id) END AS entity_name
          FROM ai_proposals p JOIN ai_agents a ON a.code = p.agent_code LEFT JOIN users u ON u.id = p.decided_by
         WHERE {w}
         ORDER BY CASE p.status WHEN 'pending_approval' THEN 0 WHEN 'approved' THEN 1 WHEN 'blocked' THEN 2 ELSE 3 END,
                  p.requires_approval DESC, p.updated_at DESC
         LIMIT :limit OFFSET :offset""", **params, limit=page.page_size, offset=page.offset)
    return paged([proposals.decorate(i) for i in items], total, page)


class DecisionIn(BaseModel):
    note: str | None = Field(None, max_length=1000)


def _decide(fn, pid: int, body: DecisionIn, request: Request, user: CurrentUser, conn: Connection):
    try:
        return fn(conn, pid, user, body.note, client_ip(request))
    except proposals.ProposalError as exc:
        raise HTTPException(exc.status, str(exc)) from None


@router.post("/proposals/{pid}/approve")
def approve(pid: int, body: DecisionIn, request: Request, user: CurrentUser = Depends(admin)):
    # Onay anındaki risk bloğu kalıcı olarak kaydedilmeli: hata durumunda bile işlem commit edilir.
    with get_engine().begin() as conn:
        try:
            return proposals.approve(conn, pid, user, body.note, client_ip(request))
        except proposals.ProposalError as exc:
            err = exc
    raise HTTPException(err.status, str(err))


@router.post("/proposals/{pid}/reject")
def reject(pid: int, body: DecisionIn, request: Request, user: CurrentUser = Depends(admin),
           conn: Connection = Depends(get_conn)):
    _decide(proposals.reject, pid, body, request, user, conn)
    return {"ok": True}


@router.post("/proposals/{pid}/executed")
def executed(pid: int, body: DecisionIn, request: Request, user: CurrentUser = Depends(admin),
             conn: Connection = Depends(get_conn)):
    _decide(proposals.mark_executed, pid, body, request, user, conn)
    return {"ok": True}


@router.post("/proposals/{pid}/execute")
def execute_proposal(pid: int, request: Request, user: CurrentUser = Depends(admin)):
    """Onaylı öneriyi gerçek platformda uygulamayı dener. Sonuç: EXECUTED / FAILED / SKIPPED / BLOCKED (her durumda kayıt
    commit edilir; başarısızlık asla EXECUTED yazılmaz)."""
    from ..services.ai import actions as ai_actions
    with get_engine().begin() as conn:
        try:
            return ai_actions.execute(conn, pid, user, client_ip(request))
        except ai_actions.ExecutionError as exc:
            err = exc
    raise HTTPException(err.status, str(err))


@router.get("/actions")
def list_actions(status: str | None = Query(None, pattern="^(EXECUTED|PROPOSED|BLOCKED|FAILED|SKIPPED)$"),
                 limit: int = Query(100, ge=1, le=500), _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    from ..services.ai import actions as ai_actions
    return {"items": ai_actions.recent(conn, limit, status), "counts_24h": ai_actions.counts(conn, 24),
            "labels": ai_actions.STATUS_TR}


class DiscountCheckIn(BaseModel):
    product_id: int
    discount_rate: Decimal = Field(gt=0, lt=1)


@router.post("/campaign/check")
def campaign_check(body: DiscountCheckIn, _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    """Planlanan indirimin net kâra etkisi (salt hesap; hiçbir şey uygulanmaz)."""
    from ..services.ai.growth import check_discount
    return check_discount(conn, body.product_id, body.discount_rate)


@router.get("/coverage")
def get_coverage(_: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    from ..services.ai.reconcile import coverage
    return coverage(conn)


@router.get("/command")
def command_center(_: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    """AI Komuta Merkezi: CEO durumu, son döngü, ajan sağlığı, aksiyonlar (durumlarına göre), hatalar, net kâr, bütçe,
    sınıflandırma kapsamı, stok riski, reklam adayları, onay bekleyenler, ajan performansı, yönetici özeti."""
    from ..services.ai import actions as ai_actions
    from ..services.ai import ads_platforms
    from ..services.ai.ceo_review import agent_performance, executive_summary, latest_summary
    from ..services.ai.data import inventory_status
    from ..services.ai.growth import ad_candidates
    from ..services.ai.reconcile import coverage
    counts = row(conn, """SELECT COUNT(*) FILTER (WHERE status = 'pending_approval' AND requires_approval) AS pending,
                                 COUNT(*) FILTER (WHERE status = 'pending_approval' AND NOT requires_approval) AS tasks,
                                 COUNT(*) FILTER (WHERE status = 'approved') AS to_apply,
                                 COUNT(*) FILTER (WHERE status = 'blocked') AS blocked FROM ai_proposals""")
    cov = coverage(conn)
    last = row(conn, """SELECT id, started_at, finished_at, status, error, output->'normalization' AS normalization
                          FROM ai_agent_runs WHERE agent_code = 'ceo' ORDER BY id DESC LIMIT 1""")
    errors = rows(conn, """SELECT agent_code, error, started_at FROM ai_agent_runs WHERE status = 'error'
                            AND started_at > NOW() - INTERVAL '24 hours' ORDER BY id DESC LIMIT 10""")
    failed = ai_actions.recent(conn, 10, "FAILED")
    return {"ceo": {"last_cycle": last, "emergency_stop": config.emergency_stop(conn), "enabled": config.enabled(conn)},
            "agents": agent_list(conn), "kpis": kpis(conn, counts), "counts": counts,
            "actions": {"counts_24h": ai_actions.counts(conn, 24), "recent": ai_actions.recent(conn, 30), "labels": ai_actions.STATUS_TR},
            "errors": {"agent_runs": errors, "failed_actions": failed},
            "budget": {**proposals.ad_budget_usage(conn), "capital": {k: capital.position(conn)[k] for k in ("usable", "justified", "unused")}},
            "coverage": cov, "stock_risks": [i for i in inventory_status(conn) if i["stockout_risk"]][:10],
            "ad_candidates": ad_candidates(conn)[:10], "performance": agent_performance(conn),
            "approvals": rows(conn, """SELECT id, agent_code, title, risk_level, required_capital, created_at FROM ai_proposals
                                        WHERE status = 'pending_approval' AND requires_approval ORDER BY id DESC LIMIT 20"""),
            "summary": latest_summary(conn) or executive_summary(conn, cov), "ad_platforms": ads_platforms.status()}


@router.get("/proposals/{pid}/trail")
def proposal_trail(pid: int, _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    """Tek öneri kimliği üzerinden tüm zincir: ajan → kâr doğrulaması → CEO → risk → onay → uygulama → sonuç ölçümü."""
    p = row(conn, "SELECT * FROM ai_proposals WHERE id = :id", id=pid)
    if p is None:
        raise HTTPException(404, "Öneri bulunamadı")
    run = row(conn, "SELECT id, agent_code, status, started_at, input_sources FROM ai_agent_runs WHERE id = :r", r=p["run_id"]) \
        if p["run_id"] else None
    decs = rows(conn, "SELECT * FROM ai_decisions WHERE proposal_id = :p ORDER BY id", p=pid)
    for x in decs:
        x["outcomes"] = rows(conn, "SELECT horizon_days, final_result, metrics, evaluated_at FROM ai_decision_outcomes WHERE decision_id = :d ORDER BY horizon_days", d=x["id"])
    return {"proposal": proposals.decorate(p), "agent_run": run, "decisions": decs,
            "activity": rows(conn, """SELECT a.id, a.created_at, a.agent_code, a.kind, a.level, a.message, u.username
                                        FROM ai_activity a LEFT JOIN users u ON u.id = a.user_id WHERE a.proposal_id = :p ORDER BY a.id""", p=pid),
            "risk_events": rows(conn, "SELECT code, severity, message, created_at FROM ai_risk_events WHERE proposal_id = :p ORDER BY id", p=pid),
            "audit": rows(conn, """SELECT occurred_at, actor, action, details FROM audit_logs
                                    WHERE (entity_type = 'ai_proposal' AND entity_id = CAST(:p AS TEXT)) ORDER BY id""", p=pid)}


class OwnerDecisionIn(BaseModel):
    decision_type: str
    entity_type: str
    entity_id: int
    note: str | None = Field(None, max_length=1000)
    override_reason: str | None = Field(None, max_length=1000)


@router.post("/decisions/assess")
def assess_decision(body: OwnerDecisionIn, _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    """Sahip bir karar vermeden önce CEO'nun kanıta dayalı görüşü (kayıt oluşturmaz)."""
    sit = decisions.situation_for(conn, body.decision_type, body.entity_type, body.entity_id)
    return decisions.assess(conn, body.decision_type, sit)


@router.post("/decisions")
def record_decision(body: OwnerDecisionIn, request: Request, user: CurrentUser = Depends(admin)):
    """Sahibin kendi kararını günlüğe yazar (sonucu 1/3/7/30 gün sonra ölçülür)."""
    with get_engine().begin() as conn:
        try:
            return decisions.record_owner_action(conn, user, decision_type=body.decision_type, entity_type=body.entity_type,
                                                 entity_id=body.entity_id, note=body.note, override_reason=body.override_reason,
                                                 ip=client_ip(request))
        except decisions.DecisionError as exc:
            err = exc
    detail = {"message": str(err), "assessment": err.assessment} if err.assessment else str(err)
    raise HTTPException(err.status, json.loads(json.dumps(detail, default=str)))


# ------------------------------------------------------------------ analiz ekranları
@router.get("/profit")
def profit(days: int = Query(None, ge=7, le=365), _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    from ..services.ai.data import period_summary
    th = thresholds(conn)
    w = Window(days or th["analysis_days"])
    items = agents.classified_products(conn, w)
    counts = {k: sum(1 for i in items if i["class"] == k) for k in agents.CLASSES}
    return {"window": w.as_dict(), "period": period_summary(conn, w), "classes": agents.CLASSES, "counts": counts,
            "products": items, "thresholds": {k: th[k] for k in ("min_units_for_data", "star_margin", "star_min_profit", "profitable_margin")},
            "note": "Net kâr = tahmini KDV sonrası kalem kârı − payına düşen reklam (kampanya harcaması eşit bölüşüm, TAHMİNİ)."}


@router.get("/ads")
def ads(_: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    return {"window_days": thresholds(conn)["ads_window_days"], "campaigns": agents.advertising_analysis(conn),
            "verdicts": agents.AD_VERDICTS,
            "note": ("Karar ölçütü ROAS değil, reklam sonrası NET kârdır: platform bildirimli reklam satışı × ürünlerin reklam "
                     "öncesi net marjı − harcama. Performans verisi yoksa karar 'Veri yetersiz' olur.")}


class BudgetIn(BaseModel):
    daily_budget: Decimal | None = Field(None, ge=0, le=Decimal("1000000"))


@router.put("/campaigns/{cid}/daily-budget")
def set_budget(cid: int, body: BudgetIn, request: Request, user: CurrentUser = Depends(operator),
               conn: Connection = Depends(get_conn)):
    if not conn.execute(text("UPDATE ad_campaigns SET daily_budget = :b WHERE id = :id"), {"b": body.daily_budget, "id": cid}).rowcount:
        raise HTTPException(404, "Kampanya bulunamadı")
    log_audit(conn, actor=user.username, user_id=user.id, action="ads.daily_budget_set", entity_type="ad_campaign", entity_id=cid,
              ip=client_ip(request), details={"daily_budget": str(body.daily_budget) if body.daily_budget is not None else None})
    return {"ok": True}


@router.get("/inventory")
def inventory(_: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    from ..services.ai.data import inventory_status
    return {"model": config.inventory_model(conn), "models": config.INVENTORY_MODELS, "items": inventory_status(conn),
            "stockout_days": thresholds(conn)["stockout_days"]}


# ------------------------------------------------------------------ gerçek pazaryeri verisi (salt okunur)
@router.get("/finance")
def get_finance(days: int = Query(30, ge=1, le=365), _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    """Finans / Nakit: bekleyen hakediş, alacak, son ödemeler, mutabakat; kârın gerçek/tahmin dağılımı."""
    from ..services.platform import finance as pf
    from ..services.platform.sources import finance_status
    w = Window(days)
    fs = finance_status(conn)
    by_type = rows(conn, """SELECT source, transaction_type, COUNT(*) AS n, SUM(debt) AS debt, SUM(credit) AS credit,
                                   SUM(commission_amount) AS commission, COUNT(*) FILTER (WHERE payment_order_id IS NULL) AS unpaid,
                                   COUNT(*) FILTER (WHERE applied_at IS NOT NULL) AS applied
                              FROM marketplace_finance_entries WHERE transaction_date >= :s GROUP BY 1, 2 ORDER BY 1, 3 DESC""",
                     s=w.start)
    recent = rows(conn, """SELECT id, source, transaction_type, transaction_date, order_number, barcode, debt, credit, commission_amount,
                                  seller_revenue, payment_order_id, payment_date, applied_at
                             FROM marketplace_finance_entries ORDER BY transaction_date DESC NULLS LAST, id DESC LIMIT 30""")
    pos = capital.position(conn)
    return {"status": {k: fs[k] for k in ("freshness", "age_hours", "stale_hours", "connected")},
            "payout": pf.payout_summary(conn), "sign_check": pf.seller_revenue_check(conn),
            "provenance": pf.profit_provenance(conn, w.start, w.end), "cash_structure": pos["cash_structure"],
            "usable": pos["usable"], "by_type": by_type, "recent": recent, "window": w.as_dict()}


@router.get("/cx")
def get_cx(days: int = Query(30, ge=7, le=90), _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    """Müşteri deneyimi: gerçek soru/iade kayıtlarından. Cevaplar otomatik GÖNDERİLMEZ."""
    from ..services.platform import cx
    return cx.analyze(conn, days)


@router.get("/data-sources")
def get_data_sources(_: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    from ..services.platform import events
    from ..services.platform.sources import data_sources
    return {"sources": data_sources(conn), "events": events.stats(conn), "recent_events": events.recent(conn, 30),
            "metric_sources": [
                {"metric": "Sipariş cirosu", "source": "Trendyol Orders (getShipmentPackages)", "kind": "ACTUAL"},
                {"metric": "Komisyon", "source": "Trendyol Finance (settlements) — yoksa ayardaki oranla TAHMİN", "kind": "ACTUAL/ESTIMATED"},
                {"metric": "Kargo", "source": "Trendyol kargo faturası kalemleri — yoksa varsayılan TAHMİN", "kind": "ACTUAL/ESTIMATED"},
                {"metric": "İade", "source": "Trendyol İadeler (getClaims) + Finance Return kaydı", "kind": "ACTUAL"},
                {"metric": "Ürün maliyeti", "source": "Tedarikçi / yerel maliyet kaydı", "kind": "LOCAL"},
                {"metric": "Reklam harcaması", "source": "Elle / CSV (Trendyol reklam API'si bağlı değil)", "kind": "MANUAL"},
                {"metric": "Kullanılabilir nakit", "source": "Elle girilen kasa (banka entegrasyonu yok)", "kind": "MANUAL"},
                {"metric": "Bekleyen hakediş", "source": "Trendyol Finance (cari hesap ekstresi)", "kind": "ACTUAL"},
            ]}


# ------------------------------------------------------------------ sermaye
@router.get("/capital")
def get_capital(_: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    return {**capital.position(conn), "kinds": capital.KIND_TR}


class AccountIn(BaseModel):
    kind: str
    name: str = Field(min_length=1, max_length=100)
    amount: Decimal = Field(ge=0, le=Decimal("1000000000"))
    as_of: str | None = None
    note: str | None = Field(None, max_length=300)


@router.put("/capital/accounts")
def upsert_account(body: AccountIn, request: Request, user: CurrentUser = Depends(admin), conn: Connection = Depends(get_conn)):
    if body.kind not in capital.KIND_TR:
        raise HTTPException(422, "Geçersiz hesap türü")
    conn.execute(text("""INSERT INTO ai_capital_accounts(kind, name, amount, as_of, note, updated_by)
                         VALUES (:k, :n, :a, COALESCE(CAST(:d AS DATE), CURRENT_DATE), :note, :u)
                         ON CONFLICT (kind, name) DO UPDATE SET amount = EXCLUDED.amount, as_of = EXCLUDED.as_of,
                         note = EXCLUDED.note, updated_by = EXCLUDED.updated_by, updated_at = NOW()"""),
                 {"k": body.kind, "n": body.name.strip(), "a": body.amount, "d": body.as_of, "note": body.note, "u": user.id})
    log_audit(conn, actor=user.username, user_id=user.id, action="ai.capital_account_set", entity_type="ai_capital_account",
              ip=client_ip(request), details={"kind": body.kind, "name": body.name, "amount": str(body.amount)})
    return {"ok": True}


@router.delete("/capital/accounts/{aid}")
def delete_account(aid: int, request: Request, user: CurrentUser = Depends(admin), conn: Connection = Depends(get_conn)):
    r = row(conn, "DELETE FROM ai_capital_accounts WHERE id = :id RETURNING kind, name, amount", id=aid)
    if r is None:
        raise HTTPException(404, "Kayıt bulunamadı")
    log_audit(conn, actor=user.username, user_id=user.id, action="ai.capital_account_deleted", entity_type="ai_capital_account",
              entity_id=aid, ip=client_ip(request), details={k: str(v) for k, v in r.items()})
    return {"ok": True}


# ------------------------------------------------------------------ karar günlüğü / sahip / karne
@router.get("/decisions")
def list_decisions(page: Page = Depends(), _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    total = conn.execute(text("SELECT COUNT(*) FROM ai_decisions")).scalar()
    items = rows(conn, """
        SELECT x.*, p.title, u.username,
               COALESCE((SELECT json_agg(json_build_object('horizon', o.horizon_days, 'result', o.final_result,
                                                           'profit_change', o.metrics->'profit_change',
                                                           'revenue_change', o.metrics->'revenue_change') ORDER BY o.horizon_days)
                           FROM ai_decision_outcomes o WHERE o.decision_id = x.id), '[]'::json) AS outcomes
          FROM ai_decisions x LEFT JOIN ai_proposals p ON p.id = x.proposal_id LEFT JOIN users u ON u.id = x.user_id
         ORDER BY x.id DESC LIMIT :limit OFFSET :offset""", limit=page.page_size, offset=page.offset)
    return {**paged(items, total, page), "note": decisions.NOTE}


@router.get("/quality-report")
def quality_report(days: int = Query(7, ge=1, le=90), _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    return decisions.quality_report(conn, days)


@router.get("/scorecard")
def scorecard(_: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    return {**decisions.scorecard(conn), "ai_accuracy": decisions.ai_accuracy(conn)}


PREFERENCE_KEYS = {
    "min_margin": "Kabul ettiğim en düşük net marj (0–1)",
    "risk_tolerance": "Risk toleransı (low / medium / high)",
    "new_product_patience_days": "Yeni ürüne tanıdığım süre (gün)",
    "ad_aggressiveness": "Reklam yaklaşımı (conservative / balanced / aggressive)",
    "stock_aggressiveness": "Stok yaklaşımı (conservative / balanced / aggressive)",
    "notes": "Serbest not",
}


@router.get("/owner")
def owner(_: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    return {**decisions.owner_patterns(conn), "preference_keys": PREFERENCE_KEYS}


class PreferenceIn(BaseModel):
    key: str
    value: str | float | int | None


@router.put("/owner/preferences")
def set_preference(body: PreferenceIn, request: Request, user: CurrentUser = Depends(admin), conn: Connection = Depends(get_conn)):
    if body.key not in PREFERENCE_KEYS:
        raise HTTPException(422, "Bilinmeyen tercih")
    v = body.value
    if body.key == "min_margin" and (not isinstance(v, (int, float)) or not 0 <= v <= 1):
        raise HTTPException(422, "Marj 0 ile 1 arasında olmalı (ör. 0.12)")
    if body.key == "risk_tolerance" and v not in ("low", "medium", "high"):
        raise HTTPException(422, "low / medium / high")
    if body.key in ("ad_aggressiveness", "stock_aggressiveness") and v not in ("conservative", "balanced", "aggressive"):
        raise HTTPException(422, "conservative / balanced / aggressive")
    if body.key == "new_product_patience_days" and (not isinstance(v, (int, float)) or not 1 <= v <= 365):
        raise HTTPException(422, "1–365 gün")
    conn.execute(text("""INSERT INTO ai_owner_preferences(key, value, source, updated_by) VALUES (:k, CAST(:v AS JSONB), 'explicit', :u)
                         ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = NOW()"""),
                 {"k": body.key, "v": json.dumps(v, ensure_ascii=False), "u": user.id})
    log_audit(conn, actor=user.username, user_id=user.id, action="ai.owner_preference_set", ip=client_ip(request),
              details={"key": body.key, "value": v})
    return {"ok": True}


# ------------------------------------------------------------------ aktivite / risk
@router.get("/activity")
def activity(page: Page = Depends(), proposal_id: int | None = None, _: CurrentUser = Depends(viewer),
             conn: Connection = Depends(get_conn)):
    w, p = ("a.proposal_id = :p", {"p": proposal_id}) if proposal_id else ("TRUE", {})
    total = conn.execute(text(f"SELECT COUNT(*) FROM ai_activity a WHERE {w}"), p).scalar()
    items = rows(conn, f"""SELECT a.*, g.name AS agent_name, u.username FROM ai_activity a
                             LEFT JOIN ai_agents g ON g.code = a.agent_code LEFT JOIN users u ON u.id = a.user_id
                            WHERE {w} ORDER BY a.id DESC LIMIT :limit OFFSET :offset""", **p, limit=page.page_size, offset=page.offset)
    return paged(items, total, page)


@router.get("/risk")
def risk(_: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    from ..services.ai.data import data_quality
    events = rows(conn, """SELECT e.*, p.title FROM ai_risk_events e LEFT JOIN ai_proposals p ON p.id = e.proposal_id
                            ORDER BY e.id DESC LIMIT 100""")
    return {"emergency_stop": config.emergency_stop(conn), "data_quality": data_quality(conn), "events": events,
            "limits": {k: thresholds(conn)[k] for k in ("ads_max_budget_step", "ads_target_margin_after_ads", "ads_data_stale_days",
                                                         "data_stale_hours", "stockout_days")}}


class StopIn(BaseModel):
    active: bool
    reason: str | None = Field(None, max_length=300)


@router.post("/emergency-stop")
def emergency_stop(body: StopIn, request: Request, user: CurrentUser = Depends(operator), conn: Connection = Depends(get_conn)):
    if not body.active and not config.can(user.role, "admin"):
        raise HTTPException(403, "Acil durdurmayı yalnızca yönetici kaldırabilir.")
    app_settings.set_value(conn, "ai.emergency_stop", body.active, user.id)
    msg = ("ACİL DURDURMA AKTİF: fiyat, reklam, kampanya, yayın ve tedarikçiye otomatik gönderim durduruldu; analiz sürüyor."
           if body.active else "Acil durdurma kaldırıldı.")
    proposals.activity(conn, msg + (f" Neden: {body.reason}" if body.reason else ""), agent="risk", kind="emergency",
                       level="error" if body.active else "success", user_id=user.id)
    log_audit(conn, actor=user.username, user_id=user.id, action="ai.emergency_stop_" + ("on" if body.active else "off"),
              ip=client_ip(request), details={"reason": body.reason})
    return {"ok": True, "emergency_stop": body.active}


# ------------------------------------------------------------------ ayarlar
@router.get("/settings")
def get_ai_settings(_: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    return {"thresholds": thresholds(conn), "defaults": config.DEFAULT_THRESHOLDS, "enabled": config.enabled(conn),
            "inventory_model": config.inventory_model(conn), "inventory_models": config.INVENTORY_MODELS,
            "cycle_minutes": int(app_settings.get(conn, "ai.cycle_minutes", 60) or 60),
            "llm_enabled": bool(app_settings.get(conn, "ai.llm_enabled", True)), "llm_configured": chat.llm_available(conn),
            "permissions": {k: list(v) for k, v in config.PERMISSIONS.items()}}


class SettingsIn(BaseModel):
    thresholds: dict | None = None
    enabled: bool | None = None
    inventory_model: str | None = None
    cycle_minutes: int | None = Field(None, ge=15, le=1440)
    llm_enabled: bool | None = None


@router.put("/settings")
def put_ai_settings(body: SettingsIn, request: Request, user: CurrentUser = Depends(admin), conn: Connection = Depends(get_conn)):
    changed = {}
    if body.thresholds is not None:
        try:
            clean = config.validate_thresholds(body.thresholds)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        merged = {**thresholds(conn), **clean}
        app_settings.set_value(conn, "ai.thresholds", merged, user.id)
        changed["thresholds"] = clean
    if body.inventory_model is not None:
        if body.inventory_model not in config.INVENTORY_MODELS:
            raise HTTPException(422, "Geçersiz stok modeli")
        app_settings.set_value(conn, "ai.inventory_model", body.inventory_model, user.id)
        changed["inventory_model"] = body.inventory_model
    for k in ("enabled", "cycle_minutes", "llm_enabled"):
        v = getattr(body, k)
        if v is not None:
            app_settings.set_value(conn, f"ai.{k}", v, user.id)
            changed[k] = v
    if changed:
        log_audit(conn, actor=user.username, user_id=user.id, action="ai.settings_updated", ip=client_ip(request), details=changed)
        proposals.activity(conn, "AI ayarları güncellendi: " + ", ".join(changed), kind="config", user_id=user.id)
    return {"ok": True, "changed": list(changed)}


# ------------------------------------------------------------------ CEO sohbeti
_hits: dict[int, deque] = defaultdict(deque)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = Field(None, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    context_product_id: int | None = Field(None, ge=1)


@router.post("/chat")
def chat_send(body: ChatIn, user: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    q = _hits[user.id]
    now = time.monotonic()
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= 20:
        raise HTTPException(429, "Çok fazla soru; bir dakika sonra tekrar deneyin.")
    q.append(now)
    conv = body.conversation_id or secrets.token_urlsafe(9)
    history = [{"role": r["role"], "content": r["content"], "tools_used": r["tools_used"]} for r in rows(conn, """
        SELECT role, content, tools_used FROM ai_chat_messages WHERE conversation_id = :c AND user_id = :u ORDER BY id DESC LIMIT 10""",
        c=conv, u=user.id)][::-1]
    conn.execute(text("INSERT INTO ai_chat_messages(user_id, conversation_id, role, content) VALUES (:u, :c, 'user', :m)"),
                 {"u": user.id, "c": conv, "m": body.message})
    from ..services.ai import orchestrator
    eng = get_engine()
    ctx_pid = body.context_product_id or orchestrator.context_product(eng, conv, user.id)
    p = orchestrator.plan(eng, body.message, "chat", ctx_pid)
    if p["intent"] == "other":
        # Uzman görevi gerektirmeyen soru: mevcut CEO sohbet motoru (konuşma geçmişiyle). Araç izi istek kaydına yazılır.
        res = _chat_with_trace(eng, conn, body.message, history, user.id, conv)
    else:
        if p["intent"].startswith("action") and not config.can(user.role, "propose"):
            raise HTTPException(403, "Aksiyon isteği (öneri oluşturma) için operatör yetkisi gerekir.")
        o = orchestrator.handle(eng, body.message, user_id=user.id, source="chat", conversation_id=conv, context_product_id=ctx_pid)
        res = {"answer": o["answer"], "engine": "orchestrator", "request_id": o["request_id"], "status": o["status"],
               "decision": o.get("decision"), "actions": o.get("actions"), "guards": o.get("guards"),
               "intent": o["intent"], "verification": o["verification"], "usage": {},
               "tools_used": list(dict.fromkeys(c["tool"] for t in o["tasks"] for c in t["tool_calls"])),
               "sources": [f"{t['agent']}.{t['task_type']} ({t['status']})" for t in o["tasks"]]}
    conn.execute(text("""INSERT INTO ai_chat_messages(user_id, conversation_id, role, content, engine, tools_used, usage)
                         VALUES (:u, :c, 'assistant', :m, :e, CAST(:t AS JSONB), CAST(:us AS JSONB))"""),
                 {"u": user.id, "c": conv, "m": res["answer"], "e": res["engine"], "t": json.dumps(res["tools_used"]),
                  "us": json.dumps(res["usage"])})
    return {"conversation_id": conv, **res}


def _chat_with_trace(eng, conn: Connection, message: str, history: list[dict], user_id: int, conv: str) -> dict:
    """Sohbet motoru cevabı + istek kaydı (ai_requests) + kullanılan araçların kaydı (ai_tool_calls)."""
    import time as _time
    import uuid as _uuid
    from ..services.ai import sanitize
    with eng.begin() as c:
        rid = c.execute(text("""INSERT INTO ai_requests(request_uid, user_id, source, conversation_id, message, intent, status)
                                VALUES (:u, :us, 'chat', :cv, :m, 'chat', 'running') RETURNING id"""),
                        {"u": _uuid.uuid4().hex, "us": user_id, "cv": conv, "m": sanitize.clean_text(message, 2000)}).scalar()
    token = chat.TOOL_LOG.set({"engine": eng, "request_id": rid, "run_id": None})
    t0 = _time.monotonic()
    try:
        res = chat.answer(conn, message, history)
    finally:
        chat.TOOL_LOG.reset(token)
    ms = int((_time.monotonic() - t0) * 1000)
    if res.get("engine") == "claude":
        res.setdefault("usage", {})["latency_ms"] = ms
    with eng.begin() as c:
        c.execute(text("""UPDATE ai_requests SET status = 'completed', answer = :a, finished_at = NOW(), duration_ms = :ms,
                                 verification = CAST(:v AS JSONB) WHERE id = :i"""),
                  {"a": res["answer"], "ms": ms, "i": rid,
                   "v": json.dumps({"overall": "UNVERIFIED", "mode": "chat_engine", "note": "Sohbet motoru; bağımsız doğrulama yapılmadı"})})
    return {**res, "request_id": rid}


@router.get("/chat/{conv}")
def chat_history(conv: str, user: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    return rows(conn, """SELECT role, content, engine, tools_used, created_at FROM ai_chat_messages
                          WHERE conversation_id = :c AND user_id = :u ORDER BY id""", c=conv, u=user.id)
