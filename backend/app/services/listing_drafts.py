"""Ürün aktarımı: katalog ürünü -> pazaryeri taslak ilanı (YALNIZCA TrendHub DB).

Akış: Tedarikçiler → Ürün Havuzu → Ürünleri Seç → Pazaryerini Seç → Fiyatlandır → Validate → Yayına Hazırla

* Her pazaryeri bağımsızdır: kendi fiyat/komisyon/stok/zorunlu alan kuralı
  (marketplace_rules) ve kategori eşleştirmesi (marketplace_category_mappings) vardır.
* Bir katalog ürünü bir pazaryerinde TEK taslağa sahiptir (UNIQUE product_id+marketplace_id);
  ürün iki tedarikçide olsa bile iki ilan oluşmaz. Pazaryerinde zaten ilanı varsa taslak
  hata verir.
* "Yayına hazır" yalnızca bir durumdur. Pazaryerine gönderim KAPALIDIR
  (CONNECTOR_WRITE_ENABLED=false); hazır taslaklar CSV olarak dışa aktarılabilir.
"""
from __future__ import annotations

import json
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..db import row, rows
from ..domain.pricing import PricingRule, estimate_profit, suggest_price, validate_draft
from ..domain.suppliers import effective_stock, select_offer
from . import app_settings
from .supplier_catalog import load_offers

DEFAULT_REQUIRED = ["barcode", "brand", "category", "images"]


def marketplace_rule(conn: Connection, marketplace: dict) -> tuple[PricingRule, dict]:
    r = row(conn, "SELECT * FROM marketplace_rules WHERE marketplace_id = :m", m=marketplace["id"]) or {}
    commission = r.get("commission_rate")
    if commission is None:
        commission = app_settings.get_decimal(conn, f"finance.commission_rate.{marketplace['code']}", "0.20")
    include_vat = str(app_settings.get(conn, "finance.include_vat", True)).lower() in ("true", "1")
    rule = PricingRule(commission_rate=Decimal(str(commission)),
                       markup_rate=Decimal(str(r.get("markup_rate", "0.30"))),
                       fixed_cost=Decimal(str(r.get("fixed_cost", "0"))),
                       shipping_cost=Decimal(str(r.get("shipping_cost", "0"))),
                       min_margin_rate=Decimal(str(r.get("min_margin_rate", "0.05"))),
                       rounding=r.get("rounding") or "x.90", include_vat=include_vat)
    extra = {"stock_buffer": r.get("stock_buffer") or 0, "min_stock": r.get("min_stock") if r.get("min_stock") is not None else 1,
             "max_stock": r.get("max_stock"), "title_max_length": r.get("title_max_length"),
             "required_fields": r.get("required_fields") if r.get("required_fields") is not None else DEFAULT_REQUIRED}
    return rule, extra


def _existing_listing(conn: Connection, marketplace_id: int, product: dict) -> int | None:
    return conn.execute(text("""
        SELECT l.id FROM marketplace_listings l JOIN stores s ON s.id = l.store_id
         WHERE s.marketplace_id = :m AND (l.product_id = :p OR (CAST(:b AS TEXT) IS NOT NULL AND l.barcode = :b))
         ORDER BY l.id LIMIT 1
    """), {"m": marketplace_id, "p": product["id"], "b": product.get("barcode") or None}).scalar()


def category_mapping(conn: Connection, marketplace_id: int, source_category: str | None) -> dict:
    return row(conn, """SELECT target_category_id, target_category_name, attributes, required_attributes
                          FROM marketplace_category_mappings WHERE marketplace_id = :m AND source_category = :c""",
               m=marketplace_id, c=source_category or "") or {}


def compute(conn: Connection, product: dict, marketplace: dict, offers, rule: PricingRule, extra: dict,
            manual_price: Decimal | None = None, manual_category: dict | None = None,
            manual_attributes: dict | None = None) -> dict:
    """Taslağın fiyat/stok/kategori/özellik ve doğrulama sonucunu hesaplar.

    Kategori: elle girilmişse o, yoksa pazaryeri kategori eşleştirmesi. Özellikler: eşleştirmedeki
    varsayılanlar + taslakta elle girilenler (elle girilen üstün). Zorunlu özellikler eşleştirmeden gelir."""
    strategy = product.get("supplier_strategy") or "manual"
    offer = select_offer(offers, strategy, product.get("preferred_supplier_id"))
    if offer is None and strategy == "manual" and not product.get("preferred_supplier_id"):
        offer = select_offer(offers, "cheapest", None)
    cost = offer.cost if offer else None
    stock = effective_stock(offer.stock if offer else 0, {"buffer": extra["stock_buffer"],
                                                          "min_stock": extra["min_stock"],
                                                          "max_stock": extra["max_stock"]})
    price = manual_price
    if price is None and cost is not None and cost > 0:
        price = suggest_price(cost, rule)
    estimate = estimate_profit(price, cost, rule, Decimal(str(product.get("vat_rate") or 20))) \
        if price is not None and cost is not None else None
    mapping = category_mapping(conn, marketplace["id"], product.get("category"))
    cat = manual_category if manual_category is not None else mapping
    attributes = {**(mapping.get("attributes") or {}), **(manual_attributes or {})}
    required_attrs = list(mapping.get("required_attributes") or []) if manual_category is None else []
    existing = _existing_listing(conn, marketplace["id"], product)
    check = validate_draft(product=product, price=price, stock=stock, cost=cost,
                           category_id=cat.get("target_category_id"), estimate=estimate, rule=rule,
                           required_fields=list(extra["required_fields"]), title_max_length=extra["title_max_length"],
                           min_stock=int(extra["min_stock"] or 0), existing_listing=bool(existing),
                           attributes=attributes, required_attributes=required_attrs)
    if offer is None:
        check.errors.insert(0, "Kullanılabilir tedarikçi teklifi yok (stokta ve fiyatı olan)")
    return {"supplier_product_id": offer.supplier_product_id if offer else None,
            "supplier_name": offer.supplier_name if offer else None,
            "price": price, "stock": stock, "cost_basis": cost, "commission_rate": rule.commission_rate,
            "estimated_profit": estimate.profit if estimate else None,
            "estimated_margin": estimate.margin if estimate else None,
            "category_id": cat.get("target_category_id"), "category_name": cat.get("target_category_name"),
            "attributes": attributes, "required_attributes": required_attrs, "existing_listing_id": existing,
            "category_is_manual": manual_category is not None, "attributes_override": manual_attributes or {},
            "estimate": estimate, "errors": check.errors, "warnings": check.warnings}


def _products(conn: Connection, ids: list[int]) -> list[dict]:
    return rows(conn, """SELECT id, sku, barcode, name, brand, category, model_code, description, images, vat_rate,
                                desi, preferred_supplier_id, supplier_strategy
                           FROM products WHERE id = ANY(:ids) ORDER BY id""", ids=list(ids))


def _save(conn: Connection, product_id: int, marketplace_id: int, c: dict, *, price_is_manual: bool,
          user_id: int | None, keep_ready: bool) -> int:
    status = "invalid" if c["errors"] else "draft"
    return conn.execute(text("""
        INSERT INTO listing_drafts(product_id, marketplace_id, supplier_product_id, price, price_is_manual, stock,
               cost_basis, commission_rate, estimated_profit, estimated_margin, category_id, category_name, attributes,
               status, errors, warnings, existing_listing_id, created_by, validated_at, updated_at,
               category_is_manual, attributes_override)
        VALUES (:p, :m, :sp, :price, :manual, :stock, :cost, :comm, :profit, :margin, :cat_id, :cat_name,
                CAST(:attrs AS JSONB), :status, CAST(:errors AS JSONB), CAST(:warnings AS JSONB), :existing, :u, NOW(), NOW(),
                :cat_manual, CAST(:attrs_override AS JSONB))
        ON CONFLICT (product_id, marketplace_id) DO UPDATE SET
               supplier_product_id = EXCLUDED.supplier_product_id, price = EXCLUDED.price,
               price_is_manual = EXCLUDED.price_is_manual, stock = EXCLUDED.stock, cost_basis = EXCLUDED.cost_basis,
               commission_rate = EXCLUDED.commission_rate, estimated_profit = EXCLUDED.estimated_profit,
               estimated_margin = EXCLUDED.estimated_margin, category_id = EXCLUDED.category_id,
               category_name = EXCLUDED.category_name, attributes = EXCLUDED.attributes,
               status = CASE WHEN EXCLUDED.status = 'draft' AND :keep AND listing_drafts.status = 'ready'
                             THEN 'ready' ELSE EXCLUDED.status END,
               errors = EXCLUDED.errors, warnings = EXCLUDED.warnings, existing_listing_id = EXCLUDED.existing_listing_id,
               category_is_manual = EXCLUDED.category_is_manual, attributes_override = EXCLUDED.attributes_override,
               validated_at = NOW(), updated_at = NOW()
        RETURNING id
    """), {"p": product_id, "m": marketplace_id, "sp": c["supplier_product_id"], "price": c["price"],
           "manual": price_is_manual, "stock": c["stock"], "cost": c["cost_basis"], "comm": c["commission_rate"],
           "profit": c["estimated_profit"], "margin": c["estimated_margin"], "cat_id": c["category_id"],
           "cat_name": c["category_name"], "attrs": json.dumps(c["attributes"], ensure_ascii=False),
           "status": status, "errors": json.dumps(c["errors"], ensure_ascii=False),
           "warnings": json.dumps(c["warnings"], ensure_ascii=False), "existing": c["existing_listing_id"],
           "u": user_id, "keep": keep_ready, "cat_manual": c["category_is_manual"],
           "attrs_override": json.dumps(c["attributes_override"], ensure_ascii=False)}).scalar()


def marketplaces_by_code(conn: Connection, codes: list[str]) -> list[dict]:
    mps = rows(conn, "SELECT id, code, name FROM marketplaces WHERE code = ANY(:c) ORDER BY id", c=list(codes))
    missing = set(codes) - {m["code"] for m in mps}
    if missing:
        raise ValueError(f"Bilinmeyen pazaryeri: {', '.join(sorted(missing))}")
    return mps


def build_drafts(conn: Connection, product_ids: list[int], marketplace_codes: list[str], user_id: int | None) -> dict:
    """Seçilen ürünler × seçilen pazaryerleri için taslak oluşturur/günceller ve doğrular."""
    mps = marketplaces_by_code(conn, marketplace_codes)
    products = _products(conn, product_ids)
    offers = load_offers(conn, [p["id"] for p in products])
    existing = {(r["product_id"], r["marketplace_id"]): r for r in rows(conn, """
        SELECT product_id, marketplace_id, price, price_is_manual, category_id, category_name, category_is_manual,
               attributes, attributes_override FROM listing_drafts
         WHERE product_id = ANY(:p) AND marketplace_id = ANY(:m)
    """, p=[p["id"] for p in products], m=[m["id"] for m in mps])}
    ids, valid, invalid = [], 0, 0
    for mp in mps:
        rule, extra = marketplace_rule(conn, mp)
        for p in products:
            prev = existing.get((p["id"], mp["id"]))
            manual = Decimal(prev["price"]) if prev and prev["price_is_manual"] and prev["price"] is not None else None
            mcat, mattr = _manual_parts(prev)
            c = compute(conn, p, mp, offers.get(p["id"], []), rule, extra, manual_price=manual,
                        manual_category=mcat, manual_attributes=mattr)
            ids.append(_save(conn, p["id"], mp["id"], c, price_is_manual=manual is not None, user_id=user_id,
                             keep_ready=True))
            valid += 0 if c["errors"] else 1
            invalid += 1 if c["errors"] else 0
    return {"draft_ids": ids, "valid": valid, "invalid": invalid}


def _manual_parts(d: dict | None) -> tuple[dict | None, dict | None]:
    if not d:
        return None, None
    mcat = ({"target_category_id": d["category_id"], "target_category_name": d["category_name"]}
            if d.get("category_is_manual") and d.get("category_id") else None)
    mattr = d.get("attributes_override") or None
    return mcat, mattr


def revalidate(conn: Connection, draft_ids: list[int], *, price: Decimal | None = None,
               reset_price: bool = False, category: dict | None = None, reset_category: bool = False,
               attributes: dict | None = None, reset_attributes: bool = False) -> list[int]:
    # Sabit sırayla kilitle: eşzamanlı doğrulamalar birbirini bekler, kilitlenme (deadlock) olmaz.
    drafts = rows(conn, """SELECT d.*, m.code AS marketplace_code FROM listing_drafts d
                             JOIN marketplaces m ON m.id = d.marketplace_id WHERE d.id = ANY(:ids)
                            ORDER BY d.id FOR UPDATE OF d""", ids=list(draft_ids))
    products = {p["id"]: p for p in _products(conn, [d["product_id"] for d in drafts])}
    offers = load_offers(conn, list(products))
    out = []
    for d in drafts:
        mp = {"id": d["marketplace_id"], "code": d["marketplace_code"]}
        rule, extra = marketplace_rule(conn, mp)
        manual = price if price is not None else (
            None if reset_price or not d["price_is_manual"] else Decimal(d["price"]) if d["price"] is not None else None)
        mcat, mattr = _manual_parts(d)
        if category is not None:
            mcat = category
        if reset_category:
            mcat = None
        if attributes is not None:
            # Verilen değerler mevcut elle girilenlerin üstüne yazılır; boş değer o özelliği kaldırır.
            merged = {**(mattr or {}), **attributes}
            mattr = {k: v for k, v in merged.items() if v} or None
        if reset_attributes:
            mattr = None
        c = compute(conn, products[d["product_id"]], mp, offers.get(d["product_id"], []), rule, extra,
                    manual_price=manual, manual_category=mcat, manual_attributes=mattr)
        out.append(_save(conn, d["product_id"], d["marketplace_id"], c, price_is_manual=manual is not None,
                         user_id=None, keep_ready=True))
    return out
