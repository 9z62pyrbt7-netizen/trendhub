"""Tedarikçi stok kuralları ve teklif (tedarikçi) seçim stratejileri — saf fonksiyonlar.

Bir katalog ürünü birden fazla tedarikçide bulunabilir (her biri bir "teklif").
Hangi teklifin kullanılacağı `products.supplier_strategy` ile belirlenir:

  manual         tercih edilen tedarikçi (products.preferred_supplier_id)
  cheapest       stokta olan en düşük alış fiyatı
  highest_stock  en yüksek kullanılabilir stok
  priority       stokta olan, öncelik numarası en küçük tedarikçi

Yeni bir strateji eklemek için STRATEGIES sözlüğüne bir fonksiyon eklemek yeterlidir.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

STRATEGY_LABELS = {
    "manual": "Tercih edilen tedarikçi",
    "cheapest": "En ucuz (stokta)",
    "highest_stock": "En yüksek stok",
    "priority": "Öncelik sırası (stokta)",
}


def effective_stock(stock: int | None, rules: dict | None) -> int:
    """Tedarikçi stoğuna kuralları uygular.

    buffer: güvenlik payı (düşülür) · min_stock: bunun altı 0 sayılır · max_stock: üst sınır."""
    rules = rules or {}
    s = max(0, int(stock or 0) - max(0, int(rules.get("buffer") or 0)))
    if s < int(rules.get("min_stock") or 0):
        s = 0
    mx = rules.get("max_stock")
    if mx not in (None, ""):
        s = min(s, max(0, int(mx)))
    return s


@dataclass
class Offer:
    supplier_product_id: int
    supplier_id: int
    supplier_name: str
    cost: Decimal | None
    stock: int                 # kurallar uygulanmış kullanılabilir stok
    priority: int = 100
    available: bool = True     # tedarikçi aktif ve ürün kaynağında mevcut

    @property
    def usable(self) -> bool:
        return self.available and self.stock > 0 and self.cost is not None and self.cost > 0


def _manual(offers: list[Offer], preferred_supplier_id: int | None) -> Offer | None:
    return next((o for o in offers if o.supplier_id == preferred_supplier_id and o.available), None)


def _cheapest(offers: list[Offer], _pref) -> Offer | None:
    c = [o for o in offers if o.usable]
    return min(c, key=lambda o: (o.cost, o.priority, -o.stock, o.supplier_id)) if c else None


def _highest_stock(offers: list[Offer], _pref) -> Offer | None:
    c = [o for o in offers if o.usable]
    return max(c, key=lambda o: (o.stock, -o.cost, -o.priority, -o.supplier_id)) if c else None


def _priority(offers: list[Offer], _pref) -> Offer | None:
    c = [o for o in offers if o.usable]
    return min(c, key=lambda o: (o.priority, o.cost, o.supplier_id)) if c else None


STRATEGIES: dict[str, Callable[[list[Offer], int | None], Offer | None]] = {
    "manual": _manual,
    "cheapest": _cheapest,
    "highest_stock": _highest_stock,
    "priority": _priority,
}


def select_offer(offers: list[Offer], strategy: str | None, preferred_supplier_id: int | None) -> Offer | None:
    fn = STRATEGIES.get(strategy or "manual", _manual)
    return fn(offers, preferred_supplier_id)
