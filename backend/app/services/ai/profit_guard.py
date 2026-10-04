"""Kâr Koruması (Profit Guard): zarar eden ürün körlemesine büyütülmez.

Ürün kâr durumu:
    UNKNOWN  maliyet/fiyat bilinmiyor veya karar için yeterli satış yok → büyük bütçe açılmaz
    DANGER   net zarar, birim kâr ≤ 0 veya net marj < 0 → otomatik/önerilen büyütme YASAK
    WARNING  marj veya birim kâr alt sınırın altında, ya da iade oranı yüksek
    SAFE     sınırların üstünde
Kaynak: realize olmuş satış (finance_view) varsa o, yoksa katalog fiyatı + maliyet modeli (basis alanında belirtilir).
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy.engine import Connection

from .config import Window, d, thresholds, tl

STATES = ("SAFE", "WARNING", "DANGER", "UNKNOWN")
SCALING_ACTIONS = {"ads.create_campaign", "ads.increase_budget", "campaign.discount"}
HIGH_RETURN_RATE = Decimal("0.20")


def state_for(e: dict | None, ue: dict | None, th: dict, exclude_ads: bool = False) -> dict:
    """e: product_economics satırı (pencere), ue: unit_economics sonucu.

    exclude_ads: kampanya düzeyi kararlarda ürünün REKLAM ÖNCESİ kârı esas alınır (reklamın kendi kârlılığı kampanya
    kontrolünde ayrıca doğrulanır; reklam payı yüzünden kârlı reklamın büyütülmesi yanlışlıkla engellenmez)."""
    reasons: list[str] = []
    if ue is None or ue.get("missing"):
        miss = ", ".join({"cost": "maliyet", "price": "fiyat", "product": "ürün"}.get(m, m) for m in (ue or {}).get("missing", ["product"]))
        return {"state": "UNKNOWN", "reasons": [f"Eksik veri: {miss}"], "basis": None}
    basis = ue.get("basis")
    units = int((e or {}).get("units") or 0)
    unit_profit, margin = ue.get("unit_profit"), ue.get("net_margin")
    period_profit = (e or {}).get("profit_before_ads" if exclude_ads else "net_profit")
    if exclude_ads and unit_profit is not None:
        unit_profit = unit_profit + d(ue.get("ads"))
        margin = (unit_profit / d(ue["price"])).quantize(Decimal("0.0001")) if d(ue.get("price")) else margin
    if e and units and period_profit is not None and period_profit < 0:
        reasons.append(f"Son {th['analysis_days']} gün {'reklam öncesi ' if exclude_ads else ''}net zarar {tl(period_profit)}")
    if unit_profit is not None and unit_profit <= 0:
        reasons.append(f"Birim net kâr {tl(unit_profit)}")
    if margin is not None and margin < 0:
        reasons.append(f"Net marj %{margin * 100:.1f}")
    if reasons:
        return {"state": "DANGER", "reasons": reasons, "basis": basis, "unit_profit": unit_profit, "net_margin": margin}
    if units < int(th["min_units_for_data"]):
        # Modelde kârlı görünse de gerçek satış kanıtı yok: büyük bütçe için yeterli değil
        return {"state": "UNKNOWN", "reasons": [f"{units} adet satış; gerçek kâr kanıtı için en az {th['min_units_for_data']}"],
                "basis": basis, "unit_profit": unit_profit, "net_margin": margin, "modeled": True}
    warn = []
    if unit_profit is not None and unit_profit < d(th["min_unit_profit"]):
        warn.append(f"Birim net kâr {tl(unit_profit)} < alt sınır {tl(th['min_unit_profit'])}")
    if margin is not None and margin < d(th["min_net_margin"]):
        warn.append(f"Net marj %{margin * 100:.1f} < alt sınır %{d(th['min_net_margin']) * 100:.0f}")
    rr = (e or {}).get("return_rate")
    if rr is not None and units >= int(th["min_units_for_data"]) and rr >= HIGH_RETURN_RATE:
        warn.append(f"İade oranı %{rr * 100:.0f}")
    if warn:
        return {"state": "WARNING", "reasons": warn, "basis": basis, "unit_profit": unit_profit, "net_margin": margin}
    return {"state": "SAFE", "reasons": [f"Birim net kâr {tl(unit_profit)}, marj %{(margin or 0) * 100:.1f}"], "basis": basis,
            "unit_profit": unit_profit, "net_margin": margin}


def product_state(conn: Connection, product_id: int, exclude_ads: bool = False) -> dict:
    from .data import product_economics
    from .economics import unit_economics
    th = thresholds(conn)
    e = next(iter(product_economics(conn, Window(th["analysis_days"]), [product_id])), None)
    ue = unit_economics(conn, product_id)
    return {"product_id": product_id, "name": ue.get("name"), "sku": ue.get("sku"), **state_for(e, ue, th, exclude_ads)}


def portfolio(conn: Connection, limit: int = 200) -> list[dict]:
    """Satışı/reklamı olan ürünlerin kâr durumu (en riskli önce)."""
    from .data import product_economics
    from .economics import unit_economics
    th = thresholds(conn)
    out = []
    for e in product_economics(conn, Window(th["analysis_days"]))[:limit]:
        ue = unit_economics(conn, e["product_id"])
        out.append({"product_id": e["product_id"], "name": e["name"], "sku": e["sku"], "units": e["units"],
                    "net_profit": e["net_profit"], **state_for(e, ue, th)})
    order = {"DANGER": 0, "UNKNOWN": 1, "WARNING": 2, "SAFE": 3}
    out.sort(key=lambda x: (order[x["state"]], x["net_profit"]))
    return out


def check(conn: Connection, p: dict, product_ids: list[int]) -> list[dict]:
    """Büyütme aksiyonları için risk motoru kontrolü."""
    from .proposals import _check
    if p["action_type"] not in SCALING_ACTIONS or not product_ids:
        return []
    th = thresholds(conn)
    out = []
    states = [product_state(conn, pid, exclude_ads=p["entity_type"] == "campaign") for pid in product_ids]
    danger = [s for s in states if s["state"] == "DANGER"]
    if danger:
        out.append(_check("profit_guard_danger", "block",
                          "Kâr Koruması (DANGER): zarar eden ürün büyütülemez — " +
                          "; ".join(f"{s['name']}: {', '.join(s['reasons'])}" for s in danger[:3]),
                          products=[s["product_id"] for s in danger]))
    unknown = [s for s in states if s["state"] == "UNKNOWN"]
    cap = d(p.get("required_capital"))
    limit = d(th.get("profit_guard_unknown_max_spend", 1000))
    if unknown and cap > limit:
        out.append(_check("profit_guard_unknown", "block",
                          f"Kâr Koruması (UNKNOWN): gerçek net kâr hesaplanamayan ürüne {tl(cap)} harcanamaz "
                          f"(kanıtsız üst sınır {tl(limit)}) — " + "; ".join(f"{s['name']}: {', '.join(s['reasons'])}" for s in unknown[:3]),
                          products=[s["product_id"] for s in unknown]))
    elif unknown:
        out.append(_check("profit_guard_unknown", "warning", "Kâr Koruması: ürün kârı doğrulanmadı; yalnızca küçük test bütçesi.",
                          products=[s["product_id"] for s in unknown]))
    warn = [s for s in states if s["state"] == "WARNING"]
    if warn:
        out.append(_check("profit_guard_warning", "warning",
                          "Kâr Koruması (WARNING): " + "; ".join(f"{s['name']}: {', '.join(s['reasons'])}" for s in warn[:3])))
    return out
