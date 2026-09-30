import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..db import get_conn, row, rows
from ..deps import CurrentUser, client_ip, operator, viewer
from ..domain import order_status as S
from ..domain.finance import COMPONENT_LABELS_TR, ZERO
from ..services import finance_view, shipping_plan
from ..services.audit import log_audit
from ..services.finance_service import recalculate_order
from .common import TZ, Page, not_found, paged

router = APIRouter(prefix="/api/orders", tags=["orders"])

SORTS = {"date_desc": "o.order_date DESC NULLS LAST, o.id DESC", "date_asc": "o.order_date ASC NULLS LAST, o.id",
         "profit_desc": "o.net_profit DESC NULLS LAST", "profit_asc": "o.net_profit ASC NULLS LAST",
         "revenue_desc": "o.gross_revenue DESC NULLS LAST"}


@router.get("/meta")
def meta(_: CurrentUser = Depends(viewer)):
    return {
        "statuses": [{"code": c, "label": S.LABELS_TR[c]} for c in S.ALL_STATUSES],
        "manual_transitions": {k: [c for c in S.ALL_STATUSES if c in v] for k, v in S.MANUAL_TRANSITIONS.items()},
        "finance_components": COMPONENT_LABELS_TR,
    }


@router.get("")
def list_orders(page: Page = Depends(), status: str | None = None, marketplace: str | None = None,
                q: str | None = Query(None, max_length=100), date_from: date | None = None,
                date_to: date | None = None, sort: str = "date_desc", loss_only: bool = False,
                _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    where, params = ["TRUE"], {}
    if status:
        if status not in S.LABELS_TR:
            raise HTTPException(422, "Geçersiz statü")
        where.append("o.internal_status = :status"); params["status"] = status
    if marketplace:
        where.append("m.code = :mp"); params["mp"] = marketplace
    if q:
        where.append("""(o.external_order_id ILIKE :q OR o.customer_name ILIKE :q OR EXISTS (
            SELECT 1 FROM order_items i WHERE i.order_id = o.id AND (i.sku ILIKE :q OR i.barcode ILIKE :q
            OR i.product_name ILIKE :q)))""")
        params["q"] = f"%{q.strip()}%"
    if date_from:
        where.append("o.order_date >= :df")
        params["df"] = datetime.combine(date_from, time.min, TZ).astimezone(timezone.utc)
    if date_to:
        where.append("o.order_date < :dt")
        params["dt"] = datetime.combine(date_to + timedelta(days=1), time.min, TZ).astimezone(timezone.utc)
    if loss_only:
        where.append("o.net_profit < 0")
    order_by = SORTS.get(sort, SORTS["date_desc"])
    base = f"""FROM orders o LEFT JOIN stores s ON s.id = o.store_id
               LEFT JOIN marketplaces m ON m.id = s.marketplace_id WHERE {' AND '.join(where)}"""
    total = conn.execute(text(f"SELECT COUNT(*) {base}"), params).scalar()
    items = rows(conn, f"""
        SELECT o.id, o.external_order_id, o.status AS marketplace_status, o.internal_status,
               o.order_date, o.customer_name, o.customer_city, o.gross_revenue, o.net_profit,
               o.finance_is_estimate, o.review_reason, m.code AS marketplace, m.name AS marketplace_name,
               (SELECT COALESCE(SUM(quantity), 0) FROM order_items i WHERE i.order_id = o.id) AS item_count,
               (SELECT string_agg(DISTINCT sh.carrier, ', ') FROM shipments sh WHERE sh.order_id = o.id) AS carrier
        {base} ORDER BY {order_by} LIMIT :limit OFFSET :offset
    """, **params, limit=page.page_size, offset=page.offset)
    fin = finance_view.order_metrics(conn, [it["id"] for it in items])
    for it in items:
        it["status_label"] = S.LABELS_TR.get(it["internal_status"], it["internal_status"])
        _apply_finance(it, fin.get(it["id"]))
    shipping_plan.attach(conn, items)
    return paged(items, total, page)


def _apply_finance(o: dict, f: dict | None) -> None:
    """Sipariş satırına tek kaynaktan finans alanlarını ekler (iptalde kâr/KDV 0)."""
    f = f or {}
    cancelled = o.get("internal_status") == S.CANCELLED
    z = lambda v: ZERO if cancelled else v  # noqa: E731
    o["net_sales"] = z(f.get("net_sales"))
    o["profit_before_vat"] = z(f.get("profit_before_vat"))
    o["vat_estimate"] = z(f.get("vat_estimate"))
    o["profit_after_vat"] = z(f.get("profit_after_vat"))
    for k in ("margin_before_vat", "margin_after_vat", "markup_before_vat", "markup_after_vat"):
        o[k] = None if cancelled else f.get(k)
    # Geriye uyumlu alanlar: net_profit = KDV öncesi kâr; margin = KDV öncesi net marj (net satış paydası)
    o["net_profit"] = o["profit_before_vat"]
    o["tax_estimate"] = o["vat_estimate"]
    o["net_profit_after_tax"] = o["profit_after_vat"]
    o["margin"] = o["margin_before_vat"]
    o["vat_known"] = f.get("vat_estimate") is not None


@router.get("/{order_id}")
def get_order(order_id: int, user: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    o = row(conn, """
        SELECT o.*, m.code AS marketplace, m.name AS marketplace_name, s.name AS store_name
          FROM orders o LEFT JOIN stores s ON s.id = o.store_id
          LEFT JOIN marketplaces m ON m.id = s.marketplace_id WHERE o.id = :id
    """, id=order_id)
    if o is None:
        raise not_found("Sipariş")
    o.pop("payload_hash", None)
    o["status_label"] = S.LABELS_TR.get(o["internal_status"], o["internal_status"])
    o["allowed_transitions"] = [{"code": c, "label": S.LABELS_TR[c]}
                                for c in S.ALL_STATUSES if c in S.MANUAL_TRANSITIONS.get(o["internal_status"], ())]
    o["items"] = rows(conn, """SELECT i.*, p.vat_rate AS product_vat_rate FROM order_items i
                                LEFT JOIN products p ON p.id = i.product_id WHERE i.order_id = :id ORDER BY i.id""",
                      id=order_id)
    # Kalem ve sipariş finansı TEK kaynaktan (finance_view): liste, dashboard ve raporlarla aynı sayılar
    cfg = finance_view.load(conn)
    im = finance_view.item_metrics(conn, order_id, cfg)
    cancelled = o["internal_status"] == S.CANCELLED
    for it in o["items"]:
        rate = it["vat_rate"] if it["vat_rate"] is not None else it.get("product_vat_rate")
        it.pop("product_vat_rate", None)
        it["vat_rate"] = rate
        f = im.get(it["id"], {})
        for k in ("revenue", "net_sales", "product_cost", "profit_before_vat", "vat_estimate", "profit_after_vat",
                  "margin_before_vat", "margin_after_vat", "markup_before_vat", "markup_after_vat"):
            it[k] = None if cancelled and k != "revenue" else f.get(k)
        it["commission_incl_vat"] = f.get("commission")
        # Geriye uyumlu alan adları
        it["net_profit"] = ZERO if cancelled else f.get("profit_before_vat")
        it["tax_estimate"] = ZERO if cancelled else f.get("vat_estimate")
        it["net_profit_after_tax"] = ZERO if cancelled else f.get("profit_after_vat")
        it["margin"] = None if cancelled else f.get("margin_before_vat")
    _apply_finance(o, finance_view.order_metrics(conn, [order_id], cfg).get(order_id))
    o["discount"] = sum((Decimal(it["discount"] or 0) for it in o["items"]), ZERO)
    o["finance_config"] = cfg.as_dict()
    o["shipments"] = rows(conn, "SELECT * FROM shipments WHERE order_id = :id ORDER BY id", id=order_id)
    o["history"] = rows(conn, """
        SELECT h.*, u.username FROM order_status_history h LEFT JOIN users u ON u.id = h.user_id
         WHERE h.order_id = :id ORDER BY h.changed_at, h.id
    """, id=order_id)
    for h in o["history"]:
        h["to_label"] = S.LABELS_TR.get(h["to_status"], h["to_status"])
        h["from_label"] = S.LABELS_TR.get(h["from_status"], h["from_status"]) if h["from_status"] else None
    shipping_plan.attach(conn, [o])
    o["supplier_orders"] = rows(conn, """
        SELECT so.*, sp.name AS supplier_name FROM supplier_orders so
          LEFT JOIN suppliers sp ON sp.id = so.supplier_id WHERE so.order_id = :id ORDER BY so.id
    """, id=order_id)
    o["transactions"] = rows(conn, """SELECT id, order_item_id, source, kind, amount, occurred_at, description
                                      FROM financial_transactions WHERE order_id = :id ORDER BY occurred_at, id""",
                             id=order_id)
    rev = o["gross_revenue"] or 0
    o["margin"] = float(o["net_profit"] / rev) if rev else None
    from .storefront_admin import order_details
    o["storefront"] = order_details(conn, order_id, user)
    return o


class StatusChangeIn(BaseModel):
    status: str
    note: str | None = Field(None, max_length=500)


@router.post("/{order_id}/status")
def change_status(order_id: int, body: StatusChangeIn, request: Request,
                  user: CurrentUser = Depends(operator), conn: Connection = Depends(get_conn)):
    cur = conn.execute(text("SELECT internal_status FROM orders WHERE id = :id FOR UPDATE"), {"id": order_id}).scalar()
    if cur is None:
        raise not_found("Sipariş")
    try:
        S.check_manual_transition(cur, body.status)
    except S.InvalidTransition as exc:
        raise HTTPException(409, str(exc)) from None
    conn.execute(text("""
        UPDATE orders SET internal_status = :s, updated_at = NOW(),
               review_reason = CASE WHEN :s = 'needs_review' THEN COALESCE(:note, review_reason) ELSE NULL END
         WHERE id = :id
    """), {"s": body.status, "id": order_id, "note": body.note})
    conn.execute(text("""
        INSERT INTO order_status_history(order_id, from_status, to_status, source, note, user_id)
        VALUES (:o, :f, :t, 'manual', :n, :u)
    """), {"o": order_id, "f": cur, "t": body.status, "n": body.note, "u": user.id})
    recalculate_order(conn, order_id)
    log_audit(conn, actor=user.username, user_id=user.id, action="order.status_changed", entity_type="order",
              entity_id=order_id, ip=client_ip(request), details={"from": cur, "to": body.status, "note": body.note})
    return {"ok": True, "status": body.status, "label": S.LABELS_TR[body.status]}


class AdjustmentIn(BaseModel):
    kind: Literal["commission", "service_fee", "shipping", "advertising", "refund", "other"]
    amount: Decimal = Field(ge=Decimal("-1000000"), le=Decimal("1000000"))
    order_item_id: int | None = None
    description: str | None = Field(None, max_length=300)


@router.post("/{order_id}/adjustments")
def add_adjustment(order_id: int, body: AdjustmentIn, request: Request,
                   user: CurrentUser = Depends(operator), conn: Connection = Depends(get_conn)):
    """Gerçek gider bilgisini manuel girer (ör. hakediş ekstresinden komisyon).
    Girilen bileşen için tahmini değer yerine bu tutar kullanılır."""
    o = row(conn, "SELECT id, store_id FROM orders WHERE id = :id", id=order_id)
    if o is None:
        raise not_found("Sipariş")
    if body.order_item_id is not None and not conn.execute(
            text("SELECT 1 FROM order_items WHERE id = :i AND order_id = :o"),
            {"i": body.order_item_id, "o": order_id}).first():
        raise HTTPException(422, "Kalem bu siparişe ait değil")
    tx_id = conn.execute(text("""
        INSERT INTO financial_transactions(store_id, order_id, order_item_id, source, external_ref, kind,
                                           amount, occurred_at, description)
        VALUES (:s, :o, :i, 'manual', :ref, :k, :a, NOW(), :d) RETURNING id
    """), {"s": o["store_id"], "o": order_id, "i": body.order_item_id, "ref": str(uuid.uuid4()),
           "k": body.kind, "a": body.amount, "d": body.description}).scalar()
    total = recalculate_order(conn, order_id)
    log_audit(conn, actor=user.username, user_id=user.id, action="order.finance_adjusted", entity_type="order",
              entity_id=order_id, ip=client_ip(request),
              details={"kind": body.kind, "amount": str(body.amount), "item": body.order_item_id})
    return {"ok": True, "transaction_id": tx_id, "recalculated": total is not None}
