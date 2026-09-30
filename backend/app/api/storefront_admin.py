"""Panel: Trendçantanız web mağazası yönetimi (ayarlar, ürün yayını, ödeme bekleyen web siparişleri).

Web siparişleri TrendHub'da ayrı satış kanalıdır (marketplaces.code = 'storefront'); ödemesi alınmış
veya kapıda ödemeli siparişler Siparişler ekranında diğer kanallarla birlikte yönetilir.
"""
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import get_settings
from ..db import get_conn, row, rows
from ..deps import CurrentUser, admin, client_ip, operator, viewer
from ..services import stock_availability
from ..services.audit import log_audit
from ..storefront import catalog, checkout, store_config
from .common import Page, not_found, paged

router = APIRouter(prefix="/api/storefront", tags=["storefront"])


@router.get("/overview")
def overview(_: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    cfg = store_config.load(conn)
    snap = catalog.snapshot(conn, fresh=True)
    hero, reason = catalog.hero(conn, snap)
    counts = row(conn, """
        SELECT (SELECT COUNT(*) FROM storefront_orders WHERE status IN ('awaiting_payment','pending_payment')) AS pending_payment,
               (SELECT COUNT(*) FROM orders o JOIN stores s ON s.id = o.store_id JOIN marketplaces m ON m.id = s.marketplace_id
                 WHERE m.code = 'storefront' AND o.order_date > NOW() - INTERVAL '30 days' AND o.internal_status <> 'cancelled') AS orders_30d,
               (SELECT COALESCE(SUM(o.gross_revenue), 0) FROM orders o JOIN stores s ON s.id = o.store_id JOIN marketplaces m ON m.id = s.marketplace_id
                 WHERE m.code = 'storefront' AND o.order_date > NOW() - INTERVAL '30 days' AND o.internal_status NOT IN ('cancelled','returned')) AS revenue_30d
    """)
    return {
        "checkout_ready": cfg.checkout_ready, "blockers": cfg.checkout_blockers(),
        "payment_methods": cfg.payment_methods(), "card_provider_configured": cfg.card_available,
        "base_url": get_settings().storefront_base_url or None,
        "published_models": len(snap.groups), "published_products": sum(len(g.variants) for g in snap.groups),
        "in_stock_models": sum(1 for g in snap.groups if g.in_stock), "categories": len(snap.by_category()),
        "has_sales_data": snap.has_sales,
        "hero": {"product_id": hero.id, "title": hero.title, "url": hero.url, "reason": reason} if hero else None,
        **counts,
    }


# ------------------------------------------------------------------ ayarlar
@router.get("/settings")
def get_settings_(_: CurrentUser = Depends(admin), conn: Connection = Depends(get_conn)):
    cfg = store_config.load(conn)
    return {"values": {k: getattr(cfg, k) for k in store_config.KEYS},
            "legal_pages": [{"slug": s, "title": t, "required": r} for s, (t, r) in store_config.LEGAL_PAGES.items()],
            "seller_fields": [{"key": k, "label": label, "required": r} for k, (label, r) in store_config.SELLER_FIELDS.items()],
            "social_fields": list(store_config.SOCIAL_FIELDS)}


class SettingsIn(BaseModel):
    values: dict


@router.put("/settings")
def put_settings(body: SettingsIn, request: Request, user: CurrentUser = Depends(admin), conn: Connection = Depends(get_conn)):
    from ..services import app_settings
    try:
        clean = store_config.validate(body.values)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from None
    if clean.get("hero_product_id") and not conn.execute(text("SELECT 1 FROM products WHERE id = :i"),
                                                         {"i": clean["hero_product_id"]}).first():
        raise HTTPException(422, "Hero için seçilen ürün bulunamadı")
    before = {k: getattr(store_config.load(conn), k) for k in clean}
    changed = [k for k, v in clean.items() if json_eq(before.get(k), v) is False]
    for k in changed:
        app_settings.set_value(conn, f"storefront.{k}", clean[k], user.id)
    if changed:
        # Yasal metin gövdeleri denetim kaydına yazılmaz (uzun metin); yalnızca değişen alan adları.
        log_audit(conn, actor=user.username, user_id=user.id, action="storefront.settings_updated", ip=client_ip(request),
                  details={"changed": changed})
    catalog.invalidate()
    return {"ok": True, "changed": changed}


def json_eq(a, b) -> bool:
    import json
    return json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


# ------------------------------------------------------------------ ürün yayını
@router.get("/products")
def list_products(page: Page = Depends(), q: str | None = Query(None, max_length=100), status: str | None = None,
                  _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    cfg = store_config.load(conn)
    where, params = ["TRUE"], {}
    if q:
        where.append("(p.sku ILIKE :q OR p.barcode ILIKE :q OR p.name ILIKE :q OR sfp.title ILIKE :q)")
        params["q"] = f"%{q.strip()}%"
    has_image = "(jsonb_array_length(COALESCE(p.images, '[]'::jsonb)) > 0 OR COALESCE(p.image_url, '') <> '')"
    visible = f"""(COALESCE(p.is_active, TRUE) AND COALESCE(p.sale_price, 0) > 0 AND {has_image}
                   AND (sfp.published IS TRUE OR (sfp.product_id IS NULL AND :auto)))"""
    if status == "visible":
        where.append(visible)
    elif status == "hidden":
        where.append(f"NOT {visible}")
    w = " AND ".join(where)
    base = f"FROM products p LEFT JOIN storefront_products sfp ON sfp.product_id = p.id WHERE {w}"
    params.update(auto=cfg.auto_publish, **stock_availability.params(conn))
    total = conn.execute(text(f"SELECT COUNT(*) {base}"), params).scalar()
    items = rows(conn, f"""
        SELECT p.id, p.sku, p.barcode, p.name, p.category, p.sale_price, p.stock, p.is_active,
               COALESCE(jsonb_array_length(COALESCE(p.images, '[]'::jsonb)), 0) + CASE WHEN COALESCE(p.image_url, '') <> '' THEN 1 ELSE 0 END AS image_count,
               sfp.published, sfp.title, sfp.compare_at_price, COALESCE(sfp.featured, FALSE) AS featured,
               COALESCE(sfp.sort_order, 0) AS sort_order, {visible} AS visible,
               {stock_availability.available_sql('p')} AS available
        {base} ORDER BY {visible} DESC, COALESCE(sfp.featured, FALSE) DESC, p.name LIMIT :limit OFFSET :offset
    """, **params, limit=page.page_size, offset=page.offset)
    for it in items:
        reasons = []
        if not it["is_active"] and it["is_active"] is not None:
            reasons.append("Ürün pasif")
        if not it["sale_price"] or Decimal(it["sale_price"]) <= 0:
            reasons.append("Satış fiyatı yok")
        if not it["image_count"]:
            reasons.append("Görsel yok")
        if it["published"] is False:
            reasons.append("Web'de gizlendi")
        elif it["published"] is None and not cfg.auto_publish:
            reasons.append("Otomatik yayın kapalı")
        it["hidden_reasons"] = reasons
        it["url"] = catalog.product_path(it["id"], it["title"] or it["name"]) if it["visible"] else None
    return {**paged(items, total, page), "auto_publish": cfg.auto_publish}


class ProductPatch(BaseModel):
    published: bool | None = None
    title: str | None = Field(None, max_length=300)
    description: str | None = Field(None, max_length=20000)
    compare_at_price: Decimal | None = Field(None, ge=0, le=Decimal("10000000"))
    featured: bool | None = None
    sort_order: int | None = Field(None, ge=-10000, le=10000)


@router.patch("/products/{product_id}")
def patch_product(product_id: int, body: ProductPatch, request: Request, user: CurrentUser = Depends(operator),
                  conn: Connection = Depends(get_conn)):
    p = row(conn, "SELECT id, sale_price FROM products WHERE id = :id", id=product_id)
    if p is None:
        raise not_found("Ürün")
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        return {"ok": True}
    cmp_ = changes.get("compare_at_price")
    if cmp_ is not None:
        if cmp_ == 0:
            changes["compare_at_price"] = None
        elif cmp_ <= Decimal(p["sale_price"] or 0):
            raise HTTPException(422, "Üstü çizili fiyat, satış fiyatından yüksek olmalı (gerçek olmayan indirim gösterilmez).")
    for k in ("title", "description"):
        if k in changes and changes[k] is not None:
            changes[k] = changes[k].strip() or None
    conn.execute(text("INSERT INTO storefront_products(product_id, updated_by) VALUES (:p, :u) ON CONFLICT (product_id) DO NOTHING"),
                 {"p": product_id, "u": user.id})
    sets = ", ".join(f"{k} = :{k}" for k in changes)
    conn.execute(text(f"UPDATE storefront_products SET {sets}, updated_by = :u, updated_at = NOW() WHERE product_id = :p"),
                 {**changes, "p": product_id, "u": user.id})
    log_audit(conn, actor=user.username, user_id=user.id, action="storefront.product_updated", entity_type="product",
              entity_id=product_id, ip=client_ip(request),
              details={k: (str(v) if isinstance(v, Decimal) else v) for k, v in changes.items() if k != "description"})
    catalog.invalidate()
    return {"ok": True}


# ------------------------------------------------------------------ web siparişleri
@router.get("/orders")
def list_orders(page: Page = Depends(), status: str | None = None, _: CurrentUser = Depends(operator),
                conn: Connection = Depends(get_conn)):
    where, params = ["TRUE"], {}
    if status == "pending":
        where.append("so.status IN ('awaiting_payment', 'pending_payment')")
    elif status:
        if status not in checkout.STATUS_LABELS:
            raise HTTPException(422, "Geçersiz durum")
        where.append("so.status = :st")
        params["st"] = status
    w = " AND ".join(where)
    total = conn.execute(text(f"SELECT COUNT(*) FROM storefront_orders so WHERE {w}"), params).scalar()
    items = rows(conn, f"""
        SELECT so.id, so.public_code, so.status, so.payment_method, so.full_name, so.city, so.total, so.items_total,
               so.shipping_fee, so.created_at, so.paid_at, so.order_id, so.lines,
               (SELECT MIN(r.expires_at) FROM stock_reservations r WHERE r.storefront_order_id = so.id AND r.released_at IS NULL) AS reserved_until
          FROM storefront_orders so WHERE {w} ORDER BY so.created_at DESC LIMIT :limit OFFSET :offset
    """, **params, limit=page.page_size, offset=page.offset)
    for it in items:
        it["status_label"] = checkout.STATUS_LABELS.get(it["status"], it["status"])
        it["item_count"] = sum(int(line.get("quantity", 0)) for line in it.pop("lines") or [])
    return paged(items, total, page)


class PaymentConfirmIn(BaseModel):
    reference: str | None = Field(None, max_length=200)


@router.post("/orders/{sfo_id}/confirm-payment")
def confirm_payment(sfo_id: int, body: PaymentConfirmIn, request: Request, user: CurrentUser = Depends(operator),
                    conn: Connection = Depends(get_conn)):
    """Havale/EFT ödemesi hesaba geçtiğinde: TrendHub siparişi oluşturulur (iç statü 'Yeni')."""
    try:
        order_id = checkout.confirm_payment(conn, sfo_id, reference=body.reference, provider="bank_transfer")
    except checkout.CheckoutError as exc:
        raise HTTPException(409, str(exc)) from None
    log_audit(conn, actor=user.username, user_id=user.id, action="storefront.payment_confirmed", entity_type="order",
              entity_id=order_id, ip=client_ip(request), details={"storefront_order_id": sfo_id, "reference": body.reference})
    return {"ok": True, "order_id": order_id}


@router.post("/orders/{sfo_id}/cancel")
def cancel(sfo_id: int, request: Request, user: CurrentUser = Depends(operator), conn: Connection = Depends(get_conn)):
    try:
        checkout.cancel_unpaid(conn, sfo_id)
    except checkout.CheckoutError as exc:
        raise HTTPException(409, str(exc)) from None
    log_audit(conn, actor=user.username, user_id=user.id, action="storefront.order_cancelled", entity_type="storefront_order",
              entity_id=sfo_id, ip=client_ip(request), details={})
    return {"ok": True}


def order_details(conn: Connection, order_id: int, user: CurrentUser) -> dict | None:
    """Sipariş detayına web siparişi bilgisi (adres/telefon/e-posta yalnızca operatör ve yöneticiye)."""
    so = row(conn, "SELECT * FROM storefront_orders WHERE order_id = :o", o=order_id)
    if so is None:
        return None
    out = {"public_code": so["public_code"], "status": so["status"],
           "status_label": checkout.STATUS_LABELS.get(so["status"], so["status"]),
           "payment_method": so["payment_method"], "shipping_fee": so["shipping_fee"], "total": so["total"],
           "paid_at": so["paid_at"], "created_at": so["created_at"], "customer_note": so["customer_note"]}
    if user.has_role("operator"):
        out.update({k: so[k] for k in ("full_name", "email", "phone", "city", "district", "address", "postal_code", "billing")})
    return out
