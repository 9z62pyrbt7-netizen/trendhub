"""Ürün birim ekonomisi: fiyat / kampanya / reklam kararlarının ORTAK kâr modeli.

Gerçekleşen satış varsa birim değerler finance_view'den (dashboard ile aynı formül) türetilir:
    birim net kâr = net satış − ürün maliyeti − komisyon − kargo − hizmet bedeli − diğer − reklam payı − tahmini KDV
Yeni bir fiyat P için:
    model(P) = P − maliyet − P × komisyon_oranı − kargo − hizmet − diğer − reklam − (P − maliyet) × KDV/(100+KDV)
    simülasyon(P) = model(P) + düzeltme, düzeltme = gerçekleşen birim kâr − model(mevcut fiyat)
Böylece mevcut fiyatta simülasyon gerçekleşen birim kârı kuruşu kuruşuna verir (iade, KDV modu vb. düzeltmeye girer).
Satış yoksa katalog fiyatı + ayarlardaki komisyon/kargo/hizmet varsayımları kullanılır ve basis='catalog_estimate' olur.
Maliyet bilinmiyorsa `missing` dolu döner: bu ürün için fiyat/kampanya/reklam kararı VERİLMEZ (NEEDS_DATA).
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.engine import Connection

from ...db import row
from .. import app_settings
from .config import Window, d, thresholds

CENT = Decimal("0.01")


def q(v) -> Decimal:
    return d(v).quantize(CENT, rounding=ROUND_HALF_UP)


def unit_economics(conn: Connection, product_id: int, window: Window | None = None) -> dict:
    from .data import product_economics
    th = thresholds(conn)
    window = window or Window(th["analysis_days"])
    p = row(conn, """SELECT p.id, p.name, p.sku, p.barcode, p.sale_price, p.cost, p.stock, COALESCE(p.vat_rate, 20) AS vat_rate,
                            (SELECT cost FROM product_costs c WHERE c.product_id = p.id ORDER BY valid_from DESC LIMIT 1) AS last_cost
                       FROM products p WHERE p.id = :i""", i=product_id)
    if p is None:
        return {"product_id": product_id, "missing": ["product"]}
    cost = d(p["last_cost"] if p["last_cost"] is not None else p["cost"])
    vat = d(p["vat_rate"])
    e = next(iter(product_economics(conn, window, [product_id])), None)
    missing = []
    if cost <= 0:
        missing.append("cost")
    if d(p["sale_price"]) <= 0 and not (e and e["units"]):
        missing.append("price")
    out = {"product_id": product_id, "name": p["name"], "sku": p["sku"], "barcode": p["barcode"], "vat_rate": vat,
           "catalog_price": q(p["sale_price"]), "unit_cost": q(cost), "missing": missing, "window_days": window.days}
    if e and e["units"] and not e["missing_cost"]:
        u = Decimal(e["units"])
        price = q(d(e["net_sales"]) / u)
        comm_rate = (d(e["commission"]) / d(e["net_sales"])) if d(e["net_sales"]) else Decimal("0")
        out.update(basis="realized", units=e["units"], price=price, commission_rate=comm_rate.quantize(Decimal("0.0001")),
                   shipping=q(d(e["shipping"]) / u), service_fee=q(d(e["service_fee"]) / u), other=q(d(e["other"]) / u),
                   ads=q((d(e["ad_spend_allocated"]) + d(e["order_level_ads"])) / u), unit_cost=q(d(e["product_cost"]) / u)
                   if d(e["product_cost"]) else q(cost), realized_unit_profit=q(d(e["net_profit"]) / u),
                   realized_net_margin=e["net_margin"])
    else:
        mp = "trendyol"
        out.update(basis="catalog_estimate", units=e["units"] if e else 0, price=q(p["sale_price"]),
                   commission_rate=app_settings.get_decimal(conn, f"finance.commission_rate.{mp}"),
                   shipping=q(app_settings.get_decimal(conn, "finance.default_shipping_cost")),
                   service_fee=q(app_settings.get_decimal(conn, "finance.service_fee_per_order")), other=Decimal("0"),
                   ads=Decimal("0"), realized_unit_profit=None, realized_net_margin=None)
        if e and e["missing_cost"]:
            missing.append("cost")
    out["missing"] = sorted(set(missing))
    if out["missing"]:
        out["unit_profit"] = None
        out["net_margin"] = None
        out["adjustment"] = Decimal("0")
        return out
    base = _model(out, out["price"])
    out["adjustment"] = q(out["realized_unit_profit"] - base) if out["realized_unit_profit"] is not None else Decimal("0")
    sim = simulate(out, out["price"])
    out["unit_profit"], out["net_margin"] = sim["unit_profit"], sim["net_margin"]
    return out


def _model(e: dict, price: Decimal) -> Decimal:
    price = d(price)
    vat_part = (price - d(e["unit_cost"])) * d(e["vat_rate"]) / (Decimal(100) + d(e["vat_rate"]))
    return (price - d(e["unit_cost"]) - price * d(e["commission_rate"]) - d(e["shipping"]) - d(e["service_fee"])
            - d(e["other"]) - d(e["ads"]) - max(vat_part, Decimal("0")))


def simulate(e: dict, price) -> dict:
    """Verilen birim satış fiyatında beklenen birim net kâr ve net marj."""
    price = d(price)
    if e.get("missing"):
        return {"price": q(price), "unit_profit": None, "net_margin": None, "missing": e["missing"]}
    up = q(_model(e, price) + d(e.get("adjustment")))
    return {"price": q(price), "unit_profit": up,
            "net_margin": (up / price).quantize(Decimal("0.0001")) if price > 0 else None}


def floor_price(e: dict, th: dict) -> Decimal | None:
    """min_unit_profit ve min_net_margin'i birlikte sağlayan en düşük fiyat (ikili arama; kuruş hassasiyeti)."""
    if e.get("missing"):
        return None
    lo, hi = Decimal("0.01"), max(d(e["price"]) * 4, d(e["unit_cost"]) * 6, Decimal("10"))

    def ok(pr):
        s = simulate(e, pr)
        return s["unit_profit"] >= d(th["min_unit_profit"]) and (s["net_margin"] or 0) >= d(th["min_net_margin"])
    if not ok(hi):
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        if ok(mid):
            hi = mid
        else:
            lo = mid
    return q(hi + CENT if not ok(q(hi)) else q(hi))


def max_safe_discount(e: dict, th: dict) -> Decimal | None:
    """Ürünün min kâr/marj sınırını bozmadan verilebilecek en yüksek indirim oranı (0–1). Sınır zaten aşılmışsa 0."""
    fp = floor_price(e, th)
    if fp is None or d(e["price"]) <= 0:
        return None
    return max(Decimal("0"), ((d(e["price"]) - fp) / d(e["price"]))).quantize(Decimal("0.0001"))
