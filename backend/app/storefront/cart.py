"""Sunucu tarafı sepet. Tarayıcıda yalnızca rastgele sepet anahtarı (HttpOnly çerez) durur; fiyat ve stok
her zaman sunucuda katalogdan hesaplanır (istemciden gelen fiyat kullanılmaz)."""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import catalog, store_config

COOKIE = "tc_sepet"
TTL_DAYS = 30
MAX_QTY = 10


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def find(conn: Connection, token: str | None) -> int | None:
    if not token or len(token) > 100:
        return None
    return conn.execute(text("SELECT id FROM storefront_carts WHERE token_hash = :h AND expires_at > NOW()"),
                        {"h": _hash(token)}).scalar()


def ensure(conn: Connection, token: str | None) -> tuple[int, str, bool]:
    """(sepet id, anahtar, yeni mi)"""
    cid = find(conn, token)
    if cid:
        conn.execute(text("UPDATE storefront_carts SET updated_at = NOW(), expires_at = NOW() + make_interval(days => :d) WHERE id = :id"),
                     {"id": cid, "d": TTL_DAYS})
        return cid, token, False
    token = secrets.token_urlsafe(32)
    cid = conn.execute(text("""INSERT INTO storefront_carts(token_hash, expires_at)
                               VALUES (:h, NOW() + make_interval(days => :d)) RETURNING id"""),
                       {"h": _hash(token), "d": TTL_DAYS}).scalar()
    return cid, token, True


class CartError(ValueError):
    pass


def _qty(conn: Connection, cart_id: int, product_id: int) -> int:
    return int(conn.execute(text("SELECT quantity FROM storefront_cart_items WHERE cart_id = :c AND product_id = :p"),
                            {"c": cart_id, "p": product_id}).scalar() or 0)


def set_quantity(conn: Connection, cart_id: int, product_id: int, quantity: int) -> dict:
    """Adedi ayarlar; stoktan fazlası istenirse kullanılabilir stoğa indirir ve bildirir."""
    quantity = max(0, min(int(quantity), MAX_QTY))
    if quantity == 0:
        conn.execute(text("DELETE FROM storefront_cart_items WHERE cart_id = :c AND product_id = :p"),
                     {"c": cart_id, "p": product_id})
        return {"quantity": 0, "limited": False}
    v = catalog.variants_by_ids(conn, [product_id]).get(product_id)
    if v is None:
        raise CartError("Bu ürün şu anda satışta değil.")
    if v.available <= 0:
        raise CartError("Bu ürün tükendi.")
    limited = quantity > v.available
    quantity = min(quantity, v.available)
    conn.execute(text("""INSERT INTO storefront_cart_items(cart_id, product_id, quantity) VALUES (:c, :p, :q)
                         ON CONFLICT (cart_id, product_id) DO UPDATE SET quantity = EXCLUDED.quantity"""),
                 {"c": cart_id, "p": product_id, "q": quantity})
    conn.execute(text("UPDATE storefront_carts SET updated_at = NOW() WHERE id = :c"), {"c": cart_id})
    return {"quantity": quantity, "limited": limited, "available": v.available}


def add(conn: Connection, cart_id: int, product_id: int, quantity: int = 1) -> dict:
    if quantity < 1:
        raise CartError("Adet en az 1 olmalı.")
    return set_quantity(conn, cart_id, product_id, _qty(conn, cart_id, product_id) + quantity)


@dataclass
class Line:
    variant: catalog.Variant
    quantity: int
    issue: str | None = None

    @property
    def total(self) -> Decimal:
        return self.variant.price * self.quantity


@dataclass
class CartView:
    lines: list[Line] = field(default_factory=list)
    items_total: Decimal = Decimal("0")
    shipping: Decimal = Decimal("0")
    total: Decimal = Decimal("0")
    count: int = 0
    free_shipping_remaining: Decimal | None = None

    @property
    def has_issues(self) -> bool:
        return any(line.issue for line in self.lines)


def view(conn: Connection, cart_id: int | None) -> CartView:
    cv = CartView()
    if not cart_id:
        return cv
    items = conn.execute(text("SELECT product_id, quantity FROM storefront_cart_items WHERE cart_id = :c ORDER BY added_at"),
                         {"c": cart_id}).all()
    variants = catalog.variants_by_ids(conn, [i.product_id for i in items])
    for it in items:
        v = variants.get(it.product_id)
        if v is None:
            # Yayından kalkmış ürün sepetten sessizce silinmez; kullanıcıya gösterilip kaldırılır.
            conn.execute(text("DELETE FROM storefront_cart_items WHERE cart_id = :c AND product_id = :p"),
                         {"c": cart_id, "p": it.product_id})
            continue
        line = Line(variant=v, quantity=int(it.quantity))
        if v.available <= 0:
            line.issue = "Tükendi — siparişe eklenemez."
        elif line.quantity > v.available:
            line.issue = f"Stokta yalnızca {v.available} adet var."
        cv.lines.append(line)
    ok_lines = [line for line in cv.lines if not line.issue]
    cv.items_total = sum((line.total for line in ok_lines), Decimal("0"))
    cv.count = sum(line.quantity for line in cv.lines)
    cfg = store_config.load(conn)
    cv.shipping = cfg.shipping_for(cv.items_total) if ok_lines else Decimal("0")
    cv.total = cv.items_total + cv.shipping
    if cfg.free_shipping_threshold is not None and cfg.shipping_fee > 0 and cv.items_total < cfg.free_shipping_threshold:
        cv.free_shipping_remaining = cfg.free_shipping_threshold - cv.items_total
    return cv


def as_json(cv: CartView) -> dict:
    from .images import img_url
    return {
        "count": cv.count,
        "items_total": str(cv.items_total), "shipping": str(cv.shipping), "total": str(cv.total),
        "items_total_fmt": store_config.fmt_try(cv.items_total), "shipping_fmt": store_config.fmt_try(cv.shipping),
        "total_fmt": store_config.fmt_try(cv.total),
        "free_shipping_remaining_fmt": store_config.fmt_try(cv.free_shipping_remaining)
        if cv.free_shipping_remaining and cv.free_shipping_remaining > 0 else None,
        "lines": [{"product_id": line.variant.id, "title": line.variant.title, "url": line.variant.url,
                   "image": img_url(line.variant.images[0], 240) if line.variant.images else None,
                   "color": line.variant.color, "size": line.variant.size, "quantity": line.quantity,
                   "price_fmt": store_config.fmt_try(line.variant.price), "total_fmt": store_config.fmt_try(line.total),
                   "available": line.variant.available, "issue": line.issue} for line in cv.lines],
    }
