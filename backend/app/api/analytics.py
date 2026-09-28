"""Dashboard, Finans ve Raporlar."""
import csv
import io
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..connectors.registry import all_connectors
from ..db import get_conn, row, rows
from ..deps import CurrentUser, client_ip, operator, viewer
from ..domain import order_status as S
from ..services import jobs
from ..services.audit import log_audit
from .common import DateRange, Page, not_found, paged
from .integrations import integration_state

router = APIRouter(tags=["analytics"])

LOCAL_DAY = "(o.order_date AT TIME ZONE 'Europe/Istanbul')::date"
EXPENSE_CATEGORIES = {"advertising": "Reklam", "shipping": "Kargo", "packaging": "Ambalaj",
                      "personnel": "Personel", "rent": "Kira", "software": "Yazılım", "other": "Diğer"}


def _num(v) -> Decimal:
    """Para değerleri Decimal olarak taşınır; JSON'a yazılırken sayıya çevrilir."""
    return Decimal(v) if v is not None else Decimal("0")


def _ratio(a: Decimal, b: Decimal) -> Decimal | None:
    return (a / b).quantize(Decimal("0.0001")) if b else None


# Kalem bazlı TAHMİNİ KDV: (net satış − maliyet) içindeki KDV. İptal ve iade edilen
# siparişlerde satış tersine döndüğü için 0 kabul edilir. Oran: kalem > ürün > %20.
TAX_SQL = """COALESCE(SUM(CASE WHEN o.internal_status = 'returned' THEN 0 ELSE
               ((i.unit_price * i.quantity) - COALESCE(i.refund_amount, 0) - COALESCE(i.unit_cost, 0) * i.quantity)
               * COALESCE(i.vat_rate, p.vat_rate, 20) / (100 + COALESCE(i.vat_rate, p.vat_rate, 20)) END), 0)"""


def order_totals(conn: Connection, rng: DateRange, marketplace: str | None = None) -> dict:
    extra = "AND m.code = :mp" if marketplace else ""
    r = row(conn, f"""
        SELECT COUNT(*) AS orders,
               COALESCE(SUM(o.gross_revenue), 0) AS revenue,
               COALESCE(SUM(o.product_cost), 0) AS product_cost,
               COALESCE(SUM(o.commission), 0) AS commission,
               COALESCE(SUM(o.service_fee), 0) AS service_fee,
               COALESCE(SUM(o.shipping_cost), 0) AS shipping,
               COALESCE(SUM(o.advertising_cost), 0) AS advertising,
               COALESCE(SUM(o.refund_cost), 0) AS refund,
               COALESCE(SUM(o.other_cost), 0) AS other,
               COALESCE(SUM(o.net_profit), 0) AS net_profit,
               COUNT(*) FILTER (WHERE o.finance_is_estimate) AS estimated_orders
          FROM orders o LEFT JOIN stores s ON s.id = o.store_id LEFT JOIN marketplaces m ON m.id = s.marketplace_id
         WHERE o.order_date >= :start AND o.order_date < :end AND o.internal_status <> 'cancelled' {extra}
    """, **rng.params(), mp=marketplace)
    out = {k: (_num(v) if k not in ("orders", "estimated_orders") else int(v)) for k, v in r.items()}
    out["total_cost"] = sum((out[k] for k in ("product_cost", "commission", "service_fee", "shipping",
                                              "advertising", "refund", "other")), Decimal("0"))
    r2 = row(conn, f"""
        SELECT COALESCE(SUM(i.discount), 0) AS discount, {TAX_SQL} AS tax_estimate
          FROM order_items i JOIN orders o ON o.id = i.order_id LEFT JOIN products p ON p.id = i.product_id
          LEFT JOIN stores s ON s.id = o.store_id LEFT JOIN marketplaces m ON m.id = s.marketplace_id
         WHERE o.order_date >= :start AND o.order_date < :end AND o.internal_status <> 'cancelled' {extra}
    """, **rng.params(), mp=marketplace)
    out["discount"] = _num(r2["discount"]).quantize(Decimal("0.01"))
    out["tax_estimate"] = _num(r2["tax_estimate"]).quantize(Decimal("0.01"))
    out["average_order_value"] = (out["revenue"] / out["orders"]).quantize(Decimal("0.01")) if out["orders"] else None
    return out


def period_expenses(conn: Connection, rng: DateRange) -> dict:
    """Siparişe bağlı olmayan dönem giderleri (expenses tablosu)."""
    items = rows(conn, """
        SELECT COALESCE(category, 'other') AS category, COALESCE(SUM(amount), 0) AS amount
          FROM expenses WHERE expense_date >= :start_date AND expense_date <= :end_date
         GROUP BY 1 ORDER BY 2 DESC
    """, **rng.params())
    return {"total": sum((_num(i["amount"]) for i in items), Decimal("0")),
            "by_category": [{"category": i["category"], "label": EXPENSE_CATEGORIES.get(i["category"], i["category"]),
                             "amount": _num(i["amount"])} for i in items]}


def summary(conn: Connection, rng: DateRange) -> dict:
    t = order_totals(conn, rng)
    exp = period_expenses(conn, rng)
    net_after = t["net_profit"] - exp["total"]
    net_after_tax = net_after - t["tax_estimate"]
    return {"orders": t, "expenses": exp, "net_profit_after_expenses": net_after,
            "tax_estimate": t["tax_estimate"], "net_profit_after_tax": net_after_tax,
            "margin": _ratio(t["net_profit"], t["revenue"]),
            "margin_after_expenses": _ratio(net_after, t["revenue"]),
            "margin_after_tax": _ratio(net_after_tax, t["revenue"]),
            # Gerçek hakediş verisi bağlanmadığı sürece tüm finans rakamları tahminidir.
            "is_estimate": True}


def daily_series(conn: Connection, rng: DateRange) -> list[dict]:
    return [{"day": r["day"].isoformat(), "orders": int(r["orders"]), "revenue": _num(r["revenue"]),
             "net_profit": _num(r["net_profit"])} for r in rows(conn, f"""
        SELECT d::date AS day, COUNT(o.id) AS orders, COALESCE(SUM(o.gross_revenue), 0) AS revenue,
               COALESCE(SUM(o.net_profit), 0) AS net_profit
          FROM generate_series(CAST(:start_date AS DATE), CAST(:end_date AS DATE), INTERVAL '1 day') d
          LEFT JOIN orders o ON {LOCAL_DAY} = d::date AND o.internal_status <> 'cancelled'
         GROUP BY d ORDER BY d
    """, **rng.params())]


# ---------------------------------------------------------------- dashboard
@router.get("/api/dashboard")
def dashboard(rng: DateRange = Depends(), _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    status_counts = {r["s"]: int(r["n"]) for r in rows(conn, "SELECT internal_status AS s, COUNT(*) AS n FROM orders GROUP BY 1")}
    period_status = {r["s"]: int(r["n"]) for r in rows(conn, """
        SELECT internal_status AS s, COUNT(*) AS n FROM orders
         WHERE order_date >= :start AND order_date < :end GROUP BY 1""", **rng.params())}
    recent = rows(conn, """
        SELECT o.id, o.external_order_id, o.internal_status, o.order_date, o.gross_revenue, o.net_profit,
               m.name AS marketplace_name, m.code AS marketplace
          FROM orders o LEFT JOIN stores s ON s.id = o.store_id LEFT JOIN marketplaces m ON m.id = s.marketplace_id
         ORDER BY o.order_date DESC NULLS LAST, o.id DESC LIMIT 8
    """)
    for r in recent:
        r["status_label"] = S.LABELS_TR.get(r["internal_status"], r["internal_status"])
    alerts = row(conn, """
        SELECT (SELECT COUNT(*) FROM orders WHERE internal_status = 'needs_review') AS needs_review,
               (SELECT COUNT(DISTINCT i.order_id) FROM order_items i JOIN orders o ON o.id = i.order_id
                 WHERE COALESCE(i.unit_cost, 0) = 0 AND o.internal_status <> 'cancelled') AS missing_cost_orders,
               (SELECT COUNT(*) FROM system_events WHERE resolved_at IS NULL AND level IN ('error','critical')) AS open_errors,
               (SELECT COUNT(*) FROM worker_heartbeats WHERE last_seen_at > NOW() - INTERVAL '90 seconds') AS live_workers
    """)
    mp_rows = {r["code"]: r for r in rows(conn, "SELECT * FROM marketplaces")}
    today = order_totals(conn, DateRange(period="today", date_from=None, date_to=None))
    pending = sum(status_counts.get(c, 0) for c in (S.NEW, S.PREPARING, S.SENT_TO_SUPPLIER, S.AWAITING_SHIPMENT))
    top_products = rows(conn, """
        SELECT COALESCE(i.sku, i.barcode, '(SKU yok)') AS sku, MAX(i.product_name) AS product_name,
               SUM(i.quantity) AS quantity, SUM(i.unit_price * i.quantity) AS revenue
          FROM order_items i JOIN orders o ON o.id = i.order_id
         WHERE o.order_date >= :start AND o.order_date < :end AND o.internal_status NOT IN ('cancelled', 'returned')
         GROUP BY 1 ORDER BY revenue DESC NULLS LAST LIMIT 5
    """, **rng.params())
    by_marketplace = rows(conn, """
        SELECT m.code, m.name, COUNT(o.id) AS orders, COALESCE(SUM(o.gross_revenue), 0) AS revenue,
               COALESCE(SUM(o.net_profit), 0) AS net_profit
          FROM marketplaces m
          LEFT JOIN stores s ON s.marketplace_id = m.id
          LEFT JOIN orders o ON o.store_id = s.id AND o.order_date >= :start AND o.order_date < :end
                             AND o.internal_status <> 'cancelled'
         GROUP BY m.id, m.code, m.name ORDER BY m.id
    """, **rng.params())
    return {
        "range": rng.as_dict(),
        "summary": summary(conn, rng),
        "today": {"orders": today["orders"], "revenue": today["revenue"], "net_profit": today["net_profit"]},
        "pending_orders": pending,
        "returns": {"period": period_status.get(S.RETURNED, 0), "open_total": status_counts.get(S.RETURNED, 0)},
        "top_products": top_products,
        "by_marketplace": by_marketplace,
        "statuses": [{"code": c, "label": S.LABELS_TR[c], "count": status_counts.get(c, 0),
                      "period_count": period_status.get(c, 0)} for c in S.ALL_STATUSES],
        "daily": daily_series(conn, rng),
        "recent_orders": recent,
        "alerts": alerts,
        "integrations": [integration_state(c, mp_rows.get(c.code)) for c in all_connectors()],
        "queue": jobs.queue_stats(conn),
    }


# ------------------------------------------------------------------- finans
@router.get("/api/finance/summary")
def finance_summary(rng: DateRange = Depends(), _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    by_mp = []
    for m in rows(conn, "SELECT code, name FROM marketplaces ORDER BY id"):
        t = order_totals(conn, rng, m["code"])
        by_mp.append({"code": m["code"], "name": m["name"], **t, "margin": _ratio(t["net_profit"], t["revenue"])})
    return {"range": rng.as_dict(), **summary(conn, rng), "by_marketplace": by_mp,
            "daily": daily_series(conn, rng)}


@router.get("/api/finance/expenses")
def list_expenses(rng: DateRange = Depends(), page: Page = Depends(), _: CurrentUser = Depends(viewer),
                  conn: Connection = Depends(get_conn)):
    base = "FROM expenses e LEFT JOIN marketplaces m ON m.id = e.marketplace_id WHERE e.expense_date >= :start_date AND e.expense_date <= :end_date"
    total = conn.execute(text(f"SELECT COUNT(*) {base}"), rng.params()).scalar()
    items = rows(conn, f"""SELECT e.id, e.category, e.description, e.amount, e.expense_date, e.sku, e.source,
                                  m.name AS marketplace_name {base}
                            ORDER BY e.expense_date DESC, e.id DESC LIMIT :limit OFFSET :offset""",
                 **rng.params(), limit=page.page_size, offset=page.offset)
    for i in items:
        i["category_label"] = EXPENSE_CATEGORIES.get(i["category"], i["category"])
    return {**paged(items, total, page), "categories": EXPENSE_CATEGORIES}


class ExpenseIn(BaseModel):
    category: str
    amount: Decimal = Field(gt=0, le=Decimal("100000000"))
    expense_date: date
    description: str | None = Field(None, max_length=500)
    marketplace: str | None = None
    sku: str | None = Field(None, max_length=100)


@router.post("/api/finance/expenses", status_code=201)
def create_expense(body: ExpenseIn, request: Request, user: CurrentUser = Depends(operator),
                   conn: Connection = Depends(get_conn)):
    if body.category not in EXPENSE_CATEGORIES:
        raise HTTPException(422, "Geçersiz gider kategorisi")
    mp_id = None
    if body.marketplace:
        mp_id = conn.execute(text("SELECT id FROM marketplaces WHERE code = :c"), {"c": body.marketplace}).scalar()
        if mp_id is None:
            raise HTTPException(422, "Geçersiz pazaryeri")
    eid = conn.execute(text("""
        INSERT INTO expenses(category, description, amount, expense_date, marketplace_id, sku, created_by, source)
        VALUES (:c, :d, :a, :dt, :mp, :sku, :u, 'manual') RETURNING id
    """), {"c": body.category, "d": body.description, "a": body.amount, "dt": body.expense_date,
           "mp": mp_id, "sku": body.sku, "u": user.id}).scalar()
    log_audit(conn, actor=user.username, user_id=user.id, action="expense.created", entity_type="expense",
              entity_id=eid, ip=client_ip(request), details=body.model_dump(mode="json"))
    return {"id": eid}


@router.delete("/api/finance/expenses/{expense_id}")
def delete_expense(expense_id: int, request: Request, user: CurrentUser = Depends(operator),
                   conn: Connection = Depends(get_conn)):
    """Yalnızca panelden manuel girilmiş giderler silinebilir; kayıt audit log'da kalır."""
    e = row(conn, "SELECT * FROM expenses WHERE id = :id", id=expense_id)
    if e is None:
        raise not_found("Gider")
    if e.get("source") != "manual" or e.get("created_by") is None:
        raise HTTPException(409, "Yalnızca panelden girilmiş giderler silinebilir")
    conn.execute(text("DELETE FROM expenses WHERE id = :id"), {"id": expense_id})
    log_audit(conn, actor=user.username, user_id=user.id, action="expense.deleted", entity_type="expense",
              entity_id=expense_id, ip=client_ip(request), details=e)
    return {"ok": True}


# ------------------------------------------------------------------ raporlar
def sku_report(conn: Connection, rng: DateRange, marketplace: str | None = None) -> list[dict]:
    from ..services import app_settings
    extra = "AND m.code = :mp" if marketplace else ""
    ret_loss = bool(app_settings.get(conn, "finance.return_product_cost_is_loss", False))
    items = rows(conn, f"""
        SELECT COALESCE(i.sku, i.barcode, '(SKU yok)') AS sku, MAX(i.product_name) AS product_name,
               SUM(i.quantity) AS quantity, COUNT(DISTINCT o.id) AS orders,
               COUNT(DISTINCT o.id) FILTER (WHERE o.internal_status = 'returned') AS returns,
               SUM(i.unit_price * i.quantity) AS revenue,
               -- İade edilen ürün varsayılan olarak stoğa döner (sipariş hesabıyla aynı kural)
               SUM(CASE WHEN o.internal_status = 'returned' AND NOT :ret_loss THEN 0
                        ELSE COALESCE(i.unit_cost, 0) * i.quantity END) AS product_cost,
               SUM(COALESCE(i.discount, 0)) AS discount, {TAX_SQL} AS tax_estimate,
               SUM(COALESCE(i.commission, 0)) AS commission, SUM(COALESCE(i.service_fee, 0)) AS service_fee,
               SUM(COALESCE(i.shipping_cost, 0)) AS shipping, SUM(COALESCE(i.advertising_cost, 0)) AS advertising,
               SUM(COALESCE(i.refund_amount, 0)) AS refund, SUM(COALESCE(i.other_cost, 0)) AS other,
               BOOL_OR(COALESCE(i.unit_cost, 0) = 0) AS missing_cost,
               BOOL_OR(i.finance_is_estimate) AS is_estimate
          FROM order_items i JOIN orders o ON o.id = i.order_id LEFT JOIN products p ON p.id = i.product_id
          LEFT JOIN stores s ON s.id = o.store_id LEFT JOIN marketplaces m ON m.id = s.marketplace_id
         WHERE o.order_date >= :start AND o.order_date < :end AND o.internal_status <> 'cancelled' {extra}
         GROUP BY 1
    """, **rng.params(), mp=marketplace, ret_loss=ret_loss)
    # SKU'ya doğrudan girilmiş dönem reklam giderleri (expenses.sku)
    direct_ads = {r["sku"]: _num(r["amount"]) for r in rows(conn, """
        SELECT sku, SUM(amount) AS amount FROM expenses
         WHERE sku IS NOT NULL AND category = 'advertising' AND expense_date >= :start_date AND expense_date <= :end_date
         GROUP BY sku""", **rng.params())}
    out = []
    for it in items:
        r = {k: (_num(v) if isinstance(v, Decimal) else v) for k, v in it.items()}
        r["advertising"] = r["advertising"] + direct_ads.get(r["sku"], Decimal("0"))
        r["tax_estimate"] = r["tax_estimate"].quantize(Decimal("0.01"))
        cost = sum((r[k] for k in ("product_cost", "commission", "service_fee", "shipping", "advertising",
                                   "refund", "other")), Decimal("0"))
        r["net_profit"] = (r["revenue"] - cost).quantize(Decimal("0.01"))
        r["net_profit_after_tax"] = r["net_profit"] - r["tax_estimate"]
        r["margin"] = _ratio(r["net_profit"], r["revenue"])
        r["return_rate"] = _ratio(Decimal(r["returns"]), Decimal(r["orders"])) if r["orders"] else None
        out.append(r)
    out.sort(key=lambda x: x["net_profit"], reverse=True)
    return out


@router.get("/api/reports/sku")
def report_sku(rng: DateRange = Depends(), marketplace: str | None = None, _: CurrentUser = Depends(viewer),
               conn: Connection = Depends(get_conn)):
    return {"range": rng.as_dict(), "items": sku_report(conn, rng, marketplace)}


CSV_COLUMNS = [("sku", "SKU"), ("product_name", "Ürün"), ("quantity", "Adet"), ("orders", "Sipariş"),
               ("returns", "İade"), ("revenue", "Ciro"), ("product_cost", "Ürün Maliyeti"),
               ("commission", "Komisyon"), ("service_fee", "Hizmet Bedeli"), ("shipping", "Kargo"),
               ("advertising", "Reklam"), ("refund", "İade Tutarı"), ("other", "Diğer"),
               ("discount", "Satıcı İndirimi (ciroya dahil)"), ("net_profit", "Net Kâr (vergi öncesi)"),
               ("tax_estimate", "Tahmini KDV"), ("net_profit_after_tax", "Net Kâr (tahmini KDV sonrası)"),
               ("margin", "Kâr Marjı"), ("is_estimate", "Tahmini")]


def _csv_safe(v):
    # Excel formül enjeksiyonuna karşı
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
        return "'" + v
    return v


@router.get("/api/reports/sku.csv")
def report_sku_csv(rng: DateRange = Depends(), marketplace: str | None = None, _: CurrentUser = Depends(viewer),
                   conn: Connection = Depends(get_conn)):
    buf = io.StringIO()
    buf.write("﻿")  # Excel'de Türkçe karakterler için BOM
    w = csv.writer(buf, delimiter=";")
    w.writerow([h for _, h in CSV_COLUMNS])
    for r in sku_report(conn, rng, marketplace):
        w.writerow([_csv_safe(r.get(k)) for k, _ in CSV_COLUMNS])
    name = f"trendhub-sku-{rng.start_date}-{rng.end_date}.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/api/reports/statuses")
def report_statuses(rng: DateRange = Depends(), _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    data = rows(conn, f"""
        SELECT m.name AS marketplace, o.internal_status AS status, COUNT(*) AS count
          FROM orders o LEFT JOIN stores s ON s.id = o.store_id LEFT JOIN marketplaces m ON m.id = s.marketplace_id
         WHERE o.order_date >= :start AND o.order_date < :end GROUP BY 1, 2 ORDER BY 1, 2
    """, **rng.params())
    for d in data:
        d["label"] = S.LABELS_TR.get(d["status"], d["status"])
    return {"range": rng.as_dict(), "items": data}


@router.get("/api/reports/top-loss")
def report_loss_orders(rng: DateRange = Depends(), limit: int = Query(20, le=100), _: CurrentUser = Depends(viewer),
                       conn: Connection = Depends(get_conn)):
    return rows(conn, """
        SELECT o.id, o.external_order_id, o.order_date, o.gross_revenue, o.net_profit, o.internal_status
          FROM orders o WHERE o.order_date >= :start AND o.order_date < :end AND o.net_profit < 0
         ORDER BY o.net_profit ASC LIMIT :limit
    """, **rng.params(), limit=limit)
