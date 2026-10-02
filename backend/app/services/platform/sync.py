"""Genişletilmiş Trendyol polling işleri (SALT OKUNUR): finans, iadeler, sorular, satıcı bilgisi, webhook listesi.

Her iş: deneme kaydı → ağ çağrısı (transaction DIŞINDA) → kaydet + iç olay üret → kaynak durumunu yaz → olayları işle.
Hata: kaynak PERMISSION_DENIED / UNSUPPORTED / ERROR olarak işaretlenir; daha önce alınmış veri silinmez (DEGRADED).
Kısmi kesinti: finans işinde settlements alınıp otherfinancials alınamazsa alınan kayıtlar yazılır ama kaynak
BAŞARILI sayılmaz (tazelik iddia edilmez).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy.engine import Engine

from ...connectors.base import (CAP_QUESTIONS_READ, CAP_RETURNS_READ, CAP_SELLER_READ, CAP_SETTLEMENTS_READ, CAP_WEBHOOKS_READ,
                                ConnectorError, NotConfigured, NotSupported)
from ...connectors.registry import get_connector
from ...db import row
from ..events import record_event, resolve_fingerprint
from ..orders_sync import ensure_store
from . import events, finance, questions, returns, sources

log = logging.getLogger("trendhub.platform.sync")

FINANCE_SYNC = "finance.sync"
RETURNS_SYNC = "returns.sync"
QUESTIONS_SYNC = "questions.sync"
SELLER_SYNC = "seller.sync"
WEBHOOKS_CHECK = "webhooks.check"
EVENTS_PROCESS = "events.process"
FINANCE_LOOKBACK_DAYS = 30      # ödeme talimatına bağlanan eski kayıtlar da tazelensin
RETURNS_LOOKBACK_DAYS = 30
QUESTIONS_FIRST_DAYS = 30
QUESTIONS_OVERLAP_DAYS = 3
CARGO_INVOICES_PER_RUN = 20

JOB_LABELS_TR = {
    FINANCE_SYNC: "Trendyol finans (cari hesap ekstresi, salt okunur)",
    RETURNS_SYNC: "Trendyol iadeler (salt okunur)",
    QUESTIONS_SYNC: "Trendyol müşteri soruları (salt okunur; cevap gönderilmez)",
    SELLER_SYNC: "Trendyol satıcı bilgileri (salt okunur)",
    WEBHOOKS_CHECK: "Trendyol webhook yapılandırması (yalnızca listeleme)",
    EVENTS_PROCESS: "Platform olaylarını işle (webhook + polling → ilgili hesaplama)",
}
# (iş tipi, yetenek, dakika)
SCHEDULE = [(FINANCE_SYNC, CAP_SETTLEMENTS_READ, 180), (RETURNS_SYNC, CAP_RETURNS_READ, 60),
            (QUESTIONS_SYNC, CAP_QUESTIONS_READ, 30), (SELLER_SYNC, CAP_SELLER_READ, 24 * 60),
            (WEBHOOKS_CHECK, CAP_WEBHOOKS_READ, 6 * 60)]
RESOURCE = {FINANCE_SYNC: "finance", RETURNS_SYNC: "returns", QUESTIONS_SYNC: "questions", SELLER_SYNC: "seller",
            WEBHOOKS_CHECK: "webhooks"}
CAPABILITY = {job: cap for job, cap, _ in SCHEDULE}


def _connector(job_type: str, settings=None):
    c = get_connector("trendyol", settings)
    if not c.is_configured():
        raise NotConfigured("Trendyol bağlı değil: eksik " + ", ".join(c.missing_credentials()))
    if not c.supports(CAPABILITY[job_type]):
        raise NotSupported(f"Trendyol: {JOB_LABELS_TR[job_type]} kapalı (TRENDYOL_EXTENDED_READ=false)")
    return c


def run(engine: Engine, job_type: str, settings=None, *, now: datetime | None = None) -> dict:
    c = _connector(job_type, settings)
    resource = RESOURCE[job_type]
    now = now or datetime.now(timezone.utc)
    with engine.begin() as conn:
        store_id = ensure_store(conn, "trendyol", c.store_external_id(), c.name)
        sources.mark_attempt(conn, store_id, resource)
        prev = sources.trendyol_state(conn, resource)
    try:
        fn = {FINANCE_SYNC: _finance, RETURNS_SYNC: _returns, QUESTIONS_SYNC: _questions, SELLER_SYNC: _seller,
              WEBHOOKS_CHECK: _webhooks}[job_type]
        result = fn(engine, c, store_id, now, prev)
    except ConnectorError as exc:
        with engine.begin() as conn:
            status = sources.mark_failure(conn, store_id, resource, exc)
            record_event(conn, level="error" if status != "ERROR" else "warning", source=f"trendyol:{resource}",
                         message=f"Trendyol {resource}: {status} — {str(exc)[:300]}", fingerprint=f"platform:{resource}")
        raise
    with engine.begin() as conn:
        resolve_fingerprint(conn, f"platform:{resource}")
    if result.pop("_events", 0):
        result["events"] = events.process_pending(engine)
    return result


# ------------------------------------------------------------------ finans
def _finance(engine: Engine, c, store_id: int, now: datetime, prev) -> dict:
    since = now - timedelta(days=FINANCE_LOOKBACK_DAYS)
    settlements = [finance.normalize_entry(r, "settlements") for r in c.fetch_settlements(since, now)]
    partial_error = None
    try:
        others = [finance.normalize_entry(r, "otherfinancials") for r in c.fetch_other_financials(since, now)]
    except ConnectorError as exc:
        others, partial_error = [], exc
    out = {"settlements": len(settlements), "otherfinancials": len(others), "inserted": 0, "paid_now": 0, "cargo_items": 0,
           "cargo_errors": []}
    new_invoices = []
    with engine.begin() as conn:
        for e in settlements + others:
            res = finance.upsert_entry(conn, store_id, e)
            if res["inserted"]:
                out["inserted"] += 1
                if e["source"] == "settlements" and e["transaction_type"] in ("Sale", "Return"):
                    events.emit(conn, source="polling", marketplace="trendyol", event_type="FINANCE_TRANSACTION_CREATED",
                                dedupe_key=f"tyfin:{e['source']}:{e['external_id']}:{e['transaction_type']}",
                                entity_type="finance_entry", entity_ref=e["order_number"], payload={"entry_id": res["id"]})
                if finance.is_cargo_invoice(e) and e["invoice_serial"]:
                    new_invoices.append(e)
            if res["paid_now"]:
                out["paid_now"] += 1
                events.emit(conn, source="polling", marketplace="trendyol", event_type="PAYOUT_UPDATED",
                            dedupe_key=f"payout:{e['payment_order_id']}", entity_type="payment_order",
                            entity_ref=e["payment_order_id"], payload={"payment_order_id": e["payment_order_id"]})
    for inv in new_invoices[:CARGO_INVOICES_PER_RUN]:
        try:
            items = c.fetch_cargo_invoice_items(inv["invoice_serial"])
        except ConnectorError as exc:  # kargo faturası detayı alınamazsa tahmini kargo kalır; iş başarısız sayılmaz
            out["cargo_errors"].append(f"{inv['invoice_serial']}: {str(exc)[:120]}")
            continue
        with engine.begin() as conn:
            for it in items:
                e = finance.normalize_cargo_item(it, inv["invoice_serial"], inv["transaction_date"], inv["payment_order_id"],
                                                 inv["payment_date"])
                res = finance.upsert_entry(conn, store_id, e)
                if res["inserted"]:
                    out["cargo_items"] += 1
                    events.emit(conn, source="polling", marketplace="trendyol", event_type="FINANCE_TRANSACTION_CREATED",
                                dedupe_key=f"tyfin:cargo:{e['external_id']}", entity_type="finance_entry",
                                entity_ref=e["order_number"], payload={"entry_id": res["id"]})
    with engine.begin() as conn:
        out["reapplied_orders"] = len(finance.apply_pending(conn, store_id))
        latest = row(conn, "SELECT MAX(transaction_date) AS t FROM marketplace_finance_entries WHERE store_id = :s", s=store_id)["t"]
        if partial_error is not None:
            sources.mark_failure(conn, store_id, "finance", ConnectorError(f"Kısmi kesinti (otherfinancials): {partial_error}"))
        else:
            sources.mark_success(conn, store_id, "finance", count=len(settlements) + len(others), latest=latest, synced_until=now,
                                 meta={"settlements": len(settlements), "otherfinancials": len(others),
                                       "cargo_errors": out["cargo_errors"][:5]})
    if partial_error is not None:
        out["partial_error"] = str(partial_error)[:300]
        out["_events"] = 1
        # Kısmi veriler işlendi ama iş başarısız sayılır (worker tekrar dener)
        events.process_pending(engine)
        raise ConnectorError(f"Finans senkronu kısmi: otherfinancials alınamadı ({str(partial_error)[:200]})")
    out["_events"] = 1
    return out


# ------------------------------------------------------------------ iadeler
def _returns(engine: Engine, c, store_id: int, now: datetime, prev) -> dict:
    items = returns.normalize_claims(c.fetch_claims(now - timedelta(days=RETURNS_LOOKBACK_DAYS), now))
    out = {"claim_items": len(items), "created": 0, "updated": 0}
    with engine.begin() as conn:
        for r in items:
            res = returns.upsert_return(conn, store_id, r, apply=False)
            if res["event"]:
                out["created" if res["event"] == "RETURN_CREATED" else "updated"] += 1
                events.emit(conn, source="polling", marketplace="trendyol", event_type=res["event"],
                            dedupe_key=f"return:{r['claim_item_id']}:{r['status']}", entity_type="return",
                            entity_ref=r["order_number"], payload={"return_id": res["id"], "status": r["status"],
                                                                   "reason": r["reason_code"]})
        latest = max((r["last_modified_at"] for r in items if r["last_modified_at"]), default=None)
        sources.mark_success(conn, store_id, "returns", count=len(items), latest=latest, synced_until=now)
    out["_events"] = 1
    return out


# ------------------------------------------------------------------ sorular
def _questions(engine: Engine, c, store_id: int, now: datetime, prev) -> dict:
    last = (prev or {}).get("synced_until")
    since = (last - timedelta(days=QUESTIONS_OVERLAP_DAYS)) if last else now - timedelta(days=QUESTIONS_FIRST_DAYS)
    raw = c.fetch_questions(since, now)
    out = {"questions": len(raw), "created": 0}
    with engine.begin() as conn:
        for q in raw:
            n = questions.normalize_question(q)
            if n is None:
                continue
            ev = questions.upsert_question(conn, store_id, n)
            if ev == "QUESTION_CREATED":
                out["created"] += 1
                events.emit(conn, source="polling", marketplace="trendyol", event_type="QUESTION_CREATED",
                            dedupe_key=f"question:{n['external_id']}", entity_type="question", entity_ref=n["external_id"],
                            payload={"category": n["category"]})
        latest = row(conn, "SELECT MAX(asked_at) AS t FROM customer_questions WHERE store_id = :s", s=store_id)["t"]
        sources.mark_success(conn, store_id, "questions", count=len(raw), latest=latest, synced_until=now)
    out["_events"] = 1
    return out


# ------------------------------------------------------------------ satıcı bilgisi
def seller_context(data) -> dict:
    """Adres yanıtından yalnızca karar için gereken özet (tam adres SAKLANMAZ)."""
    addrs = []
    if isinstance(data, dict):
        for k in ("supplierAddresses", "addresses", "content"):
            if isinstance(data.get(k), list):
                addrs = data[k]
                break
    elif isinstance(data, list):
        addrs = data
    types = sorted({str(a.get("addressType") or a.get("type") or "?") for a in addrs if isinstance(a, dict)})
    cities = sorted({str(a.get("city")) for a in addrs if isinstance(a, dict) and a.get("city")})
    return {"address_count": len(addrs), "address_types": types, "cities": cities,
            "has_return_address": any("return" in t.lower() for t in types),
            "fields": sorted({k for a in addrs if isinstance(a, dict) for k in a.keys()})}


def _seller(engine: Engine, c, store_id: int, now: datetime, prev) -> dict:
    ctx = seller_context(c.fetch_seller_addresses())
    with engine.begin() as conn:
        # Satıcı bağlamı yalnızca sync_state.meta'da tutulur (tam adres kopyalanmaz)
        sources.mark_success(conn, store_id, "seller", count=ctx["address_count"], latest=None, synced_until=now, meta=ctx)
    return ctx


# ------------------------------------------------------------------ webhook yapılandırması (yalnızca okuma)
def webhook_summary(items: list[dict]) -> dict:
    return {"count": len(items),
            "active": sum(1 for w in items if str(w.get("status") or "").upper() == "ACTIVE"),
            "webhooks": [{"id": w.get("id"), "status": w.get("status"), "authentication_type": w.get("authenticationType"),
                          "host": urlparse(str(w.get("url") or "")).hostname, "subscribed_statuses": w.get("subscribedStatuses")}
                         for w in items]}


def _webhooks(engine: Engine, c, store_id: int, now: datetime, prev) -> dict:
    summary = webhook_summary(c.fetch_webhooks())
    with engine.begin() as conn:
        sources.mark_success(conn, store_id, "webhooks", count=summary["count"], latest=None, synced_until=now, meta=summary)
    return summary
