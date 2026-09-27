"""Sipariş finansının hesaplanıp veritabanına yazılması.

Kaynak önceliği (her bileşen için):
  1. `financial_transactions` (pazaryeri hakedişi / manuel düzeltme) - gerçek
  2. Sipariş/sevkiyat üzerindeki bilinen tutarlar (ör. shipments.cost)
  3. Ayarlardaki tahmini oran/tutar  -> finance_is_estimate = TRUE

Reklam ve diğer giderler yalnızca `financial_transactions` üzerinden girilir
(order_item_id doluysa kaleme, boşsa siparişe ait kabul edilir ve kalemlere
ciro payına göre dağıtılır). orders/order_items üzerindeki tutar kolonları
hesaplanmış sonuçtur.

Kalemi olmayan eski (legacy) siparişlerin toplamlarına DOKUNULMAZ.
"""
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..domain import order_status as S
from ..domain.finance import ZERO, Breakdown, LineInput, OrderCosts, compute_order, d, money
from . import app_settings


def _unit_cost_at(conn: Connection, product_id: int | None, at) -> Decimal | None:
    if product_id is None:
        return None
    c = conn.execute(text("""
        SELECT cost FROM product_costs
         WHERE product_id = :p AND (CAST(:at AS TIMESTAMPTZ) IS NULL OR valid_from <= :at)
         ORDER BY valid_from DESC LIMIT 1
    """), {"p": product_id, "at": at}).scalar()
    if c is not None:
        return Decimal(c)
    c = conn.execute(text("SELECT cost FROM products WHERE id = :p"), {"p": product_id}).scalar()
    return Decimal(c) if c is not None else None


def recalculate_order(conn: Connection, order_id: int) -> Breakdown | None:
    order = conn.execute(text("""
        SELECT o.id, o.internal_status, o.advertising_cost, o.other_cost, o.order_date, m.code AS marketplace
          FROM orders o
          LEFT JOIN stores s ON s.id = o.store_id
          LEFT JOIN marketplaces m ON m.id = s.marketplace_id
         WHERE o.id = :id
    """), {"id": order_id}).mappings().first()
    if order is None:
        return None
    items = conn.execute(text("""
        SELECT id, quantity, unit_price, unit_cost, product_id
          FROM order_items WHERE order_id = :id ORDER BY id
    """), {"id": order_id}).mappings().all()
    if not items:
        return None  # legacy / kalemsiz sipariş: mevcut toplamlar korunur

    tx = conn.execute(text("""
        SELECT order_item_id, kind, SUM(amount) AS amount
          FROM financial_transactions WHERE order_id = :id
         GROUP BY order_item_id, kind
    """), {"id": order_id}).mappings().all()
    item_tx: dict[tuple[int, str], Decimal] = {}
    order_tx: dict[str, Decimal] = {}
    for t in tx:
        if t["order_item_id"] is None:
            order_tx[t["kind"]] = order_tx.get(t["kind"], ZERO) + Decimal(t["amount"])
        else:
            item_tx[(t["order_item_id"], t["kind"])] = Decimal(t["amount"])

    status = order["internal_status"]
    estimate = False

    shipping = order_tx.get("shipping")
    if shipping is None:
        known = conn.execute(text("""
            SELECT COUNT(*) FILTER (WHERE cost IS NOT NULL) AS known, COALESCE(SUM(cost), 0) AS total, COUNT(*) AS n
              FROM shipments WHERE order_id = :id
        """), {"id": order_id}).mappings().first()
        if known["known"]:
            shipping = Decimal(known["total"])
        else:
            shipping = app_settings.get_decimal(conn, "finance.default_shipping_cost")
            estimate = True
    service_fee = order_tx.get("service_fee")
    if service_fee is None:
        service_fee = app_settings.get_decimal(conn, "finance.service_fee_per_order")
        estimate = True
    commission_rate = app_settings.get_decimal(conn, f"finance.commission_rate.{order['marketplace']}")
    returned = status == S.RETURNED
    # İadede: ciro iade tutarıyla sıfırlanır, pazaryeri komisyonu iade eder
    # (hakedişte gerçek komisyon varsa o kullanılır), ürün varsayılan olarak
    # stoğa döner. Kargo/hizmet bedeli gibi giderler kalır.
    return_cost_is_loss = bool(app_settings.get(conn, "finance.return_product_cost_is_loss", False))

    lines: list[LineInput] = []
    missing_cost = False
    for it in items:
        unit_cost = it["unit_cost"]
        if unit_cost is None or Decimal(unit_cost) == 0:
            snap = _unit_cost_at(conn, it["product_id"], order["order_date"])
            if snap is not None:
                unit_cost = snap
                conn.execute(text("UPDATE order_items SET unit_cost = :c WHERE id = :id"),
                             {"c": snap, "id": it["id"]})
            else:
                missing_cost = True
        revenue = money(d(it["unit_price"]) * it["quantity"])
        refund = item_tx.get((it["id"], "refund"))
        if refund is None:
            refund = revenue if status == S.RETURNED else ZERO
        lines.append(LineInput(
            quantity=it["quantity"],
            unit_price=d(it["unit_price"]),
            unit_cost=ZERO if returned and not return_cost_is_loss else d(unit_cost),
            commission=item_tx.get((it["id"], "commission")),
            commission_rate=ZERO if returned else commission_rate,
            refund_amount=refund,
            advertising=item_tx.get((it["id"], "advertising"), ZERO),
            other=item_tx.get((it["id"], "other"), ZERO),
        ))

    if status == S.CANCELLED:
        # İptal edilen siparişte ne ciro ne gider oluşur.
        result_lines = [Breakdown() for _ in lines]
        is_estimate = False
    else:
        # Kaleme atanmış reklam/diğer giderler LineInput'ta; sipariş
        # seviyesinde girilenler ciro payına göre kalemlere dağıtılır.
        of = compute_order(lines, OrderCosts(
            shipping=shipping, service_fee=service_fee,
            advertising=d(order_tx.get("advertising", ZERO)),
            other=d(order_tx.get("other", ZERO)),
        ), commission_rate)
        result_lines = of.lines
        is_estimate = estimate or of.is_estimate or missing_cost

    for it, b in zip(items, result_lines):
        conn.execute(text("""
            UPDATE order_items SET commission = :commission, commission_rate = :rate,
                   service_fee = :service_fee, shipping_cost = :shipping,
                   advertising_cost = :advertising, other_cost = :other,
                   refund_amount = :refund, finance_is_estimate = :est
             WHERE id = :id
        """), {"commission": b.commission, "rate": commission_rate, "service_fee": b.service_fee,
               "shipping": b.shipping, "advertising": b.advertising, "other": b.other,
               "refund": b.refund, "est": is_estimate, "id": it["id"]})

    total = Breakdown()
    for b in result_lines:
        total = total + b
    conn.execute(text("""
        UPDATE orders SET gross_revenue = :revenue, product_cost = :product_cost,
               commission = :commission, service_fee = :service_fee, shipping_cost = :shipping,
               advertising_cost = :advertising, other_cost = :other, refund_cost = :refund,
               net_profit = :net, finance_is_estimate = :est, updated_at = NOW()
         WHERE id = :id
    """), {"revenue": total.revenue, "product_cost": total.product_cost,
           "commission": total.commission, "service_fee": total.service_fee,
           "shipping": total.shipping, "advertising": total.advertising, "other": total.other,
           "refund": total.refund, "net": total.net_profit, "est": is_estimate, "id": order_id})
    return total
