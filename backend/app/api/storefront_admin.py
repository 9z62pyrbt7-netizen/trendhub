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
from ..services import einvoice, order_notifications, stock_availability, supplier_forwarding
from ..services.audit import log_audit
from ..storefront import catalog, checkout, payments, store_config
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
PRODUCT_STATUSES = {"visible", "hidden", "eligible_unpublished", "not_eligible"}


def _product_filter(q: str | None, status: str | None, auto: bool) -> tuple[str, dict]:
    where, params = ["TRUE"], {"auto": auto}
    if q:
        where.append("(p.sku ILIKE :q OR p.barcode ILIKE :q OR p.name ILIKE :q OR sfp.title ILIKE :q OR p.category ILIKE :q)")
        params["q"] = f"%{q.strip()}%"
    visible = f"({catalog.ELIGIBLE_SQL} AND (sfp.published IS TRUE OR (sfp.product_id IS NULL AND :auto)))"
    if status == "visible":
        where.append(visible)
    elif status == "hidden":
        where.append(f"NOT {visible}")
    elif status == "eligible_unpublished":
        where.append(f"({catalog.ELIGIBLE_SQL}) AND NOT {visible}")
    elif status == "not_eligible":
        where.append(f"NOT ({catalog.ELIGIBLE_SQL})")
    elif status:
        raise HTTPException(422, "Geçersiz durum filtresi")
    return " AND ".join(where), params


PRODUCT_FROM = f"""FROM products p LEFT JOIN storefront_products sfp ON sfp.product_id = p.id
          {catalog.SP_LATERAL}"""


@router.get("/products")
def list_products(page: Page = Depends(), q: str | None = Query(None, max_length=100), status: str | None = None,
                  _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    cfg = store_config.load(conn)
    w, params = _product_filter(q, status, cfg.auto_publish)
    visible = f"({catalog.ELIGIBLE_SQL} AND (sfp.published IS TRUE OR (sfp.product_id IS NULL AND :auto)))"
    params.update(**stock_availability.params(conn))
    total = conn.execute(text(f"SELECT COUNT(*) {PRODUCT_FROM} WHERE {w}"), params).scalar()
    items = rows(conn, f"""
        SELECT p.id, p.sku, p.barcode, p.name, p.category, p.sale_price, p.stock, p.is_active, p.model_code,
               p.images, p.image_url, sp.images AS supplier_images,
               sp.color AS supplier_color, sp.size AS supplier_size, sp.parent_code,
               sfp.published, sfp.title, sfp.compare_at_price, COALESCE(sfp.featured, FALSE) AS featured,
               COALESCE(sfp.sort_order, 0) AS sort_order, sfp.color, sfp.size, sfp.group_code,
               sfp.seo_title, sfp.seo_description, sfp.description AS web_description,
               {catalog.GROUP_KEY} AS group_key, {visible} AS visible, ({catalog.ELIGIBLE_SQL}) AS eligible,
               {stock_availability.available_sql('p')} AS available
        {PRODUCT_FROM} WHERE {w}
        ORDER BY {visible} DESC, COALESCE(sfp.featured, FALSE) DESC, {catalog.GROUP_KEY}, p.name
        LIMIT :limit OFFSET :offset
    """, **params, limit=page.page_size, offset=page.offset)
    group_sizes = {}
    keys = list({it["group_key"] for it in items})
    if keys:
        group_sizes = {r["k"]: r["n"] for r in rows(conn, f"""
            SELECT {catalog.GROUP_KEY} AS k, COUNT(*) AS n {PRODUCT_FROM}
             WHERE {catalog.GROUP_KEY} = ANY(:keys) GROUP BY 1""", keys=keys)}
    for it in items:
        imgs = catalog._image_list(it.pop("images")) or ([it["image_url"]] if it["image_url"] else []) \
            or catalog._image_list(it.pop("supplier_images", None))
        it.pop("supplier_images", None)
        it["image_count"] = len(imgs)
        it["thumbnail"] = imgs[0] if imgs else None
        it["effective_color"] = it["color"] or it["supplier_color"]
        it["effective_size"] = it["size"] or it["supplier_size"]
        it["variant_count"] = group_sizes.get(it["group_key"], 1)
        reasons = []
        if it["is_active"] is False:
            reasons.append("Ürün pasif")
        if not it["sale_price"] or Decimal(it["sale_price"]) <= 0:
            reasons.append("Satış fiyatı yok")
        if not imgs:
            reasons.append("Görsel yok")
        if it["published"] is False:
            reasons.append("Web'de gizlendi")
        elif it["published"] is None and not cfg.auto_publish:
            reasons.append("Yayınlanmadı (otomatik yayın kapalı)")
        it["hidden_reasons"] = reasons
        it["url"] = catalog.product_path(it["id"], it["title"] or it["name"]) if it["visible"] else None
    counts = row(conn, f"""
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE {visible}) AS visible,
               COUNT(*) FILTER (WHERE ({catalog.ELIGIBLE_SQL}) AND NOT {visible}) AS eligible_unpublished,
               COUNT(*) FILTER (WHERE NOT ({catalog.ELIGIBLE_SQL})) AS not_eligible
        {PRODUCT_FROM}""", auto=cfg.auto_publish)
    return {**paged(items, total, page), "auto_publish": cfg.auto_publish, "counts": counts}


class ProductPatch(BaseModel):
    published: bool | None = None
    title: str | None = Field(None, max_length=300)
    description: str | None = Field(None, max_length=20000)
    compare_at_price: Decimal | None = Field(None, ge=0, le=Decimal("10000000"))
    featured: bool | None = None
    sort_order: int | None = Field(None, ge=-10000, le=10000)
    color: str | None = Field(None, max_length=60)
    size: str | None = Field(None, max_length=60)
    group_code: str | None = Field(None, max_length=100)
    seo_title: str | None = Field(None, max_length=70)
    seo_description: str | None = Field(None, max_length=170)


def _upsert(conn: Connection, product_id: int, changes: dict, user_id: int) -> None:
    conn.execute(text("INSERT INTO storefront_products(product_id, updated_by) VALUES (:p, :u) ON CONFLICT (product_id) DO NOTHING"),
                 {"p": product_id, "u": user_id})
    if changes:
        sets = ", ".join(f"{k} = :{k}" for k in changes)
        conn.execute(text(f"UPDATE storefront_products SET {sets}, updated_by = :u, updated_at = NOW() WHERE product_id = :p"),
                     {**changes, "p": product_id, "u": user_id})


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
    for k in ("title", "description", "color", "size", "group_code", "seo_title", "seo_description"):
        if k in changes and changes[k] is not None:
            changes[k] = changes[k].strip() or None
    _upsert(conn, product_id, changes, user.id)
    log_audit(conn, actor=user.username, user_id=user.id, action="storefront.product_updated", entity_type="product",
              entity_id=product_id, ip=client_ip(request),
              details={k: (str(v) if isinstance(v, Decimal) else v) for k, v in changes.items() if k != "description"})
    catalog.invalidate()
    return {"ok": True}


class BulkIn(BaseModel):
    action: str
    product_ids: list[int] | None = Field(None, max_length=1000)
    # Seçim yerine filtre: "Bu filtredeki tüm ürünler" (ör. yayına uygun ama yayında olmayanların tümü)
    q: str | None = Field(None, max_length=100)
    status: str | None = None


BULK_ACTIONS = {"publish": {"published": True}, "unpublish": {"published": False},
                "feature": {"featured": True}, "unfeature": {"featured": False}}


@router.post("/products/bulk")
def bulk_products(body: BulkIn, request: Request, user: CurrentUser = Depends(operator), conn: Connection = Depends(get_conn)):
    """Toplu yayın. Yayına uygun olmayan (fiyatı/görseli yok, pasif) ürünler yayınlanmaz; nedenleriyle raporlanır."""
    if body.action not in BULK_ACTIONS:
        raise HTTPException(422, "Geçersiz işlem")
    cfg = store_config.load(conn)
    if body.product_ids:
        ids = sorted(set(body.product_ids))
        found = rows(conn, f"SELECT p.id, ({catalog.ELIGIBLE_SQL}) AS eligible {PRODUCT_FROM} WHERE p.id = ANY(:ids)", ids=ids)
    elif body.q is not None or body.status is not None:
        w, params = _product_filter(body.q, body.status, cfg.auto_publish)
        found = rows(conn, f"SELECT p.id, ({catalog.ELIGIBLE_SQL}) AS eligible {PRODUCT_FROM} WHERE {w} LIMIT 5001", **params)
        if len(found) > 5000:
            raise HTTPException(422, "Tek seferde en fazla 5000 ürün; filtreyi daraltın.")
    else:
        raise HTTPException(422, "Ürün seçin veya filtre verin")
    changed, skipped = [], []
    for r in found:
        if body.action == "publish" and not r["eligible"]:
            skipped.append(r["id"])
            continue
        _upsert(conn, r["id"], BULK_ACTIONS[body.action], user.id)
        changed.append(r["id"])
    missing = sorted(set(body.product_ids or []) - {r["id"] for r in found})
    log_audit(conn, actor=user.username, user_id=user.id, action=f"storefront.products_bulk_{body.action}",
              entity_type="product", ip=client_ip(request),
              details={"count": len(changed), "skipped_not_eligible": len(skipped), "filter": {"q": body.q, "status": body.status}
                       if not body.product_ids else None, "product_ids": changed[:200]})
    catalog.invalidate()
    return {"ok": True, "changed": len(changed), "skipped_not_eligible": skipped, "not_found": missing}


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
        out["supplier_orders"] = rows(conn, """
            SELECT x.id, x.status, x.method, x.external_supplier_order_id, x.sent_at, x.last_error, s.name AS supplier_name
              FROM supplier_orders x LEFT JOIN suppliers s ON s.id = x.supplier_id
             WHERE x.order_id = :o AND x.channel = 'storefront' ORDER BY x.id""", o=order_id)
        for x in out["supplier_orders"]:
            x["status_label"] = supplier_forwarding.STATUS_LABELS.get(x["status"], x["status"])
    return out


# ------------------------------------------------------------------ entegrasyon durumu
@router.get("/integrations")
def integrations(_: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    """Harici servislerin durumu (secret göstermez; yalnızca yapılandırılmış mı)."""
    s = get_settings()
    provider = payments.card_provider()
    channels = order_notifications.channels_enabled(conn)
    counts = row(conn, """
        SELECT COUNT(*) FILTER (WHERE status = 'queued') AS queued, COUNT(*) FILTER (WHERE status = 'failed') AS failed,
               COUNT(*) FILTER (WHERE status = 'sent' AND sent_at > NOW() - INTERVAL '7 days') AS sent_7d
          FROM notification_outbox""")
    return {
        "payment": {"selected": s.storefront_payment_provider or None, "active": provider.name if provider else None,
                    "available": list(payments.PROVIDERS), "test_mode": bool(provider and provider.code == "paytr" and s.paytr_test_mode)},
        "email": {"configured": order_notifications.email_configured(), "active": channels["email"],
                  "owner_alerts": bool(s.storefront_order_alert_emails.strip())},
        "sms": {"provider": s.sms_provider or None, "configured": order_notifications.sms_configured(), "active": channels["sms"]},
        "notifications": counts,
        "einvoice": einvoice.status(conn),
        "supplier_forwarding": {"mode": supplier_forwarding.mode(conn), "connectors": list(supplier_forwarding.CONNECTORS)},
        "base_url": s.storefront_base_url or None,
    }


@router.get("/notifications")
def list_notifications(page: Page = Depends(), _: CurrentUser = Depends(operator), conn: Connection = Depends(get_conn)):
    total = conn.execute(text("SELECT COUNT(*) FROM notification_outbox")).scalar()
    items = rows(conn, """
        SELECT n.id, n.channel, n.template, n.subject, n.status, n.attempts, n.last_error, n.provider, n.created_at, n.sent_at,
               so.public_code
          FROM notification_outbox n LEFT JOIN storefront_orders so ON so.id = n.storefront_order_id
         ORDER BY n.id DESC LIMIT :limit OFFSET :offset""", limit=page.page_size, offset=page.offset)
    return paged(items, total, page)


# ------------------------------------------------------------------ tedarikçiye aktarım (yalnızca web kanalı)
@router.get("/supplier-orders")
def list_supplier_orders(page: Page = Depends(), status: str | None = None, _: CurrentUser = Depends(operator),
                         conn: Connection = Depends(get_conn)):
    where, params = ["x.channel = 'storefront'"], {}
    if status:
        if status not in supplier_forwarding.STATUS_LABELS:
            raise HTTPException(422, "Geçersiz durum")
        where.append("x.status = :st")
        params["st"] = status
    w = " AND ".join(where)
    total = conn.execute(text(f"SELECT COUNT(*) FROM supplier_orders x WHERE {w}"), params).scalar()
    items = rows(conn, f"""
        SELECT x.id, x.order_id, x.status, x.method, x.cost, x.payload, x.external_supplier_order_id, x.sent_at,
               x.last_error, x.created_at, s.name AS supplier_name, s.code AS supplier_code, o.external_order_id AS order_code
          FROM supplier_orders x LEFT JOIN suppliers s ON s.id = x.supplier_id LEFT JOIN orders o ON o.id = x.order_id
         WHERE {w} ORDER BY x.id DESC LIMIT :limit OFFSET :offset""", **params, limit=page.page_size, offset=page.offset)
    for it in items:
        it["status_label"] = supplier_forwarding.STATUS_LABELS.get(it["status"], it["status"])
        it["can_send"] = supplier_forwarding.connector_for(it["supplier_code"]) is not None
    return {**paged(items, total, page), "mode": supplier_forwarding.mode(conn)}


def _forwarding(fn, conn, request, user, action, entity_id, **details):
    try:
        result = fn()
    except supplier_forwarding.ForwardingError as exc:
        raise HTTPException(409, str(exc)) from None
    log_audit(conn, actor=user.username, user_id=user.id, action=action, entity_type="supplier_order",
              entity_id=entity_id, ip=client_ip(request), details=details)
    return result


@router.post("/orders/{order_id}/supplier/prepare")
def supplier_prepare(order_id: int, request: Request, user: CurrentUser = Depends(operator), conn: Connection = Depends(get_conn)):
    ids = _forwarding(lambda: supplier_forwarding.prepare(conn, order_id), conn, request, user,
                      "storefront.supplier_order_prepared", order_id, order_id=order_id)
    if not ids:
        raise HTTPException(409, "Sipariş kalemleri için tanımlı tedarikçi bulunamadı (ürünlerde tercih edilen tedarikçi yok).")
    return {"ok": True, "supplier_order_ids": ids}


@router.post("/supplier-orders/{so_id}/send")
def supplier_send(so_id: int, request: Request, user: CurrentUser = Depends(operator), conn: Connection = Depends(get_conn)):
    # Gönderim hatası 200 + ok=false döner: hata kaydı (status='failed', last_error) işlemle birlikte saklanır.
    return _forwarding(lambda: supplier_forwarding.send(conn, so_id), conn, request, user,
                       "storefront.supplier_order_sent", so_id)


class ManualSentIn(BaseModel):
    external_id: str | None = Field(None, max_length=100)


@router.post("/supplier-orders/{so_id}/mark-sent")
def supplier_mark_sent(so_id: int, body: ManualSentIn, request: Request, user: CurrentUser = Depends(operator),
                       conn: Connection = Depends(get_conn)):
    _forwarding(lambda: supplier_forwarding.mark_manual(conn, so_id, body.external_id), conn, request, user,
                "storefront.supplier_order_manual_sent", so_id, external_id=body.external_id)
    return {"ok": True}


@router.post("/supplier-orders/{so_id}/cancel")
def supplier_cancel(so_id: int, request: Request, user: CurrentUser = Depends(operator), conn: Connection = Depends(get_conn)):
    _forwarding(lambda: supplier_forwarding.cancel(conn, so_id), conn, request, user, "storefront.supplier_order_cancelled", so_id)
    return {"ok": True}
