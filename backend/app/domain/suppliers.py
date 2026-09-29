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
    cost_note: str | None = None   # maliyet neden kullanılamıyor / neden uyarılı (kur, KDV)

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


def effective_cost(cost, currency: str | None, vat_rate, price_vat_mode: str | None,
                   fx_rates: dict) -> tuple[Decimal | None, str | None]:
    """Tedarikçi fiyatından finansta kullanılacak maliyet: TL, KDV DAHİL.

    * Para birimi TRY değilse ve kur tanımlı değilse maliyet YOKTUR (sessizce TL sayılmaz).
    * Tedarikçi fiyatları KDV hariçse ürünün KDV oranı eklenir; oran bilinmiyorsa değer uydurulmaz (maliyet yok).
    * KDV durumu belirtilmemişse fiyat olduğu gibi kullanılır (önceki davranış) ve uyarı döner.
    Dönen: (maliyet | None, not | None)"""
    if cost is None:
        return None, None
    cur = (currency or "TRY").strip().upper()
    cur = {"TL": "TRY", "YTL": "TRY"}.get(cur, cur)
    rate = fx_rates.get(cur)
    if rate is None:
        return None, f"Döviz kuru tanımlı değil ({cur}); maliyet finansa girmedi"
    base = Decimal(cost) * Decimal(rate)
    note = None if cur == "TRY" else f"{cur} × {rate} kuruyla TL'ye çevrildi"
    if price_vat_mode == "excluded":
        if vat_rate is None:
            return None, "Fiyat KDV hariç ama ürünün KDV oranı yok; maliyet hesaplanamadı"
        base = base * (1 + Decimal(vat_rate) / 100)
    elif price_vat_mode != "included":
        note = ((note + "; ") if note else "") + "Tedarikçi fiyatının KDV durumu belirtilmedi (KDV dahil varsayıldı)"
    return base.quantize(Decimal("0.01")), note
