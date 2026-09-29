"""Ürün aktarımı: havuzdan kataloğa, katalogdan pazaryeri taslak ilanlarına.

Tedarikçiler → Ürün Havuzu → Ürünleri Seç → Pazaryerini Seç → Fiyatlandır → Validate → Yayına Hazırla

Pazaryerine gönderim YOKTUR (CONNECTOR_WRITE_ENABLED=false). "Yayına hazır" taslaklar
yalnızca TrendHub'da tutulur ve CSV olarak dışa aktarılabilir.
"""
import csv
import io
import json
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import get_settings
from ..db import get_conn, row, rows
from ..deps import CurrentUser, admin, client_ip, operator, viewer
from ..services.audit import log_audit
from ..services.listing_drafts import DEFAULT_REQUIRED, build_drafts, marketplace_rule, revalidate
from ..services.supplier_catalog import import_to_catalog
from .analytics import _csv_safe
from .common import Page, not_found, paged

router = APIRouter(tags=["transfer"])

DRAFT_STATUS_TR = {"draft": "Taslak", "invalid": "Hatalı", "ready": "Yayına hazır", "cancelled": "İptal"}
REQUIRABLE = ["barcode", "brand", "category", "images", "description", "model_code", "desi", "vat_rate"]


class IdsIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=5000)


class ImportIn(BaseModel):
    supplier_product_ids: list[int] = Field(min_length=1, max_length=5000)


class DraftsIn(BaseModel):
    product_ids: list[int] = Field(min_length=1, max_length=5000)
    marketplaces: list[str] = Field(min_length=1, max_length=20)


def _clean_attrs(v):
    if v is None:
        return v
    if len(v) > 50 or any(len(str(k)) > 100 or len(str(x)) > 300 for k, x in v.items()):
        raise ValueError("En fazla 50 özellik; ad 100, değer 300 karakter")
    return {str(k).strip(): str(x).strip() for k, x in v.items() if str(k).strip()}


class DraftPatch(BaseModel):
    price: Decimal | None = Field(None, ge=0, le=Decimal("10000000"))
    auto_price: bool = False
    # "" -> elle girilen kategoriyi kaldır, eşleştirmeye dön
    category_id: str | None = Field(None, max_length=100)
    attributes: dict[str, str] | None = None
    reset_attributes: bool = False

    @field_validator("attributes")
    @classmethod
    def _attrs(cls, v):
        return _clean_attrs(v)

    category_name: str | None = Field(None, max_length=300)


class RuleIn(BaseModel):
    commission_rate: Decimal | None = Field(None, ge=0, lt=1)
    markup_rate: Decimal = Field(Decimal("0.30"), ge=0, le=10)
    fixed_cost: Decimal = Field(Decimal("0"), ge=0, le=Decimal("100000"))
    shipping_cost: Decimal = Field(Decimal("0"), ge=0, le=Decimal("100000"))
    min_margin_rate: Decimal = Field(Decimal("0.05"), ge=-1, le=1)
    rounding: str = Field("x.90", pattern=r"^(none|integer|x\.90|x\.99)$")
    stock_buffer: int = Field(0, ge=0, le=100000)
    min_stock: int = Field(1, ge=0, le=100000)
    max_stock: int | None = Field(None, ge=0, le=1000000)
    title_max_length: int | None = Field(None, ge=10, le=1000)
    required_fields: list[str] = Field(default_factory=lambda: list(DEFAULT_REQUIRED))


class CategoryMapIn(BaseModel):
    marketplace: str
    source_category: str = Field(min_length=1, max_length=500)
    target_category_id: str = Field(min_length=1, max_length=100)
    target_category_name: str | None = Field(None, max_length=300)
    # Pazaryeri kategori özellikleri (ör. {"Renk": "Kahverengi"}); taslaklara varsayılan olarak kopyalanır
    attributes: dict[str, str] | None = None
    # Bu kategoride doldurulması zorunlu özellik adları (doğrulamada kontrol edilir)
    required_attributes: list[str] | None = Field(None, max_length=50)

    @field_validator("attributes")
    @classmethod
    def _attrs(cls, v):
        return _clean_attrs(v)

    @field_validator("required_attributes")
    @classmethod
    def _req(cls, v):
        if v is None:
            return v
        out = []
        for x in v:
            x = str(x).strip()[:100]
            if x and x not in out:
                out.append(x)
        return out


class MarketplaceIn(BaseModel):
    code: str = Field(min_length=2, max_length=40, pattern=r"^[a-z0-9_]+$")
    name: str = Field(min_length=2, max_length=100)


def _mp(conn: Connection, code: str) -> dict:
    mp = row(conn, "SELECT id, code, name FROM marketplaces WHERE code = :c", c=code)
    if mp is None:
        raise not_found("Pazaryeri")
    return mp


# ------------------------------------------------------------------ akış
@router.post("/api/transfer/import")
def transfer_import(body: ImportIn, request: Request, user: CurrentUser = Depends(operator),
                    conn: Connection = Depends(get_conn)):
    """Adım: Ürünleri Seç — havuz ürünlerini kataloğa alır (barkod eşleşirse mevcut ürüne bağlar)."""
    r = import_to_catalog(conn, body.supplier_product_ids)
    log_audit(conn, actor=user.username, user_id=user.id, action="transfer.imported", entity_type="product",
              ip=client_ip(request), details={k: v for k, v in r.items() if k != "product_ids"})
    return r


@router.post("/api/transfer/drafts")
def transfer_drafts(body: DraftsIn, request: Request, user: CurrentUser = Depends(operator),
                    conn: Connection = Depends(get_conn)):
    """Adımlar: Pazaryerini Seç → Fiyatlandır → Validate (taslak oluşturur/günceller)."""
    try:
        r = build_drafts(conn, body.product_ids, body.marketplaces, user.id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    log_audit(conn, actor=user.username, user_id=user.id, action="transfer.drafts_built", entity_type="listing_draft",
              ip=client_ip(request), details={"products": len(body.product_ids), "marketplaces": body.marketplaces,
                                              "valid": r["valid"], "invalid": r["invalid"]})
    return r


@router.get("/api/listing-drafts")
def list_drafts(page: Page = Depends(), marketplace: str | None = None,
                status: str | None = Query(None, pattern=r"^(draft|invalid|ready|cancelled)$"),
                q: str | None = Query(None, max_length=100), ids: str | None = Query(None, max_length=20000),
                _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    where, params = ["TRUE"], {}
    if marketplace:
        where.append("m.code = :mp"); params["mp"] = marketplace
    if status:
        where.append("d.status = :st"); params["st"] = status
    else:
        where.append("d.status <> 'cancelled'")
    if q:
        where.append("(p.sku ILIKE :q OR p.barcode ILIKE :q OR p.name ILIKE :q)"); params["q"] = f"%{q.strip()}%"
    if ids:
        try:
            params["ids"] = [int(x) for x in ids.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(422, "ids virgülle ayrılmış sayılar olmalı") from None
        where.append("d.id = ANY(:ids)")
    base = f"""FROM listing_drafts d JOIN products p ON p.id = d.product_id JOIN marketplaces m ON m.id = d.marketplace_id
               LEFT JOIN supplier_products sp ON sp.id = d.supplier_product_id LEFT JOIN suppliers s ON s.id = sp.supplier_id
               WHERE {' AND '.join(where)}"""
    total = conn.execute(text(f"SELECT COUNT(*) {base}"), params).scalar()
    items = rows(conn, f"""
        SELECT d.id, d.product_id, p.sku, p.barcode, p.name, p.brand, p.category, m.code AS marketplace,
               m.name AS marketplace_name, d.price, d.price_is_manual, d.stock, d.cost_basis, d.commission_rate,
               d.estimated_profit, d.estimated_margin, d.category_id, d.category_name, d.status, d.errors, d.warnings,
               d.existing_listing_id, d.validated_at, d.updated_at, s.name AS supplier_name, d.attributes,
               d.category_is_manual, d.attributes_override
        {base} ORDER BY p.name, m.id LIMIT :limit OFFSET :offset
    """, **params, limit=page.page_size, offset=page.offset)
    for it in items:
        it["status_label"] = DRAFT_STATUS_TR.get(it["status"], it["status"])
    summary = {r["status"]: r["count"] for r in rows(conn, """
        SELECT d.status, COUNT(*) AS count FROM listing_drafts d JOIN marketplaces m ON m.id = d.marketplace_id
         WHERE (CAST(:mp AS TEXT) IS NULL OR m.code = :mp) GROUP BY d.status""", mp=marketplace)}
    return {**paged(items, total, page), "summary": summary,
            "publishing_enabled": False, "write_enabled": get_settings().connector_write_enabled}


@router.patch("/api/listing-drafts/{draft_id}")
def patch_draft(draft_id: int, body: DraftPatch, request: Request, user: CurrentUser = Depends(operator),
                conn: Connection = Depends(get_conn)):
    d = row(conn, "SELECT id, status FROM listing_drafts WHERE id = :id", id=draft_id)
    if d is None:
        raise not_found("Taslak")
    cat, reset_cat = None, False
    if body.category_id is not None:
        if body.category_id.strip():
            cat = {"target_category_id": body.category_id.strip(), "target_category_name": body.category_name}
        else:
            reset_cat = True
    revalidate(conn, [draft_id], price=None if body.auto_price else body.price, reset_price=body.auto_price,
               category=cat, reset_category=reset_cat, attributes=body.attributes,
               reset_attributes=body.reset_attributes)
    log_audit(conn, actor=user.username, user_id=user.id, action="listing_draft.updated", entity_type="listing_draft",
              entity_id=draft_id, ip=client_ip(request), details=body.model_dump(mode="json"))
    return row(conn, """SELECT id, price, status, errors, warnings, estimated_profit, estimated_margin, category_id,
                              attributes, category_is_manual, attributes_override FROM listing_drafts WHERE id = :id""",
               id=draft_id)


def _publish_status(code: str) -> dict:
    from ..connectors.registry import CONNECTOR_CLASSES, get_connector
    if code not in CONNECTOR_CLASSES:
        return {"connector": False, "connected": False, "can_publish": False,
                "reason": "Bu pazaryeri için connector yok; taslaklar CSV ile dışa aktarılır."}
    c = get_connector(code)
    return {"connector": True, "connected": c.is_configured(), **c.publish_status()}


@router.get("/api/listing-drafts/{draft_id}/preview")
def preview_draft(draft_id: int, _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    """Yayın önizlemesi: taslağın güncel hesaplanmış hâli, fiyat/kâr dökümü, doğrulama ve yayın durumu.

    Veritabanına yazmaz, pazaryerine hiçbir istek göndermez. `payload` TrendHub'ın nötr alan
    adlarıyladır; pazaryeri API şeması DEĞİLDİR (ürün oluşturma sözleşmeleri doğrulanmadı)."""
    from ..services.listing_drafts import _manual_parts, _products, compute
    from ..services.supplier_catalog import load_offers
    d = row(conn, """SELECT d.*, m.code AS marketplace_code, m.name AS marketplace_name FROM listing_drafts d
                      JOIN marketplaces m ON m.id = d.marketplace_id WHERE d.id = :id""", id=draft_id)
    if d is None:
        raise not_found("Taslak")
    mp = {"id": d["marketplace_id"], "code": d["marketplace_code"]}
    rule, extra = marketplace_rule(conn, mp)
    product = _products(conn, [d["product_id"]])[0]
    offers = load_offers(conn, [d["product_id"]]).get(d["product_id"], [])
    mcat, mattr = _manual_parts(d)
    manual_price = Decimal(d["price"]) if d["price_is_manual"] and d["price"] is not None else None
    c = compute(conn, product, mp, offers, rule, extra, manual_price=manual_price,
                manual_category=mcat, manual_attributes=mattr)
    e = c["estimate"]
    status = _publish_status(d["marketplace_code"])
    return {
        "draft": {"id": d["id"], "status": d["status"], "status_label": DRAFT_STATUS_TR.get(d["status"], d["status"]),
                  "marketplace": d["marketplace_code"], "marketplace_name": d["marketplace_name"]},
        "publish": {**status, "will_send": False,
                    "message": "Gönderim yapılmaz. " + status["reason"]},
        "valid": not c["errors"], "errors": c["errors"], "warnings": c["warnings"],
        "required_attributes": c["required_attributes"],
        "pricing": None if e is None else {
            "price": e.price, "commission_rate": rule.commission_rate, "commission": e.commission,
            "shipping": e.shipping, "fixed": e.fixed, "cost": e.cost, "vat": e.vat, "profit": e.profit,
            "margin": e.margin, "min_margin_rate": rule.min_margin_rate, "is_estimate": True},
        "payload": {"barcode": product["barcode"], "sku": product["sku"], "model_code": product["model_code"],
                    "title": product["name"], "brand": product["brand"], "category_id": c["category_id"],
                    "category_name": c["category_name"], "attributes": c["attributes"], "price": c["price"],
                    "stock": c["stock"], "vat_rate": product["vat_rate"], "desi": product["desi"],
                    "description": product["description"], "images": product["images"] or [],
                    "supplier": c["supplier_name"]},
        "payload_note": "TrendHub alanları; pazaryeri API şeması değildir.",
    }


@router.post("/api/listing-drafts/validate")
def validate_drafts(body: IdsIn, _: CurrentUser = Depends(operator), conn: Connection = Depends(get_conn)):
    ids = revalidate(conn, body.ids)
    counts = row(conn, """SELECT COUNT(*) FILTER (WHERE status = 'invalid') AS invalid,
                                 COUNT(*) FILTER (WHERE status <> 'invalid') AS valid
                            FROM listing_drafts WHERE id = ANY(:ids)""", ids=ids)
    return {"validated": len(ids), **counts}


@router.post("/api/listing-drafts/prepare")
def prepare_drafts(body: IdsIn, request: Request, user: CurrentUser = Depends(operator),
                   conn: Connection = Depends(get_conn)):
    """Adım: Yayına Hazırla — yeniden doğrular, hatasız taslakları 'ready' yapar. Pazaryerine GÖNDERMEZ."""
    revalidate(conn, body.ids)
    ready = conn.execute(text("""UPDATE listing_drafts SET status = 'ready', updated_at = NOW()
                                 WHERE id = ANY(:ids) AND status = 'draft' AND errors = '[]'::jsonb"""),
                         {"ids": body.ids}).rowcount
    already = conn.execute(text("SELECT COUNT(*) FROM listing_drafts WHERE id = ANY(:ids) AND status = 'ready'"),
                           {"ids": body.ids}).scalar()
    invalid = conn.execute(text("SELECT COUNT(*) FROM listing_drafts WHERE id = ANY(:ids) AND status = 'invalid'"),
                           {"ids": body.ids}).scalar()
    log_audit(conn, actor=user.username, user_id=user.id, action="listing_draft.prepared", entity_type="listing_draft",
              ip=client_ip(request), details={"ids": body.ids[:200], "ready": ready})
    return {"ready": already, "newly_ready": ready, "invalid": invalid,
            "message": "Taslaklar yayına hazır. Pazaryerine otomatik gönderim kapalıdır (salt okunur mod)."}


@router.post("/api/listing-drafts/cancel")
def cancel_drafts(body: IdsIn, request: Request, user: CurrentUser = Depends(operator),
                  conn: Connection = Depends(get_conn)):
    n = conn.execute(text("UPDATE listing_drafts SET status = 'cancelled', updated_at = NOW() WHERE id = ANY(:ids)"),
                     {"ids": body.ids}).rowcount
    log_audit(conn, actor=user.username, user_id=user.id, action="listing_draft.cancelled", entity_type="listing_draft",
              ip=client_ip(request), details={"ids": body.ids[:200]})
    return {"cancelled": n}


EXPORT_COLUMNS = [("barcode", "Barkod"), ("sku", "Stok Kodu"), ("model_code", "Model Kodu"), ("name", "Ürün Adı"),
                  ("brand", "Marka"), ("category_id", "Kategori ID"), ("category_name", "Kategori"),
                  ("price", "Satış Fiyatı"), ("stock", "Stok"), ("vat_rate", "KDV"), ("desi", "Desi"),
                  ("images", "Görseller"), ("description", "Açıklama"), ("supplier_name", "Tedarikçi")]


@router.get("/api/listing-drafts/export.csv")
def export_ready(marketplace: str = "trendyol", _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    mp = _mp(conn, marketplace)
    data = rows(conn, """
        SELECT p.barcode, p.sku, p.model_code, p.name, p.brand, d.category_id, d.category_name, d.price, d.stock,
               p.vat_rate, p.desi, p.images, p.description, s.name AS supplier_name
          FROM listing_drafts d JOIN products p ON p.id = d.product_id
          LEFT JOIN supplier_products sp ON sp.id = d.supplier_product_id LEFT JOIN suppliers s ON s.id = sp.supplier_id
         WHERE d.marketplace_id = :m AND d.status = 'ready' ORDER BY p.name
    """, m=mp["id"])
    buf = io.StringIO()
    buf.write("﻿")
    w = csv.writer(buf, delimiter=";")
    w.writerow([h for _, h in EXPORT_COLUMNS])
    for r in data:
        r["images"] = " | ".join(r["images"] or [])
        w.writerow([_csv_safe(r.get(k)) for k, _ in EXPORT_COLUMNS])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="trendhub-hazir-{mp["code"]}.csv"'})


# ------------------------------------------------------------ pazaryeri kuralları
@router.get("/api/marketplace-rules")
def list_rules(_: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    from ..connectors.registry import CONNECTOR_CLASSES, get_connector
    out = []
    for mp in rows(conn, "SELECT id, code, name FROM marketplaces ORDER BY id"):
        if mp["code"] in CONNECTOR_CLASSES:
            c = get_connector(mp["code"])
            connector = {"exists": True, "connected": c.is_configured(), **c.publish_status()}
        else:
            connector = {"exists": False, "connected": False, "can_publish": False,
                         "reason": "Bu pazaryeri için connector yok; taslaklar CSV ile dışa aktarılır."}
        rule, extra = marketplace_rule(conn, mp)
        stored = row(conn, "SELECT commission_rate FROM marketplace_rules WHERE marketplace_id = :m", m=mp["id"]) or {}
        out.append({"code": mp["code"], "name": mp["name"], "commission_rate": stored.get("commission_rate"),
                    "effective_commission_rate": rule.commission_rate, "markup_rate": rule.markup_rate,
                    "fixed_cost": rule.fixed_cost, "shipping_cost": rule.shipping_cost,
                    "min_margin_rate": rule.min_margin_rate, "rounding": rule.rounding, "connector": connector, **extra})
    return {"items": out, "requirable_fields": REQUIRABLE}


@router.put("/api/marketplace-rules/{code}")
def put_rule(code: str, body: RuleIn, request: Request, user: CurrentUser = Depends(admin),
             conn: Connection = Depends(get_conn)):
    mp = _mp(conn, code)
    bad = set(body.required_fields) - set(REQUIRABLE)
    if bad:
        raise HTTPException(422, f"Bilinmeyen zorunlu alan: {', '.join(sorted(bad))}")
    conn.execute(text("""
        INSERT INTO marketplace_rules(marketplace_id, commission_rate, markup_rate, fixed_cost, shipping_cost,
               min_margin_rate, rounding, stock_buffer, min_stock, max_stock, title_max_length, required_fields, updated_at)
        VALUES (:m, :commission_rate, :markup_rate, :fixed_cost, :shipping_cost, :min_margin_rate, :rounding,
                :stock_buffer, :min_stock, :max_stock, :title_max_length, CAST(:rf AS JSONB), NOW())
        ON CONFLICT (marketplace_id) DO UPDATE SET commission_rate = EXCLUDED.commission_rate,
               markup_rate = EXCLUDED.markup_rate, fixed_cost = EXCLUDED.fixed_cost, shipping_cost = EXCLUDED.shipping_cost,
               min_margin_rate = EXCLUDED.min_margin_rate, rounding = EXCLUDED.rounding, stock_buffer = EXCLUDED.stock_buffer,
               min_stock = EXCLUDED.min_stock, max_stock = EXCLUDED.max_stock, title_max_length = EXCLUDED.title_max_length,
               required_fields = EXCLUDED.required_fields, updated_at = NOW()
    """), {**body.model_dump(exclude={"required_fields"}), "m": mp["id"], "rf": json.dumps(body.required_fields)})
    log_audit(conn, actor=user.username, user_id=user.id, action="marketplace_rule.updated", entity_type="marketplace",
              entity_id=mp["id"], ip=client_ip(request), details=body.model_dump(mode="json"))
    return {"ok": True}


# ------------------------------------------------------------ kategori eşleştirme
@router.get("/api/category-mappings")
def list_category_mappings(marketplace: str = "trendyol", _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    mp = _mp(conn, marketplace)
    items = rows(conn, """
        WITH cats AS (
            SELECT category AS source_category, COUNT(*) AS product_count FROM products
             WHERE category IS NOT NULL AND category <> '' GROUP BY category
            UNION ALL
            SELECT category, 0 FROM supplier_products WHERE category IS NOT NULL AND category <> '' GROUP BY category
        )
        SELECT c.source_category, SUM(c.product_count) AS product_count, m.target_category_id, m.target_category_name,
               COALESCE(m.attributes, '{}'::jsonb) AS attributes,
               COALESCE(m.required_attributes, '[]'::jsonb) AS required_attributes
          FROM cats c LEFT JOIN marketplace_category_mappings m
            ON m.marketplace_id = :m AND m.source_category = c.source_category
         GROUP BY c.source_category, m.target_category_id, m.target_category_name, m.attributes, m.required_attributes
         ORDER BY (m.target_category_id IS NULL) DESC, SUM(c.product_count) DESC, c.source_category LIMIT 500
    """, m=mp["id"])
    return {"marketplace": mp, "items": items}


@router.put("/api/category-mappings")
def put_category_mapping(body: CategoryMapIn, request: Request, user: CurrentUser = Depends(operator),
                         conn: Connection = Depends(get_conn)):
    mp = _mp(conn, body.marketplace)
    conn.execute(text("""
        INSERT INTO marketplace_category_mappings(marketplace_id, source_category, target_category_id, target_category_name,
               attributes, required_attributes, updated_at)
        VALUES (:m, :s, :t, :n, CAST(:a AS JSONB), CAST(:r AS JSONB), NOW())
        ON CONFLICT (marketplace_id, source_category) DO UPDATE SET target_category_id = EXCLUDED.target_category_id,
               target_category_name = EXCLUDED.target_category_name,
               attributes = CASE WHEN :keep THEN marketplace_category_mappings.attributes ELSE EXCLUDED.attributes END,
               required_attributes = CASE WHEN :keep_req THEN marketplace_category_mappings.required_attributes
                                          ELSE EXCLUDED.required_attributes END,
               updated_at = NOW()
    """), {"m": mp["id"], "s": body.source_category, "t": body.target_category_id.strip(),
           "n": body.target_category_name, "a": json.dumps(body.attributes or {}, ensure_ascii=False),
           "keep": body.attributes is None, "r": json.dumps(body.required_attributes or [], ensure_ascii=False),
           "keep_req": body.required_attributes is None})
    log_audit(conn, actor=user.username, user_id=user.id, action="category_mapping.updated", entity_type="marketplace",
              entity_id=mp["id"], ip=client_ip(request), details=body.model_dump())
    return {"ok": True}


@router.post("/api/marketplaces", status_code=201)
def add_marketplace(body: MarketplaceIn, request: Request, user: CurrentUser = Depends(admin),
                    conn: Connection = Depends(get_conn)):
    """Yeni pazaryeri (ör. n11, Çiçeksepeti) ekler: kendi kural ve kategori eşleştirmesiyle taslak
    hazırlanabilir. API connector'ı yoksa "Bağlı değil" kalır; sipariş/ilan verisi üretilmez."""
    mid = conn.execute(text("""INSERT INTO marketplaces(code, name, enabled) VALUES (:c, :n, FALSE)
                               ON CONFLICT (code) DO NOTHING RETURNING id"""), {"c": body.code, "n": body.name}).scalar()
    if mid is None:
        raise HTTPException(409, "Bu pazaryeri kodu zaten kayıtlı")
    conn.execute(text("INSERT INTO marketplace_rules(marketplace_id) VALUES (:m) ON CONFLICT DO NOTHING"), {"m": mid})
    log_audit(conn, actor=user.username, user_id=user.id, action="marketplace.created", entity_type="marketplace",
              entity_id=mid, ip=client_ip(request), details=body.model_dump())
    return {"id": mid}


# ------------------------------------------------------------ kontrollü yayın (WRITE)
class PublishPreviewIn(BaseModel):
    marketplace: str = Field(pattern=r"^[a-z0-9_]{2,40}$")
    draft_ids: list[int] = Field(min_length=1, max_length=1000)


class PublishConfirmIn(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    confirm: bool


@router.post("/api/publish/preview")
def publish_preview(body: PublishPreviewIn, request: Request, user: CurrentUser = Depends(admin),
                    conn: Connection = Depends(get_conn)):
    """Yayın önizlemesi + tek kullanımlık onay anahtarı. Pazaryerine istek GÖNDERMEZ."""
    from ..services import publishing
    try:
        pv = publishing.preview(conn, body.marketplace, body.draft_ids, user.id)
    except publishing.PublishError as exc:
        raise HTTPException(422, str(exc)) from exc
    log_audit(conn, actor=user.username, user_id=user.id, action="publish.previewed", entity_type="publish_request",
              entity_id=pv["request_id"], ip=client_ip(request),
              details={"marketplace": body.marketplace, "sendable": len(pv["sendable"]), "blocked": len(pv["blocked"]),
                       "can_confirm": pv["can_confirm"]})
    return pv


@router.post("/api/publish/confirm")
def publish_confirm(body: PublishConfirmIn, request: Request, user: CurrentUser = Depends(admin),
                    conn: Connection = Depends(get_conn)):
    from ..services import publishing
    if not body.confirm:
        raise HTTPException(422, "Gönderim için açık onay gerekli")
    try:
        result = publishing.confirm(conn, body.token, user.id)
    except publishing.PublishError as exc:
        raise HTTPException(409, str(exc)) from exc
    log_audit(conn, actor=user.username, user_id=user.id, action="publish.confirmed", entity_type="publish_request",
              ip=client_ip(request), details={k: v for k, v in result.items() if k != "gates"})
    return result


@router.get("/api/publish/history")
def publish_history(page: Page = Depends(), _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    total = conn.execute(text("SELECT COUNT(*) FROM listing_publications")).scalar()
    items = rows(conn, """
        SELECT lp.id, lp.status, lp.message, lp.external_ref, lp.attempts, lp.created_at, lp.updated_at,
               p.name AS product_name, p.barcode, m.name AS marketplace_name, u.username AS created_by_name
          FROM listing_publications lp JOIN products p ON p.id = lp.product_id
          JOIN marketplaces m ON m.id = lp.marketplace_id LEFT JOIN users u ON u.id = lp.created_by
         ORDER BY lp.id DESC LIMIT :lim OFFSET :off""", lim=page.page_size, off=page.offset)
    return paged(items, total, page)
