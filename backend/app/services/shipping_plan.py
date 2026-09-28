"""Siparişlerin kargo planı tahmini: hangi tedarikçi kuralının uygulanacağını belirler.

Tedarikçi sırasıyla şuradan belirlenir (yalnızca TrendHub verisi okunur, hiçbir yere yazılmaz):
  1. Siparişe ait tedarikçi siparişi kaydı (supplier_orders.supplier_id) — en son kayıt
  2. Sipariş kalemlerindeki katalog ürünlerinin tercih edilen tedarikçisi (tek tedarikçiyse)
  3. Ayar `shipping_plan.default_supplier_code` (varsayılan "canta_bayim": canlı Trendyol
     siparişleri Çanta Bayim'e aktarıldığı için). Boş bırakılırsa varsayılan uygulanmaz.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.engine import Connection

from ..db import rows
from ..domain import shipping_plan as SP
from . import app_settings

DEFAULT_SUPPLIER_SETTING = "shipping_plan.default_supplier_code"


def resolve_suppliers(conn: Connection, order_ids: list[int]) -> dict[int, str | None]:
    if not order_ids:
        return {}
    default = app_settings.get(conn, DEFAULT_SUPPLIER_SETTING, "canta_bayim") or None
    out: dict[int, str | None] = {}
    for r in rows(conn, """
        SELECT o.id,
               (SELECT s.code FROM supplier_orders so JOIN suppliers s ON s.id = so.supplier_id
                 WHERE so.order_id = o.id ORDER BY so.id DESC LIMIT 1) AS from_supplier_order,
               (SELECT CASE WHEN COUNT(DISTINCT p.preferred_supplier_id) = 1 THEN MIN(s.code) END
                  FROM order_items i JOIN products p ON p.id = i.product_id
                  JOIN suppliers s ON s.id = p.preferred_supplier_id
                 WHERE i.order_id = o.id) AS from_products
          FROM orders o WHERE o.id = ANY(:ids)
    """, ids=list(order_ids)):
        out[r["id"]] = r["from_supplier_order"] or r["from_products"] or default
    return out


def attach(conn: Connection, orders: list[dict], now: datetime | None = None) -> None:
    """Her sipariş sözlüğüne `shipping_plan` ekler (backend'de, Türkiye saatiyle hesaplanır)."""
    now = now or datetime.now(timezone.utc)
    suppliers = resolve_suppliers(conn, [o["id"] for o in orders])
    same = app_settings.get(conn, "shipping.same_day_before", "11:00")
    nxt = app_settings.get(conn, "shipping.next_day_from", "12:00")
    names = {r["code"]: r["name"] for r in rows(conn, "SELECT code, name FROM suppliers WHERE code = ANY(:c)",
                                                 c=[c for c in set(suppliers.values()) if c])}
    rules = {code: SP.rule_from_settings(code, names.get(code) or ("Çanta Bayim" if code == "canta_bayim" else code),
                                         same, nxt)
             for code in set(suppliers.values()) if code}
    for o in orders:
        code = suppliers.get(o["id"])
        o["shipping_plan"] = SP.plan(o.get("order_date"), now, status=o.get("internal_status"),
                                     supplier_code=code, rule=rules.get(code))
