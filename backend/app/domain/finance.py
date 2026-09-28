"""Sipariş / SKU seviyesinde kârlılık hesabı.

Tüm tutarlar Decimal ile hesaplanır, kuruşa yuvarlanır. Kalem (SKU) bazında
hesaplanan bileşenler toplanarak sipariş toplamı elde edilir; böylece
raporlar hem sipariş hem SKU seviyesinde aynı sayıları verir.

    net_kar = ciro - urun_maliyeti - komisyon - hizmet_bedeli - kargo
              - reklam - iade - diger
    kar_marji = net_kar / ciro   (ciro 0 ise None)

Sipariş seviyesindeki paylaşılan giderler (kargo, hizmet bedeli, reklam,
diğer) kalemlere ciro payı oranında dağıtılır; kuruş farkı son kaleme
eklenir, toplam hiçbir zaman kaymaz.
"""
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
ZERO = Decimal("0")

COMPONENTS = ("revenue", "product_cost", "commission", "service_fee", "shipping",
              "advertising", "refund", "other")
COMPONENT_LABELS_TR = {
    "revenue": "Ciro",
    "product_cost": "Ürün Maliyeti",
    "commission": "Komisyon",
    "service_fee": "Hizmet Bedeli",
    "shipping": "Kargo",
    "advertising": "Reklam",
    "refund": "İade",
    "other": "Diğer Giderler",
    "net_profit": "Net Kâr",
}


def d(value) -> Decimal:
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def money(value) -> Decimal:
    return d(value).quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class Breakdown:
    revenue: Decimal = ZERO
    product_cost: Decimal = ZERO
    commission: Decimal = ZERO
    service_fee: Decimal = ZERO
    shipping: Decimal = ZERO
    advertising: Decimal = ZERO
    refund: Decimal = ZERO
    other: Decimal = ZERO

    @property
    def total_cost(self) -> Decimal:
        return money(self.product_cost + self.commission + self.service_fee + self.shipping
                     + self.advertising + self.refund + self.other)

    @property
    def net_profit(self) -> Decimal:
        return money(self.revenue - self.total_cost)

    @property
    def margin(self) -> Decimal | None:
        if self.revenue == 0:
            return None
        return (self.net_profit / self.revenue).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    def __add__(self, other: "Breakdown") -> "Breakdown":
        return Breakdown(**{c: money(getattr(self, c) + getattr(other, c)) for c in COMPONENTS})

    def as_dict(self) -> dict:
        out = {c: float(getattr(self, c)) for c in COMPONENTS}
        out["total_cost"] = float(self.total_cost)
        out["net_profit"] = float(self.net_profit)
        out["margin"] = float(self.margin) if self.margin is not None else None
        return out


@dataclass
class LineInput:
    quantity: int
    unit_price: Decimal            # müşteriye satış birim fiyatı (indirim sonrası)
    unit_cost: Decimal             # ürün birim maliyeti
    commission: Decimal | None = None       # pazaryerinin bildirdiği komisyon (varsa)
    commission_rate: Decimal | None = None  # yoksa oran ile tahmin
    refund_amount: Decimal = ZERO
    advertising: Decimal = ZERO             # kaleme doğrudan atanmış reklam
    other: Decimal = ZERO


@dataclass
class OrderCosts:
    """Sipariş seviyesinde bilinen, kalemlere dağıtılacak giderler."""
    shipping: Decimal = ZERO
    service_fee: Decimal = ZERO
    advertising: Decimal = ZERO
    other: Decimal = ZERO


@dataclass
class OrderFinance:
    lines: list[Breakdown] = field(default_factory=list)
    is_estimate: bool = False

    @property
    def total(self) -> Breakdown:
        out = Breakdown()
        for line in self.lines:
            out = out + line
        return out


def allocate(amount: Decimal, weights: list[Decimal]) -> list[Decimal]:
    """Tutarı ağırlıklara göre kuruş kaybı olmadan dağıtır."""
    amount = money(amount)
    if not weights:
        return []
    total = sum(weights, ZERO)
    if total == 0:
        weights = [Decimal(1)] * len(weights)
        total = Decimal(len(weights))
    parts = [money(amount * w / total) for w in weights]
    parts[-1] = money(parts[-1] + (amount - sum(parts, ZERO)))
    return parts


def compute_order(lines: list[LineInput], order_costs: OrderCosts,
                  default_commission_rate: Decimal = ZERO) -> OrderFinance:
    revenues = [money(d(l.unit_price) * l.quantity) for l in lines]
    shipping = allocate(order_costs.shipping, revenues)
    service = allocate(order_costs.service_fee, revenues)
    ads = allocate(order_costs.advertising, revenues)
    other = allocate(order_costs.other, revenues)

    result = OrderFinance()
    for i, line in enumerate(lines):
        if line.commission is not None:
            commission = money(line.commission)
        else:
            rate = line.commission_rate if line.commission_rate is not None else default_commission_rate
            commission = money(revenues[i] * d(rate))
            result.is_estimate = True
        result.lines.append(Breakdown(
            revenue=revenues[i],
            product_cost=money(d(line.unit_cost) * line.quantity),
            commission=commission,
            service_fee=service[i],
            shipping=shipping[i],
            advertising=money(d(line.advertising) + ads[i]),
            refund=money(line.refund_amount),
            other=money(d(line.other) + other[i]),
        ))
    return result


def vat_portion(amount_incl_vat, rate) -> Decimal:
    """KDV dahil tutarın içindeki KDV: tutar * r / (100 + r)."""
    r = d(rate if rate is not None else 20)
    if r <= 0:
        return ZERO
    return money(d(amount_incl_vat) * r / (Decimal(100) + r))


def estimated_vat_payable(revenue, product_cost, refund, rate) -> Decimal:
    """TAHMİNİ ödenecek KDV = satış KDV'si − maliyet KDV'si (aynı oran varsayımı).

    Tutarlar KDV dahil kabul edilir; iade edilen satışın KDV'si düşülür.
    Gerçek beyanname değildir; muhasebe kayıtlarının yerini tutmaz.
    """
    return money(vat_portion(d(revenue) - d(refund), rate) - vat_portion(product_cost, rate))
