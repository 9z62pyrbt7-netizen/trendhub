"""Bütçe Yöneticisi (Budget Governor): CEO ve ajanlar limitsiz para harcayamaz.

Sahip işletme bütçesini tanımlar (ör. 50.000 TL). Para gerektiren her öneri risk motorunda buradan geçer:
    total_budget              sahibin AI'ya ayırdığı toplam bütçe (tanımsızsa: sermaye motorunun kullanılabilir kasası)
    reserved_budget           onaylanmış ama henüz uygulanmamış harcamalar (rezervasyon)
    committed                 uygulanmış harcamalar
    available_budget          = total − reserved − committed
    daily_limit / weekly_limit  son 1 / 7 günde onaylanan (reserved+committed) toplam üst sınır
    per_agent_limit           tek ajanın 7 günlük toplamı
    per_campaign_limit        tek kampanya/ürünün 7 günlük toplamı
    max_single_action_amount  tek işlemin üst sınırı
    max_daily_ad_spend        aktif günlük reklam bütçeleri + açık artışlar (thresholds.daily_ad_budget_limit ile AYNI değer)

Sınır aşılırsa öneri BLOCKED olur (risk motoru bloğu; sahip onayı bile aşamaz).
Rezervasyon defteri (ai_budget_ledger): onayda reserved → uygulamada committed → ret/başarısız/süresi dolmuşta released.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ...db import row, rows
from .. import app_settings, finance_view
from .config import d, thresholds, tl

KEY = "ai.budget"
DEFAULTS = {"total_budget": None, "daily_limit": 20000, "weekly_limit": 50000, "per_agent_limit": 20000,
            "per_campaign_limit": 5000, "max_single_action_amount": 20000}
FIELDS_TR = {"total_budget": "Toplam bütçe", "daily_limit": "Günlük limit", "weekly_limit": "Haftalık limit",
             "per_agent_limit": "Ajan başına (7 gün)", "per_campaign_limit": "Kampanya/ürün başına (7 gün)",
             "max_single_action_amount": "Tek işlem üst sınırı", "max_daily_ad_spend": "Günlük reklam harcaması üst sınırı"}


def config(conn: Connection) -> dict:
    raw = app_settings.get(conn, KEY, {}) or {}
    out = dict(DEFAULTS)
    for k in DEFAULTS:
        v = raw.get(k)
        if v is None or (isinstance(v, (int, float)) and not isinstance(v, bool)):
            out[k] = v if k in raw else out[k]
    out["max_daily_ad_spend"] = thresholds(conn)["daily_ad_budget_limit"]
    return out


def validate(values: dict) -> dict:
    out = {}
    for k, v in values.items():
        if k not in DEFAULTS and k != "max_daily_ad_spend":
            raise ValueError(f"Bilinmeyen bütçe alanı: {k}")
        if v is None and k == "total_budget":
            out[k] = None
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or v > 100_000_000:
            raise ValueError(f"{FIELDS_TR.get(k, k)}: 0 veya pozitif sayı olmalı")
        out[k] = v
    return out


def save(conn: Connection, values: dict, user_id: int | None) -> dict:
    vals = validate(values)
    if "max_daily_ad_spend" in vals:
        th = dict(app_settings.get(conn, "ai.thresholds", {}) or {})
        th["daily_ad_budget_limit"] = vals.pop("max_daily_ad_spend")
        app_settings.set_value(conn, "ai.thresholds", th, user_id)
    cur = dict(app_settings.get(conn, KEY, {}) or {})
    cur.update(vals)
    app_settings.set_value(conn, KEY, cur, user_id)
    return config(conn)


def campaign_key(p: dict) -> str | None:
    if p.get("entity_type") in ("campaign", "product") and p.get("entity_id") is not None:
        return f"{p['entity_type']}:{p['entity_id']}"
    return None


def _used(conn: Connection, where: str = "", exclude: int | None = None, **params) -> Decimal:
    return d(conn.execute(text(f"""SELECT COALESCE(SUM(amount), 0) FROM ai_budget_ledger
                                   WHERE status IN ('reserved', 'committed') AND (CAST(:ex AS BIGINT) IS NULL OR proposal_id <> :ex)
                                   {where}"""), {"ex": exclude, **params}).scalar())


def status(conn: Connection, exclude_proposal: int | None = None) -> dict:
    from .capital import position
    cfg = config(conn)
    pos = position(conn)
    total_src = "owner" if cfg["total_budget"] is not None else ("capital_usable" if pos["usable"] is not None else None)
    total = d(cfg["total_budget"]) if cfg["total_budget"] is not None else (d(pos["usable"]) if pos["usable"] is not None else None)
    reserved = d(conn.execute(text("SELECT COALESCE(SUM(amount), 0) FROM ai_budget_ledger WHERE status = 'reserved'")).scalar())
    committed = d(conn.execute(text("SELECT COALESCE(SUM(amount), 0) FROM ai_budget_ledger WHERE status = 'committed'")).scalar())
    if exclude_proposal is not None:
        mine = row(conn, "SELECT status, amount FROM ai_budget_ledger WHERE proposal_id = :p AND status <> 'released'",
                   p=exclude_proposal)
        if mine:
            if mine["status"] == "reserved":
                reserved -= d(mine["amount"])
            else:
                committed -= d(mine["amount"])
    q = finance_view.q2
    from .proposals import ad_budget_usage
    return {"config": cfg, "total_budget": q(total) if total is not None else None, "total_source": total_src,
            "reserved_budget": q(reserved), "committed": q(committed),
            "available_budget": q(total - reserved - committed) if total is not None else None,
            "spent_today": q(_used(conn, "AND created_at > NOW() - INTERVAL '1 day'", exclude_proposal)),
            "spent_7d": q(_used(conn, "AND created_at > NOW() - INTERVAL '7 days'", exclude_proposal)),
            "ad_budget": ad_budget_usage(conn, exclude_proposal),
            "by_agent": rows(conn, """SELECT agent_code, SUM(amount) AS amount FROM ai_budget_ledger
                                        WHERE status IN ('reserved', 'committed') AND created_at > NOW() - INTERVAL '7 days'
                                        GROUP BY 1 ORDER BY 2 DESC""")}


def check(conn: Connection, p: dict) -> list[dict]:
    """Para gerektiren öneri için sınır kontrolleri. Dönen her kayıt risk motoru kontrolü biçimindedir."""
    from .proposals import _check
    amount = d(p.get("required_capital"))
    if amount <= 0:
        return []
    out: list[dict] = []
    pid = p.get("id")
    st = status(conn, exclude_proposal=pid)
    cfg = st["config"]
    if cfg["max_single_action_amount"] is not None and amount > d(cfg["max_single_action_amount"]):
        out.append(_check("governor_single_action", "block",
                          f"Bütçe Yöneticisi: tek işlem {tl(amount)} > tek işlem üst sınırı {tl(cfg['max_single_action_amount'])}."))
    if st["total_budget"] is None:
        out.append(_check("governor_no_budget", "block",
                          "Bütçe Yöneticisi: toplam bütçe tanımlı değil ve kasa bilgisi yok; harcanacak para doğrulanamıyor."))
    elif amount > st["available_budget"]:
        out.append(_check("governor_total", "block",
                          f"Bütçe Yöneticisi: kalan bütçe {tl(st['available_budget'])} (toplam {tl(st['total_budget'])}, ayrılmış "
                          f"{tl(st['reserved_budget'])}, harcanmış {tl(st['committed'])}); istenen {tl(amount)}.",
                          available=st["available_budget"]))
    if cfg["daily_limit"] is not None and st["spent_today"] + amount > d(cfg["daily_limit"]):
        out.append(_check("governor_daily", "block",
                          f"Bütçe Yöneticisi: günlük limit {tl(cfg['daily_limit'])} aşılır (bugün onaylanan {tl(st['spent_today'])} + {tl(amount)})."))
    if cfg["weekly_limit"] is not None and st["spent_7d"] + amount > d(cfg["weekly_limit"]):
        out.append(_check("governor_weekly", "block",
                          f"Bütçe Yöneticisi: haftalık limit {tl(cfg['weekly_limit'])} aşılır (7 günde onaylanan {tl(st['spent_7d'])} + {tl(amount)})."))
    if cfg["per_agent_limit"] is not None:
        used = _used(conn, "AND agent_code = :a AND created_at > NOW() - INTERVAL '7 days'", pid, a=p["agent_code"])
        if used + amount > d(cfg["per_agent_limit"]):
            out.append(_check("governor_agent", "block",
                              f"Bütçe Yöneticisi: {p['agent_code']} ajanının 7 günlük limiti {tl(cfg['per_agent_limit'])} aşılır "
                              f"(kullanılan {tl(used)} + {tl(amount)})."))
    key = campaign_key(p)
    if key and cfg["per_campaign_limit"] is not None:
        used = _used(conn, "AND campaign_key = :k AND created_at > NOW() - INTERVAL '7 days'", pid, k=key)
        if used + amount > d(cfg["per_campaign_limit"]):
            out.append(_check("governor_campaign", "block",
                              f"Bütçe Yöneticisi: kampanya/ürün başına 7 günlük limit {tl(cfg['per_campaign_limit'])} aşılır "
                              f"(kullanılan {tl(used)} + {tl(amount)})."))
    return out


def reserve(conn: Connection, p: dict) -> None:
    amount = d(p.get("required_capital"))
    if amount <= 0:
        return
    conn.execute(text("""INSERT INTO ai_budget_ledger(proposal_id, agent_code, category, campaign_key, amount, status)
                         VALUES (:p, :a, :c, :k, :m, 'reserved')
                         ON CONFLICT (proposal_id) DO UPDATE SET status = 'reserved', amount = EXCLUDED.amount, updated_at = NOW()
                          WHERE ai_budget_ledger.status = 'released'"""),
                 {"p": p["id"], "a": p["agent_code"], "c": p.get("capital_category"), "k": campaign_key(p), "m": amount})


def commit(conn: Connection, pid: int) -> None:
    conn.execute(text("UPDATE ai_budget_ledger SET status = 'committed', updated_at = NOW() WHERE proposal_id = :p AND status = 'reserved'"),
                 {"p": pid})


def release_stale(conn: Connection) -> int:
    """Uygulanmadan kapanan önerilerin rezervasyonunu serbest bırakır (ret, başarısız, süresi dolmuş, yenisiyle değişmiş)."""
    return conn.execute(text("""UPDATE ai_budget_ledger l SET status = 'released', updated_at = NOW(),
                                       note = 'öneri durumu: ' || p.status
                                  FROM ai_proposals p WHERE p.id = l.proposal_id AND l.status = 'reserved'
                                   AND p.status IN ('rejected', 'failed', 'expired', 'superseded', 'blocked')""")).rowcount
