"""Pazaryeri finans defteri (Trendyol cari hesap ekstresi) → NAKİT ve GERÇEKLEŞEN kâr.

KÂR ≠ NAKİT:
  * Kâr tarafı: eşleşen satış/iade kayıtlarındaki GERÇEK komisyon ve iade tutarı, kargo faturası kalemlerindeki GERÇEK
    kargo bedeli `financial_transactions`'a (source='trendyol_finance') yazılır; mevcut kâr motoru
    (`finance_service.recalculate_order`) bunları tahminin yerine kullanır. Gerçek kayıt yoksa tahmin kullanılır ve
    sipariş `finance_is_estimate = TRUE` kalır.
  * Nakit tarafı: ödenmemiş (paymentOrderId boş) kayıtların net tutarı = bekleyen hakediş / pazaryeri alacağı.
    Bekleyen hakediş KULLANILABİLİR NAKİT DEĞİLDİR (sermaye motoru bunu asla harcanabilir saymaz).

Kayıt başına net etki (satıcı lehine +):
    settlements:      (credit − debt) − komisyon  (alacak kaydında komisyon düşer, borç kaydında komisyon iade olur)
    otherfinancials:  credit − debt               (kesinti faturası vb.; PaymentOrder = yapılmış ödeme, alacağa dahil edilmez)
Bu işaret kuralı resmî dokümandaki alanlardan çıkarılmıştır; canlı veride `sellerRevenue` ile çapraz kontrol edilir
(smoke test ve Finans ekranındaki mutabakat satırı).
"""
from __future__ import annotations

import hashlib
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ...db import row, rows
from ..ai.config import TZ, d, thresholds
from ..finance_service import recalculate_order

log = logging.getLogger("trendhub.platform.finance")

SOURCE = "trendyol_finance"
SALE_TYPES = {"Sale"}
RETURN_TYPES = {"Return"}
CARGO_HINTS = ("kargo", "cargo")


# ------------------------------------------------------------------ eşleme (ham → defter satırı)
def _dt(v) -> datetime | None:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
        return datetime.fromtimestamp(int(v) / 1000, tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ)


def _dec(v) -> Decimal | None:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except Exception:  # noqa: BLE001
        return None


def _s(v) -> str | None:
    return None if v in (None, "") else str(v)


def normalize_entry(raw: dict, source: str) -> dict:
    ttype = str(raw.get("_type") or raw.get("transactionType") or "unknown")
    ext = _s(raw.get("id"))
    if ext is None:  # kimliksiz kayıt: içerikten kararlı anahtar
        ext = "h:" + hashlib.sha256("|".join(str(raw.get(k)) for k in (
            "transactionDate", "orderNumber", "barcode", "debt", "credit", "receiptId")).encode()).hexdigest()[:24]
    return {
        "source": source, "external_id": ext, "transaction_type": ttype,
        "transaction_sub_type": _s(raw.get("transactionSubType")),
        "transaction_date": _dt(raw.get("transactionDate")), "order_number": _s(raw.get("orderNumber")),
        "shipment_package_id": _s(raw.get("shipmentPackageId")), "barcode": _s(raw.get("barcode")),
        "description": _s(raw.get("description")), "debt": _dec(raw.get("debt")) or Decimal("0"),
        "credit": _dec(raw.get("credit")) or Decimal("0"), "commission_rate": _dec(raw.get("commissionRate")),
        "commission_amount": _dec(raw.get("commissionAmount")), "seller_revenue": _dec(raw.get("sellerRevenue")),
        "payment_order_id": _s(raw.get("paymentOrderId")), "payment_date": _dt(raw.get("paymentDate")),
        "receipt_id": _s(raw.get("receiptId")),
        "invoice_serial": _s(raw.get("invoiceSerialNumber") or raw.get("commissionInvoiceSerialNumber")
                             or (raw.get("id") if ttype == "DeductionInvoices" else None)),
    }


def normalize_cargo_item(raw: dict, invoice_serial: str, invoice_date: datetime | None,
                         payment_order_id: str | None, payment_date: datetime | None) -> dict:
    parcel = _s(raw.get("parcelUniqueId")) or hashlib.sha256(repr(sorted(raw.items())).encode()).hexdigest()[:16]
    return {
        "source": "cargo_invoice", "external_id": f"{invoice_serial}:{parcel}",
        "transaction_type": str(raw.get("shipmentPackageType") or "Kargo"), "transaction_sub_type": None,
        "transaction_date": invoice_date, "order_number": _s(raw.get("orderNumber")), "shipment_package_id": None,
        "barcode": None, "description": f"desi {raw.get('desi')}" if raw.get("desi") is not None else None,
        "debt": _dec(raw.get("amount")) or Decimal("0"), "credit": Decimal("0"), "commission_rate": None,
        "commission_amount": None, "seller_revenue": None, "payment_order_id": payment_order_id,
        "payment_date": payment_date, "receipt_id": None, "invoice_serial": invoice_serial,
    }


# ------------------------------------------------------------------ yazma
def upsert_entry(conn: Connection, store_id: int, e: dict) -> dict:
    """İdempotent. Dönüş: {'id', 'inserted', 'paid_now'} (paid_now: bu senkronda ödeme talimatına bağlandı)."""
    prev = row(conn, """SELECT id, payment_order_id FROM marketplace_finance_entries
                         WHERE store_id = :s AND source = :src AND external_id = :x AND transaction_type = :t""",
               s=store_id, src=e["source"], x=e["external_id"], t=e["transaction_type"])
    r = row(conn, """
        INSERT INTO marketplace_finance_entries(store_id, source, external_id, transaction_type, transaction_sub_type,
            transaction_date, order_number, shipment_package_id, barcode, description, debt, credit, commission_rate,
            commission_amount, seller_revenue, payment_order_id, payment_date, receipt_id, invoice_serial)
        VALUES (:store, :source, :external_id, :transaction_type, :transaction_sub_type, :transaction_date, :order_number,
            :shipment_package_id, :barcode, :description, :debt, :credit, :commission_rate, :commission_amount,
            :seller_revenue, :payment_order_id, :payment_date, :receipt_id, :invoice_serial)
        ON CONFLICT (store_id, source, external_id, transaction_type) DO UPDATE SET
            payment_order_id = COALESCE(EXCLUDED.payment_order_id, marketplace_finance_entries.payment_order_id),
            payment_date = COALESCE(EXCLUDED.payment_date, marketplace_finance_entries.payment_date),
            debt = EXCLUDED.debt, credit = EXCLUDED.credit, commission_amount = EXCLUDED.commission_amount,
            commission_rate = EXCLUDED.commission_rate, seller_revenue = EXCLUDED.seller_revenue,
            description = EXCLUDED.description, updated_at = NOW()
        RETURNING id""", store=store_id, **e)
    return {"id": r["id"], "inserted": prev is None,
            "paid_now": bool(e["payment_order_id"]) and prev is not None and prev["payment_order_id"] is None}


def _match(conn: Connection, store_id: int, order_number: str | None, barcode: str | None) -> tuple[int | None, int | None]:
    if not order_number:
        return None, None
    oid = conn.execute(text("SELECT id FROM orders WHERE store_id = :s AND external_order_id = :n"),
                       {"s": store_id, "n": order_number}).scalar()
    if oid is None:
        return None, None
    iid = None
    if barcode:
        iid = conn.execute(text("SELECT id FROM order_items WHERE order_id = :o AND barcode = :b ORDER BY id LIMIT 1"),
                           {"o": oid, "b": barcode}).scalar()
    return oid, iid


def _fin_tx(conn: Connection, store_id: int, order_id: int, item_id: int | None, ref: str, kind: str, amount: Decimal,
            at: datetime | None, desc: str) -> None:
    conn.execute(text("""
        INSERT INTO financial_transactions(store_id, order_id, order_item_id, source, external_ref, kind, amount, occurred_at, description)
        VALUES (:s, :o, :i, :src, :ref, :k, :a, COALESCE(:at, NOW()), :d)
        ON CONFLICT (source, external_ref) DO UPDATE SET amount = EXCLUDED.amount, order_id = EXCLUDED.order_id,
            order_item_id = EXCLUDED.order_item_id"""),
        {"s": store_id, "o": order_id, "i": item_id, "src": SOURCE, "ref": ref, "k": kind, "a": amount, "at": at, "d": desc})


def apply_entry(conn: Connection, store_id: int, entry_id: int) -> int | None:
    """Defter satırını kâr motoruna aktarır (eşleşme varsa). Etkilenen sipariş id'si döner."""
    e = row(conn, "SELECT * FROM marketplace_finance_entries WHERE id = :i", i=entry_id)
    oid, iid = _match(conn, store_id, e["order_number"], e["barcode"])
    if oid is None:
        return None
    t = e["transaction_type"]
    at = e["transaction_date"]
    applied = False
    if e["source"] == "settlements" and iid is not None and t in SALE_TYPES | RETURN_TYPES:
        comm = d(e["commission_amount"])
        if e["commission_amount"] is not None:
            _fin_tx(conn, store_id, oid, iid, f"che:{e['id']}:commission", "commission",
                    comm if t in SALE_TYPES else -comm, at, f"Trendyol gerçek komisyon ({t})")
        if t in RETURN_TYPES and d(e["debt"]) > 0:
            from .returns import drop_claim_refund_for_item
            drop_claim_refund_for_item(conn, iid)   # claim kaynaklı geçici iade satırı yerine gerçek kayıt
            _fin_tx(conn, store_id, oid, iid, f"che:{e['id']}:refund", "refund", d(e["debt"]), at, "Trendyol gerçek iade tutarı")
        applied = True
    elif e["source"] == "cargo_invoice" and d(e["debt"]) > 0:
        _fin_tx(conn, store_id, oid, None, f"che:{e['id']}:shipping", "shipping", d(e["debt"]), at,
                f"Trendyol kargo faturası: {e['transaction_type']}")
        applied = True
    conn.execute(text("UPDATE marketplace_finance_entries SET order_id = :o, order_item_id = :i, applied_at = CASE WHEN :ap THEN NOW() ELSE applied_at END WHERE id = :id"),
                 {"o": oid, "i": iid, "ap": applied, "id": entry_id})
    if applied:
        recalculate_order(conn, oid)
        return oid
    return None


def apply_pending(conn: Connection, store_id: int) -> list[int]:
    """Henüz kâra aktarılmamış (sipariş sonradan gelmiş olabilir) satırları yeniden eşler."""
    ids = conn.execute(text("""SELECT id FROM marketplace_finance_entries WHERE store_id = :s AND applied_at IS NULL
                                 AND order_number IS NOT NULL
                                 AND (source = 'cargo_invoice' OR transaction_type IN ('Sale', 'Return'))
                               ORDER BY id"""), {"s": store_id}).scalars().all()
    touched = []
    for i in ids:
        oid = apply_entry(conn, store_id, i)
        if oid:
            touched.append(oid)
    return touched


def is_cargo_invoice(e: dict) -> bool:
    return e["transaction_type"] == "DeductionInvoices" and any(h in (e.get("description") or "").lower() for h in CARGO_HINTS)


# ------------------------------------------------------------------ nakit özeti
NET_SQL = """CASE WHEN f.source = 'settlements'
                  THEN (f.credit - f.debt) + CASE WHEN f.credit >= f.debt THEN -COALESCE(f.commission_amount, 0)
                                                  ELSE COALESCE(f.commission_amount, 0) END
                  ELSE (f.credit - f.debt) END"""


def payout_summary(conn: Connection, today: date | None = None) -> dict:
    """Bekleyen hakediş / alacak / son ödemeler. Kaynak: Trendyol cari hesap ekstresi (veri yoksa connected=False)."""
    th = thresholds(conn)
    today = today or datetime.now(TZ).date()
    window_end = today + timedelta(days=int(th["payout_window_days"]))
    overdue_before = today - timedelta(days=14)
    base = """FROM marketplace_finance_entries f
               WHERE f.payment_order_id IS NULL
                 AND (f.source = 'settlements' OR (f.source = 'otherfinancials' AND f.transaction_type = 'DeductionInvoices'))"""
    r = row(conn, f"""
        SELECT COUNT(*) AS n,
               COALESCE(SUM({NET_SQL}) FILTER (WHERE f.payment_date IS NOT NULL AND (f.payment_date AT TIME ZONE 'Europe/Istanbul')::date <= :we
                                                AND (f.payment_date AT TIME ZONE 'Europe/Istanbul')::date >= :od), 0) AS pending,
               COALESCE(SUM({NET_SQL}) FILTER (WHERE f.payment_date IS NULL OR (f.payment_date AT TIME ZONE 'Europe/Istanbul')::date > :we), 0) AS receivable,
               COUNT(*) FILTER (WHERE f.payment_date IS NOT NULL AND (f.payment_date AT TIME ZONE 'Europe/Istanbul')::date < :od) AS overdue_unverified,
               MIN(f.payment_date) FILTER (WHERE f.payment_date IS NOT NULL AND (f.payment_date AT TIME ZONE 'Europe/Istanbul')::date >= :td) AS next_payment_date
          {base}""", we=window_end, od=overdue_before, td=today)
    total = row(conn, "SELECT COUNT(*) AS n, MAX(fetched_at) AS last_fetch FROM marketplace_finance_entries")
    payments = rows(conn, """SELECT payment_order_id, transaction_date, payment_date, ABS(debt - credit) AS amount
                               FROM marketplace_finance_entries WHERE transaction_type = 'PaymentOrder'
                              ORDER BY COALESCE(payment_date, transaction_date) DESC NULLS LAST LIMIT 5""")
    recon = None
    if payments and payments[0]["payment_order_id"]:
        pid = payments[0]["payment_order_id"]
        s = row(conn, f"""SELECT COALESCE(SUM({NET_SQL}), 0) AS net, COUNT(*) AS n FROM marketplace_finance_entries f
                           WHERE f.payment_order_id = :p AND f.transaction_type <> 'PaymentOrder'
                             AND (f.source = 'settlements' OR f.transaction_type = 'DeductionInvoices')""", p=pid)
        recon = {"payment_order_id": pid, "paid": d(payments[0]["amount"]), "ledger_net": d(s["net"]), "entries": int(s["n"]),
                 "difference": d(payments[0]["amount"]) - d(s["net"])}
    return {"connected": bool(total["n"]), "entries": int(total["n"] or 0), "last_fetch": total["last_fetch"],
            "pending_payout": d(r["pending"]), "receivable": d(r["receivable"]), "next_payment_date": r["next_payment_date"],
            "overdue_unverified": int(r["overdue_unverified"] or 0), "recent_payments": payments, "reconciliation": recon,
            "window_days": int(th["payout_window_days"]),
            "source": "Trendyol Finans (cari hesap ekstresi)"}


def seller_revenue_check(conn: Connection) -> dict:
    """İşaret kuralının canlı veriyle sağlaması: |net| ile |sellerRevenue| arasındaki fark (kuruş toleranslı)."""
    r = row(conn, f"""SELECT COUNT(*) FILTER (WHERE f.seller_revenue IS NOT NULL) AS n,
                             COUNT(*) FILTER (WHERE f.seller_revenue IS NOT NULL AND ABS(ABS({NET_SQL}) - ABS(f.seller_revenue)) > 0.01) AS mismatch
                        FROM marketplace_finance_entries f WHERE f.source = 'settlements' AND f.transaction_type IN ('Sale', 'Return')""")
    return {"checked": int(r["n"] or 0), "mismatch": int(r["mismatch"] or 0)}


def profit_provenance(conn: Connection, start, end) -> dict:
    """Dönem kârının hangi kısmı GERÇEK pazaryeri verisine, hangi kısmı TAHMİNE dayanıyor."""
    r = row(conn, """
        SELECT COUNT(*) AS items,
               COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM financial_transactions t WHERE t.order_item_id = i.id
                                              AND t.kind = 'commission' AND t.source = :src)) AS actual_commission_items,
               COALESCE(SUM(i.commission) FILTER (WHERE EXISTS (SELECT 1 FROM financial_transactions t WHERE t.order_item_id = i.id
                                              AND t.kind = 'commission' AND t.source = :src)), 0) AS actual_commission,
               COALESCE(SUM(i.commission) FILTER (WHERE NOT EXISTS (SELECT 1 FROM financial_transactions t WHERE t.order_item_id = i.id
                                              AND t.kind = 'commission' AND t.source = :src)), 0) AS estimated_commission,
               COUNT(DISTINCT o.id) AS orders,
               COUNT(DISTINCT o.id) FILTER (WHERE EXISTS (SELECT 1 FROM financial_transactions t WHERE t.order_id = o.id
                                              AND t.kind = 'shipping' AND t.source = :src)) AS actual_shipping_orders,
               COUNT(DISTINCT o.id) FILTER (WHERE NOT o.finance_is_estimate) AS fully_actual_orders
          FROM order_items i JOIN orders o ON o.id = i.order_id
         WHERE o.order_date >= :start AND o.order_date < :end AND o.internal_status <> 'cancelled'""",
            src=SOURCE, start=start, end=end)
    items = int(r["items"] or 0)
    return {"items": items, "actual_commission_items": int(r["actual_commission_items"] or 0),
            "estimated_commission_items": items - int(r["actual_commission_items"] or 0),
            "actual_commission": d(r["actual_commission"]), "estimated_commission": d(r["estimated_commission"]),
            "orders": int(r["orders"] or 0), "actual_shipping_orders": int(r["actual_shipping_orders"] or 0),
            "fully_actual_orders": int(r["fully_actual_orders"] or 0),
            # Veri yok ≠ tahmin: dönemde kalem yoksa NO_DATA
            "status": "NO_DATA" if not items else ("ACTUAL" if int(r["fully_actual_orders"] or 0) == int(r["orders"] or 0)
                       else "PARTIAL" if int(r["actual_commission_items"] or 0) else "ESTIMATED")}
