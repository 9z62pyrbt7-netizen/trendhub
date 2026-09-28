"""Ürün aktarımı: havuzdan kataloğa, katalogdan pazaryeri taslak ilanlarına.

Tedarikçiler → Ürün Havuzu → Ürünleri Seç → Pazaryerini Seç → Fiyatlandır → Validate → Yayına Hazırla

Pazaryerine gönderim YOKTUR (CONNECTOR_WRITE_ENABLED=false). "Yayına hazır" taslaklar
yalnızca TrendHub'da tutulur ve CSV olarak dışa aktarılabilir.
"""
import csv
import io
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
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


class DraftPatch(BaseModel):
    price: Decimal | None = Field(None, ge=0, le=Decimal("10000000"))
    auto_price: bool = False
    category_id: str | None = Field(None, max_length=100)
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
               d.existing_listing_id, d.validated_at, d.updated_at, s.name AS supplier_name
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
    cat = None
    if body.category_id is not None:
        cat = {"target_category_id": body.category_id.strip() or None, "target_category_name": body.category_name,
               "attributes": {}}
    revalidate(conn, [draft_id], price=None if body.auto_price else body.price, reset_price=body.auto_price,
               category=cat)
    log_audit(conn, actor=user.username, user_id=user.id, action="listing_draft.updated", entity_type="listing_draft",
              entity_id=draft_id, ip=client_ip(request), details=body.model_dump(mode="json"))
    return row(conn, "SELECT id, price, status, errors, warnings, estimated_profit, estimated_margin FROM listing_drafts WHERE id = :id",
               id=draft_id)


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
    out = []
    for mp in rows(conn, "SELECT id, code, name FROM marketplaces ORDER BY id"):
        rule, extra = marketplace_rule(conn, mp)
        stored = row(conn, "SELECT commission_rate FROM marketplace_rules WHERE marketplace_id = :m", m=mp["id"]) or {}
        out.append({"code": mp["code"], "name": mp["name"], "commission_rate": stored.get("commission_rate"),
                    "effective_commission_rate": rule.commission_rate, "markup_rate": rule.markup_rate,
                    "fixed_cost": rule.fixed_cost, "shipping_cost": rule.shipping_cost,
                    "min_margin_rate": rule.min_margin_rate, "rounding": rule.rounding, **extra})
    return {"items": out, "requirable_fields": REQUIRABLE}


@router.put("/api/marketplace-rules/{code}")
def put_rule(code: str, body: RuleIn, request: Request, user: CurrentUser = Depends(admin),
             conn: Connection = Depends(get_conn)):
    mp = _mp(conn, code)
    bad = set(body.required_fields) - set(REQUIRABLE)
    if bad:
        raise HTTPException(422, f"Bilinmeyen zorunlu alan: {', '.join(sorted(bad))}")
    import json
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
        SELECT c.source_category, SUM(c.product_count) AS product_count, m.target_category_id, m.target_category_name
          FROM cats c LEFT JOIN marketplace_category_mappings m
            ON m.marketplace_id = :m AND m.source_category = c.source_category
         GROUP BY c.source_category, m.target_category_id, m.target_category_name
         ORDER BY (m.target_category_id IS NULL) DESC, SUM(c.product_count) DESC, c.source_category LIMIT 500
    """, m=mp["id"])
    return {"marketplace": mp, "items": items}


@router.put("/api/category-mappings")
def put_category_mapping(body: CategoryMapIn, request: Request, user: CurrentUser = Depends(operator),
                         conn: Connection = Depends(get_conn)):
    mp = _mp(conn, body.marketplace)
    conn.execute(text("""
        INSERT INTO marketplace_category_mappings(marketplace_id, source_category, target_category_id, target_category_name, updated_at)
        VALUES (:m, :s, :t, :n, NOW())
        ON CONFLICT (marketplace_id, source_category) DO UPDATE SET target_category_id = EXCLUDED.target_category_id,
               target_category_name = EXCLUDED.target_category_name, updated_at = NOW()
    """), {"m": mp["id"], "s": body.source_category, "t": body.target_category_id.strip(),
           "n": body.target_category_name})
    log_audit(conn, actor=user.username, user_id=user.id, action="category_mapping.updated", entity_type="marketplace",
              entity_id=mp["id"], ip=client_ip(request), details=body.model_dump())
    return {"ok": True}
