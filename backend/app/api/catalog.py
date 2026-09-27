"""Ürün & Stok, Kargo ve Tedarikçiler."""
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from ..db import get_conn, row, rows
from ..deps import CurrentUser, client_ip, operator, viewer
from ..domain import order_status as S
from ..services import app_settings
from ..services.audit import log_audit
from ..services.finance_service import recalculate_order
from .common import Page, not_found, paged

router = APIRouter(tags=["catalog"])


# ------------------------------------------------------------------ ürünler
@router.get("/api/products")
def list_products(page: Page = Depends(), q: str | None = Query(None, max_length=100),
                  low_stock: bool = False, missing_cost: bool = False,
                  _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    threshold = int(app_settings.get(conn, "stock.low_stock_threshold", 3))
    where, params = ["TRUE"], {"th": threshold}
    if q:
        where.append("(p.sku ILIKE :q OR p.barcode ILIKE :q OR p.name ILIKE :q)")
        params["q"] = f"%{q.strip()}%"
    if low_stock:
        where.append("COALESCE(p.stock, 0) <= :th")
    if missing_cost:
        where.append("COALESCE(p.cost, 0) = 0")
    w = " AND ".join(where)
    total = conn.execute(text(f"SELECT COUNT(*) FROM products p WHERE {w}"), params).scalar()
    items = rows(conn, f"""
        SELECT p.id, p.sku, p.barcode, p.name, p.brand, p.category, p.cost, p.sale_price, p.stock,
               p.vat_rate, p.is_active, p.updated_at,
               COALESCE(p.stock, 0) <= :th AS is_low_stock,
               (SELECT COALESCE(SUM(i.quantity), 0) FROM order_items i JOIN orders o ON o.id = i.order_id
                 WHERE i.product_id = p.id AND o.internal_status <> 'cancelled'
                   AND o.order_date > NOW() - INTERVAL '30 days') AS sold_30d
          FROM products p WHERE {w}
         ORDER BY p.name LIMIT :limit OFFSET :offset
    """, **params, limit=page.page_size, offset=page.offset)
    summary = row(conn, """
        SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE COALESCE(stock,0) <= :th) AS low_stock,
               COUNT(*) FILTER (WHERE COALESCE(cost,0) = 0) AS missing_cost,
               COALESCE(SUM(GREATEST(stock,0) * cost), 0) AS stock_value
          FROM products
    """, th=threshold)
    return {**paged(items, total, page), "summary": summary, "low_stock_threshold": threshold}


class ProductIn(BaseModel):
    sku: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=300)
    barcode: str | None = Field(None, max_length=100)
    brand: str | None = None
    category: str | None = None
    cost: Decimal = Field(Decimal("0"), ge=0)
    sale_price: Decimal = Field(Decimal("0"), ge=0)
    stock: int = 0
    vat_rate: Decimal = Field(Decimal("20"), ge=0, le=100)


@router.post("/api/products", status_code=201)
def create_product(body: ProductIn, request: Request, user: CurrentUser = Depends(operator),
                   conn: Connection = Depends(get_conn)):
    if conn.execute(text("SELECT 1 FROM products WHERE sku = :s"), {"s": body.sku.strip()}).first():
        raise HTTPException(409, "Bu SKU zaten kayıtlı")
    pid = conn.execute(text("""
        INSERT INTO products(sku, barcode, name, brand, category, cost, sale_price, stock, vat_rate,
                             stock_updated_at, updated_at)
        VALUES (:sku, :barcode, :name, :brand, :category, :cost, :sale_price, :stock, :vat, NOW(), NOW())
        RETURNING id
    """), {**body.model_dump(), "sku": body.sku.strip(), "vat": body.vat_rate}).scalar()
    if body.cost > 0:
        conn.execute(text("INSERT INTO product_costs(product_id, cost, source, created_by) VALUES (:p, :c, 'manual', :u)"),
                     {"p": pid, "c": body.cost, "u": user.id})
    # Bu SKU ile gelmiş, ürüne bağlanmamış sipariş kalemlerini bağla.
    conn.execute(text("UPDATE order_items SET product_id = :p WHERE product_id IS NULL AND (sku = :s OR (barcode = :b AND CAST(:b AS TEXT) IS NOT NULL))"),
                 {"p": pid, "s": body.sku.strip(), "b": body.barcode})
    log_audit(conn, actor=user.username, user_id=user.id, action="product.created", entity_type="product",
              entity_id=pid, ip=client_ip(request), details=body.model_dump(mode="json"))
    return {"id": pid}


class ProductPatch(BaseModel):
    name: str | None = Field(None, max_length=300)
    barcode: str | None = Field(None, max_length=100)
    brand: str | None = None
    category: str | None = None
    sale_price: Decimal | None = Field(None, ge=0)
    stock: int | None = None
    vat_rate: Decimal | None = Field(None, ge=0, le=100)
    is_active: bool | None = None


@router.patch("/api/products/{product_id}")
def update_product(product_id: int, body: ProductPatch, request: Request,
                   user: CurrentUser = Depends(operator), conn: Connection = Depends(get_conn)):
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        return {"ok": True}
    before = row(conn, "SELECT * FROM products WHERE id = :id FOR UPDATE", id=product_id)
    if before is None:
        raise not_found("Ürün")
    sets = ", ".join(f"{k} = :{k}" for k in changes)
    extra = ", stock_updated_at = NOW()" if "stock" in changes else ""
    conn.execute(text(f"UPDATE products SET {sets}{extra}, updated_at = NOW() WHERE id = :id"),
                 {**changes, "id": product_id})
    log_audit(conn, actor=user.username, user_id=user.id, action="product.updated", entity_type="product",
              entity_id=product_id, ip=client_ip(request),
              details={k: {"from": before.get(k), "to": v} for k, v in changes.items()})
    return {"ok": True}


class CostIn(BaseModel):
    cost: Decimal = Field(ge=0, le=Decimal("10000000"))
    valid_from: datetime | None = None
    note: str | None = Field(None, max_length=300)
    recalculate_missing: bool = True


@router.post("/api/products/{product_id}/cost")
def set_cost(product_id: int, body: CostIn, request: Request, user: CurrentUser = Depends(operator),
             conn: Connection = Depends(get_conn)):
    """Maliyet geçmişine yeni kayıt ekler. Geçmiş siparişlerin maliyet
    snapshot'ı DEĞİŞMEZ; yalnızca maliyeti hiç girilmemiş (0) kalemler
    yeniden hesaplanır."""
    if not conn.execute(text("SELECT 1 FROM products WHERE id = :id"), {"id": product_id}).first():
        raise not_found("Ürün")
    conn.execute(text("""
        INSERT INTO product_costs(product_id, cost, valid_from, source, note, created_by)
        VALUES (:p, :c, COALESCE(:vf, NOW()), 'manual', :n, :u)
    """), {"p": product_id, "c": body.cost, "vf": body.valid_from, "n": body.note, "u": user.id})
    conn.execute(text("UPDATE products SET cost = :c, updated_at = NOW() WHERE id = :id"),
                 {"c": body.cost, "id": product_id})
    recalculated = 0
    if body.recalculate_missing:
        ids = [r[0] for r in conn.execute(text("""
            SELECT DISTINCT order_id FROM order_items WHERE product_id = :p AND COALESCE(unit_cost, 0) = 0
        """), {"p": product_id})]
        for oid in ids:
            recalculate_order(conn, oid)
        recalculated = len(ids)
    log_audit(conn, actor=user.username, user_id=user.id, action="product.cost_set", entity_type="product",
              entity_id=product_id, ip=client_ip(request),
              details={"cost": str(body.cost), "valid_from": body.valid_from, "recalculated_orders": recalculated})
    return {"ok": True, "recalculated_orders": recalculated}


@router.get("/api/products/{product_id}/costs")
def cost_history(product_id: int, _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    return rows(conn, """SELECT c.id, c.cost, c.valid_from, c.source, c.note, u.username
                           FROM product_costs c LEFT JOIN users u ON u.id = c.created_by
                          WHERE c.product_id = :p ORDER BY c.valid_from DESC""", p=product_id)


# -------------------------------------------------------------------- kargo
@router.get("/api/shipments")
def list_shipments(page: Page = Depends(), status: str | None = None, carrier: str | None = None,
                   q: str | None = Query(None, max_length=100), _: CurrentUser = Depends(viewer),
                   conn: Connection = Depends(get_conn)):
    where, params = ["TRUE"], {}
    if status:
        where.append("o.internal_status = :st"); params["st"] = status
    if carrier:
        where.append("sh.carrier = :c"); params["c"] = carrier
    if q:
        where.append("(sh.tracking_number ILIKE :q OR o.external_order_id ILIKE :q)"); params["q"] = f"%{q.strip()}%"
    w = " AND ".join(where)
    base = f"FROM shipments sh JOIN orders o ON o.id = sh.order_id LEFT JOIN stores s ON s.id = o.store_id LEFT JOIN marketplaces m ON m.id = s.marketplace_id WHERE {w}"
    total = conn.execute(text(f"SELECT COUNT(*) {base}"), params).scalar()
    items = rows(conn, f"""
        SELECT sh.id, sh.order_id, o.external_order_id, o.internal_status, m.name AS marketplace_name,
               sh.carrier, sh.tracking_number, sh.tracking_url, sh.marketplace_status, sh.cost, sh.desi,
               sh.shipped_at, sh.delivered_at, sh.updated_at, o.order_date
        {base} ORDER BY o.order_date DESC NULLS LAST, sh.id DESC LIMIT :limit OFFSET :offset
    """, **params, limit=page.page_size, offset=page.offset)
    for it in items:
        it["status_label"] = S.LABELS_TR.get(it["internal_status"], it["internal_status"])
    summary = rows(conn, """
        SELECT o.internal_status AS status, COUNT(DISTINCT o.id) AS count
          FROM orders o WHERE o.internal_status IN ('awaiting_shipment','shipped','delivered','returned')
         GROUP BY o.internal_status
    """)
    carriers = [r["carrier"] for r in rows(conn, "SELECT DISTINCT carrier FROM shipments WHERE carrier IS NOT NULL ORDER BY 1")]
    return {**paged(items, total, page), "summary": {r["status"]: r["count"] for r in summary}, "carriers": carriers}


class ShipmentPatch(BaseModel):
    cost: Decimal | None = Field(None, ge=0, le=Decimal("100000"))


@router.patch("/api/shipments/{shipment_id}")
def update_shipment(shipment_id: int, body: ShipmentPatch, request: Request,
                    user: CurrentUser = Depends(operator), conn: Connection = Depends(get_conn)):
    order_id = conn.execute(text("UPDATE shipments SET cost = :c, updated_at = NOW() WHERE id = :id RETURNING order_id"),
                            {"c": body.cost, "id": shipment_id}).scalar()
    if order_id is None:
        raise not_found("Sevkiyat")
    recalculate_order(conn, order_id)
    log_audit(conn, actor=user.username, user_id=user.id, action="shipment.cost_set", entity_type="shipment",
              entity_id=shipment_id, ip=client_ip(request), details={"cost": str(body.cost)})
    return {"ok": True}


# -------------------------------------------------------------- tedarikçiler
class SupplierIn(BaseModel):
    code: str = Field(min_length=1, max_length=50, pattern=r"^[a-z0-9_\-]+$")
    name: str = Field(min_length=1, max_length=200)
    contact_name: str | None = None
    phone: str | None = None
    email: str | None = None
    lead_time_days: int | None = Field(None, ge=0, le=365)
    integration_type: str = Field("manual", pattern=r"^(manual|external|api)$")
    notes: str | None = Field(None, max_length=2000)
    is_active: bool = True


@router.get("/api/suppliers")
def list_suppliers(_: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    return rows(conn, """
        SELECT sp.*,
               (SELECT COUNT(*) FROM supplier_products x WHERE x.supplier_id = sp.id) AS product_count,
               (SELECT COUNT(*) FROM supplier_orders so WHERE so.supplier_id = sp.id) AS order_count,
               (SELECT COUNT(*) FROM supplier_orders so WHERE so.supplier_id = sp.id AND so.last_error IS NOT NULL) AS error_count
          FROM suppliers sp ORDER BY sp.name
    """)


@router.post("/api/suppliers", status_code=201)
def create_supplier(body: SupplierIn, request: Request, user: CurrentUser = Depends(operator),
                    conn: Connection = Depends(get_conn)):
    try:
        with conn.begin_nested():
            sid = conn.execute(text("""
                INSERT INTO suppliers(code, name, contact_name, phone, email, lead_time_days, integration_type, notes, is_active)
                VALUES (:code, :name, :contact_name, :phone, :email, :lead_time_days, :integration_type, :notes, :is_active)
                RETURNING id
            """), body.model_dump()).scalar()
    except IntegrityError:
        raise HTTPException(409, "Bu tedarikçi kodu zaten kayıtlı") from None
    log_audit(conn, actor=user.username, user_id=user.id, action="supplier.created", entity_type="supplier",
              entity_id=sid, ip=client_ip(request), details=body.model_dump())
    return {"id": sid}


@router.put("/api/suppliers/{supplier_id}")
def update_supplier(supplier_id: int, body: SupplierIn, request: Request, user: CurrentUser = Depends(operator),
                    conn: Connection = Depends(get_conn)):
    n = conn.execute(text("""
        UPDATE suppliers SET name = :name, contact_name = :contact_name, phone = :phone, email = :email,
               lead_time_days = :lead_time_days, integration_type = :integration_type, notes = :notes,
               is_active = :is_active, updated_at = NOW()
         WHERE id = :id
    """), {**body.model_dump(), "id": supplier_id}).rowcount
    if not n:
        raise not_found("Tedarikçi")
    log_audit(conn, actor=user.username, user_id=user.id, action="supplier.updated", entity_type="supplier",
              entity_id=supplier_id, ip=client_ip(request), details=body.model_dump())
    return {"ok": True}


@router.get("/api/supplier-orders")
def list_supplier_orders(page: Page = Depends(), _: CurrentUser = Depends(viewer),
                         conn: Connection = Depends(get_conn)):
    total = conn.execute(text("SELECT COUNT(*) FROM supplier_orders")).scalar()
    items = rows(conn, """
        SELECT so.id, so.order_id, o.external_order_id, COALESCE(sp.name, so.supplier) AS supplier_name,
               so.external_supplier_order_id, so.cost, so.status, so.last_error, so.created_at
          FROM supplier_orders so LEFT JOIN orders o ON o.id = so.order_id
          LEFT JOIN suppliers sp ON sp.id = so.supplier_id
         ORDER BY so.created_at DESC NULLS LAST, so.id DESC LIMIT :limit OFFSET :offset
    """, limit=page.page_size, offset=page.offset)
    return paged(items, total, page)


# ------------------------------------------------- pazaryeri ilanları (salt okunur)
LISTING_STATUS_TR = {"on_sale": "Satışta", "not_on_sale": "Satışta değil", "archived": "Arşivde",
                     "pending": "Onay bekliyor", "rejected": "Reddedildi"}


@router.get("/api/listings")
def list_listings(page: Page = Depends(), q: str | None = Query(None, max_length=100),
                  marketplace: str | None = None, unlinked: bool = False, stock_mismatch: bool = False,
                  _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    where, params = ["TRUE"], {}
    if q:
        where.append("(l.sku ILIKE :q OR l.barcode ILIKE :q OR l.title ILIKE :q)"); params["q"] = f"%{q.strip()}%"
    if marketplace:
        where.append("m.code = :mp"); params["mp"] = marketplace
    if unlinked:
        where.append("l.product_id IS NULL")
    if stock_mismatch:
        where.append("p.id IS NOT NULL AND COALESCE(p.stock, 0) <> COALESCE(l.listed_stock, 0)")
    base = f"""FROM marketplace_listings l JOIN stores s ON s.id = l.store_id
               JOIN marketplaces m ON m.id = s.marketplace_id LEFT JOIN products p ON p.id = l.product_id
               WHERE {' AND '.join(where)}"""
    total = conn.execute(text(f"SELECT COUNT(*) {base}"), params).scalar()
    items = rows(conn, f"""
        SELECT l.id, l.external_product_id, l.barcode, l.sku, l.title, l.listed_price, l.list_price, l.listed_stock,
               l.status, l.last_synced_at, m.code AS marketplace, m.name AS marketplace_name,
               p.id AS product_id, p.stock AS local_stock, p.cost AS local_cost
        {base} ORDER BY l.title LIMIT :limit OFFSET :offset
    """, **params, limit=page.page_size, offset=page.offset)
    for it in items:
        it["status_label"] = LISTING_STATUS_TR.get(it["status"], it["status"])
        price, cost = it["listed_price"], it["local_cost"]
        it["gross_margin_hint"] = float((price - cost) / price) if price and cost else None
    summary = row(conn, """
        SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE product_id IS NULL) AS unlinked,
               COUNT(*) FILTER (WHERE status = 'on_sale') AS on_sale, MAX(last_synced_at) AS last_synced_at
          FROM marketplace_listings
    """)
    return {**paged(items, total, page), "summary": summary}


@router.post("/api/listings/import-products")
def import_listings_as_products(request: Request, user: CurrentUser = Depends(operator),
                                conn: Connection = Depends(get_conn)):
    """Bağlanmamış ilanlardan yerel ürün kartı oluşturur (yalnızca TrendHub DB'si)."""
    from ..services.listings_sync import import_unlinked_as_products
    created = import_unlinked_as_products(conn)
    log_audit(conn, actor=user.username, user_id=user.id, action="listings.imported_as_products",
              entity_type="product", ip=client_ip(request), details={"created": created})
    return {"ok": True, "created": created}
