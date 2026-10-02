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
    return {"brief": b, "emergency_stop": config.emergency_stop(conn), "enabled": config.enabled(conn),
            "agents": agent_list(conn), "counts": counts, "last_cycle": last,
            "llm": {"available": chat.llm_available(conn), "model": None}}


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
    return decisions.scorecard(conn)


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
    history = [{"role": r["role"], "content": r["content"]} for r in rows(conn, """
        SELECT role, content FROM ai_chat_messages WHERE conversation_id = :c AND user_id = :u ORDER BY id DESC LIMIT 10""",
        c=conv, u=user.id)][::-1]
    conn.execute(text("INSERT INTO ai_chat_messages(user_id, conversation_id, role, content) VALUES (:u, :c, 'user', :m)"),
                 {"u": user.id, "c": conv, "m": body.message})
    res = chat.answer(conn, body.message, history)
    conn.execute(text("""INSERT INTO ai_chat_messages(user_id, conversation_id, role, content, engine, tools_used, usage)
                         VALUES (:u, :c, 'assistant', :m, :e, CAST(:t AS JSONB), CAST(:us AS JSONB))"""),
                 {"u": user.id, "c": conv, "m": res["answer"], "e": res["engine"], "t": json.dumps(res["tools_used"]),
                  "us": json.dumps(res["usage"])})
    return {"conversation_id": conv, **res}


@router.get("/chat/{conv}")
def chat_history(conv: str, user: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    return rows(conn, """SELECT role, content, engine, tools_used, created_at FROM ai_chat_messages
                          WHERE conversation_id = :c AND user_id = :u ORDER BY id""", c=conv, u=user.id)
