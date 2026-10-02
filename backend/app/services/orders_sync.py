"""Normalize edilmiş siparişlerin veritabanına idempotent yazılması.

Idempotency:
  * Sipariş anahtarı: (store_id, external_order_id) - mevcut UNIQUE kısıt.
  * Kalem anahtarı: (order_id, external_line_id)   - partial unique index.
  * Sevkiyat anahtarı: (order_id, external_package_id).
  * Yük özeti (payload_hash) değişmediyse satır hiç güncellenmez; aynı
    senkronizasyon N kez çalışsa da sonuç ve geçmiş kayıtları aynıdır.
  * Eşzamanlı iki worker aynı siparişi yazarsa INSERT ... ON CONFLICT ve
    SELECT ... FOR UPDATE ile yarış güvenlidir.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..connectors.base import NormalizedOrder
from ..domain.order_status import resolve_sync_status
from .finance_service import recalculate_order


@dataclass
class SyncStats:
    received: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    status_changes: int = 0
    errors: list[str] = field(default_factory=list)
    # (sipariş id, ORDER_CREATED | ORDER_UPDATED, iç statü) — iç olay modeli için; iş sonucuna yazılmaz
    changes: list[tuple] = field(default_factory=list)

    def as_dict(self) -> dict:
        out = asdict(self)
        out.pop("changes", None)
        return out


def payload_hash(order: NormalizedOrder) -> str:
    data = asdict(order)
    data.pop("last_modified", None)
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def ensure_store(conn: Connection, marketplace_code: str, external_id: str, name: str) -> int:
    mp_id = conn.execute(text("SELECT id FROM marketplaces WHERE code = :c"), {"c": marketplace_code}).scalar()
    if mp_id is None:
        raise ValueError(f"marketplaces tablosunda '{marketplace_code}' yok")
    # Eşzamanlı oluşturmaya karşı marketplace bazlı kilit.
    conn.execute(text("SELECT pg_advisory_xact_lock(7342100, CAST(:m AS INTEGER))"), {"m": mp_id})
    sid = conn.execute(text("""
        SELECT id FROM stores WHERE marketplace_id = :m AND external_id = :e ORDER BY id LIMIT 1
    """), {"m": mp_id, "e": external_id}).scalar()
    if sid is None:
        sid = conn.execute(text("""
            INSERT INTO stores(marketplace_id, name, external_id) VALUES (:m, :n, :e) RETURNING id
        """), {"m": mp_id, "n": name, "e": external_id}).scalar()
    return int(sid)


def _find_product(conn: Connection, sku: str | None, barcode: str | None) -> int | None:
    if sku:
        pid = conn.execute(text("SELECT id FROM products WHERE sku = :s ORDER BY id LIMIT 1"), {"s": sku}).scalar()
        if pid:
            return pid
    if barcode:
        return conn.execute(text("SELECT id FROM products WHERE barcode = :b ORDER BY id LIMIT 1"),
                            {"b": barcode}).scalar()
    return None


def upsert_order(conn: Connection, store_id: int, order: NormalizedOrder, stats: SyncStats) -> int:
    h = payload_hash(order)
    existing = conn.execute(text("""
        SELECT id, internal_status, payload_hash FROM orders
         WHERE store_id = :s AND external_order_id = :e FOR UPDATE
    """), {"s": store_id, "e": order.external_order_id}).mappings().first()

    if existing is None:
        new_id = conn.execute(text("""
            INSERT INTO orders(store_id, external_order_id, status, internal_status, order_date,
                               currency, customer_name, customer_city, review_reason, payload_hash,
                               source, last_synced_at, created_at, updated_at)
            VALUES (:s, :e, :raw, :internal, :date, :cur, :cname, :ccity, :review, :h,
                    'sync', NOW(), NOW(), NOW())
            ON CONFLICT (store_id, external_order_id) DO NOTHING
            RETURNING id
        """), {"s": store_id, "e": order.external_order_id, "raw": order.marketplace_status,
               "internal": order.internal_status, "date": order.order_date, "cur": order.currency,
               "cname": order.customer_name, "ccity": order.customer_city,
               "review": order.review_reason, "h": h}).scalar()
        if new_id is None:  # başka bir süreç aynı anda ekledi
            return upsert_order(conn, store_id, order, stats)
        conn.execute(text("""
            INSERT INTO order_status_history(order_id, from_status, to_status, marketplace_status, source)
            VALUES (:o, NULL, :to, :raw, 'sync')
        """), {"o": new_id, "to": order.internal_status, "raw": order.marketplace_status})
        stats.created += 1
        order_id = int(new_id)
        stats.changes.append((order_id, "ORDER_CREATED", order.internal_status))
    else:
        order_id = int(existing["id"])
        if existing["payload_hash"] == h:
            conn.execute(text("UPDATE orders SET last_synced_at = NOW() WHERE id = :id"), {"id": order_id})
            stats.unchanged += 1
            return order_id
        decision = resolve_sync_status(existing["internal_status"], order.internal_status)
        conn.execute(text("""
            UPDATE orders SET status = :raw, internal_status = :internal, order_date = :date,
                   currency = :cur, customer_name = COALESCE(:cname, customer_name),
                   customer_city = COALESCE(:ccity, customer_city),
                   review_reason = CASE WHEN :internal = 'needs_review'
                                        THEN COALESCE(:review, review_reason) ELSE review_reason END,
                   payload_hash = :h, last_synced_at = NOW(), updated_at = NOW()
             WHERE id = :id
        """), {"raw": order.marketplace_status, "internal": decision.status, "date": order.order_date,
               "cur": order.currency, "cname": order.customer_name, "ccity": order.customer_city,
               "review": order.review_reason, "h": h, "id": order_id})
        if decision.changed:
            conn.execute(text("""
                INSERT INTO order_status_history(order_id, from_status, to_status, marketplace_status, source)
                VALUES (:o, :from, :to, :raw, 'sync')
            """), {"o": order_id, "from": existing["internal_status"], "to": decision.status,
                   "raw": order.marketplace_status})
            stats.status_changes += 1
            stats.changes.append((order_id, "ORDER_UPDATED", decision.status))
        stats.updated += 1

    for line in order.lines:
        conn.execute(text("""
            INSERT INTO order_items(order_id, product_id, external_line_id, sku, barcode, product_name,
                                    quantity, unit_price, discount, vat_rate, line_status)
            VALUES (:o, :p, :lid, :sku, :bc, :name, :q, :price, :disc, :vat, :ls)
            ON CONFLICT (order_id, external_line_id) WHERE external_line_id IS NOT NULL
            DO UPDATE SET product_id = COALESCE(order_items.product_id, EXCLUDED.product_id),
                          sku = EXCLUDED.sku, barcode = EXCLUDED.barcode,
                          product_name = EXCLUDED.product_name, quantity = EXCLUDED.quantity,
                          unit_price = EXCLUDED.unit_price, discount = EXCLUDED.discount,
                          vat_rate = EXCLUDED.vat_rate, line_status = EXCLUDED.line_status
        """), {"o": order_id, "p": _find_product(conn, line.sku, line.barcode),
               "lid": line.external_line_id, "sku": line.sku, "bc": line.barcode,
               "name": line.product_name, "q": line.quantity, "price": line.unit_price,
               "disc": line.discount, "vat": line.vat_rate, "ls": line.line_status})

    for sh in order.shipments:
        conn.execute(text("""
            INSERT INTO shipments(order_id, external_package_id, carrier, tracking_number, tracking_url,
                                  status, marketplace_status, desi, shipped_at, delivered_at, updated_at)
            VALUES (:o, :pid, :carrier, :tn, :url, :status, :raw, :desi, :sat, :dat, NOW())
            ON CONFLICT (order_id, external_package_id) WHERE external_package_id IS NOT NULL
            DO UPDATE SET carrier = EXCLUDED.carrier, tracking_number = EXCLUDED.tracking_number,
                          tracking_url = EXCLUDED.tracking_url, status = EXCLUDED.status,
                          marketplace_status = EXCLUDED.marketplace_status, desi = EXCLUDED.desi,
                          shipped_at = COALESCE(EXCLUDED.shipped_at, shipments.shipped_at),
                          delivered_at = COALESCE(EXCLUDED.delivered_at, shipments.delivered_at),
                          updated_at = NOW()
        """), {"o": order_id, "pid": sh.external_package_id, "carrier": sh.carrier,
               "tn": sh.tracking_number, "url": sh.tracking_url, "status": sh.marketplace_status,
               "raw": sh.marketplace_status, "desi": sh.desi, "sat": sh.shipped_at, "dat": sh.delivered_at})

    recalculate_order(conn, order_id)
    return order_id


def upsert_orders(conn: Connection, store_id: int, orders: list[NormalizedOrder]) -> SyncStats:
    stats = SyncStats(received=len(orders))
    for order in orders:
        # Her sipariş kendi savepoint'inde: bir siparişteki hata diğerlerini geri almaz.
        sp = conn.begin_nested()
        try:
            upsert_order(conn, store_id, order, stats)
            sp.commit()
        except Exception as exc:  # noqa: BLE001 - hatayı kaydedip devam ediyoruz
            sp.rollback()
            stats.errors.append(f"{order.external_order_id}: {exc.__class__.__name__}: {exc}"[:500])
    return stats
