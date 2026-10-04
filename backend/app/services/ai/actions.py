"""Aksiyon kaydı ve executor'lar.

Her ajan çıktısı tam olarak bir durumdadır:
    EXECUTED  gerçek API / veritabanı işlemi BAŞARIYLA yapıldı (yalnızca burada, başarı dönüşünden sonra yazılır)
    PROPOSED  öneri oluştu; sahibin onayını veya manuel uygulamasını bekliyor
    BLOCKED   risk motoru / guardrail / CEO çatışma kuralı engelledi (reason_code: needs_data, stock_risk, budget_limit, …)
    FAILED    uygulama denendi ama dış servis/işlem hata verdi (hata metni kaydedilir)
    SKIPPED   uygulanmadı: bağlantı yok, yazma kapalı, döngü limiti dolu (neden kaydedilir)

Kayıt alanları: ajan, zaman, gerekçe, girdi verisi, beklenen etki, gerçek aksiyon, durum, hata, sonuç.
"""
from __future__ import annotations

import json
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ...db import row, rows
from ..audit import log_audit
from . import config
from .config import d, tl

STATUSES = ("EXECUTED", "PROPOSED", "BLOCKED", "FAILED", "SKIPPED")
STATUS_TR = {"EXECUTED": "Uygulandı", "PROPOSED": "Önerildi", "BLOCKED": "Engellendi", "FAILED": "Başarısız",
             "SKIPPED": "Atlandı"}
PROPOSAL_TO_ACTION = {"pending_approval": "PROPOSED", "approved": "PROPOSED", "blocked": "BLOCKED", "executed": "EXECUTED",
                      "failed": "FAILED", "rejected": "SKIPPED", "expired": "SKIPPED", "superseded": "SKIPPED", "info": "PROPOSED"}


def _j(v) -> str:
    return json.dumps(v if v is not None else {}, ensure_ascii=False, default=str)


def log(conn: Connection, *, agent: str, action_type: str, status: str, reason: str, entity_type: str | None = None,
        entity_id: int | None = None, reason_code: str | None = None, input_data: dict | None = None,
        expected_impact: dict | None = None, actual_action: str | None = None, error: str | None = None,
        result: dict | None = None, proposal_id: int | None = None, cycle_id: int | None = None,
        dedupe_key: str | None = None) -> int:
    assert status in STATUSES, status
    params = {"c": cycle_id, "a": agent, "t": action_type, "et": entity_type, "eid": entity_id, "s": status, "r": reason[:2000],
              "rc": reason_code, "i": _j(input_data), "x": _j(expected_impact), "act": actual_action, "e": (error or None),
              "res": _j(result), "p": proposal_id, "k": dedupe_key}
    if dedupe_key:
        return conn.execute(text("""
            INSERT INTO ai_actions(cycle_id, agent_code, action_type, entity_type, entity_id, status, reason, reason_code, input_data,
                                   expected_impact, actual_action, error, result, proposal_id, dedupe_key)
            VALUES (:c, :a, :t, :et, :eid, :s, :r, :rc, CAST(:i AS JSONB), CAST(:x AS JSONB), :act, :e, CAST(:res AS JSONB), :p, :k)
            ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO UPDATE SET status = EXCLUDED.status, reason = EXCLUDED.reason,
                reason_code = EXCLUDED.reason_code, input_data = EXCLUDED.input_data, expected_impact = EXCLUDED.expected_impact,
                actual_action = EXCLUDED.actual_action, error = EXCLUDED.error, result = EXCLUDED.result,
                cycle_id = COALESCE(EXCLUDED.cycle_id, ai_actions.cycle_id), created_at = NOW()
            RETURNING id"""), params).scalar()
    return conn.execute(text("""
        INSERT INTO ai_actions(cycle_id, agent_code, action_type, entity_type, entity_id, status, reason, reason_code, input_data,
                               expected_impact, actual_action, error, result, proposal_id)
        VALUES (:c, :a, :t, :et, :eid, :s, :r, :rc, CAST(:i AS JSONB), CAST(:x AS JSONB), :act, :e, CAST(:res AS JSONB), :p)
        RETURNING id"""), params).scalar()


def sync_proposal(conn: Connection, pid: int, cycle_id: int | None = None) -> None:
    """Önerinin güncel durumunu aksiyon kaydına yansıtır (öneri başına tek satır, yerinde güncellenir)."""
    p = row(conn, "SELECT * FROM ai_proposals WHERE id = :i", i=pid)
    if p is None:
        return
    status = PROPOSAL_TO_ACTION.get(p["status"], "PROPOSED")
    blocks = [c for c in (p["risk_checks"] or []) if c.get("severity") == "block"]
    reason = p["reason"] if status != "BLOCKED" else "; ".join(c["message"] for c in blocks) or p["reason"]
    log(conn, agent=p["agent_code"], action_type=p["action_type"], status=status, reason=reason, entity_type=p["entity_type"],
        entity_id=p["entity_id"], reason_code=(blocks[0]["code"] if blocks else None), input_data=p["evidence"],
        expected_impact={**(p["expected_result"] or {}), "params": p["params"], "required_capital": p["required_capital"]},
        actual_action=None if status != "EXECUTED" else (p["execution_result"] or {}).get("actual_action", "uygulandı"),
        result=p["execution_result"] or {}, proposal_id=pid, cycle_id=cycle_id or p["run_id"], dedupe_key=f"proposal:{pid}")


def recent(conn: Connection, limit: int = 100, status: str | None = None) -> list[dict]:
    return rows(conn, f"""SELECT a.*, g.name AS agent_name FROM ai_actions a JOIN ai_agents g ON g.code = a.agent_code
                          {"WHERE a.status = :s" if status else ""} ORDER BY a.created_at DESC, a.id DESC LIMIT :n""",
                s=status, n=limit)


def counts(conn: Connection, hours: int = 24) -> dict:
    out = {s: 0 for s in STATUSES}
    for r in rows(conn, """SELECT status, COUNT(*) AS n FROM ai_actions WHERE created_at > NOW() - make_interval(hours => :h)
                           GROUP BY 1""", h=hours):
        out[r["status"]] = int(r["n"])
    return out


# ------------------------------------------------------------------ executor'lar
class ExecutionError(ValueError):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def _manual(p: dict) -> str:
    from .proposals import MANUAL_STEPS
    tpl = MANUAL_STEPS.get(p["action_type"], "Öneriyi ilgili panelde uygulayın, sonra 'Uyguladım' deyin.")
    try:
        return tpl.format(**(p["params"] or {}))
    except (KeyError, IndexError):
        return tpl


def _finish(conn: Connection, p: dict, status: str, *, actual: str | None, error: str | None, result: dict, user, ip,
            reason: str) -> dict:
    """Uygulama denemesinin sonucunu öneriye, aksiyon kaydına, aktiviteye ve denetim kaydına yazar."""
    from .proposals import activity
    if status == "EXECUTED":
        conn.execute(text("""UPDATE ai_proposals SET status = 'executed', executed_by = :u, executed_at = NOW(),
                             execution_result = CAST(:r AS JSONB), updated_at = NOW() WHERE id = :id"""),
                     {"u": user.id, "r": _j({**result, "actual_action": actual, "mode": "automatic"}), "id": p["id"]})
        conn.execute(text("UPDATE ai_decisions SET executed_at = NOW() WHERE proposal_id = :p AND decision = 'approved'"), {"p": p["id"]})
        from . import governor
        governor.commit(conn, p["id"])
    elif status == "FAILED":
        conn.execute(text("""UPDATE ai_proposals SET status = 'failed', execution_result = CAST(:r AS JSONB), updated_at = NOW()
                             WHERE id = :id"""), {"r": _j({**result, "error": error, "mode": "automatic"}), "id": p["id"]})
    log(conn, agent=p["agent_code"], action_type=f"execute:{p['action_type']}", status=status, reason=reason,
        entity_type=p["entity_type"], entity_id=p["entity_id"], input_data={"params": p["params"]},
        expected_impact=p["expected_result"] or {}, actual_action=actual, error=error, result=result, proposal_id=p["id"])
    sync_proposal(conn, p["id"])
    level = {"EXECUTED": "success", "FAILED": "error", "BLOCKED": "warning", "SKIPPED": "info"}[status]
    activity(conn, f"Uygulama {STATUS_TR[status].lower()}: {p['title']}" + (f" — {error}" if error else f" — {reason}"),
             agent=p["agent_code"], kind="action", level=level, proposal_id=p["id"], user_id=user.id)
    log_audit(conn, actor=user.username, user_id=user.id, action=f"ai.execute.{status.lower()}", entity_type="ai_proposal",
              entity_id=p["id"], ip=ip, details={"action_type": p["action_type"], "params": p["params"], "actual": actual,
                                                 "error": error, "result": result})
    return {"status": status, "actual_action": actual, "error": error, "result": result, "reason": reason}


def execute(conn: Connection, pid: int, user, ip: str | None = None, *, connector=None) -> dict:
    """Onaylanmış öneriyi gerçek platformda uygulamayı dener. Başarı yalnızca platform/DB başarılı dönerse EXECUTED olur."""
    from .proposals import evaluate
    p = row(conn, "SELECT * FROM ai_proposals WHERE id = :i FOR UPDATE", i=pid)
    if p is None:
        raise ExecutionError("Öneri bulunamadı", 404)
    if p["status"] == "executed":
        raise ExecutionError("Bu öneri zaten uygulandı (tekrar uygulanmaz).")
    if p["status"] != "approved":
        raise ExecutionError("Yalnızca onaylanmış öneri uygulanabilir.")
    if not config.write_allowed(conn):
        return _finish(conn, p, "BLOCKED", actual=None, error=None, result={}, user=user, ip=ip,
                       reason="Acil durdurma aktif: hiçbir yazma işlemi yapılmaz.")
    checks, _level = evaluate(conn, p, at_approval=True)
    blocks = [c for c in checks if c["severity"] == "block"]
    if blocks:
        conn.execute(text("UPDATE ai_proposals SET status = 'blocked', risk_checks = CAST(:c AS JSONB), updated_at = NOW() WHERE id = :id"),
                     {"c": _j(checks), "id": pid})
        return _finish(conn, {**p, "status": "blocked"}, "BLOCKED", actual=None, error=None, result={"checks": blocks}, user=user,
                       ip=ip, reason="Uygulama anında risk kontrolü: " + "; ".join(c["message"] for c in blocks))
    action = p["action_type"]
    params = p["params"] or {}
    if action == "pricing.change_price":
        return _execute_price(conn, p, params, user, ip, connector)
    if action.startswith("ads."):
        from .ads_platforms import AdsPlatformError, adapter_for
        adapter = adapter_for(p["channel"])
        if not adapter.connected():
            return _finish(conn, p, "SKIPPED", actual=None, error=None, result={"manual_steps": _manual(p)}, user=user, ip=ip,
                           reason=f"{adapter.not_connected_reason()}. Manuel uygulayın: {_manual(p)}")
        ext = row(conn, "SELECT external_id FROM ad_campaigns WHERE id = :i", i=p["entity_id"])
        try:
            if action == "ads.pause":
                res = adapter.pause(ext["external_id"])
                conn.execute(text("UPDATE ad_campaigns SET status = 'paused' WHERE id = :i"), {"i": p["entity_id"]})
            else:
                res = adapter.set_daily_budget(ext["external_id"], d(params["new_daily_budget"]))
                conn.execute(text("UPDATE ad_campaigns SET daily_budget = :b WHERE id = :i"),
                             {"b": d(params["new_daily_budget"]), "i": p["entity_id"]})
        except AdsPlatformError as exc:
            return _finish(conn, p, "FAILED", actual=None, error=f"{exc.__class__.__name__}: {exc}", result={}, user=user, ip=ip,
                           reason="Reklam platformu hata verdi")
        return _finish(conn, p, "EXECUTED", actual=f"{adapter.name}: {action}", error=None, result=res or {}, user=user, ip=ip,
                       reason="Reklam platformunda uygulandı")
    return _finish(conn, p, "SKIPPED", actual=None, error=None, result={"manual_steps": _manual(p)}, user=user, ip=ip,
                   reason=f"Bu aksiyonun otomatik uygulayıcısı yok; manuel uygulanır: {_manual(p)}")


def _execute_price(conn: Connection, p: dict, params: dict, user, ip, connector) -> dict:
    """Fiyat değişikliği: eski/yeni fiyat ve beklenen net kâr denetim kaydına yazılır; yalnızca pazaryeri başarılı dönerse EXECUTED."""
    from ...connectors.base import ConnectorError, WriteDisabled
    from ...connectors.registry import get_connector
    prod = row(conn, "SELECT id, barcode, sale_price FROM products WHERE id = :i", i=p["entity_id"])
    old, new = d(params.get("old_price")), d(params.get("new_price"))
    if prod is None or not prod["barcode"]:
        return _finish(conn, p, "FAILED", actual=None, error="Ürünün barkodu yok; pazaryerinde eşleşemez", result={}, user=user,
                       ip=ip, reason="Fiyat güncellenemedi")
    if d(prod["sale_price"]) != old:
        return _finish(conn, p, "BLOCKED", actual=None, error=None,
                       result={"current_price": prod["sale_price"], "proposal_old_price": old}, user=user, ip=ip,
                       reason=f"Fiyat öneriden sonra değişmiş ({tl(old)} → şu an {tl(prod['sale_price'])}); öneri yenilenmeli.")
    c = connector or get_connector("trendyol")
    audit = {"barcode": prod["barcode"], "old_price": old, "new_price": new,
             "expected_unit_profit_old": params.get("unit_profit_old"), "expected_unit_profit_new": params.get("unit_profit_new")}
    if not c.settings.connector_write_enabled:
        return _finish(conn, p, "SKIPPED", actual=None, error=None, result=audit, user=user, ip=ip,
                       reason=("Pazaryerine yazma kapalı (CONNECTOR_WRITE_ENABLED=false). Trendyol panelinde fiyatı "
                               f"{tl(old)} → {tl(new)} yapın, sonra 'Uyguladım' deyin."))
    try:
        c.update_price(prod["barcode"], new)
    except (ConnectorError, WriteDisabled) as exc:
        return _finish(conn, p, "FAILED", actual=None, error=f"{exc.__class__.__name__}: {str(exc)[:300]}", result=audit,
                       user=user, ip=ip, reason="Trendyol fiyat güncellemesi başarısız; fiyat DEĞİŞMEDİ")
    except Exception as exc:  # noqa: BLE001 — beklenmeyen hata da FAILED (asla EXECUTED değil)
        return _finish(conn, p, "FAILED", actual=None, error=f"{exc.__class__.__name__}: {str(exc)[:300]}", result=audit,
                       user=user, ip=ip, reason="Beklenmeyen hata; fiyat değişmedi")
    conn.execute(text("UPDATE products SET sale_price = :n, updated_at = NOW() WHERE id = :i"), {"n": new, "i": prod["id"]})
    log_audit(conn, actor=user.username, user_id=user.id, action="price.changed", entity_type="product", entity_id=prod["id"],
              ip=ip, details={**audit, "proposal_id": p["id"]})
    return _finish(conn, p, "EXECUTED", actual=f"Trendyol fiyatı {tl(old)} → {tl(new)}", error=None, result=audit, user=user,
                   ip=ip, reason="Pazaryeri fiyatı güncellendi")


def skip(conn: Connection, *, agent: str, action_type: str, entity_type: str, entity_id: int, reason: str,
         reason_code: str, input_data: dict | None = None, cycle_id: int | None = None) -> None:
    """Uygulanmayan/önerilmeyen aksiyon (ör. döngü limiti). Aynı gün aynı varlık için tek satır."""
    log(conn, agent=agent, action_type=action_type, status="SKIPPED", reason=reason, reason_code=reason_code,
        entity_type=entity_type, entity_id=entity_id, input_data=input_data, cycle_id=cycle_id,
        dedupe_key=f"skip:{agent}:{action_type}:{entity_type}:{entity_id}:{config.today()}")


def block(conn: Connection, *, agent: str, action_type: str, entity_type: str, entity_id: int, reason: str,
          reason_code: str, input_data: dict | None = None, expected_impact: dict | None = None,
          cycle_id: int | None = None) -> None:
    """Öneri bile oluşturulmadan engellenen aksiyon (ör. maliyet bilinmiyor → fiyat değişikliği NEEDS_DATA)."""
    log(conn, agent=agent, action_type=action_type, status="BLOCKED", reason=reason, reason_code=reason_code,
        entity_type=entity_type, entity_id=entity_id, input_data=input_data, expected_impact=expected_impact,
        cycle_id=cycle_id, dedupe_key=f"block:{agent}:{action_type}:{entity_type}:{entity_id}:{config.today()}")


def money(v) -> Decimal:
    return d(v).quantize(Decimal("0.01"))
