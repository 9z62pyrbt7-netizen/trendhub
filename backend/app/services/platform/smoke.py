"""Gerçek Trendyol hesabıyla SALT OKUNUR entegrasyon smoke testi.

    docker compose exec api python -m app.cli trendyol-smoke            (tablo)
    docker compose exec api python -m app.cli trendyol-smoke --json     (JSON)

* Yalnızca GET istekleri (ResilientClient GET/HEAD dışını ağa çıkmadan reddeder). Veritabanına YAZMAZ.
* Çıktıda müşteri kişisel verisi YOK: yalnızca HTTP sonucu, kayıt sayısı, en son kayıt zamanı, tazelik ve eşleme
  sonucu (alan adları / dolu alan oranı). API anahtarları loglanmaz.
* Her servis için durum: CONNECTED | PERMISSION_DENIED | NO_DATA | UNSUPPORTED | ERROR | NOT_CONFIGURED
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ...connectors.base import AuthError, ConnectorError
from ...connectors.registry import get_connector
from . import finance, questions, returns
from .sync import seller_context, webhook_summary


def _http(exc: Exception | None) -> str:
    if exc is None:
        return "200"
    msg = str(exc)
    for code in ("401", "403", "404", "429", "500", "502", "503", "504", "400"):
        if f"HTTP {code}" in msg:
            return code
    return "network" if "Ağ hatası" in msg else "error"


def _status(exc: Exception | None, count: int) -> str:
    if exc is None:
        return "CONNECTED" if count else "NO_DATA"
    if isinstance(exc, AuthError):
        return "PERMISSION_DENIED"
    if "HTTP 404" in str(exc):
        return "UNSUPPORTED"
    return "ERROR"


def _fresh(latest: datetime | None, now: datetime, hours: float) -> str:
    if latest is None:
        return "NO_DATA"
    return "FRESH" if latest >= now - timedelta(hours=hours) else "STALE"


def _fill(rows_: list[dict], fields: list[str]) -> dict:
    n = len(rows_)
    return {f: (f"{sum(1 for r in rows_ if r.get(f) not in (None, ''))}/{n}") for f in fields} if n else {}


def _call(fn):
    try:
        return fn(), None
    except ConnectorError as exc:
        return None, exc


def run(settings=None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    c = get_connector("trendyol", settings)
    if not c.is_configured():
        return {"configured": False, "missing": c.missing_credentials(), "results": []}
    c.client.max_attempts = 2      # smoke: hızlı sonuç
    out = []

    def add(name, perm, exc, count, latest=None, fresh_h=48, mapping=None, extra=None):
        out.append({"service": name, "permission": perm, "http": _http(exc), "status": _status(exc, count),
                    "record_count": count, "latest_record_at": latest.isoformat() if latest else None,
                    "freshness": _fresh(latest, now, fresh_h) if exc is None else None,
                    "mapping": mapping or {}, "error": str(exc)[:200] if exc else None, **(extra or {})})

    # 1. Siparişler (son 2 gün)
    data, exc = _call(lambda: c._get_orders({"startDate": c._ms(now - timedelta(days=2)), "endDate": c._ms(now),
                                             "page": 0, "size": 50}))
    pk = (data or {}).get("content") or []
    norm = c.normalize(pk) if pk else []
    latest = max((o.last_modified or o.order_date for o in norm), default=None)
    add("Orders (getShipmentPackages " + c._orders_version + ")", "Sipariş Entegrasyonu", exc,
        int((data or {}).get("totalElements") or len(pk)), latest, 24,
        {"packages_sample": len(pk), "orders_mapped": len(norm), "lines_mapped": sum(len(o.lines) for o in norm),
         "lines_with_barcode": sum(1 for o in norm for ln in o.lines if ln.barcode),
         "unmapped_status": sum(1 for o in norm if o.internal_status == "needs_review")})

    # 2. Ürünler (V2 onaylı ürün filtresi, ilk sayfa)
    prods, exc = _call(lambda: c.fetch_products_v2(max_pages=1, page_size=20))
    prods = prods or []
    keys = sorted({k for p in prods for k in p.keys()})[:40]
    add("Products (V2 approved filter)", "Ürün Entegrasyonu", exc, len(prods), None, 0,
        {"top_level_fields": keys, "has_variants": sum(1 for p in prods if p.get("variants"))},
        {"note": "V1 ürün servisleri 15.10.2026'da kapanıyor; mevcut ilan senkronu V1 kullanıyor (varsayılan kapalı)."})

    # 3. İadeler (son 14 gün)
    claims, exc = _call(lambda: c.fetch_claims(now - timedelta(days=14), now))
    items = returns.normalize_claims(claims or [])
    latest = max((r["last_modified_at"] for r in items if r["last_modified_at"]), default=None)
    add("Returns (getClaims)", "İade Entegrasyonu", exc, len(items), latest, 72,
        {"claims": len(claims or []), "claim_items": len(items),
         **_fill(items, ["order_number", "barcode", "sku", "amount", "reason_code", "status", "shipment_package_id"])})

    # 4. Finans — settlements (son 15 gün, yalnızca Sale + Return) ve otherfinancials (PaymentOrder, DeductionInvoices)
    st, exc = _call(lambda: c.fetch_settlements(now - timedelta(days=15), now, types=("Sale", "Return")))
    ents = [finance.normalize_entry(r, "settlements") for r in st or []]
    latest = max((e["transaction_date"] for e in ents if e["transaction_date"]), default=None)
    sign = {"sale_commission_positive": sum(1 for e in ents if e["transaction_type"] == "Sale" and (e["commission_amount"] or 0) > 0),
            "sale_credit_rows": sum(1 for e in ents if e["transaction_type"] == "Sale" and e["credit"] > 0),
            "return_debt_rows": sum(1 for e in ents if e["transaction_type"] == "Return" and e["debt"] > 0),
            "unpaid_rows": sum(1 for e in ents if not e["payment_order_id"]),
            "net_vs_sellerRevenue_mismatch": sum(
                1 for e in ents if e["seller_revenue"] is not None and abs(
                    abs((e["credit"] - e["debt"]) + (-(e["commission_amount"] or 0) if e["credit"] >= e["debt"] else (e["commission_amount"] or 0)))
                    - abs(e["seller_revenue"])) > 0.01)}
    add("Finance — settlements (Sale/Return)", "Muhasebe ve Finans Entegrasyonu", exc, len(ents), latest, 72,
        {**_fill(ents, ["order_number", "barcode", "commission_amount", "commission_rate", "seller_revenue", "payment_date",
                        "payment_order_id", "shipment_package_id"]), "sign_checks": sign})
    of, exc = _call(lambda: c.fetch_other_financials(now - timedelta(days=15), now, types=("PaymentOrder", "DeductionInvoices")))
    oe = [finance.normalize_entry(r, "otherfinancials") for r in of or []]
    latest = max((e["transaction_date"] for e in oe if e["transaction_date"]), default=None)
    add("Finance — otherfinancials (PaymentOrder/DeductionInvoices)", "Muhasebe ve Finans Entegrasyonu", exc, len(oe), latest, 24 * 8,
        {"payment_orders": sum(1 for e in oe if e["transaction_type"] == "PaymentOrder"),
         "deduction_invoices": sum(1 for e in oe if e["transaction_type"] == "DeductionInvoices"),
         "cargo_invoices_detected": sum(1 for e in oe if finance.is_cargo_invoice(e)),
         "descriptions": sorted({(e["description"] or "")[:40] for e in oe if e["transaction_type"] == "DeductionInvoices"})[:10],
         **_fill(oe, ["payment_order_id", "payment_date", "invoice_serial"])})

    # 5. Sorular (son 14 gün)
    qs, exc = _call(lambda: c.fetch_questions(now - timedelta(days=14), now))
    nq = [x for x in (questions.normalize_question(q) for q in qs or []) if x]
    latest = max((q["asked_at"] for q in nq if q["asked_at"]), default=None)
    cats: dict[str, int] = {}
    for q in nq:
        cats[q["category"]] = cats.get(q["category"], 0) + 1
    add("Questions (filter)", "Soru Cevap Entegrasyonu", exc, len(nq), latest, 24 * 7,
        {**_fill(nq, ["product_main_id", "barcode", "product_name", "status", "answer_text", "asked_at"]), "categories": cats})

    # 6. Satıcı bilgisi
    sd, exc = _call(c.fetch_seller_addresses)
    ctx = seller_context(sd) if exc is None else {}
    add("Seller info (addresses)", "Satıcı Bilgileri Entegrasyonu", exc, ctx.get("address_count", 0), None, 0,
        {"address_types": ctx.get("address_types"), "fields": ctx.get("fields")})

    # 7. Webhook yapılandırması (yalnızca listeleme)
    wh, exc = _call(c.fetch_webhooks)
    summ = webhook_summary(wh or [])
    add("Webhooks (list)", "Webhook Entegrasyonu", exc, summ["count"], None, 0, summ)
    return {"configured": True, "seller_id_set": bool(c.store_external_id()), "checked_at": now.isoformat(), "results": out}


def format_table(rep: dict) -> str:
    if not rep.get("configured"):
        return "Trendyol bağlı değil: eksik " + ", ".join(rep.get("missing") or [])
    lines = [f"Trendyol salt okunur smoke testi — {rep['checked_at']}", ""]
    for r in rep["results"]:
        lines.append(f"[{r['status']:<17}] {r['service']}  (izin: {r['permission']})")
        lines.append(f"    HTTP {r['http']} · kayıt: {r['record_count']} · en son kayıt: {r['latest_record_at'] or '-'}"
                     f" · tazelik: {r['freshness'] or '-'}")
        if r.get("error"):
            lines.append(f"    hata: {r['error']}")
        if r.get("mapping"):
            lines.append("    eşleme: " + json.dumps(r["mapping"], ensure_ascii=False, default=str)[:600])
        if r.get("note"):
            lines.append("    not: " + r["note"])
    return "\n".join(lines)
