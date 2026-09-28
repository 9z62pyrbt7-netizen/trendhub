"""Uyarılar / Sorunlar merkezi ve bildirim zili."""
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.engine import Connection

from ..db import get_conn, row, rows
from ..deps import CurrentUser, client_ip, operator, viewer
from ..services import alerts as alert_svc
from ..services.audit import log_audit
from ..services.notifications import channels_status
from .common import Page, not_found, paged

router = APIRouter(prefix="/api/alerts", tags=["alerts"])

SEVERITY_ORDER = "CASE a.severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END"


@router.get("")
def list_alerts(status: str = Query("open", pattern="^(open|resolved|all)$"),
                severity: str | None = Query(None, pattern="^(critical|warning|info)$"),
                category: str | None = Query(None, pattern="^(supplier|marketplace|order|shipping|product|system)$"),
                q: str | None = Query(None, max_length=100), product_id: int | None = None,
                supplier_id: int | None = None, order_id: int | None = None,
                page: Page = Depends(), _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    where, params = ["TRUE"], {"lim": page.page_size, "off": page.offset}
    if status != "all":
        where.append("a.status = :status")
        params["status"] = status
    for key, val in (("severity", severity), ("category", category), ("product_id", product_id),
                     ("supplier_id", supplier_id), ("order_id", order_id)):
        if val is not None:
            where.append(f"a.{key} = :{key}")
            params[key] = val
    if q:
        where.append("(a.title ILIKE :q OR a.description ILIKE :q)")
        params["q"] = f"%{q}%"
    w = " AND ".join(where)
    total = row(conn, f"SELECT COUNT(*) AS n FROM alerts a WHERE {w}", **params)["n"]
    items = rows(conn, f"""
        SELECT a.id, a.category, a.severity, a.code, a.title, a.description, a.source, a.status, a.link,
               a.product_id, a.supplier_id, a.supplier_product_id, a.marketplace_id, a.order_id, a.details,
               a.occurrences, a.first_detected_at, a.last_detected_at, a.last_checked_at, a.resolved_at,
               a.resolution, p.name AS product_name, s.name AS supplier_name, m.name AS marketplace_name,
               o.external_order_id, u.username AS resolved_by_name
          FROM alerts a
          LEFT JOIN products p ON p.id = a.product_id LEFT JOIN suppliers s ON s.id = a.supplier_id
          LEFT JOIN marketplaces m ON m.id = a.marketplace_id LEFT JOIN orders o ON o.id = a.order_id
          LEFT JOIN users u ON u.id = a.resolved_by
         WHERE {w}
         ORDER BY (a.status = 'open') DESC, {SEVERITY_ORDER}, a.last_detected_at DESC, a.id DESC
         LIMIT :lim OFFSET :off
    """, **params)
    for it in items:
        it["severity_label"] = alert_svc.SEVERITY_TR[it["severity"]]
        it["category_label"] = alert_svc.CATEGORY_TR[it["category"]]
    return paged(items, total, page)


@router.get("/summary")
def summary(_: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    s = alert_svc.summary(conn)
    by_cat = {r["category"]: r["n"] for r in rows(conn, """SELECT category, COUNT(*) AS n FROM alerts
                                                            WHERE status = 'open' GROUP BY category""")}
    last = row(conn, "SELECT MAX(last_checked_at) AS t FROM alerts")
    return {**s, "by_category": by_cat, "last_checked_at": last["t"] if last else None,
            "channels": channels_status(conn)}


class ResolveIn(BaseModel):
    note: str | None = Field(None, max_length=300)


@router.post("/{alert_id}/resolve")
def resolve(alert_id: int, body: ResolveIn, request: Request, user: CurrentUser = Depends(operator),
            conn: Connection = Depends(get_conn)):
    a = row(conn, "SELECT id, code, title, status FROM alerts WHERE id = :id", id=alert_id)
    if a is None:
        raise not_found("Uyarı")
    if a["status"] != "open":
        return {"ok": True, "already_resolved": True}
    alert_svc.resolve(conn, alert_id, user.id, body.note)
    log_audit(conn, actor=user.username, user_id=user.id, action="alert.resolved", entity_type="alert",
              entity_id=alert_id, ip=client_ip(request), details={"code": a["code"], "title": a["title"]})
    return {"ok": True}


@router.post("/scan")
def scan_now(request: Request, user: CurrentUser = Depends(operator), conn: Connection = Depends(get_conn)):
    """Uyarıları şimdi tarar (yalnızca veritabanını okur; dış sisteme istek GÖNDERMEZ)."""
    result = alert_svc.scan(conn)
    log_audit(conn, actor=user.username, user_id=user.id, action="alerts.scanned", ip=client_ip(request),
              details=result)
    return result
