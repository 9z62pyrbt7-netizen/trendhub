"""Pazaryeri bazında fiyatlandırma ve ilan doğrulama — saf fonksiyonlar (Decimal).

Fiyat formülü (her pazaryeri kendi kuralını kullanır):

    taban = maliyet × (1 + kâr oranı) + kargo + sabit gider
    fiyat = taban / (1 − komisyon oranı)          → komisyon satış fiyatından alınır
    fiyat yuvarlanır (x.90 / x.99 / tam sayı / yok)

Tahmini kâr = fiyat − komisyon − kargo − sabit gider − maliyet − tahmini KDV farkı.
Bu değerler TAHMİNİDİR; gerçek komisyon kategoriye ve kampanyaya göre değişir.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from .finance import estimated_vat_payable

CENT = Decimal("0.01")
ZERO = Decimal("0")


@dataclass
class PricingRule:
    commission_rate: Decimal = Decimal("0.20")
    markup_rate: Decimal = Decimal("0.30")
    fixed_cost: Decimal = ZERO
    shipping_cost: Decimal = ZERO
    min_margin_rate: Decimal = Decimal("0.05")
    rounding: str = "x.90"
    include_vat: bool = True


def round_price(p: Decimal, mode: str) -> Decimal:
    if p <= 0:
        return p.quantize(CENT)
    if mode == "integer":
        return p.to_integral_value(rounding=ROUND_CEILING).quantize(CENT)
    if mode in ("x.90", "x.99"):
        frac = Decimal("0.90") if mode == "x.90" else Decimal("0.99")
        base = p.to_integral_value(rounding=ROUND_CEILING) - 1 + frac
        if base < p:
            base += 1
        return base.quantize(CENT)
    return p.quantize(CENT, rounding=ROUND_HALF_UP)


def suggest_price(cost: Decimal, rule: PricingRule) -> Decimal:
    base = cost * (1 + rule.markup_rate) + rule.shipping_cost + rule.fixed_cost
    divisor = 1 - rule.commission_rate
    if divisor <= 0:
        raise ValueError("Komisyon oranı %100'den küçük olmalı")
    return round_price(base / divisor, rule.rounding)


@dataclass
class ProfitEstimate:
    price: Decimal
    commission: Decimal
    shipping: Decimal
    fixed: Decimal
    cost: Decimal
    vat: Decimal
    profit: Decimal
    margin: Decimal | None


def estimate_profit(price: Decimal, cost: Decimal, rule: PricingRule, vat_rate: Decimal) -> ProfitEstimate:
    commission = (price * rule.commission_rate).quantize(CENT, rounding=ROUND_HALF_UP)
    vat = estimated_vat_payable(price, cost, ZERO, vat_rate) if rule.include_vat else ZERO
    profit = (price - commission - rule.shipping_cost - rule.fixed_cost - cost - vat).quantize(CENT)
    margin = (profit / price).quantize(Decimal("0.0001")) if price > 0 else None
    return ProfitEstimate(price, commission, rule.shipping_cost, rule.fixed_cost, cost, vat, profit, margin)


FIELD_LABELS = {"barcode": "Barkod", "brand": "Marka", "category": "Pazaryeri kategorisi", "images": "Görsel",
                "name": "Ürün adı", "description": "Açıklama", "vat_rate": "KDV oranı", "desi": "Desi",
                "model_code": "Model kodu"}


@dataclass
class DraftCheck:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_draft(*, product: dict, price: Decimal | None, stock: int | None, cost: Decimal | None,
                   category_id: str | None, estimate: ProfitEstimate | None, rule: PricingRule,
                   required_fields: list[str], title_max_length: int | None, min_stock: int,
                   existing_listing: bool, attributes: dict | None = None,
                   required_attributes: list[str] | None = None) -> DraftCheck:
    c = DraftCheck()
    if existing_listing:
        c.errors.append("Bu ürün bu pazaryerinde zaten ilanda; ikinci ilan açılmaz (mevcut ilanı güncelleyin).")
    if not product.get("name"):
        c.errors.append("Ürün adı boş")
    elif title_max_length and len(product["name"]) > title_max_length:
        c.errors.append(f"Ürün adı {title_max_length} karakteri aşıyor ({len(product['name'])})")
    for f in required_fields:
        if f == "category":
            if not category_id:
                c.errors.append("Pazaryeri kategorisi eşleştirilmedi")
        elif f == "images":
            if not product.get("images"):
                c.errors.append("Görsel yok")
        elif not product.get(f):
            c.errors.append(f"{FIELD_LABELS.get(f, f)} boş")
    if cost is None or cost <= 0:
        c.errors.append("Tedarikçi alış fiyatı yok; kâr hesaplanamaz")
    if price is None or price <= 0:
        c.errors.append("Satış fiyatı geçersiz")
    elif estimate is not None and estimate.margin is not None:
        if estimate.profit < 0:
            c.errors.append(f"Tahmini zarar: {estimate.profit} TL")
        elif estimate.margin < rule.min_margin_rate:
            c.errors.append(f"Tahmini marj %{(estimate.margin * 100).quantize(Decimal('0.1'))} < "
                            f"minimum %{(rule.min_margin_rate * 100).quantize(Decimal('0.1'))}")
    if stock is None or stock <= 0:
        c.errors.append("Kullanılabilir stok yok")
    elif stock < min_stock:
        c.warnings.append(f"Stok ({stock}) pazaryeri minimumunun ({min_stock}) altında")
    attrs = attributes or {}
    missing = [a for a in (required_attributes or []) if not str(attrs.get(a) or "").strip()]
    if missing:
        c.errors.append("Zorunlu kategori özelliği eksik: " + ", ".join(missing))
    if not product.get("description"):
        c.warnings.append("Açıklama boş")
    return c
