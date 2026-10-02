"""Paylaşılan veri katmanı: tüm ajanlar ve CEO buradan okur (yalnızca okuma).

Kâr formülleri YENİDEN YAZILMAZ: kalem bazındaki gelir/gider/kâr `finance_view.item_columns` ile, dönem toplamı
`analytics.summary` ile hesaplanır — dashboard ve raporlarla birebir aynı rakamlar.

Ürün bazında net kâr (AI tanımı):
    net_profit = Σ kalem tahmini KDV sonrası kâr − payına düşen reklam harcaması
    payına düşen reklam = kampanya harcaması ÷ kampanyadaki ürün sayısı (eşit bölüşüm, TAHMİNİ)
                          + SKU'ya doğrudan girilmiş reklam giderleri (expenses.category='advertising')
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ...db import row, rows
from .. import finance_view, stock_availability
from .config import Window, d, thresholds


def _q(v) -> Decimal:
    return finance_view.q2(d(v))


# ------------------------------------------------------------------ ürün ekonomisi
def product_economics(conn: Connection, window: Window, product_ids: list[int] | None = None) -> list[dict]:
    """Pencerede satışı veya reklam harcaması olan ürünlerin birim ekonomisi."""
    cfg = finance_view.load(conn)
    pfilter = "AND i.product_id = ANY(:pids)" if product_ids else ""
    sales = {r["product_id"]: r for r in rows(conn, f"""
        SELECT i.product_id, SUM(i.quantity) AS units, COUNT(DISTINCT i.order_id) AS orders,
               COUNT(DISTINCT i.order_id) FILTER (WHERE o.internal_status = 'returned') AS returned_orders,
               SUM(fi.revenue) AS revenue, SUM(fi.refund) AS refund, SUM(fi.net_sales) AS net_sales,
               SUM(fi.product_cost) AS product_cost, SUM(fi.commission) AS commission,
               SUM(fi.service_fee) AS service_fee, SUM(fi.shipping) AS shipping, SUM(fi.advertising) AS order_ads,
               SUM(fi.other) AS other, SUM(fi.discount) AS discount,
               SUM(fi.profit_before_vat) AS profit_before_vat, SUM(fi.vat_estimate) AS vat_estimate,
               BOOL_OR(COALESCE(i.unit_cost, 0) = 0) AS missing_cost
          FROM order_items i JOIN orders o ON o.id = i.order_id LEFT JOIN products p ON p.id = i.product_id
         CROSS JOIN LATERAL (SELECT {finance_view.item_columns(cfg)}) fi
         WHERE o.order_date >= :start AND o.order_date < :end AND o.internal_status <> 'cancelled'
           AND i.product_id IS NOT NULL {pfilter}
         GROUP BY i.product_id""", **window.params(), **cfg.params(), pids=product_ids or [])}
    ads = ad_allocation(conn, window)
    ids = set(sales) | set(ads)
    if product_ids:
        ids &= set(product_ids)
    if not ids:
        return []
    meta = {r["id"]: r for r in rows(conn, """
        SELECT p.id, p.sku, p.name, p.category, p.sale_price, p.cost, p.stock, p.is_active
          FROM products p WHERE p.id = ANY(:ids)""", ids=list(ids))}
    out = []
    for pid in ids:
        m = meta.get(pid)
        if m is None:
            continue
        s = sales.get(pid) or {}
        units = int(s.get("units") or 0)
        before_ads = _q(s.get("profit_before_vat")) - _q(s.get("vat_estimate"))
        ad = ads.get(pid, Decimal("0"))
        net = before_ads - ad
        net_sales = _q(s.get("net_sales"))
        r = {
            "product_id": pid, "sku": m["sku"], "name": m["name"], "category": m["category"],
            "list_price": m["sale_price"], "unit_cost_catalog": m["cost"], "stock": m["stock"],
            "units": units, "orders": int(s.get("orders") or 0), "returned_orders": int(s.get("returned_orders") or 0),
            "revenue": _q(s.get("revenue")), "refund": _q(s.get("refund")), "net_sales": net_sales,
            "product_cost": _q(s.get("product_cost")), "commission": _q(s.get("commission")),
            "service_fee": _q(s.get("service_fee")), "shipping": _q(s.get("shipping")),
            "order_level_ads": _q(s.get("order_ads")), "other": _q(s.get("other")), "discount": _q(s.get("discount")),
            "vat_estimate": _q(s.get("vat_estimate")), "profit_before_ads": before_ads, "ad_spend_allocated": ad,
            "net_profit": net, "missing_cost": bool(s.get("missing_cost")) if s else False,
            "margin_before_ads": finance_view.ratio(before_ads, net_sales),
            "net_margin": finance_view.ratio(net, net_sales),
            "return_rate": finance_view.ratio(s.get("returned_orders") or 0, s.get("orders")) if s.get("orders") else None,
            "ad_estimate": ad > 0,
        }
        if units:
            for k in ("net_sales", "product_cost", "commission", "shipping", "service_fee", "other", "net_profit"):
                r[f"{k}_per_unit"] = finance_view.q2(r[k] / units)
            r["ad_per_unit"] = finance_view.q2(ad / units)
        out.append(r)
    out.sort(key=lambda x: x["net_profit"])
    return out


def ad_allocation(conn: Connection, window: Window) -> dict[int, Decimal]:
    """Ürün başına reklam harcaması payı (kampanya eşit bölüşüm + SKU'ya bağlı reklam giderleri)."""
    out: dict[int, Decimal] = {}
    for r in rows(conn, """
        WITH camp AS (
            SELECT c.id, SUM(s.amount) AS spend FROM ad_campaigns c JOIN ad_spend s ON s.campaign_id = c.id
             WHERE s.spend_date >= :start_date AND s.spend_date <= :end_date GROUP BY c.id),
        n AS (SELECT campaign_id, COUNT(*) AS n FROM ad_campaign_products GROUP BY campaign_id)
        SELECT x.product_id, SUM(camp.spend / n.n) AS spend
          FROM ad_campaign_products x JOIN camp ON camp.id = x.campaign_id JOIN n ON n.campaign_id = x.campaign_id
         GROUP BY x.product_id""", **window.params()):
        out[r["product_id"]] = out.get(r["product_id"], Decimal("0")) + d(r["spend"])
    for r in rows(conn, """
        SELECT p.id AS product_id, SUM(e.amount) AS spend FROM expenses e JOIN products p ON p.sku = e.sku
         WHERE e.category = 'advertising' AND e.sku IS NOT NULL
           AND e.expense_date >= :start_date AND e.expense_date <= :end_date GROUP BY p.id""", **window.params()):
        out[r["product_id"]] = out.get(r["product_id"], Decimal("0")) + d(r["spend"])
    return {k: finance_view.q2(v) for k, v in out.items()}


# ------------------------------------------------------------------ reklam
def campaign_performance(conn: Connection, window: Window, margin_window: Window | None = None) -> list[dict]:
    """Kampanya bazında harcama + platform/kullanıcı bildirimli performans + kampanya ürünlerinin reklam öncesi marjı."""
    margin_window = margin_window or Window(thresholds(conn)["analysis_days"], end_date=window.end_date)
    camps = rows(conn, """
        SELECT c.id, c.name, c.status, c.daily_budget, c.external_id, a.channel, a.name AS account_name,
               m.code AS marketplace
          FROM ad_campaigns c JOIN ad_accounts a ON a.id = c.account_id LEFT JOIN marketplaces m ON m.id = c.marketplace_id
         ORDER BY c.id""")
    if not camps:
        return []
    spend = {r["campaign_id"]: r for r in rows(conn, """
        SELECT campaign_id, SUM(amount) AS spend, MAX(spend_date) AS last_spend_date, COUNT(DISTINCT spend_date) AS days
          FROM ad_spend WHERE spend_date >= :start_date AND spend_date <= :end_date GROUP BY campaign_id""", **window.params())}
    # Aynı gün için birden fazla kaynak varsa tek kaynak alınır (api > csv > manual); çift sayım yapılmaz.
    perf = {r["campaign_id"]: r for r in rows(conn, """
        SELECT campaign_id, SUM(impressions) AS impressions, SUM(clicks) AS clicks,
               SUM(attributed_orders) AS orders, SUM(attributed_revenue) AS revenue, MAX(perf_date) AS last_perf_date,
               COUNT(*) AS days
          FROM (SELECT DISTINCT ON (campaign_id, perf_date) * FROM ad_performance
                 WHERE perf_date >= :start_date AND perf_date <= :end_date
                 ORDER BY campaign_id, perf_date, CASE source WHEN 'api' THEN 0 WHEN 'csv' THEN 1 ELSE 2 END) x
         GROUP BY campaign_id""", **window.params())}
    links: dict[int, list[int]] = {}
    for r in rows(conn, "SELECT campaign_id, product_id FROM ad_campaign_products"):
        links.setdefault(r["campaign_id"], []).append(r["product_id"])
    all_pids = sorted({p for v in links.values() for p in v})
    econ = {e["product_id"]: e for e in product_economics(conn, margin_window, all_pids)} if all_pids else {}
    out = []
    for c in camps:
        pids = links.get(c["id"], [])
        ns = sum((econ[p]["net_sales"] for p in pids if p in econ), Decimal("0"))
        pb = sum((econ[p]["profit_before_ads"] for p in pids if p in econ), Decimal("0"))
        missing_cost = any(econ[p]["missing_cost"] for p in pids if p in econ)
        sp, pf = spend.get(c["id"]) or {}, perf.get(c["id"]) or {}
        spend_amt = _q(sp.get("spend"))
        rev = _q(pf.get("revenue")) if pf.get("revenue") is not None else None
        clicks = int(pf["clicks"]) if pf.get("clicks") is not None else None
        impressions = int(pf["impressions"]) if pf.get("impressions") is not None else None
        orders = int(pf["orders"]) if pf.get("orders") is not None else None
        margin = finance_view.ratio(pb, ns)
        contribution = finance_view.q2(rev * margin) if rev is not None and margin is not None else None
        ad_net = contribution - spend_amt if contribution is not None else None
        out.append({
            **c, "product_ids": pids, "spend": spend_amt, "spend_days": int(sp.get("days") or 0),
            "last_spend_date": sp.get("last_spend_date"), "last_perf_date": pf.get("last_perf_date"),
            "impressions": impressions, "clicks": clicks, "attributed_orders": orders, "attributed_revenue": rev,
            "ctr": finance_view.ratio(clicks, impressions) if clicks is not None and impressions else None,
            "cpc": finance_view.q2(spend_amt / clicks) if clicks else None,
            "conversion_rate": finance_view.ratio(orders, clicks) if orders is not None and clicks else None,
            "roas": finance_view.ratio(rev, spend_amt) if rev is not None and spend_amt else None,
            "product_margin_before_ads": margin, "missing_cost": missing_cost,
            "contribution_before_ads": contribution, "ad_net_profit": ad_net,
            "net_margin_after_ads": finance_view.ratio(ad_net, rev) if ad_net is not None and rev else None,
        })
    return out


# ------------------------------------------------------------------ stok
def inventory_status(conn: Connection, product_ids: list[int] | None = None) -> list[dict]:
    """Satış hızı, kanallar arası kullanılabilir stok, tedarikçi stoğu, kalan gün."""
    th = thresholds(conn)
    now = datetime.now(timezone.utc)
    where = "p.id = ANY(:ids)" if product_ids else """p.id IN (
        SELECT i.product_id FROM order_items i JOIN orders o ON o.id = i.order_id
         WHERE o.order_date > NOW() - make_interval(days => :dead) AND o.internal_status <> 'cancelled'
           AND i.product_id IS NOT NULL)
        OR (COALESCE(p.stock, 0) > 0 AND COALESCE(p.is_active, TRUE) AND EXISTS (SELECT 1 FROM ad_campaign_products x WHERE x.product_id = p.id))"""
    items = rows(conn, f"""
        SELECT p.id, p.sku, p.name, p.stock, p.cost, p.created_at, p.is_active,
               (SELECT SUM(sp.stock) FROM supplier_products sp WHERE sp.product_id = p.id
                  AND COALESCE(sp.status, 'active') = 'active') AS supplier_stock,
               COALESCE(s.u7, 0) AS units_7d, COALESCE(s.u30, 0) AS units_30d, COALESCE(s.udead, 0) AS units_dead
          FROM products p
          LEFT JOIN LATERAL (
              SELECT SUM(i.quantity) FILTER (WHERE o.order_date > :now - INTERVAL '7 days') AS u7,
                     SUM(i.quantity) FILTER (WHERE o.order_date > :now - INTERVAL '30 days') AS u30,
                     SUM(i.quantity) AS udead
                FROM order_items i JOIN orders o ON o.id = i.order_id
               WHERE i.product_id = p.id AND o.internal_status <> 'cancelled'
                 AND o.order_date > :now - make_interval(days => :dead)) s ON TRUE
         WHERE {where}""", ids=product_ids or [], now=now, dead=int(th["dead_stock_days"]))
    avail = stock_availability.available_map(conn, [i["id"] for i in items])
    out = []
    for it in items:
        u7, u30 = int(it["units_7d"]), int(it["units_30d"])
        velocity = Decimal(u7) / 7 if u7 else (Decimal(u30) / 30 if u30 else Decimal("0"))
        a = avail.get(it["id"], 0)
        doi = (Decimal(a) / velocity).quantize(Decimal("0.1")) if velocity > 0 else None
        old_enough = it["created_at"] is None or it["created_at"] < now - timedelta(days=int(th["dead_stock_days"]))
        out.append({
            "product_id": it["id"], "sku": it["sku"], "name": it["name"], "stock": it["stock"], "available": a,
            "supplier_stock": int(it["supplier_stock"]) if it["supplier_stock"] is not None else None,
            "units_7d": u7, "units_30d": u30, "daily_velocity": velocity.quantize(Decimal("0.01")),
            "velocity_basis": "7 gün" if u7 else ("30 gün" if u30 else None),
            "days_of_inventory": doi,
            "stockout_risk": bool(velocity > 0 and (a == 0 or (doi is not None and doi < th["stockout_days"]))),
            "out_of_stock": a <= 0,
            "dead_stock": bool(a > 0 and int(it["units_dead"]) == 0 and old_enough),
            "capital_tied": finance_view.q2(d(it["cost"]) * a) if it["cost"] else None,
        })
    out.sort(key=lambda x: (not x["stockout_risk"], x["days_of_inventory"] if x["days_of_inventory"] is not None else Decimal("9999")))
    return out


# ------------------------------------------------------------------ dönem ve veri kalitesi
def period_summary(conn: Connection, window: Window) -> dict:
    """Dashboard ile aynı dönem özeti (sipariş kârı − dönem giderleri − reklam harcaması − tahmini KDV)."""
    from ...api.analytics import summary
    s = summary(conn, window)
    o = s["orders"]
    return {"window": window.as_dict(), "orders": o["orders"], "revenue": o["revenue"], "net_sales": s["net_sales"],
            "refund": o["refund"], "product_cost": o["product_cost"], "commission": o["commission"],
            "shipping": o["shipping"], "service_fee": o["service_fee"], "ad_spend": s["ad_spend"],
            "expenses": s["expenses"]["total"], "vat_estimate": s["tax_estimate"],
            "net_profit": s["net_profit_after_tax"], "net_margin": s["margin_after_tax"],
            "missing_cost_orders": o["missing_cost_orders"], "orders_without_items": o["orders_without_items"],
            "is_estimate": True}


def data_quality(conn: Connection) -> list[dict]:
    """Kararları etkileyen veri sorunları. Ajanlar bunları kanıt eksikliği olarak raporlar."""
    th = thresholds(conn)
    out = []
    r = row(conn, """
        SELECT (SELECT MAX(last_sync_at) FROM marketplaces WHERE code <> 'storefront') AS last_sync,
               (SELECT COUNT(*) FROM marketplaces WHERE code <> 'storefront' AND last_sync_at IS NOT NULL) AS synced,
               (SELECT COUNT(*) FROM orders WHERE order_date > NOW() - INTERVAL '30 days') AS orders_30d,
               (SELECT COUNT(*) FROM order_items i JOIN orders o ON o.id = i.order_id
                 WHERE o.order_date > NOW() - INTERVAL '30 days' AND o.internal_status <> 'cancelled'
                   AND COALESCE(i.unit_cost, 0) = 0) AS items_missing_cost,
               (SELECT MAX(spend_date) FROM ad_spend) AS last_ad_spend,
               (SELECT MAX(perf_date) FROM ad_performance) AS last_ad_perf,
               (SELECT COUNT(*) FROM ad_campaigns WHERE status = 'active') AS active_campaigns""")
    now = datetime.now(timezone.utc)
    if r["synced"] and r["last_sync"] and r["last_sync"] < now - timedelta(hours=th["data_stale_hours"]):
        out.append({"code": "orders_stale", "severity": "warning",
                    "message": f"Pazaryeri sipariş senkronu {int((now - r['last_sync']).total_seconds() // 3600)} saattir yapılmadı; son veriler eksik olabilir."})
    if not r["synced"]:
        out.append({"code": "no_marketplace_sync", "severity": "info",
                    "message": "Henüz hiçbir pazaryerinden sipariş senkronu yapılmadı."})
    if r["items_missing_cost"]:
        out.append({"code": "missing_cost", "severity": "warning",
                    "message": f"Son 30 günde {r['items_missing_cost']} sipariş kaleminde ürün maliyeti yok; bu ürünlerin kârı olduğundan yüksek görünür."})
    if r["active_campaigns"] and not r["last_ad_perf"]:
        out.append({"code": "no_ad_performance", "severity": "warning",
                    "message": "Aktif kampanya var ama hiç performans (tıklama/sipariş) verisi girilmemiş; reklam kararları verilemez."})
    elif r["last_ad_perf"] and r["last_ad_perf"] < (now.date() - timedelta(days=th["ads_data_stale_days"])):
        out.append({"code": "ads_stale", "severity": "warning",
                    "message": f"Son reklam performans verisi {r['last_ad_perf'].isoformat()} tarihli; reklam kararları bayat veriye dayanır."})
    return out


def capital_accounts(conn: Connection) -> list[dict]:
    return rows(conn, "SELECT id, kind, name, amount, as_of, note, updated_at FROM ai_capital_accounts ORDER BY kind, name")


def monthly_opex(conn: Connection) -> Decimal:
    """Son 90 günün sabit (siparişe/SKU'ya bağlı olmayan, reklam dışı) giderlerinin aylık ortalaması."""
    v = conn.execute(text("""SELECT COALESCE(SUM(amount), 0) FROM expenses
                             WHERE expense_date > CURRENT_DATE - 90 AND sku IS NULL
                               AND COALESCE(category, 'other') <> 'advertising'""")).scalar()
    return finance_view.q2(d(v) / 3)
