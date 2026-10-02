"""Reklam merkezi: hesaplar, kampanyalar, harcama ve performans girişi, analiz.

İlkeler:
  * Reklam API'leri (Trendyol Reklam, Meta, Google) henüz bağlanmadı -> hesaplar 'manuel'.
  * TrendHub ilişkilendirme (attribution) UYDURMAZ. "Doğrudan ilişkilendirilmiş" satış yalnızca
    platformun bildirdiği / kullanıcının girdiği `ad_performance` kayıtlarından gelir.
  * Ayrıca "genel dönem analizi" gösterilir: dönem cirosu ile reklam harcamasının oranı. Bu bir
    ilişkilendirme DEĞİLDİR ve öyle etiketlenir.
  * Ürün bazlı reklam sonrası kâr: kampanya harcaması kampanyaya bağlı ürünlere EŞİT bölünür (tahmini).
"""
import csv
import io
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from ..db import get_conn, row, rows
from ..deps import CurrentUser, client_ip, finance_editor, viewer
from ..services.audit import log_audit
from .analytics import _csv_safe, _num, _ratio
from .common import DateRange, Page, not_found, paged

router = APIRouter(prefix="/api/ads", tags=["ads"])

CHANNELS = {"trendyol_ads": "Trendyol Reklam", "meta": "Meta (Facebook/Instagram)", "google": "Google Ads",
            "hepsiburada_ads": "Hepsiburada Reklam", "amazon_ads": "Amazon Ads", "other": "Diğer"}
ATTR_NOTE = ("Doğrudan ilişkilendirilmiş: yalnızca reklam platformunun bildirdiği veya elle girilen "
             "reklam kaynaklı sipariş/ciro. TrendHub tahmin üretmez.")
GENERAL_NOTE = ("Genel dönem analizi: dönemdeki TÜM satışlar ile reklam harcamasının oranı. "
                "Satışların reklamdan geldiği anlamına gelmez.")


def _q2(v) -> Decimal | None:
    return None if v is None else Decimal(v).quantize(Decimal("0.01"))


# -------------------------------------------------------------------- hesaplar
class AccountIn(BaseModel):
    channel: str = Field(pattern=r"^[a-z_]{2,30}$")
    name: str = Field(min_length=1, max_length=120)


@router.get("/channels")
def channels(_: CurrentUser = Depends(viewer)):
    return [{"code": k, "label": v, "api_status": "manual",
             "api_note": "API bağlantısı henüz yok; harcama ve performans elle girilir."} for k, v in CHANNELS.items()]


@router.get("/accounts")
def list_accounts(_: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    items = rows(conn, """SELECT a.*, (SELECT COUNT(*) FROM ad_campaigns c WHERE c.account_id = a.id) AS campaign_count
                            FROM ad_accounts a ORDER BY a.channel, a.name""")
    for i in items:
        i["channel_label"] = CHANNELS.get(i["channel"], i["channel"])
    return items


@router.post("/accounts", status_code=201)
def create_account(body: AccountIn, request: Request, user: CurrentUser = Depends(finance_editor),
                   conn: Connection = Depends(get_conn)):
    if body.channel not in CHANNELS:
        raise HTTPException(422, "Geçersiz reklam kanalı")
    try:
        with conn.begin_nested():
            aid = conn.execute(text("INSERT INTO ad_accounts(channel, name) VALUES (:c, :n) RETURNING id"),
                               {"c": body.channel, "n": body.name.strip()}).scalar()
    except IntegrityError:
        raise HTTPException(409, "Bu kanalda aynı isimde hesap zaten var") from None
    log_audit(conn, actor=user.username, user_id=user.id, action="ads.account_created", entity_type="ad_account",
              entity_id=aid, ip=client_ip(request), details=body.model_dump())
    return {"id": aid}


# ------------------------------------------------------------------ kampanyalar
class CampaignIn(BaseModel):
    account_id: int
    name: str = Field(min_length=1, max_length=200)
    external_id: str | None = Field(None, max_length=100)
    marketplace: str | None = None
    status: str = Field("active", pattern=r"^(active|paused|ended)$")
    start_date: date | None = None
    end_date: date | None = None
    notes: str | None = Field(None, max_length=1000)
    product_ids: list[int] = Field(default_factory=list, max_length=2000)

    @model_validator(mode="after")
    def _dates(self):
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("Başlangıç tarihi bitişten sonra olamaz")
        return self


def _mp_id(conn, code):
    if not code:
        return None
    mid = conn.execute(text("SELECT id FROM marketplaces WHERE code = :c"), {"c": code}).scalar()
    if mid is None:
        raise HTTPException(422, "Geçersiz pazaryeri")
    return mid


def _set_products(conn, cid: int, product_ids: list[int]) -> None:
    conn.execute(text("DELETE FROM ad_campaign_products WHERE campaign_id = :c"), {"c": cid})
    if product_ids:
        conn.execute(text("""INSERT INTO ad_campaign_products(campaign_id, product_id)
                             SELECT :c, id FROM products WHERE id = ANY(:ids) ON CONFLICT DO NOTHING"""),
                     {"c": cid, "ids": product_ids})


@router.get("/campaigns")
def list_campaigns(_: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    items = rows(conn, """
        SELECT c.*, a.name AS account_name, a.channel, m.name AS marketplace_name,
               (SELECT COALESCE(array_agg(product_id ORDER BY product_id), '{}') FROM ad_campaign_products x
                 WHERE x.campaign_id = c.id) AS product_ids
          FROM ad_campaigns c JOIN ad_accounts a ON a.id = c.account_id LEFT JOIN marketplaces m ON m.id = c.marketplace_id
         ORDER BY c.status, c.name""")
    for i in items:
        i["channel_label"] = CHANNELS.get(i["channel"], i["channel"])
    return items


@router.post("/campaigns", status_code=201)
def create_campaign(body: CampaignIn, request: Request, user: CurrentUser = Depends(finance_editor),
                    conn: Connection = Depends(get_conn)):
    if row(conn, "SELECT id FROM ad_accounts WHERE id = :i", i=body.account_id) is None:
        raise HTTPException(422, "Reklam hesabı bulunamadı")
    try:
        with conn.begin_nested():
            cid = conn.execute(text("""
                INSERT INTO ad_campaigns(account_id, name, external_id, marketplace_id, status, start_date, end_date, notes)
                VALUES (:a, :n, :e, :m, :s, :sd, :ed, :no) RETURNING id"""),
                {"a": body.account_id, "n": body.name.strip(), "e": body.external_id, "m": _mp_id(conn, body.marketplace),
                 "s": body.status, "sd": body.start_date, "ed": body.end_date, "no": body.notes}).scalar()
    except IntegrityError:
        raise HTTPException(409, "Bu hesapta aynı isimde kampanya zaten var") from None
    _set_products(conn, cid, body.product_ids)
    log_audit(conn, actor=user.username, user_id=user.id, action="ads.campaign_created", entity_type="ad_campaign",
              entity_id=cid, ip=client_ip(request), details=body.model_dump(mode="json"))
    return {"id": cid}


@router.put("/campaigns/{cid}")
def update_campaign(cid: int, body: CampaignIn, request: Request, user: CurrentUser = Depends(finance_editor),
                    conn: Connection = Depends(get_conn)):
    n = conn.execute(text("""UPDATE ad_campaigns SET account_id = :a, name = :n, external_id = :e, marketplace_id = :m,
                                    status = :s, start_date = :sd, end_date = :ed, notes = :no WHERE id = :id"""),
                     {"a": body.account_id, "n": body.name.strip(), "e": body.external_id,
                      "m": _mp_id(conn, body.marketplace), "s": body.status, "sd": body.start_date,
                      "ed": body.end_date, "no": body.notes, "id": cid}).rowcount
    if not n:
        raise not_found("Kampanya")
    _set_products(conn, cid, body.product_ids)
    log_audit(conn, actor=user.username, user_id=user.id, action="ads.campaign_updated", entity_type="ad_campaign",
              entity_id=cid, ip=client_ip(request), details=body.model_dump(mode="json"))
    return {"ok": True}


# -------------------------------------------------------------------- harcama
class SpendIn(BaseModel):
    campaign_id: int
    spend_date: date
    amount: Decimal = Field(ge=0, le=Decimal("100000000"))
    note: str | None = Field(None, max_length=500)


@router.get("/spend")
def list_spend(rng: DateRange = Depends(), page: Page = Depends(), _: CurrentUser = Depends(viewer),
               conn: Connection = Depends(get_conn)):
    base = """FROM ad_spend s JOIN ad_campaigns c ON c.id = s.campaign_id JOIN ad_accounts a ON a.id = c.account_id
              LEFT JOIN users u ON u.id = s.created_by
              WHERE s.spend_date >= :start_date AND s.spend_date <= :end_date"""
    total = conn.execute(text(f"SELECT COUNT(*) {base}"), rng.params()).scalar()
    items = rows(conn, f"""SELECT s.id, s.spend_date, s.amount, s.currency, s.source, s.note, s.created_at,
                                  c.name AS campaign_name, a.channel, a.name AS account_name, u.username AS created_by_name
                             {base} ORDER BY s.spend_date DESC, s.id DESC LIMIT :limit OFFSET :offset""",
                 **rng.params(), limit=page.page_size, offset=page.offset)
    for i in items:
        i["channel_label"] = CHANNELS.get(i["channel"], i["channel"])
    return paged(items, total, page)


@router.post("/spend", status_code=201)
def add_spend(body: SpendIn, request: Request, user: CurrentUser = Depends(finance_editor),
              conn: Connection = Depends(get_conn)):
    if row(conn, "SELECT id FROM ad_campaigns WHERE id = :i", i=body.campaign_id) is None:
        raise HTTPException(422, "Kampanya bulunamadı")
    sid = conn.execute(text("""INSERT INTO ad_spend(campaign_id, spend_date, amount, note, created_by, source)
                               VALUES (:c, :d, :a, :n, :u, 'manual') RETURNING id"""),
                       {"c": body.campaign_id, "d": body.spend_date, "a": body.amount, "n": body.note, "u": user.id}).scalar()
    log_audit(conn, actor=user.username, user_id=user.id, action="ads.spend_created", entity_type="ad_spend",
              entity_id=sid, ip=client_ip(request), details=body.model_dump(mode="json"))
    return {"id": sid}


@router.delete("/spend/{sid}")
def delete_spend(sid: int, request: Request, user: CurrentUser = Depends(finance_editor),
                 conn: Connection = Depends(get_conn)):
    s = row(conn, "SELECT * FROM ad_spend WHERE id = :i", i=sid)
    if s is None:
        raise not_found("Harcama")
    if s["source"] != "manual":
        raise HTTPException(409, "Yalnızca elle girilen harcamalar silinebilir")
    conn.execute(text("DELETE FROM ad_spend WHERE id = :i"), {"i": sid})
    log_audit(conn, actor=user.username, user_id=user.id, action="ads.spend_deleted", entity_type="ad_spend",
              entity_id=sid, ip=client_ip(request),
              details={"amount": str(s["amount"]), "spend_date": str(s["spend_date"]), "campaign_id": s["campaign_id"]})
    return {"ok": True}


# ---------------------------------------------------------------- performans
class PerformanceIn(BaseModel):
    campaign_id: int
    perf_date: date
    impressions: int | None = Field(None, ge=0)
    clicks: int | None = Field(None, ge=0)
    attributed_orders: int | None = Field(None, ge=0)
    attributed_revenue: Decimal | None = Field(None, ge=0, le=Decimal("1000000000"))


@router.post("/performance")
def upsert_performance(body: PerformanceIn, request: Request, user: CurrentUser = Depends(finance_editor),
                       conn: Connection = Depends(get_conn)):
    """Platformun bildirdiği metrikler (elle). Aynı kampanya + gün için günceller."""
    if row(conn, "SELECT id FROM ad_campaigns WHERE id = :i", i=body.campaign_id) is None:
        raise HTTPException(422, "Kampanya bulunamadı")
    pid = conn.execute(text("""
        INSERT INTO ad_performance(campaign_id, perf_date, impressions, clicks, attributed_orders, attributed_revenue,
                                   source, created_by)
        VALUES (:c, :d, :i, :cl, :o, :r, 'manual', :u)
        ON CONFLICT (campaign_id, perf_date, source) DO UPDATE SET impressions = EXCLUDED.impressions,
               clicks = EXCLUDED.clicks, attributed_orders = EXCLUDED.attributed_orders,
               attributed_revenue = EXCLUDED.attributed_revenue
        RETURNING id"""), {"c": body.campaign_id, "d": body.perf_date, "i": body.impressions, "cl": body.clicks,
                           "o": body.attributed_orders, "r": body.attributed_revenue, "u": user.id}).scalar()
    log_audit(conn, actor=user.username, user_id=user.id, action="ads.performance_saved", entity_type="ad_performance",
              entity_id=pid, ip=client_ip(request), details=body.model_dump(mode="json"))
    return {"id": pid}


# --------------------------------------------------------------------- analiz
def spend_total(conn: Connection, rng: DateRange) -> Decimal:
    return _num(conn.execute(text("""SELECT COALESCE(SUM(amount), 0) FROM ad_spend
                                      WHERE spend_date >= :start_date AND spend_date <= :end_date"""),
                             rng.params()).scalar())


def _metrics(spend: Decimal, perf: dict) -> dict:
    """Metrikler yalnızca veri varsa hesaplanır; yoksa None (arayüzde '—')."""
    has_attr = perf.get("perf_rows", 0) > 0 and perf.get("attributed_revenue") is not None
    rev = _num(perf["attributed_revenue"]) if has_attr else None
    orders = int(perf["attributed_orders"]) if perf.get("attributed_orders") is not None else None
    clicks = int(perf["clicks"]) if perf.get("clicks") is not None else None
    impressions = int(perf["impressions"]) if perf.get("impressions") is not None else None
    return {"spend": spend, "impressions": impressions, "clicks": clicks,
            "attributed_orders": orders, "attributed_revenue": rev, "has_attribution": has_attr,
            "roas": _ratio(rev, spend) if rev is not None and spend else None,
            "cpa": (spend / orders).quantize(Decimal("0.01")) if orders else None,
            "ctr": _ratio(Decimal(clicks), Decimal(impressions)) if clicks is not None and impressions else None,
            "conversion_rate": _ratio(Decimal(orders), Decimal(clicks)) if orders is not None and clicks else None}


@router.get("/summary")
def ads_summary(rng: DateRange = Depends(), _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    p = rng.params()
    camps = rows(conn, """
        SELECT c.id, c.name, c.status, a.channel, a.name AS account_name,
               (SELECT COALESCE(SUM(amount), 0) FROM ad_spend s WHERE s.campaign_id = c.id
                  AND s.spend_date >= :start_date AND s.spend_date <= :end_date) AS spend,
               (SELECT COUNT(*) FROM ad_performance x WHERE x.campaign_id = c.id
                  AND x.perf_date >= :start_date AND x.perf_date <= :end_date) AS perf_rows,
               (SELECT SUM(impressions) FROM ad_performance x WHERE x.campaign_id = c.id
                  AND x.perf_date >= :start_date AND x.perf_date <= :end_date) AS impressions,
               (SELECT SUM(clicks) FROM ad_performance x WHERE x.campaign_id = c.id
                  AND x.perf_date >= :start_date AND x.perf_date <= :end_date) AS clicks,
               (SELECT SUM(attributed_orders) FROM ad_performance x WHERE x.campaign_id = c.id
                  AND x.perf_date >= :start_date AND x.perf_date <= :end_date) AS attributed_orders,
               (SELECT SUM(attributed_revenue) FROM ad_performance x WHERE x.campaign_id = c.id
                  AND x.perf_date >= :start_date AND x.perf_date <= :end_date) AS attributed_revenue
          FROM ad_campaigns c JOIN ad_accounts a ON a.id = c.account_id ORDER BY c.name""", **p)
    by_campaign, by_channel = [], {}
    for c in camps:
        m = _metrics(_num(c["spend"]), c)
        by_campaign.append({"id": c["id"], "name": c["name"], "status": c["status"], "channel": c["channel"],
                            "channel_label": CHANNELS.get(c["channel"], c["channel"]), **m})
        ch = by_channel.setdefault(c["channel"], {"spend": Decimal("0"), "perf_rows": 0, "impressions": None,
                                                  "clicks": None, "attributed_orders": None, "attributed_revenue": None})
        ch["spend"] += _num(c["spend"])
        ch["perf_rows"] += int(c["perf_rows"])
        for k in ("impressions", "clicks", "attributed_orders", "attributed_revenue"):
            if c[k] is not None:
                ch[k] = (ch[k] or 0) + c[k]
    channels_out = [{"channel": k, "channel_label": CHANNELS.get(k, k), **_metrics(v["spend"], v)}
                    for k, v in sorted(by_channel.items())]
    total_spend = spend_total(conn, rng)
    all_perf = {"perf_rows": sum(int(c["perf_rows"]) for c in camps)}
    for k in ("impressions", "clicks", "attributed_orders", "attributed_revenue"):
        vals = [c[k] for c in camps if c[k] is not None]
        all_perf[k] = sum(vals) if vals else None
    revenue = _num(conn.execute(text("""SELECT COALESCE(SUM(gross_revenue), 0) FROM orders
                                         WHERE order_date >= :start AND order_date < :end AND internal_status <> 'cancelled'"""),
                                p).scalar())
    orders = conn.execute(text("""SELECT COUNT(*) FROM orders WHERE order_date >= :start AND order_date < :end
                                   AND internal_status <> 'cancelled'"""), p).scalar()
    return {
        "range": rng.as_dict(),
        "direct": {**_metrics(total_spend, all_perf), "note": ATTR_NOTE},
        "general": {"spend": total_spend, "revenue": revenue, "orders": orders, "note": GENERAL_NOTE,
                    "spend_share_of_revenue": _ratio(total_spend, revenue),
                    "revenue_per_spend": _ratio(revenue, total_spend) if total_spend else None,
                    "cost_per_order": (total_spend / orders).quantize(Decimal("0.01")) if orders and total_spend else None},
        "by_channel": channels_out, "by_campaign": by_campaign,
        "products": product_profit_after_ads(conn, rng),
    }


def product_profit_after_ads(conn: Connection, rng: DateRange) -> list[dict]:
    """Kampanyaya bağlı ürünlerin dönem kârı − payına düşen reklam (TAHMİNİ, eşit bölüşüm).
    Tek kaynak: AI Control Center ürün ekonomisi (finance_view kalem kârı + finance_view reklam bölüşümü)."""
    from ..services.ai.data import product_economics
    linked = [r["product_id"] for r in rows(conn, "SELECT DISTINCT product_id FROM ad_campaign_products")]
    if not linked:
        return []
    out = [{"product_id": e["product_id"], "name": e["name"], "sku": e["sku"], "quantity": e["units"], "revenue": e["net_sales"],
            "profit_before_ads": e["profit_before_ads"], "ad_spend": e["ad_spend_allocated"], "profit_after_ads": e["net_profit"],
            "is_estimate": True} for e in product_economics(conn, rng, linked) if e["ad_spend_allocated"] > 0]
    out.sort(key=lambda x: (-x["ad_spend"], x["product_id"]))
    return out


@router.get("/spend.csv")
def spend_csv(rng: DateRange = Depends(), _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    items = rows(conn, """SELECT s.spend_date, a.channel, a.name AS account_name, c.name AS campaign_name, s.amount,
                                 s.currency, s.source, s.note
                            FROM ad_spend s JOIN ad_campaigns c ON c.id = s.campaign_id JOIN ad_accounts a ON a.id = c.account_id
                           WHERE s.spend_date >= :start_date AND s.spend_date <= :end_date ORDER BY s.spend_date, s.id""",
                 **rng.params())
    buf = io.StringIO()
    buf.write("﻿")
    w = csv.writer(buf, delimiter=";")
    w.writerow(["Tarih", "Kanal", "Hesap", "Kampanya", "Tutar", "Para birimi", "Kaynak", "Not"])
    for i in items:
        w.writerow([_csv_safe(v) for v in (i["spend_date"], CHANNELS.get(i["channel"], i["channel"]), i["account_name"],
                                           i["campaign_name"], i["amount"], i["currency"],
                                           "Elle" if i["source"] == "manual" else i["source"], i["note"])])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f'attachment; filename="trendhub-reklam-{rng.start_date}-{rng.end_date}.csv"'})
