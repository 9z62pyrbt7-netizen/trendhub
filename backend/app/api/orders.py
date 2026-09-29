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
from ..domain.finance import COMPONENT_LABELS_TR, ZERO, estimated_vat_payable, money
from ..services import app_settings, shipping_plan
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
    for it in items:
        it["status_label"] = S.LABELS_TR.get(it["internal_status"], it["internal_status"])
        rev = it["gross_revenue"] or 0
        it["margin"] = float(it["net_profit"] / rev) if rev else None
    shipping_plan.attach(conn, items)
    return paged(items, total, page)


@router.get("/{order_id}")
def get_order(order_id: int, _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
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
    status = o["internal_status"]
    restocked = status == S.RETURNED and not app_settings.get(conn, "finance.return_product_cost_is_loss", False)
    tax_total = discount_total = ZERO
    for it in o["items"]:
        qty = it["quantity"] or 0
        revenue = money(Decimal(it["unit_price"] or 0) * qty)
        product_cost = ZERO if restocked else money(Decimal(it["unit_cost"] or 0) * qty)
        costs = product_cost + sum((Decimal(it[k] or 0) for k in ("commission", "service_fee", "shipping_cost",
                                                                   "advertising_cost", "refund_amount", "other_cost")), ZERO)
        rate = it["vat_rate"] if it["vat_rate"] is not None else it.pop("product_vat_rate", None)
        it.pop("product_vat_rate", None)
        tax = ZERO if status in (S.CANCELLED, S.RETURNED) else estimated_vat_payable(
            revenue, product_cost, it["refund_amount"] or 0, rate)
        it["revenue"] = revenue
        it["product_cost"] = product_cost
        it["net_profit"] = ZERO if status == S.CANCELLED else revenue - costs
        it["tax_estimate"] = tax
        it["net_profit_after_tax"] = it["net_profit"] - tax
        it["margin"] = float(it["net_profit"] / revenue) if revenue and status != S.CANCELLED else None
        tax_total += tax
        discount_total += Decimal(it["discount"] or 0)
    o["tax_estimate"] = tax_total
    o["discount"] = discount_total
    o["net_profit_after_tax"] = (o["net_profit"] or ZERO) - tax_total
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
