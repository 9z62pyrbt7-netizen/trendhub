"""Tedarikçi teklifleri <-> global katalog.

Kimlikler birbirinden ayrıdır:
  * Tedarikçi SKU'su (`supplier_products.supplier_sku`) yalnızca o tedarikçi içinde tekildir.
  * Global katalog kimliği `products.id`'dir; tedarikçiler arası eşleşme BARKOD ile yapılır.
Aynı barkod iki tedarikçide varsa ikisi de aynı katalog ürününe bağlanır (tek ürün,
birden fazla teklif) ve pazaryerinde tek ilan açılır.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..db import rows
from ..domain.suppliers import Offer, effective_stock, select_offer


def link_by_barcode(conn: Connection, supplier_id: int | None = None) -> int:
    """Kataloğa bağlanmamış tedarikçi ürünlerini, aynı barkodlu katalog ürününe bağlar."""
    return conn.execute(text("""
        WITH cand AS (
            SELECT DISTINCT ON (sp.supplier_id, p.id) sp.id AS spid, p.id AS pid
              FROM supplier_products sp
              JOIN LATERAL (SELECT id FROM products WHERE barcode = sp.barcode ORDER BY id LIMIT 1) p ON TRUE
             WHERE sp.product_id IS NULL AND sp.barcode IS NOT NULL AND sp.barcode <> ''
               AND (CAST(:s AS BIGINT) IS NULL OR sp.supplier_id = :s)
               AND NOT EXISTS (SELECT 1 FROM supplier_products x
                                WHERE x.supplier_id = sp.supplier_id AND x.product_id = p.id)
             ORDER BY sp.supplier_id, p.id, sp.id
        )
        UPDATE supplier_products sp SET product_id = cand.pid, updated_at = NOW()
          FROM cand WHERE sp.id = cand.spid
    """), {"s": supplier_id}).rowcount


def load_offers(conn: Connection, product_ids: list[int]) -> dict[int, list[Offer]]:
    if not product_ids:
        return {}
    out: dict[int, list[Offer]] = defaultdict(list)
    for r in rows(conn, """
        SELECT sp.id, sp.product_id, sp.supplier_id, s.name AS supplier_name, sp.cost, sp.stock, s.priority,
               s.stock_rules, s.is_active, COALESCE(sp.status, 'active') AS status
          FROM supplier_products sp JOIN suppliers s ON s.id = sp.supplier_id
         WHERE sp.product_id = ANY(:ids)
    """, ids=list(product_ids)):
        out[r["product_id"]].append(Offer(
            supplier_product_id=r["id"], supplier_id=r["supplier_id"], supplier_name=r["supplier_name"],
            cost=Decimal(r["cost"]) if r["cost"] is not None else None,
            stock=effective_stock(r["stock"], r["stock_rules"]), priority=int(r["priority"] or 100),
            available=bool(r["is_active"]) and r["status"] == "active"))
    return out


def refresh_catalog(conn: Connection, product_ids: list[int]) -> int:
    """Seçilen teklife göre katalog ürününün stok ve maliyetini günceller (yalnızca TrendHub DB).

    Strateji 'manual' ve tercih edilen tedarikçi yoksa ürüne dokunulmaz (elle yönetilen stok).
    Seçilebilir teklif kalmadıysa stok 0 yapılır: kaynağı olmayan ürün satılmasın."""
    if not product_ids:
        return 0
    offers = load_offers(conn, product_ids)
    products = rows(conn, """SELECT id, stock, cost, preferred_supplier_id, COALESCE(supplier_strategy, 'manual') AS strategy
                               FROM products WHERE id = ANY(:ids)""", ids=list(product_ids))
    changed = 0
    for p in products:
        if p["strategy"] == "manual" and not p["preferred_supplier_id"]:
            continue
        sel = select_offer(offers.get(p["id"], []), p["strategy"], p["preferred_supplier_id"])
        new_stock = sel.stock if sel else 0
        if int(p["stock"] or 0) != new_stock:
            conn.execute(text("UPDATE products SET stock = :s, stock_updated_at = NOW(), updated_at = NOW() WHERE id = :id"),
                         {"s": new_stock, "id": p["id"]})
            changed += 1
        if sel and sel.cost is not None and sel.cost > 0 and Decimal(p["cost"] or 0) != sel.cost:
            conn.execute(text("""INSERT INTO product_costs(product_id, cost, source, note)
                                 VALUES (:p, :c, 'supplier', :n)"""),
                         {"p": p["id"], "c": sel.cost, "n": f"{sel.supplier_name} teklifi"[:300]})
            conn.execute(text("UPDATE products SET cost = :c, updated_at = NOW() WHERE id = :id"),
                         {"c": sel.cost, "id": p["id"]})
            changed += 1
    return changed


def import_to_catalog(conn: Connection, supplier_product_ids: list[int]) -> dict:
    """Seçilen havuz ürünlerini kataloğa alır.

    Barkodu katalogda varsa MEVCUT ürüne bağlanır (duplicate açılmaz); yoksa yeni katalog
    ürünü oluşturulur. Yeni ürünün SKU'su tedarikçi kodu + tedarikçi SKU'sudur
    (tedarikçi SKU'su global kimlik olmadığı için çakışmaz); tercih edilen tedarikçi
    bu tedarikçi olur."""
    created = linked = already = 0
    product_ids: list[int] = []
    items = rows(conn, """
        SELECT sp.*, s.code AS supplier_code FROM supplier_products sp JOIN suppliers s ON s.id = sp.supplier_id
         WHERE sp.id = ANY(:ids) ORDER BY sp.id FOR UPDATE OF sp
    """, ids=list(supplier_product_ids))
    for it in items:
        if it["product_id"]:
            already += 1
            product_ids.append(it["product_id"])
            continue
        pid = None
        if it["barcode"]:
            pid = conn.execute(text("SELECT id FROM products WHERE barcode = :b ORDER BY id LIMIT 1"),
                               {"b": it["barcode"]}).scalar()
        if pid is not None:
            taken = conn.execute(text("SELECT 1 FROM supplier_products WHERE supplier_id = :s AND product_id = :p"),
                                 {"s": it["supplier_id"], "p": pid}).first()
            if taken:
                continue  # aynı tedarikçide aynı barkodlu ikinci satır: bağlanmaz
            linked += 1
        else:
            sku = f"{it['supplier_code']}-{it['supplier_sku']}"[:100]
            if conn.execute(text("SELECT 1 FROM products WHERE sku = :s"), {"s": sku}).first():
                sku = f"{sku[:90]}-{it['id']}"
            pid = conn.execute(text("""
                INSERT INTO products(sku, barcode, name, brand, category, model_code, description, images,
                                     cost, sale_price, stock, vat_rate, desi, preferred_supplier_id, supplier_strategy,
                                     stock_updated_at, updated_at)
                VALUES (:sku, :barcode, :name, :brand, :category, :model_code, :description, CAST(:images AS JSONB),
                        0, COALESCE(:sale_price, 0), 0, COALESCE(:vat, 20), :desi, :sid, 'manual', NOW(), NOW())
                RETURNING id
            """), {"sku": sku, "barcode": it["barcode"], "name": it["name"] or it["supplier_sku"], "brand": it["brand"],
                   "category": it["category"], "model_code": it["model_code"], "description": it["description"],
                   "images": _json(it["images"]), "sale_price": it["sale_price"], "vat": it["vat_rate"],
                   "desi": it["desi"], "sid": it["supplier_id"]}).scalar()
            # Bu SKU/barkod ile gelmiş, bağlanmamış sipariş kalemleri
            conn.execute(text("""UPDATE order_items SET product_id = :p WHERE product_id IS NULL
                                 AND (sku = :s OR (CAST(:b AS TEXT) IS NOT NULL AND barcode = :b))"""),
                         {"p": pid, "s": sku, "b": it["barcode"]})
            created += 1
        conn.execute(text("UPDATE supplier_products SET product_id = :p, updated_at = NOW() WHERE id = :id"),
                     {"p": pid, "id": it["id"]})
        product_ids.append(pid)
    product_ids = sorted(set(product_ids))
    refresh_catalog(conn, product_ids)
    return {"created": created, "linked": linked, "already_in_catalog": already, "product_ids": product_ids}


def _json(v) -> str:
    import json
    return json.dumps(v if isinstance(v, list) else [], ensure_ascii=False)
