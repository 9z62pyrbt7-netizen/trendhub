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
from ..deps import CurrentUser, client_ip, finance_editor, operator, viewer
from ..domain import order_status as S
from ..services import app_settings, finance_view, jobs
from ..services.audit import log_audit
from .suppliers import supplier_overview
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


# Tüm finans rakamları TEK kaynaktan: services/finance_view (sipariş, dashboard, finans, rapor aynı sayıyı verir)
def order_totals(conn: Connection, rng: DateRange, marketplace: str | None = None) -> dict:
    t = finance_view.totals(conn, rng.start, rng.end, marketplace)
    out = {k: t[k] for k in ("orders", "estimated_orders", "orders_without_items", "missing_cost_orders", "revenue",
                             "refund", "net_sales", "product_cost", "commission", "service_fee", "shipping",
                             "advertising", "other", "discount", "margin_before_vat", "margin_after_vat",
                             "markup_before_vat", "markup_after_vat", "vat_complete")}
    # Geriye uyumlu alan adları: net_profit = KDV öncesi kâr, tax_estimate = tahmini KDV
    out["net_profit"] = t["profit_before_vat"]
    out["tax_estimate"] = t["vat_estimate"]
    out["net_profit_after_tax"] = t["profit_after_vat"]
    out["margin"] = t["margin_before_vat"]
    out["total_cost"] = sum((out[k] for k in ("product_cost", "commission", "service_fee", "shipping",
                                              "advertising", "refund", "other")), Decimal("0"))
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
    """Dönem özeti. Dashboard, Finans ve Finans tablosu AYNI fonksiyonu kullanır.

    net_profit_after_expenses = KDV öncesi sipariş kârı − dönem giderleri − reklam harcaması (Reklamlar)
    net_profit_after_tax      = yukarıdaki − tahmini KDV   (ana gösterge: "Tahmini KDV sonrası kâr")
    Marjların paydası net satıştır (ciro − iade)."""
    from .ads import spend_total
    t = order_totals(conn, rng)
    exp = period_expenses(conn, rng)
    ads = spend_total(conn, rng)
    net_after = t["net_profit"] - exp["total"] - ads
    net_after_tax = net_after - t["tax_estimate"]
    cfg = finance_view.load(conn)
    return {"orders": t, "expenses": exp, "ad_spend": ads, "net_sales": t["net_sales"],
            "net_profit_after_expenses": net_after, "tax_estimate": t["tax_estimate"],
            "net_profit_after_tax": net_after_tax,
            "margin": finance_view.ratio(t["net_profit"], t["net_sales"]),
            "margin_after_expenses": finance_view.ratio(net_after, t["net_sales"]),
            "margin_after_tax": finance_view.ratio(net_after_tax, t["net_sales"]),
            "markup_after_tax": finance_view.ratio(net_after_tax, t["product_cost"]),
            "vat": {**cfg.as_dict(), "orders_without_items": t["orders_without_items"],
                    "complete": t["vat_complete"]},
            # Gerçek hakediş verisi bağlanmadığı sürece tüm finans rakamları tahminidir.
            "is_estimate": True}


def daily_series(conn: Connection, rng: DateRange) -> list[dict]:
    cfg = finance_view.load(conn)
    where = "o.order_date >= :start AND o.order_date < :end AND o.internal_status <> 'cancelled'"
    return [{"day": r["day"].isoformat(), "orders": int(r["orders"]), "revenue": _num(r["revenue"]),
             "net_profit": _num(r["net_profit"]), "net_profit_after_tax": _num(r["after"])} for r in rows(conn, f"""
        SELECT d::date AS day, COUNT(x.id) AS orders, COALESCE(SUM(x.revenue), 0) AS revenue,
               COALESCE(SUM(x.profit_before_vat), 0) AS net_profit, COALESCE(SUM(x.profit_after_vat), 0) AS after
          FROM generate_series(CAST(:start_date AS DATE), CAST(:end_date AS DATE), INTERVAL '1 day') d
          LEFT JOIN ({finance_view.order_from(cfg, where)}) x
                 ON (x.order_date AT TIME ZONE 'Europe/Istanbul')::date = d::date
         GROUP BY d ORDER BY d
    """, **rng.params(), **cfg.params())]


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
    fin = finance_view.order_metrics(conn, [r["id"] for r in recent])
    for r in recent:
        r["status_label"] = S.LABELS_TR.get(r["internal_status"], r["internal_status"])
        f = fin.get(r["id"], {})
        r["net_profit"] = f.get("profit_before_vat")
        r["net_profit_after_tax"] = f.get("profit_after_vat")
        r["margin"] = f.get("margin_before_vat")
        r["margin_after_tax"] = f.get("margin_after_vat")
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
    by_marketplace = []
    for m in rows(conn, "SELECT code, name FROM marketplaces ORDER BY id"):
        t = order_totals(conn, rng, m["code"])
        by_marketplace.append({"code": m["code"], "name": m["name"], "orders": t["orders"], "revenue": t["revenue"],
                               "net_profit": t["net_profit"], "net_profit_after_tax": t["net_profit_after_tax"]})
    from .ads import spend_total
    from ..services.alerts import summary as alert_summary
    today_rng = DateRange(period="today", date_from=None, date_to=None)
    cfg = finance_view.load(conn)
    # En kârlı ürünler: KDV sonrası tahmini kâr (tek finans kaynağı); maliyeti eksik ürünler hariç
    most_profitable = rows(conn, f"""
        SELECT sku, MAX(product_name) AS product_name, SUM(quantity) AS quantity, SUM(profit_after_vat) AS profit,
               SUM(profit_before_vat) AS profit_before_vat
          FROM (SELECT COALESCE(i.sku, i.barcode, '(SKU yok)') AS sku, i.product_name, i.quantity, i.unit_cost,
                       {finance_view.item_columns(cfg)}
                  FROM order_items i JOIN orders o ON o.id = i.order_id LEFT JOIN products p ON p.id = i.product_id
                 WHERE o.order_date >= :start AND o.order_date < :end
                   AND o.internal_status NOT IN ('cancelled', 'returned')) x
         GROUP BY sku HAVING BOOL_AND(COALESCE(unit_cost, 0) > 0) ORDER BY profit DESC NULLS LAST LIMIT 5
    """, **rng.params(), **cfg.params())
    threshold = int(app_settings.get(conn, "alerts.critical_stock_threshold", 2))
    critical_stock = rows(conn, """
        SELECT p.id, p.sku, p.name, p.stock FROM products p
         WHERE p.is_active AND COALESCE(p.stock, 0) <= :th
           AND EXISTS (SELECT 1 FROM marketplace_listings l WHERE l.product_id = p.id
                        OR (p.barcode IS NOT NULL AND p.barcode <> '' AND l.barcode = p.barcode))
         ORDER BY p.stock NULLS FIRST, p.name LIMIT 10""", th=threshold)
    return {
        "range": rng.as_dict(),
        "summary": summary(conn, rng),
        "today": {"orders": today["orders"], "revenue": today["revenue"], "net_profit": today["net_profit"],
                  "net_profit_after_tax": today["net_profit_after_tax"], "ad_spend": spend_total(conn, today_rng)},
        "ad_spend": spend_total(conn, rng),
        "alert_summary": alert_summary(conn),
        "most_profitable": most_profitable,
        "critical_stock": critical_stock,
        "critical_stock_threshold": threshold,
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
        "suppliers": [{k: sp[k] for k in ("id", "code", "name", "is_active", "integration_type", "product_count",
                                         "active_count", "missing_count", "in_stock_count", "out_of_stock_count",
                                         "linked_count", "last_sync_at", "last_sync_status", "last_sync_error", "health")}
                      for sp in supplier_overview(conn)],
    }


# ------------------------------------------------------------------- finans
@router.get("/api/finance/summary")
def finance_summary(rng: DateRange = Depends(), _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    by_mp = []
    for m in rows(conn, "SELECT code, name FROM marketplaces ORDER BY id"):
        t = order_totals(conn, rng, m["code"])
        by_mp.append({"code": m["code"], "name": m["name"], **t})
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
def create_expense(body: ExpenseIn, request: Request, user: CurrentUser = Depends(finance_editor),
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
def delete_expense(expense_id: int, request: Request, user: CurrentUser = Depends(finance_editor),
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
    """SKU kârlılığı: kalem kolonları finance_view.item_columns'tan (sipariş ekranıyla aynı formül)."""
    cfg = finance_view.load(conn)
    extra = "AND m.code = :mp" if marketplace else ""
    items = rows(conn, f"""
        SELECT sku, MAX(product_name) AS product_name, SUM(quantity) AS quantity, COUNT(DISTINCT order_id) AS orders,
               COUNT(DISTINCT order_id) FILTER (WHERE internal_status = 'returned') AS returns,
               SUM(revenue) AS revenue, SUM(refund) AS refund, SUM(net_sales) AS net_sales,
               SUM(product_cost) AS product_cost, SUM(discount) AS discount, SUM(commission) AS commission,
               SUM(service_fee) AS service_fee, SUM(shipping) AS shipping, SUM(advertising) AS advertising,
               SUM(other) AS other, SUM(profit_before_vat) AS profit_before_vat, SUM(vat_estimate) AS vat_estimate,
               BOOL_OR(COALESCE(unit_cost, 0) = 0) AS missing_cost, BOOL_OR(finance_is_estimate) AS is_estimate
          FROM (SELECT COALESCE(i.sku, i.barcode, '(SKU yok)') AS sku, i.product_name, i.quantity, i.order_id,
                       o.internal_status, i.unit_cost, i.finance_is_estimate, {finance_view.item_columns(cfg)}
                  FROM order_items i JOIN orders o ON o.id = i.order_id LEFT JOIN products p ON p.id = i.product_id
                  LEFT JOIN stores s ON s.id = o.store_id LEFT JOIN marketplaces m ON m.id = s.marketplace_id
                 WHERE o.order_date >= :start AND o.order_date < :end AND o.internal_status <> 'cancelled' {extra}) x
         GROUP BY sku
    """, **rng.params(), mp=marketplace, **cfg.params())
    # Reklam payı: SKU'ya doğrudan girilmiş reklam giderleri + kampanya harcamasının eşit bölüşümü (tek kaynak:
    # finance_view; Reklam merkezi ve AI Control Center ile aynı ürün kârı)
    direct_ads = {k: _num(v) for k, v in finance_view.sku_ad_expenses(conn, rng.start_date, rng.end_date).items()}
    camp = finance_view.campaign_ad_allocation(conn, rng.start_date, rng.end_date)
    if camp:
        for r in rows(conn, "SELECT id, sku FROM products WHERE id = ANY(:ids) AND sku IS NOT NULL", ids=list(camp)):
            direct_ads[r["sku"]] = direct_ads.get(r["sku"], Decimal("0")) + camp[r["id"]]
    out = []
    for it in items:
        r = {k: (_num(v) if isinstance(v, Decimal) else v) for k, v in it.items()}
        extra_ads = direct_ads.get(r["sku"], Decimal("0"))
        r["advertising"] = r["advertising"] + extra_ads
        r["profit_before_vat"] = r["profit_before_vat"] - extra_ads
        r["profit_after_vat"] = r["profit_before_vat"] - r["vat_estimate"]
        finance_view.decorate(r)
        # Geriye uyumlu alan adları
        r["net_profit"], r["tax_estimate"] = r["profit_before_vat"], r["vat_estimate"]
        r["net_profit_after_tax"], r["margin"] = r["profit_after_vat"], r["margin_before_vat"]
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
    cfg = finance_view.load(conn)
    where = "o.order_date >= :start AND o.order_date < :end"
    out = rows(conn, f"""
        SELECT x.id, x.external_order_id, x.order_date, x.revenue AS gross_revenue, x.profit_before_vat AS net_profit,
               x.profit_after_vat AS net_profit_after_tax, x.internal_status
          FROM ({finance_view.order_from(cfg, where)}) x WHERE x.profit_before_vat < 0
         ORDER BY x.profit_before_vat ASC LIMIT :limit
    """, **rng.params(), **cfg.params(), limit=limit)
    for r in out:
        for k in ("gross_revenue", "net_profit", "net_profit_after_tax"):
            r[k] = finance_view.q2(r[k])
    return out


# ------------------------------------------------ finans tablosu (gerçek / tahmini)
BASIS_TR = {"actual": "Gerçek", "estimate": "Tahmini", "entered": "Girilen", "mixed": "Kısmen tahmini", "info": "Bilgi"}


def statement(conn: Connection, rng: DateRange) -> dict:
    """Dönem finans tablosu — summary() ile AYNI kaynak (Dashboard ve Finans kartlarıyla birebir tutar).

    Satıcı indirimi ciroya zaten yansımıştır (birim fiyat indirim sonrasıdır); yalnızca BİLGİ satırıdır,
    tekrar düşülmez. Net satış = ciro − iade. Her satır kaynağına göre etiketlenir:
    Gerçek = pazaryerinden gelen tutar / hakediş, Tahmini = TrendHub hesabı, Girilen = kullanıcı kaydı."""
    sm = summary(conn, rng)
    t, exp, ads = sm["orders"], sm["expenses"], sm["ad_spend"]
    n, est = t["orders"], t["estimated_orders"]
    fee_basis = "actual" if n and not est else "estimate" if est == n else "mixed"
    net_sales = t["net_sales"]
    contribution = net_sales - t["product_cost"] - t["commission"] - t["service_fee"] - t["shipping"]
    other_exp = sum((e["amount"] for e in exp["by_category"] if e["category"] != "advertising"), Decimal("0"))
    adv_exp = exp["total"] - other_exp
    net = sm["net_profit_after_expenses"]   # tek kaynak: kartlarla tablo aynı sayı
    lines = [
        ("gross_sales", "Brüt satış (indirim sonrası fiyatla)", t["revenue"], "actual", True),
        ("discounts", "Satıcı indirimi (bilgi — ciroya yansımış, tekrar düşülmez)", t["discount"], "info", False),
        ("refunds", "İadeler", -t["refund"], "actual", True),
        ("net_sales", "Net satış", net_sales, "actual", True),
        ("product_cost", "Ürün maliyeti", -t["product_cost"], "entered", True),
        ("commission", "Komisyon" + (" (KDV dahil)" if sm["vat"]["commission_vat_mode"] == "excluded" else ""),
         -t["commission"], fee_basis, True),
        ("service_fee", "Hizmet bedeli", -t["service_fee"], fee_basis, True),
        ("shipping", "Kargo", -t["shipping"], fee_basis, True),
        ("contribution", "Katkı payı", contribution, "estimate" if fee_basis != "actual" else "actual", True),
        ("marketplace_ads", "Pazaryeri reklam kesintisi (sipariş bazlı)", -t["advertising"], fee_basis, True),
        ("ad_spend", "Reklam harcaması (Reklamlar)", -ads, "entered", True),
        ("ad_expenses", "Reklam gideri (Giderler)", -adv_exp, "entered", True),
        ("other_order_costs", "Diğer sipariş giderleri", -t["other"], fee_basis, True),
        ("other_expenses", "Diğer giderler", -other_exp, "entered", True),
        ("net_profit", "KDV öncesi kâr", net, "estimate", True),
        ("vat_estimate", "Tahmini ödenecek KDV", -sm["tax_estimate"], "estimate", True),
        ("net_after_vat", "Tahmini KDV sonrası kâr", sm["net_profit_after_tax"], "estimate", True),
    ]
    warnings = list(sm["vat"]["notes"])
    if t["orders_without_items"]:
        warnings.append(f"{t['orders_without_items']} eski siparişte kalem bilgisi yok: KDV'leri hesaplanamadı.")
    if ads and (t["advertising"] or adv_exp):
        warnings.append("Reklam harcaması birden fazla kaynakta var (Reklamlar, Giderler, pazaryeri kesintisi). "
                        "Aynı harcamayı iki yere girmediğinizden emin olun.")
    if est:
        warnings.append(f"{est} siparişte komisyon/kargo/hizmet bedeli tahmini (pazaryeri hakedişi henüz gelmedi).")
    if t["missing_cost_orders"]:
        warnings.append(f"{t['missing_cost_orders']} siparişte ürün maliyeti eksik: kâr olduğundan yüksek görünür.")
    if not n:
        warnings.append("Bu dönemde sipariş yok.")
    return {"range": rng.as_dict(), "orders": n, "estimated_orders": est,
            "lines": [{"key": k, "label": l, "amount": v.quantize(Decimal("0.01")), "basis": b,
                       "basis_label": BASIS_TR[b], "in_total": tot} for k, l, v, b, tot in lines],
            "margin": sm["margin_after_expenses"], "margin_after_tax": sm["margin_after_tax"],
            "contribution_margin": _ratio(contribution, net_sales), "markup_after_tax": sm["markup_after_tax"],
            "vat": sm["vat"], "warnings": warnings}


@router.get("/api/finance/statement")
def finance_statement(rng: DateRange = Depends(), _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    return statement(conn, rng)


def _csv_response(name: str, header: list[str], data: list[list]) -> StreamingResponse:
    buf = io.StringIO()
    buf.write("﻿")  # Excel'de Türkçe karakterler için BOM
    w = csv.writer(buf, delimiter=";")
    w.writerow(header)
    for r in data:
        w.writerow([_csv_safe(v) for v in r])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/api/finance/statement.csv")
def finance_statement_csv(rng: DateRange = Depends(), _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    s = statement(conn, rng)
    return _csv_response(f"trendhub-finans-{rng.start_date}-{rng.end_date}.csv", ["Kalem", "Tutar", "Kaynak"],
                         [[x["label"], x["amount"], x["basis_label"]] for x in s["lines"]])


@router.get("/api/finance/expenses.csv")
def expenses_csv(rng: DateRange = Depends(), _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    items = rows(conn, """SELECT e.expense_date, e.category, e.description, e.amount, e.sku, m.name AS marketplace_name, e.source
                            FROM expenses e LEFT JOIN marketplaces m ON m.id = e.marketplace_id
                           WHERE e.expense_date >= :start_date AND e.expense_date <= :end_date ORDER BY e.expense_date, e.id""",
                 **rng.params())
    return _csv_response(f"trendhub-giderler-{rng.start_date}-{rng.end_date}.csv",
                         ["Tarih", "Kategori", "Açıklama", "Tutar", "SKU", "Pazaryeri", "Kaynak"],
                         [[i["expense_date"], EXPENSE_CATEGORIES.get(i["category"], i["category"]), i["description"],
                           i["amount"], i["sku"], i["marketplace_name"], i["source"]] for i in items])


@router.get("/api/finance/orders.csv")
def orders_csv(rng: DateRange = Depends(), _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    cfg = finance_view.load(conn)
    items = rows(conn, f"""SELECT x.*, (x.order_date AT TIME ZONE 'Europe/Istanbul') AS order_date_tr
                             FROM ({finance_view.order_from(cfg, "o.order_date >= :start AND o.order_date < :end")}) x
                            ORDER BY x.order_date, x.id""", **rng.params(), **cfg.params())
    items = [finance_view.decorate(i) for i in items]
    pct = lambda v: None if v is None else (v * 100).quantize(Decimal("0.01"))  # noqa: E731
    return _csv_response(f"trendhub-siparisler-{rng.start_date}-{rng.end_date}.csv",
                         ["Sipariş no", "Tarih (TR)", "Pazaryeri", "Durum", "Ciro", "İade", "Net satış", "Ürün maliyeti",
                          "Komisyon", "Hizmet bedeli", "Kargo", "Reklam", "Diğer", "KDV öncesi kâr", "Tahmini KDV",
                          "Tahmini KDV sonrası kâr", "Net marj % (KDV sonrası)", "Markup % (KDV sonrası)", "Tahmini mi"],
                         [[i["external_order_id"], i["order_date_tr"], i["marketplace_name"],
                           S.LABELS_TR.get(i["internal_status"], i["internal_status"]), i["revenue"], i["refund"],
                           i["net_sales"], i["product_cost"], i["commission"], i["service_fee"], i["shipping"],
                           i["advertising"], i["other"], i["profit_before_vat"], i["vat_estimate"], i["profit_after_vat"],
                           pct(i["margin_after_vat"]), pct(i["markup_after_vat"]),
                           "Evet" if i["finance_is_estimate"] else "Hayır"] for i in items])
