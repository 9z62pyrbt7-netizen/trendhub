"""AI döngüsünün ilk aşaması: VERİ NORMALİZASYONU + kapsam teşhisi.

Kök neden (ürün sınıflandırmasının "STAR 0 / PROFITABLE 0 / WATCH 1" çıkması):
  1. Sipariş kalemi ürünle yalnızca `products.sku/barcode` BİREBİR eşleşmesiyle bağlanıyordu (büyük/küçük harf, boşluk
     farkı eşleşmeyi bozar); pazaryeri ilanı (`marketplace_listings`) ve tedarikçi ürünü (`supplier_products`) eşleşmeleri
     kullanılmıyordu. Eşleşmeyen satış (`order_items.product_id IS NULL`) ürün analizinde HİÇ görünmez.
  2. Eşleşme veya maliyet SONRADAN oluşursa eski kalemler güncellenmiyordu: sipariş yükü değişmediği için sipariş senkronu
     yeniden hesap yapmaz; tedarikçi içe aktarımı maliyet yazınca geçmiş kalemlerin `unit_cost`'u 0 kalır →
     ürün sonsuza dek "maliyet eksik → NO_DATA".
Bu modül her döngüde (sınırlı, idempotent) eşleşmeyi tamamlar ve maliyeti bilinmeyen kalemli siparişleri yeniden hesaplar.
Eşik değiştirilmez; sınıflandırma kapsamı (neden NO_DATA?) ayrıca raporlanır.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ...db import row
from ..finance_service import recalculate_order
from .config import Window, thresholds

MAX_RECALC_PER_CYCLE = 2000

NORM = "UPPER(BTRIM({}))"


def backfill_product_links(conn: Connection) -> dict:
    """order_items.product_id boş kalemleri sırasıyla katalog SKU/barkod, ilan ve tedarikçi eşleşmesiyle bağlar.
    Birden fazla ürüne eşleşen anahtar KULLANILMAZ (yanlış ürüne kâr yazılmaz)."""
    out = {}
    sources = {
        "products.sku": "SELECT {k} AS k, MIN(id) AS pid, COUNT(DISTINCT id) AS n FROM products WHERE sku IS NOT NULL GROUP BY 1".format(k=NORM.format("sku")),
        "products.barcode": "SELECT {k} AS k, MIN(id) AS pid, COUNT(DISTINCT id) AS n FROM products WHERE barcode IS NOT NULL GROUP BY 1".format(k=NORM.format("barcode")),
        "listing.barcode": "SELECT {k} AS k, MIN(product_id) AS pid, COUNT(DISTINCT product_id) AS n FROM marketplace_listings WHERE product_id IS NOT NULL AND barcode IS NOT NULL GROUP BY 1".format(k=NORM.format("barcode")),
        "listing.sku": "SELECT {k} AS k, MIN(product_id) AS pid, COUNT(DISTINCT product_id) AS n FROM marketplace_listings WHERE product_id IS NOT NULL AND sku IS NOT NULL GROUP BY 1".format(k=NORM.format("sku")),
        "supplier.barcode": "SELECT {k} AS k, MIN(product_id) AS pid, COUNT(DISTINCT product_id) AS n FROM supplier_products WHERE product_id IS NOT NULL AND barcode IS NOT NULL GROUP BY 1".format(k=NORM.format("barcode")),
        "supplier.sku": "SELECT {k} AS k, MIN(product_id) AS pid, COUNT(DISTINCT product_id) AS n FROM supplier_products WHERE product_id IS NOT NULL AND supplier_sku IS NOT NULL GROUP BY 1".format(k=NORM.format("supplier_sku")),
    }
    for name, src in sources.items():
        col = "barcode" if name.endswith("barcode") else "sku"
        n = conn.execute(text(f"""
            UPDATE order_items i SET product_id = m.pid
              FROM ({src}) m
             WHERE i.product_id IS NULL AND m.n = 1 AND m.k <> '' AND {NORM.format('i.' + col)} = m.k""")).rowcount
        if n:
            out[name] = n
    return out


def recalc_missing_costs(conn: Connection, limit: int = MAX_RECALC_PER_CYCLE) -> int:
    """Maliyeti 0 olan ama artık maliyeti bilinen (ürün kartı veya maliyet geçmişi) kalemlerin siparişlerini yeniden hesaplar.
    Maliyeti zaten girilmiş kalemlerin snapshot'ı DEĞİŞMEZ (recalculate_order yalnızca 0 olanı doldurur)."""
    ids = conn.execute(text("""
        SELECT DISTINCT i.order_id FROM order_items i JOIN products p ON p.id = i.product_id
          JOIN orders o ON o.id = i.order_id
         WHERE COALESCE(i.unit_cost, 0) = 0 AND o.internal_status <> 'cancelled'
           AND (COALESCE(p.cost, 0) > 0 OR EXISTS (SELECT 1 FROM product_costs c WHERE c.product_id = p.id AND c.cost > 0))
         ORDER BY i.order_id DESC LIMIT :n"""), {"n": limit}).scalars().all()
    for oid in ids:
        recalculate_order(conn, oid)
    return len(ids)


def normalize(conn: Connection) -> dict:
    links = backfill_product_links(conn)
    recalced = recalc_missing_costs(conn)   # yeni bağlanan kalemler de burada maliyetini alır
    return {"linked_items": links, "linked_total": sum(links.values()), "recalculated_orders": recalced}


def coverage(conn: Connection, window: Window | None = None) -> dict:
    """Sınıflandırma kapsamı: satışların ne kadarı karar verilebilir durumda ve neden NO_DATA."""
    th = thresholds(conn)
    window = window or Window(th["analysis_days"])
    r = row(conn, """
        SELECT COUNT(*) AS lines,
               COUNT(*) FILTER (WHERE i.product_id IS NULL) AS unmatched_lines,
               COALESCE(SUM(i.unit_price * i.quantity) FILTER (WHERE i.product_id IS NULL), 0) AS unmatched_revenue,
               COALESCE(SUM(i.unit_price * i.quantity), 0) AS revenue,
               COUNT(*) FILTER (WHERE i.product_id IS NOT NULL AND COALESCE(i.unit_cost, 0) = 0) AS missing_cost_lines,
               COUNT(*) FILTER (WHERE COALESCE(i.unit_price, 0) <= 0) AS zero_price_lines,
               COUNT(DISTINCT i.product_id) AS products_sold
          FROM order_items i JOIN orders o ON o.id = i.order_id
         WHERE o.order_date >= :start AND o.order_date < :end AND o.internal_status <> 'cancelled'""", **window.params())
    from .agents import classified_products
    classes: dict[str, int] = {}
    nodata_reasons = {"low_units": 0, "missing_cost": 0}
    for p in classified_products(conn, window):
        classes[p["class"]] = classes.get(p["class"], 0) + 1
        if p["class"] == "NO_DATA":
            nodata_reasons["missing_cost" if p["missing_cost"] and p["units"] >= th["min_units_for_data"] else "low_units"] += 1
    catalog = row(conn, """SELECT COUNT(*) AS products, COUNT(*) FILTER (WHERE COALESCE(cost, 0) = 0) AS no_cost,
                                  COUNT(*) FILTER (WHERE COALESCE(sale_price, 0) <= 0) AS zero_price,
                                  COUNT(*) FILTER (WHERE stock_updated_at IS NULL OR stock_updated_at < NOW() - INTERVAL '3 days') AS stale_stock
                             FROM products WHERE COALESCE(is_active, TRUE)""")
    dup = conn.execute(text(f"""SELECT COUNT(*) FROM (SELECT {NORM.format('barcode')} FROM products WHERE barcode IS NOT NULL
                                 GROUP BY 1 HAVING COUNT(*) > 1) x""")).scalar()
    lines = int(r["lines"] or 0)
    decidable = sum(v for k, v in classes.items() if k != "NO_DATA")
    issues = []
    if r["unmatched_lines"]:
        issues.append(f"{r['unmatched_lines']} satış kalemi hiçbir ürünle eşleşmiyor (ciro {r['unmatched_revenue']:.2f} TL); "
                      "bu satışlar ürün kâr analizinde görünmez. Ürün SKU/barkodunu Trendyol stockCode/barkoduyla eşleştirin.")
    if r["missing_cost_lines"]:
        issues.append(f"{r['missing_cost_lines']} satış kaleminde ürün maliyeti yok; bu ürünler NO_DATA kalır.")
    if r["zero_price_lines"]:
        issues.append(f"{r['zero_price_lines']} satış kalemi 0 TL fiyatlı (veri hatası).")
    if catalog["zero_price"]:
        issues.append(f"Katalogda {catalog['zero_price']} aktif ürünün satış fiyatı 0 / boş.")
    if dup:
        issues.append(f"{dup} barkod birden fazla üründe tanımlı (duplicate); bu barkodlar eşleştirmede kullanılmaz.")
    if nodata_reasons["low_units"]:
        issues.append(f"{nodata_reasons['low_units']} ürün {window.days} günde {th['min_units_for_data']} adetten az sattı (karar için veri az).")
    return {"window": window.as_dict(), "sales_lines": lines, "unmatched_lines": int(r["unmatched_lines"] or 0),
            "unmatched_revenue": r["unmatched_revenue"], "revenue": r["revenue"],
            "matched_share": round(1 - (int(r["unmatched_lines"] or 0) / lines), 3) if lines else None,
            "missing_cost_lines": int(r["missing_cost_lines"] or 0), "zero_price_lines": int(r["zero_price_lines"] or 0),
            "products_sold": int(r["products_sold"] or 0), "classes": classes, "decidable_products": decidable,
            "no_data_reasons": nodata_reasons, "catalog": catalog, "duplicate_barcodes": int(dup or 0), "issues": issues}
