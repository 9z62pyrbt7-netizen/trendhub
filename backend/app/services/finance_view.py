"""TEK finans kaynağı: Dashboard, Siparişler, Sipariş detayı, Finans, Raporlar, Reklam ve Ürün detayı
aynı SQL ifadelerini kullanır; aynı sipariş/dönem için farklı kâr veya marj çıkamaz.

Tanımlar (tüm tutarlar pazaryerinin gönderdiği gibi KDV DAHİL):

    ciro            = birim fiyat × adet          (satıcı indirimi DÜŞÜLMÜŞ fiyat; indirim ayrıca gider değildir)
    net satış       = ciro − iade
    KDV öncesi kâr  = net satış − ürün maliyeti − komisyon (+ komisyon KDV'si, oran KDV hariçse)
                      − hizmet bedeli − kargo − reklam − diğer
    tahmini KDV     = satış KDV'si − ürün maliyeti KDV'si − komisyon KDV'si* − gider KDV'si*
    KDV sonrası kâr = KDV öncesi kâr − tahmini KDV            (TAHMİNİ)
    net marj        = kâr ÷ net satış
    markup          = kâr ÷ ürün maliyeti                     (maliyet üzeri kâr; marjla karıştırılmaz)

  * Komisyon ve gider KDV'si yalnızca Ayarlar'da KDV durumu belirtilmişse hesaba girer. Belirtilmemişse
    (varsayılan) hiçbir değer uydurulmaz: indirim yapılmaz ve sonuç "eksik bilgiyle tahmini" işaretlenir.
    Varsayılan ayarlarla sonuçlar önceki sürümle birebir aynıdır (geriye uyumlu).

Kalemsiz eski (legacy) siparişlerde KDV hesaplanamaz (NULL); toplamlar bunları ayrıca sayar.
İptal edilen siparişler toplamlara girmez. Yalnızca okuma yapar.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.engine import Connection

from ..db import row, rows
from . import app_settings

CENT = Decimal("0.01")
COMMISSION_VAT_MODES = {"unset": "Belirtilmedi", "included": "Komisyon KDV dahil", "excluded": "Komisyon KDV hariç (faturada KDV eklenir)"}
EXPENSE_VAT_MODES = {"unset": "Belirtilmedi", "included": "Kargo/hizmet/reklam giderleri KDV dahil"}


def q2(v) -> Decimal | None:
    return None if v is None else Decimal(v).quantize(CENT, rounding=ROUND_HALF_UP)


def ratio(a, b) -> Decimal | None:
    if a is None or b is None or Decimal(b) == 0:
        return None
    return (Decimal(a) / Decimal(b)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class FinanceConfig:
    commission_vat_mode: str = "unset"
    commission_vat_rate: Decimal = Decimal("20")
    expense_vat_mode: str = "unset"
    expense_vat_rate: Decimal = Decimal("20")
    return_cost_is_loss: bool = False

    def params(self) -> dict:
        return {"cvr": self.commission_vat_rate, "evr": self.expense_vat_rate, "ret_loss": self.return_cost_is_loss}

    @property
    def vat_complete(self) -> bool:
        return self.commission_vat_mode != "unset" and self.expense_vat_mode != "unset"

    @property
    def notes(self) -> list[str]:
        out = []
        if self.commission_vat_mode == "unset":
            out.append("Komisyonun KDV durumu Ayarlar'da belirtilmedi: komisyon KDV'si hesaba katılmadı.")
        if self.expense_vat_mode == "unset":
            out.append("Kargo/hizmet/reklam giderlerinin KDV durumu belirtilmedi: gider KDV'si indirilmedi.")
        return out

    def as_dict(self) -> dict:
        return {"commission_vat_mode": self.commission_vat_mode, "commission_vat_rate": self.commission_vat_rate,
                "expense_vat_mode": self.expense_vat_mode, "expense_vat_rate": self.expense_vat_rate,
                "vat_complete": self.vat_complete, "notes": self.notes}


def load(conn: Connection) -> FinanceConfig:
    def pct(key, default):
        try:
            v = Decimal(str(app_settings.get(conn, key, default)))
            return v if Decimal("0") <= v <= Decimal("100") else Decimal(default)
        except Exception:  # noqa: BLE001
            return Decimal(default)
    cm = str(app_settings.get(conn, "finance.commission_vat_mode", "unset"))
    em = str(app_settings.get(conn, "finance.expense_vat_mode", "unset"))
    return FinanceConfig(
        commission_vat_mode=cm if cm in COMMISSION_VAT_MODES else "unset",
        commission_vat_rate=pct("finance.commission_vat_rate", "20"),
        expense_vat_mode=em if em in EXPENSE_VAT_MODES else "unset",
        expense_vat_rate=pct("finance.expense_vat_rate", "20"),
        return_cost_is_loss=bool(app_settings.get(conn, "finance.return_product_cost_is_loss", False)))


# ------------------------------------------------------------------ SQL parçaları
def _commission_extra(col: str, cfg: FinanceConfig) -> str:
    """Oran KDV hariçse komisyon faturasına eklenen KDV (satıcının ödediği ek tutar)."""
    return f"(({col}) * :cvr / 100)" if cfg.commission_vat_mode == "excluded" else "0"


def _commission_vat(col: str, cfg: FinanceConfig) -> str:
    if cfg.commission_vat_mode == "excluded":
        return f"(({col}) * :cvr / 100)"
    if cfg.commission_vat_mode == "included":
        return f"(({col}) * :cvr / (100 + :cvr))"
    return "0"


def _expense_vat(col: str, cfg: FinanceConfig) -> str:
    return f"(({col}) * :evr / (100 + :evr))" if cfg.expense_vat_mode == "included" else "0"


RATE = "COALESCE(i.vat_rate, p.vat_rate, 20)"
# İade edilen ürün varsayılan olarak stoğa döner (sipariş hesabıyla aynı kural)
ITEM_COST = "(CASE WHEN o.internal_status = 'returned' AND NOT :ret_loss THEN 0 ELSE COALESCE(i.unit_cost, 0) * i.quantity END)"
ITEM_EXPENSES = ("(COALESCE(i.service_fee, 0) + COALESCE(i.shipping_cost, 0) + COALESCE(i.advertising_cost, 0)"
                 " + COALESCE(i.other_cost, 0))")


def item_columns(cfg: FinanceConfig) -> str:
    """order_items i / orders o / products p için kalem bazında finans kolonları (SELECT listesi)."""
    rev = "(i.unit_price * i.quantity)"
    refund = "COALESCE(i.refund_amount, 0)"
    comm = "COALESCE(i.commission, 0)"
    before = f"({rev} - {refund} - {ITEM_COST} - {comm} - {_commission_extra(comm, cfg)} - {ITEM_EXPENSES})"
    product_vat = (f"(CASE WHEN o.internal_status = 'returned' THEN 0 ELSE "
                   f"({rev} - {refund} - {ITEM_COST}) * {RATE} / (100 + {RATE}) END)")
    vat = f"({product_vat} - {_commission_vat(comm, cfg)} - {_expense_vat(ITEM_EXPENSES, cfg)})"
    return f"""{rev} AS revenue, {refund} AS refund, ({rev} - {refund}) AS net_sales, {ITEM_COST} AS product_cost,
        {comm} + {_commission_extra(comm, cfg)} AS commission, COALESCE(i.service_fee, 0) AS service_fee,
        COALESCE(i.shipping_cost, 0) AS shipping, COALESCE(i.advertising_cost, 0) AS advertising,
        COALESCE(i.other_cost, 0) AS other, COALESCE(i.discount, 0) AS discount,
        {before} AS profit_before_vat, {vat} AS vat_estimate, ({before} - {vat}) AS profit_after_vat"""


def order_from(cfg: FinanceConfig, where: str) -> str:
    """Sipariş bazında finans kolonları: FROM ... WHERE parçasıyla birlikte (alias o, m)."""
    comm = "COALESCE(o.commission, 0)"
    exp = ("(COALESCE(o.service_fee, 0) + COALESCE(o.shipping_cost, 0) + COALESCE(o.advertising_cost, 0)"
           " + COALESCE(o.other_cost, 0))")
    before = f"(COALESCE(o.net_profit, 0) - {_commission_extra(comm, cfg)})"
    vat = (f"(CASE WHEN iv.n_items > 0 THEN iv.product_vat - {_commission_vat(comm, cfg)} - {_expense_vat(exp, cfg)} "
           f"ELSE NULL END)")
    return f"""
        SELECT o.id, o.external_order_id, o.internal_status, o.order_date, o.finance_is_estimate,
               m.code AS marketplace, m.name AS marketplace_name,
               COALESCE(o.gross_revenue, 0) AS revenue, COALESCE(o.refund_cost, 0) AS refund,
               COALESCE(o.gross_revenue, 0) - COALESCE(o.refund_cost, 0) AS net_sales,
               COALESCE(o.product_cost, 0) AS product_cost,
               {comm} + {_commission_extra(comm, cfg)} AS commission,
               COALESCE(o.service_fee, 0) AS service_fee, COALESCE(o.shipping_cost, 0) AS shipping,
               COALESCE(o.advertising_cost, 0) AS advertising, COALESCE(o.other_cost, 0) AS other,
               COALESCE(iv.discount, 0) AS discount, COALESCE(iv.n_items, 0) AS n_items,
               COALESCE(iv.missing_cost, FALSE) AS missing_cost,
               {before} AS profit_before_vat, {vat} AS vat_estimate, ({before} - {vat}) AS profit_after_vat
          FROM orders o
          LEFT JOIN stores s ON s.id = o.store_id LEFT JOIN marketplaces m ON m.id = s.marketplace_id
          LEFT JOIN LATERAL (
              SELECT COUNT(*) AS n_items, SUM(COALESCE(i.discount, 0)) AS discount,
                     BOOL_OR(COALESCE(i.unit_cost, 0) = 0) AS missing_cost,
                     SUM(CASE WHEN o.internal_status = 'returned' THEN 0 ELSE
                         ((i.unit_price * i.quantity) - COALESCE(i.refund_amount, 0) - {ITEM_COST})
                         * {RATE} / (100 + {RATE}) END) AS product_vat
                FROM order_items i LEFT JOIN products p ON p.id = i.product_id WHERE i.order_id = o.id
          ) iv ON TRUE
         WHERE {where}"""


def decorate(r: dict) -> dict:
    """Satıra oranları ekler ve tutarları kuruşa yuvarlar."""
    for k in ("revenue", "refund", "net_sales", "product_cost", "commission", "service_fee", "shipping",
              "advertising", "other", "discount", "profit_before_vat", "vat_estimate", "profit_after_vat"):
        if k in r:
            r[k] = q2(r[k])
    # Tek yuvarlama kuralı: KDV sonrası = (kuruşa yuvarlanmış) KDV öncesi − (kuruşa yuvarlanmış) KDV.
    # Böylece her ekranda "öncesi − KDV = sonrası" eşitliği kuruşu kuruşuna tutar.
    if "profit_before_vat" in r and "vat_estimate" in r:
        r["profit_after_vat"] = (None if r["vat_estimate"] is None or r["profit_before_vat"] is None
                                 else r["profit_before_vat"] - r["vat_estimate"])
    r["margin_before_vat"] = ratio(r.get("profit_before_vat"), r.get("net_sales"))
    r["margin_after_vat"] = ratio(r.get("profit_after_vat"), r.get("net_sales"))
    r["markup_before_vat"] = ratio(r.get("profit_before_vat"), r.get("product_cost"))
    r["markup_after_vat"] = ratio(r.get("profit_after_vat"), r.get("product_cost"))
    return r


def order_metrics(conn: Connection, order_ids: list[int], cfg: FinanceConfig | None = None) -> dict[int, dict]:
    if not order_ids:
        return {}
    cfg = cfg or load(conn)
    return {r["id"]: decorate(r) for r in rows(conn, order_from(cfg, "o.id = ANY(:ids)"), ids=list(order_ids),
                                                **cfg.params())}


def item_metrics(conn: Connection, order_id: int, cfg: FinanceConfig | None = None) -> dict[int, dict]:
    cfg = cfg or load(conn)
    return {r["id"]: decorate(r) for r in rows(conn, f"""
        SELECT i.id, {item_columns(cfg)} FROM order_items i JOIN orders o ON o.id = i.order_id
          LEFT JOIN products p ON p.id = i.product_id WHERE i.order_id = :oid""", oid=order_id, **cfg.params())}


def totals(conn: Connection, start, end, marketplace: str | None = None, cfg: FinanceConfig | None = None) -> dict:
    """Dönem toplamları (iptaller hariç). Tüm ekranların kullandığı tek toplama."""
    cfg = cfg or load(conn)
    where = "o.order_date >= :start AND o.order_date < :end AND o.internal_status <> 'cancelled'"
    if marketplace:
        where += " AND m.code = :mp"
    r = row(conn, f"""
        SELECT COUNT(*) AS orders,
               COUNT(*) FILTER (WHERE x.finance_is_estimate) AS estimated_orders,
               COUNT(*) FILTER (WHERE x.n_items = 0) AS orders_without_items,
               COUNT(*) FILTER (WHERE x.missing_cost) AS missing_cost_orders,
               COALESCE(SUM(x.revenue), 0) AS revenue, COALESCE(SUM(x.refund), 0) AS refund,
               COALESCE(SUM(x.net_sales), 0) AS net_sales, COALESCE(SUM(x.product_cost), 0) AS product_cost,
               COALESCE(SUM(x.commission), 0) AS commission, COALESCE(SUM(x.service_fee), 0) AS service_fee,
               COALESCE(SUM(x.shipping), 0) AS shipping, COALESCE(SUM(x.advertising), 0) AS advertising,
               COALESCE(SUM(x.other), 0) AS other, COALESCE(SUM(x.discount), 0) AS discount,
               COALESCE(SUM(x.profit_before_vat), 0) AS profit_before_vat,
               COALESCE(SUM(x.vat_estimate), 0) AS vat_estimate
          FROM ({order_from(cfg, where)}) x""", start=start, end=end, mp=marketplace, **cfg.params())
    out = {k: (int(v) if k in ("orders", "estimated_orders", "orders_without_items", "missing_cost_orders") else q2(v))
           for k, v in r.items()}
    out["profit_after_vat"] = out["profit_before_vat"] - out["vat_estimate"]
    out["vat_complete"] = cfg.vat_complete and not out["orders_without_items"]
    return decorate(out)


def campaign_ad_allocation(conn: Connection, start_date, end_date) -> dict[int, Decimal]:
    """Ürün başına kampanya reklam harcaması payı: kampanya harcaması ÷ kampanyadaki ürün sayısı (eşit bölüşüm, TAHMİNİ).
    SKU raporu, Reklam merkezi ve AI Control Center AYNI fonksiyonu kullanır (ürün kârı tek kaynak)."""
    out: dict[int, Decimal] = {}
    for r in rows(conn, """
        WITH camp AS (
            SELECT c.id, SUM(s.amount) AS spend FROM ad_campaigns c JOIN ad_spend s ON s.campaign_id = c.id
             WHERE s.spend_date >= :sd AND s.spend_date <= :ed GROUP BY c.id),
        n AS (SELECT campaign_id, COUNT(*) AS n FROM ad_campaign_products GROUP BY campaign_id)
        SELECT x.product_id, SUM(camp.spend / n.n) AS spend
          FROM ad_campaign_products x JOIN camp ON camp.id = x.campaign_id JOIN n ON n.campaign_id = x.campaign_id
         GROUP BY x.product_id""", sd=start_date, ed=end_date):
        out[r["product_id"]] = Decimal(r["spend"])
    return out


def sku_ad_expenses(conn: Connection, start_date, end_date) -> dict[str, Decimal]:
    """SKU'ya doğrudan girilmiş reklam giderleri (expenses.category='advertising')."""
    return {r["sku"]: Decimal(r["amount"]) for r in rows(conn, """
        SELECT sku, SUM(amount) AS amount FROM expenses
         WHERE sku IS NOT NULL AND category = 'advertising' AND expense_date >= :sd AND expense_date <= :ed
         GROUP BY sku""", sd=start_date, ed=end_date)}

