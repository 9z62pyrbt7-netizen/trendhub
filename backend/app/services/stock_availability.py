"""Kanallar arası kullanılabilir stok (overselling koruması).

TrendHub'da tek merkezi stok `products.stock`'tur: tedarikçi beslemesinden (seçilen teklif,
`supplier_catalog.refresh_catalog`) veya ilan içe aktarımından gelir ve `stock_updated_at` ile
zaman damgalanır. Pazaryerlerine stok yazımı bilinçli olarak kapalıdır (canlı Trendyol → Çanta Bayim
otomasyonu ayrı sistemdedir), bu yüzden bir kanaldaki satış merkezi stok kaynağına ancak bir sonraki
tedarikçi senkronunda yansır.

Aradaki süre için kullanılabilir stok şöyle hesaplanır (hiçbir tabloyu DEĞİŞTİRMEZ):

    kullanılabilir = stok
                   − son stok güncellemesinden SONRA gelen, iptal/iade edilmemiş TÜM kanal siparişleri
                     (Trendyol, Hepsiburada, Amazon, web — yalnızca son `committed_window_hours` saat)
                   − süresi dolmamış stok ayırmaları (ödeme bekleyen web siparişleri)
                   − güvenlik payı

Böylece Trendyol'da satılan bir çanta, tedarikçi beslemesi güncellenmeden web sitesinde de düşer;
web satışı da panelde diğer kanalların kullanılabilir stoğunu azaltır. Pencere, tedarikçi aynı
stok sayısını tekrar gönderdiğinde (stock_updated_at değişmediğinde) eski siparişlerin sonsuza
kadar düşülmesini engeller.
"""
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import app_settings

EXCLUDED_STATUSES = ("cancelled", "returned")


def params(conn: Connection) -> dict:
    return {
        "buffer": max(0, int(app_settings.get(conn, "storefront.stock_buffer", 0) or 0)),
        "window_hours": max(1, int(app_settings.get(conn, "storefront.committed_window_hours", 48) or 48)),
    }


def available_sql(alias: str = "p") -> str:
    """`products` satırı için kullanılabilir stok ifadesi (:buffer, :window_hours parametreleri gerekir)."""
    return f"""GREATEST(0, COALESCE({alias}.stock, 0)
        - COALESCE((SELECT SUM(i.quantity) FROM order_items i JOIN orders o ON o.id = i.order_id
                     WHERE i.product_id = {alias}.id
                       AND o.internal_status NOT IN ('cancelled', 'returned')
                       AND o.order_date > GREATEST(COALESCE({alias}.stock_updated_at, 'epoch'::timestamptz),
                                                   NOW() - make_interval(hours => :window_hours))), 0)
        - COALESCE((SELECT SUM(r.quantity) FROM stock_reservations r
                     WHERE r.product_id = {alias}.id AND r.released_at IS NULL AND r.expires_at > NOW()), 0)
        - :buffer)"""


def available_map(conn: Connection, product_ids: Iterable[int], *, lock: bool = False) -> dict[int, int]:
    """Ürün başına kullanılabilir stok. `lock=True` ürün satırlarını (id sırasıyla) kilitler:
    aynı ürün için eşzamanlı iki web siparişi birbirini görmeden stoğu aşamaz."""
    ids = sorted({int(i) for i in product_ids})
    if not ids:
        return {}
    if lock:
        conn.execute(text("SELECT id FROM products WHERE id = ANY(:ids) ORDER BY id FOR UPDATE"), {"ids": ids})
    result = conn.execute(text(f"SELECT p.id, {available_sql('p')} AS available FROM products p WHERE p.id = ANY(:ids)"),
                          {"ids": ids, **params(conn)})
    return {int(r.id): int(r.available) for r in result}
