"""Ajan çalışma zamanı API'si: CEO'ya görev ver, iz (görev/mesaj/araç/kanıt/hata), Kontrol Merkezi, Bütçe Yöneticisi,
Kâr Koruması, olaylar, hafıza, deneyler, kreatifler, metrikler.

Panel yalnızca backend'in gerçek durumunu gösterir: her durum ai_agent_runs / ai_agent_tasks / ai_tool_calls / ai_proposals
kayıtlarından hesaplanır; sabit "ONLINE" yoktur.
"""
from __future__ import annotations

import json
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..db import get_conn, get_engine, row, rows
from ..deps import CurrentUser, admin, client_ip, viewer
from ..services.ai import config, creative, experiments as experiments_svc, governor, memory, metrics, operations, orchestrator, profit_guard, roles, tools
from ..services.audit import log_audit

router = APIRouter(prefix="/api/agents", tags=["agents"])
STATUS_TR = {"IDLE": "Boşta", "RUNNING": "Çalışıyor", "WAITING_APPROVAL": "Onay bekliyor", "BLOCKED": "Engelli", "ERROR": "Hata",
             "OFFLINE": "Kapalı"}


def _j(v):
    return json.loads(json.dumps(v, default=str, ensure_ascii=False))


class RequestIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


@router.post("/requests")
def create_request(body: RequestIn, request: Request, user: CurrentUser = Depends(viewer)):
    eng = get_engine()
    p = orchestrator.plan(eng, body.message)
    if p["intent"].startswith("action") and not config.can(user.role, "propose"):
        raise HTTPException(403, "Aksiyon isteği (öneri oluşturma) için operatör yetkisi gerekir.")
    res = orchestrator.handle(eng, body.message, user_id=user.id, source="api")
    with eng.begin() as c:
        log_audit(c, actor=user.username, user_id=user.id, action="agents.request", entity_type="ai_request",
                  entity_id=res["request_id"], ip=client_ip(request), details={"intent": res["intent"], "status": res["status"]})
    return _j(res)


@router.get("/requests")
def list_requests(limit: int = Query(30, ge=1, le=200), _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    return rows(conn, """SELECT r.id, r.request_uid, r.source, r.message, r.intent, r.status, r.started_at, r.duration_ms,
                                r.verification->>'overall' AS verification,
                                (SELECT COUNT(*) FROM ai_agent_tasks t WHERE t.request_id = r.id) AS tasks,
                                (SELECT COUNT(*) FROM ai_tool_calls c WHERE c.request_id = r.id) AS tool_calls
                           FROM ai_requests r ORDER BY r.id DESC LIMIT :l""", l=limit)


@router.get("/requests/{rid}")
def request_trace(rid: int, _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    r = row(conn, "SELECT * FROM ai_requests WHERE id = :i", i=rid)
    if r is None:
        raise HTTPException(404, "İstek bulunamadı")
    return {"request": r,
            "tasks": rows(conn, "SELECT * FROM ai_agent_tasks WHERE request_id = :i ORDER BY id", i=rid),
            "messages": rows(conn, "SELECT * FROM ai_agent_messages WHERE request_id = :i ORDER BY id", i=rid),
            "tool_calls": rows(conn, """SELECT id, call_uid, task_id, run_id, agent_code, tool, access, risk_level, arguments, status,
                                               attempts, started_at, completed_at, duration_ms, result_summary, result_hash, error,
                                               proposal_id, external_ref FROM ai_tool_calls WHERE request_id = :i ORDER BY id""", i=rid),
            "evidence": rows(conn, "SELECT * FROM ai_evidence WHERE request_id = :i ORDER BY id", i=rid),
            "errors": rows(conn, "SELECT * FROM ai_agent_errors WHERE request_id = :i ORDER BY id", i=rid),
            "approvals": rows(conn, """SELECT id, agent_code, action_type, title, status, risk_level, required_capital, risk_checks
                                         FROM ai_proposals WHERE request_id = :i ORDER BY id""", i=rid)}


@router.get("/tool-calls/{cid}")
def tool_call(cid: int, _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    r = row(conn, "SELECT * FROM ai_tool_calls WHERE id = :i", i=cid)
    if r is None:
        raise HTTPException(404, "Araç çağrısı bulunamadı")
    return r


# ------------------------------------------------------------------ Kontrol Merkezi
def agent_status(conn: Connection) -> list[dict]:
    stop, on = config.emergency_stop(conn), config.enabled(conn)
    agents = rows(conn, "SELECT * FROM ai_agents ORDER BY code")
    out = []
    for a in agents:
        code = a["code"]
        last = row(conn, """SELECT id, status, started_at, finished_at, duration_ms, error, trigger, usage FROM ai_agent_runs
                             WHERE agent_code = :c ORDER BY id DESC LIMIT 1""", c=code)
        running = row(conn, """SELECT t.id, t.objective, t.task_uid FROM ai_agent_tasks t WHERE t.agent_code = :c AND t.status = 'running'
                                AND t.started_at > NOW() - INTERVAL '30 minutes' ORDER BY id DESC LIMIT 1""", c=code)
        run_running = row(conn, """SELECT id FROM ai_agent_runs WHERE agent_code = :c AND status = 'running'
                                    AND started_at > NOW() - INTERVAL '30 minutes' LIMIT 1""", c=code)
        last_task = row(conn, "SELECT status, objective, verification_status, finished_at FROM ai_agent_tasks WHERE agent_code = :c ORDER BY id DESC LIMIT 1",
                        c=code)
        pending = conn.execute(text("SELECT COUNT(*) FROM ai_proposals WHERE agent_code = :c AND status = 'pending_approval' AND requires_approval"),
                               {"c": code}).scalar()
        stats = row(conn, """SELECT COUNT(*) FILTER (WHERE status IN ('ok', 'degraded')) AS ok, COUNT(*) FILTER (WHERE status = 'error') AS err,
                                    COALESCE(SUM((usage->>'input_tokens')::bigint), 0) AS tin, COALESCE(SUM((usage->>'output_tokens')::bigint), 0) AS tout,
                                    AVG(duration_ms)::int AS avg_ms
                               FROM ai_agent_runs WHERE agent_code = :c AND started_at > NOW() - INTERVAL '7 days'""", c=code)
        tl_ = rows(conn, """SELECT tool, COUNT(*) AS n, COUNT(*) FILTER (WHERE status IN ('failed', 'timeout', 'denied')) AS failed
                              FROM ai_tool_calls WHERE agent_code = :c AND started_at > NOW() - INTERVAL '7 days'
                             GROUP BY 1 ORDER BY 2 DESC LIMIT 8""", c=code)
        ev = row(conn, """SELECT COUNT(*) AS n, COUNT(*) FILTER (WHERE verification = 'VERIFIED') AS v,
                                 COUNT(*) FILTER (WHERE verification = 'FAILED') AS f
                            FROM ai_evidence WHERE agent_code = :c AND created_at > NOW() - INTERVAL '7 days'""", c=code)
        decision = row(conn, """SELECT id, title, status, created_at FROM ai_proposals WHERE agent_code = :c ORDER BY id DESC LIMIT 1""", c=code)
        if not a["available"] or not a["enabled"] or not on:
            st = "OFFLINE"
            why = a["unavailable_reason"] or ("Ajan kapalı" if not a["enabled"] else "AI sistemi kapalı (ai.enabled=false)")
        elif running or run_running:
            st, why = "RUNNING", (running or {}).get("objective") or "Döngü çalışıyor"
        elif last and last["status"] == "error":
            st, why = "ERROR", last["error"]
        elif stop and code not in ("reality_checker", "analytics", "finance"):
            st, why = "BLOCKED", "Acil durdurma aktif: yazma/öneri uygulanamaz"
        elif last_task and last_task["status"] in ("blocked", "failed", "no_evidence") and (not last or last_task["finished_at"] and
                                                                                             last_task["finished_at"] >= (last["finished_at"] or last_task["finished_at"])):
            st = "BLOCKED" if last_task["status"] == "blocked" else "ERROR"
            why = f"Son görev: {last_task['status']}"
        elif pending:
            st, why = "WAITING_APPROVAL", f"{pending} öneri sahibin onayını bekliyor"
        else:
            st, why = "IDLE", None
        cost = {"input_tokens": int(stats["tin"]), "output_tokens": int(stats["tout"]),
                "note": "LLM çağrısı yok (deterministik ajan; maliyet 0)" if not stats["tin"] else "LLM token kullanımı (7 gün)"}
        out.append({"code": code, "name": a["name"], "unit": a["unit"], "status": st, "status_label": STATUS_TR[st], "status_reason": why,
                    "current_task": (running or {}).get("objective"), "last_run": last,
                    "success_7d": int(stats["ok"] or 0), "failure_7d": int(stats["err"] or 0), "avg_duration_ms": stats["avg_ms"],
                    "tools_used": tl_, "evidence_7d": {"claims": int(ev["n"]), "verified": int(ev["v"]), "failed": int(ev["f"])},
                    "cost": cost, "last_decision": decision, "last_task": last_task, "pending_approvals": int(pending),
                    "role": roles.ROLES.get(code)})
    return out


@router.get("/control-center")
def control_center(_: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    return _j({"agents": agent_status(conn), "emergency_stop": config.emergency_stop(conn), "enabled": config.enabled(conn),
               "metrics": metrics.db_metrics(conn), "budget": governor.status(conn), "incidents": operations.open_incidents(conn),
               "requests": rows(conn, """SELECT id, message, intent, status, started_at, duration_ms, verification->>'overall' AS verification
                                           FROM ai_requests ORDER BY id DESC LIMIT 10""")})


@router.get("/activity")
def activity(limit: int = Query(100, ge=1, le=500), request_id: int | None = None, after_id: int | None = None,
             _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    return rows(conn, """SELECT a.id, a.agent_code, g.name AS agent_name, a.kind, a.level, a.message, a.created_at, a.request_id,
                                a.task_id, a.proposal_id, a.run_id
                           FROM ai_activity a LEFT JOIN ai_agents g ON g.code = a.agent_code
                          WHERE (CAST(:r AS BIGINT) IS NULL OR a.request_id = :r) AND (CAST(:af AS BIGINT) IS NULL OR a.id > :af)
                          ORDER BY a.id DESC LIMIT :l""", r=request_id, af=after_id, l=limit)


@router.get("/tools")
def tool_catalog(_: CurrentUser = Depends(viewer)):
    return tools.catalog()


@router.get("/roles")
def role_cards(_: CurrentUser = Depends(viewer)):
    return {"upstream": roles.UPSTREAM, "roles": roles.ROLES, "units": roles.UNIT_OF}


# ------------------------------------------------------------------ bütçe / kâr koruması
@router.get("/budget")
def budget(_: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    return _j({**governor.status(conn), "fields": governor.FIELDS_TR,
               "ledger": rows(conn, """SELECT l.*, p.title FROM ai_budget_ledger l JOIN ai_proposals p ON p.id = l.proposal_id
                                        ORDER BY l.id DESC LIMIT 50""")})


class BudgetIn(BaseModel):
    total_budget: float | None = None
    daily_limit: float | None = None
    weekly_limit: float | None = None
    per_agent_limit: float | None = None
    per_campaign_limit: float | None = None
    max_single_action_amount: float | None = None
    max_daily_ad_spend: float | None = None


@router.put("/budget")
def set_budget(body: BudgetIn, request: Request, user: CurrentUser = Depends(admin), conn: Connection = Depends(get_conn)):
    vals = body.model_dump(exclude_unset=True)
    try:
        cfg = governor.save(conn, vals, user.id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    log_audit(conn, actor=user.username, user_id=user.id, action="agents.budget_updated", ip=client_ip(request), details=vals)
    conn.execute(text("INSERT INTO ai_activity(agent_code, kind, level, message, user_id) VALUES ('ceo', 'config', 'info', :m, :u)"),
                 {"m": "Bütçe Yöneticisi ayarları güncellendi: " + ", ".join(vals), "u": user.id})
    return _j(cfg)


@router.get("/profit-guard")
def guard(product_id: int | None = None, _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    if product_id:
        return _j(profit_guard.product_state(conn, product_id))
    return _j(profit_guard.portfolio(conn))


# ------------------------------------------------------------------ olaylar / hafıza / deneyler / kreatifler
@router.get("/incidents")
def incidents(status: str = Query("open", pattern="^(open|resolved|all)$"), _: CurrentUser = Depends(viewer),
              conn: Connection = Depends(get_conn)):
    return rows(conn, """SELECT * FROM ai_incidents WHERE (:s = 'all' OR status = :s) ORDER BY id DESC LIMIT 100""", s=status)


@router.get("/memory")
def memory_view(limit: int = Query(50, ge=1, le=200), _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    return _j(memory.history(conn, limit))


@router.get("/experiments")
def experiments(_: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    return rows(conn, "SELECT * FROM ai_experiments ORDER BY id DESC LIMIT 100")


@router.post("/experiments/{eid}/start")
def start_experiment(eid: int, request: Request, user: CurrentUser = Depends(admin), conn: Connection = Depends(get_conn)):
    from datetime import timedelta

    from ..services.ai.config import today
    e = row(conn, "SELECT * FROM ai_experiments WHERE id = :i FOR UPDATE", i=eid)
    if e is None or e["status"] != "proposed":
        raise HTTPException(409, "Yalnızca önerilmiş deney başlatılabilir.")
    if Decimal(str(e["cost"])) > 0:
        raise HTTPException(409, "Para gerektiren deney Onaylar ekranından (Bütçe Yöneticisi) geçmelidir; burada başlatılamaz.")
    t = today()
    base = experiments_svc.metric(conn, e, t - timedelta(days=e["duration_days"]), t - timedelta(days=1))
    conn.execute(text("""UPDATE ai_experiments SET status = 'running', started_at = NOW(), measure_after = :m,
                                baseline = CAST(:b AS JSONB), updated_at = NOW() WHERE id = :i"""),
                 {"m": t + timedelta(days=e["duration_days"]), "b": json.dumps({"value": str(base) if base is not None else None,
                                                                                 "window_days": e["duration_days"]}), "i": eid})
    log_audit(conn, actor=user.username, user_id=user.id, action="agents.experiment_started", entity_type="ai_experiment",
              entity_id=eid, ip=client_ip(request), details={"baseline": str(base)})
    return {"ok": True, "baseline": str(base) if base is not None else None}


@router.get("/creatives")
def creatives(product_id: int | None = None, _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    return _j(creative.performance(conn, product_id))


# ------------------------------------------------------------------ gözlemlenebilirlik
@router.get("/metrics")
def metrics_json(hours: int = Query(24, ge=1, le=720), _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    return _j({**metrics.db_metrics(conn, hours), "process": metrics.snapshot()})


@router.get("/metrics/prometheus", response_class=PlainTextResponse)
def metrics_prom(_: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    return metrics.prometheus(conn)
