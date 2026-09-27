"""Pazaryeri ilanlarının (ürün/stok/fiyat) salt okunur senkronizasyonu.

İlanlar `marketplace_listings` tablosuna yazılır; ana ürün kataloğu
(`products`) ve yerel stok DEĞİŞTİRİLMEZ. İlan, SKU (stockCode) veya barkod
ile mevcut ürüne bağlanır. Pazaryerine hiçbir şey gönderilmez.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..connectors.base import NormalizedListing


@dataclass
class ListingStats:
    received: int = 0
    upserted: int = 0
    linked: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _match_product(conn: Connection, sku: str | None, barcode: str | None) -> int | None:
    if sku:
        pid = conn.execute(text("SELECT id FROM products WHERE sku = :s ORDER BY id LIMIT 1"), {"s": sku}).scalar()
        if pid:
            return pid
    if barcode:
        return conn.execute(text("SELECT id FROM products WHERE barcode = :b ORDER BY id LIMIT 1"),
                            {"b": barcode}).scalar()
    return None


def upsert_listings(conn: Connection, store_id: int, listings: list[NormalizedListing]) -> ListingStats:
    stats = ListingStats(received=len(listings))
    for li in listings:
        sp = conn.begin_nested()
        try:
            pid = _match_product(conn, li.sku, li.barcode)
            conn.execute(text("""
                INSERT INTO marketplace_listings(product_id, store_id, external_product_id, barcode, sku, title,
                                                 listed_price, list_price, listed_stock, status, brand, category,
                                                 vat_rate, image_url, last_synced_at, updated_at)
                VALUES (:pid, :store, :ext, :barcode, :sku, :title, :price, :list_price, :stock, :status, :brand,
                        :category, :vat, :img, NOW(), NOW())
                ON CONFLICT (store_id, external_product_id) DO UPDATE SET
                    product_id = COALESCE(marketplace_listings.product_id, EXCLUDED.product_id),
                    barcode = EXCLUDED.barcode, sku = EXCLUDED.sku, title = EXCLUDED.title,
                    listed_price = EXCLUDED.listed_price, list_price = EXCLUDED.list_price,
                    listed_stock = EXCLUDED.listed_stock, status = EXCLUDED.status, brand = EXCLUDED.brand,
                    category = EXCLUDED.category, vat_rate = EXCLUDED.vat_rate, image_url = EXCLUDED.image_url,
                    last_synced_at = NOW(), updated_at = NOW()
            """), {**{k: v for k, v in asdict(li).items() if k not in ("external_product_id", "price", "stock",
                                                                        "vat_rate", "image_url")},
                   "pid": pid, "store": store_id, "ext": li.external_product_id, "price": li.price,
                   "stock": li.stock, "vat": li.vat_rate, "img": li.image_url})
            sp.commit()
            stats.upserted += 1
            stats.linked += 1 if pid else 0
        except Exception as exc:  # noqa: BLE001
            sp.rollback()
            stats.errors.append(f"{li.external_product_id}: {exc.__class__.__name__}: {exc}"[:300])
    return stats


def import_unlinked_as_products(conn: Connection, store_id: int | None = None) -> int:
    """Bağlanmamış ilanlardan (SKU'su olan) yerel ürün kartı oluşturur.

    Yalnızca TrendHub veritabanına yazar. Maliyet 0 (eksik) başlar;
    kullanıcı panelden girer. Aynı SKU'ya sahip ürün varsa yeni kart açılmaz.
    """
    where = "AND l.store_id = :store" if store_id else ""
    rows = conn.execute(text(f"""
        SELECT DISTINCT ON (l.sku) l.id, l.sku, l.barcode, l.title, l.brand, l.category, l.listed_price,
               l.listed_stock, l.vat_rate, l.image_url
          FROM marketplace_listings l
         WHERE l.product_id IS NULL AND l.sku IS NOT NULL AND l.sku <> '' {where}
           AND NOT EXISTS (SELECT 1 FROM products p WHERE p.sku = l.sku)
         ORDER BY l.sku, l.id
    """), {"store": store_id}).mappings().all()
    created = 0
    for r in rows:
        pid = conn.execute(text("""
            INSERT INTO products(sku, barcode, name, brand, category, cost, sale_price, stock, vat_rate, image_url,
                                 stock_updated_at, updated_at)
            VALUES (:sku, :barcode, :title, :brand, :category, 0, COALESCE(:price, 0), COALESCE(:stock, 0),
                    COALESCE(:vat, 20), :img, NOW(), NOW())
            RETURNING id
        """), {"sku": r["sku"], "barcode": r["barcode"], "title": r["title"] or r["sku"], "brand": r["brand"],
               "category": r["category"], "price": r["listed_price"], "stock": r["listed_stock"],
               "vat": r["vat_rate"], "img": r["image_url"]}).scalar()
        conn.execute(text("UPDATE marketplace_listings SET product_id = :p WHERE product_id IS NULL AND sku = :s"),
                     {"p": pid, "s": r["sku"]})
        conn.execute(text("""UPDATE order_items SET product_id = :p
                              WHERE product_id IS NULL AND (sku = :s OR (CAST(:b AS TEXT) IS NOT NULL AND barcode = :b))"""),
                     {"p": pid, "s": r["sku"], "b": r["barcode"]})
        created += 1
    return created
