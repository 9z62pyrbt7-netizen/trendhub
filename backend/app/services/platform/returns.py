"""Trendyol iadeleri (getClaims) → sipariş / paket / kalem / ürün / sebep / statü / finansal etki.

Mevcut durum (V1): iade yalnızca sipariş paketinin statüsünden ('Returned') anlaşılıyordu; sebep, kısmi iade ve
kalem bilgisi yoktu. Artık her iade KALEMİ ayrı kaydedilir.

Kâr etkisi:
  * Kabul edilen (Accepted) iade kalemi için, Trendyol finans kaydı henüz yoksa, iade tutarı (satış satırı fiyatı)
    `financial_transactions` (source='trendyol_claims', kind='refund') olarak yazılır ve sipariş kârı yeniden hesaplanır.
  * Cari hesapta gerçek "Return" kaydı gelince o kayıt esas alınır ve claim kaynaklı satır silinir (çift sayım yok).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ...db import row
from ..finance_service import recalculate_order
from .pii import scrub

CLAIM_SOURCE = "trendyol_claims"
REFUND_STATUSES = {"accepted"}
STATUS_TR = {"created": "Oluşturuldu", "waitinginaction": "Satıcıda / aksiyon bekliyor", "accepted": "Kabul edildi",
             "unresolved": "İhtilaflı", "rejected": "Reddedildi", "cancelled": "İptal", "inanalysis": "Analizde"}


def _dt(v) -> datetime | None:
    if v in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(v) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError:
            return None


def _reason(obj) -> tuple[str | None, str | None]:
    if not isinstance(obj, dict):
        return None, None
    return (str(obj.get("code")) if obj.get("code") else None), obj.get("name")


def normalize_claims(claims: list[dict]) -> list[dict]:
    out = []
    for c in claims:
        claim_id = str(c.get("id") or c.get("claimId") or "")
        if not claim_id:
            continue
        for it in c.get("items") or []:
            line = it.get("orderLine") or {}
            for ci in it.get("claimItems") or []:
                cid = ci.get("id") or ci.get("claimItemId")
                if not cid:
                    continue
                rc, rn = _reason(ci.get("customerClaimItemReason"))
                tc, tn = _reason(ci.get("trendyolClaimItemReason"))
                st = ci.get("claimItemStatus")
                status = (st.get("name") if isinstance(st, dict) else st) or None
                price = line.get("price") if line.get("price") is not None else line.get("lineUnitPrice")
                out.append({
                    "claim_id": claim_id, "claim_item_id": str(cid), "order_number": str(c.get("orderNumber") or "") or None,
                    "shipment_package_id": str(c.get("orderShipmentPackageId")) if c.get("orderShipmentPackageId") else None,
                    "order_line_id": str(line.get("id") or line.get("lineId")) if (line.get("id") or line.get("lineId")) else None,
                    "barcode": line.get("barcode"), "sku": line.get("merchantSku") or line.get("stockCode"),
                    "product_name": line.get("productName"), "amount": Decimal(str(price)) if price is not None else None,
                    "reason_code": rc, "reason_name": rn, "trendyol_reason_code": tc, "trendyol_reason_name": tn,
                    "status": status, "accepted_by_seller": ci.get("acceptedBySeller"),
                    "customer_note": scrub(ci.get("customerNote") or ci.get("note")),
                    "claim_date": _dt(c.get("claimDate")), "last_modified_at": _dt(c.get("lastModifiedDate")),
                })
    return out


def _match(conn: Connection, store_id: int, r: dict) -> tuple[int | None, int | None, int | None]:
    oid = conn.execute(text("SELECT id FROM orders WHERE store_id = :s AND external_order_id = :n"),
                       {"s": store_id, "n": r["order_number"]}).scalar() if r["order_number"] else None
    iid = pid = None
    if oid:
        it = row(conn, """SELECT id, product_id FROM order_items WHERE order_id = :o
                            AND ((CAST(:lid AS TEXT) IS NOT NULL AND external_line_id = :lid) OR barcode = :b OR sku = :sku)
                          ORDER BY (external_line_id = :lid) DESC NULLS LAST, id LIMIT 1""",
                 o=oid, lid=r["order_line_id"], b=r["barcode"], sku=r["sku"])
        if it:
            iid, pid = it["id"], it["product_id"]
    if pid is None and (r["sku"] or r["barcode"]):
        pid = conn.execute(text("SELECT id FROM products WHERE sku = :s OR barcode = :b ORDER BY (sku = :s) DESC, id LIMIT 1"),
                           {"s": r["sku"], "b": r["barcode"]}).scalar()
    return oid, iid, pid


def upsert_return(conn: Connection, store_id: int, r: dict, *, apply: bool = True) -> dict:
    """Dönüş: {'event': RETURN_CREATED | RETURN_UPDATED | None, 'order_id', 'id'}.
    apply=False: kâr etkisi Event Router'da (RETURN_* olayı işlenirken) uygulanır."""
    oid, iid, pid = _match(conn, store_id, r)
    prev = row(conn, "SELECT id, status FROM marketplace_returns WHERE store_id = :s AND claim_item_id = :c",
               s=store_id, c=r["claim_item_id"])
    rid = conn.execute(text("""
        INSERT INTO marketplace_returns(store_id, claim_id, claim_item_id, order_number, order_id, order_item_id,
            shipment_package_id, order_line_id, barcode, sku, product_id, product_name, amount, reason_code, reason_name,
            trendyol_reason_code, trendyol_reason_name, status, accepted_by_seller, customer_note, claim_date, last_modified_at)
        VALUES (:store, :claim_id, :claim_item_id, :order_number, :oid, :iid, :shipment_package_id, :order_line_id, :barcode,
            :sku, :pid, :product_name, :amount, :reason_code, :reason_name, :trendyol_reason_code, :trendyol_reason_name,
            :status, :accepted_by_seller, :customer_note, :claim_date, :last_modified_at)
        ON CONFLICT (store_id, claim_item_id) DO UPDATE SET status = EXCLUDED.status,
            order_id = COALESCE(EXCLUDED.order_id, marketplace_returns.order_id),
            order_item_id = COALESCE(EXCLUDED.order_item_id, marketplace_returns.order_item_id),
            product_id = COALESCE(EXCLUDED.product_id, marketplace_returns.product_id),
            accepted_by_seller = EXCLUDED.accepted_by_seller, trendyol_reason_code = EXCLUDED.trendyol_reason_code,
            trendyol_reason_name = EXCLUDED.trendyol_reason_name, last_modified_at = EXCLUDED.last_modified_at,
            updated_at = NOW()
        RETURNING id"""), {"store": store_id, "oid": oid, "iid": iid, "pid": pid, **r}).scalar()
    event = "RETURN_CREATED" if prev is None else ("RETURN_UPDATED" if prev["status"] != r["status"] else None)
    if apply and event and iid and oid:
        apply_refund(conn, store_id, rid)
    return {"event": event, "order_id": oid, "id": rid}


def apply_refund(conn: Connection, store_id: int, return_id: int) -> bool:
    """Kabul edilen iade kalemini kâra yansıtır (gerçek finans kaydı yoksa). Kâr yeniden hesaplandıysa True."""
    r = row(conn, "SELECT * FROM marketplace_returns WHERE id = :i", i=return_id)
    if not r["order_item_id"] or not r["order_id"]:
        return False
    ref = f"claim:{r['claim_item_id']}:refund"
    accepted = (r["status"] or "").replace(" ", "").lower() in REFUND_STATUSES
    has_actual = conn.execute(text("""SELECT 1 FROM financial_transactions WHERE order_item_id = :i AND kind = 'refund'
                                        AND source = 'trendyol_finance' LIMIT 1"""), {"i": r["order_item_id"]}).first()
    if accepted and not has_actual and r["amount"] is not None:
        conn.execute(text("""
            INSERT INTO financial_transactions(store_id, order_id, order_item_id, source, external_ref, kind, amount, occurred_at, description)
            VALUES (:s, :o, :i, :src, :ref, 'refund', :a, COALESCE(:at, NOW()), :d)
            ON CONFLICT (source, external_ref) DO UPDATE SET amount = EXCLUDED.amount"""),
            {"s": store_id, "o": r["order_id"], "i": r["order_item_id"], "src": CLAIM_SOURCE, "ref": ref, "a": r["amount"],
             "at": r["last_modified_at"], "d": f"Trendyol iade (claim) — {r['reason_name'] or 'sebep yok'}"})
    elif not accepted:
        conn.execute(text("DELETE FROM financial_transactions WHERE source = :src AND external_ref = :ref"),
                     {"src": CLAIM_SOURCE, "ref": ref})
    recalculate_order(conn, r["order_id"])
    return True


def drop_claim_refund_for_item(conn: Connection, order_item_id: int) -> None:
    """Gerçek finans iade kaydı geldiğinde claim kaynaklı tahmini satır kaldırılır (çift sayım olmaz)."""
    conn.execute(text("DELETE FROM financial_transactions WHERE source = :src AND order_item_id = :i AND kind = 'refund'"),
                 {"src": CLAIM_SOURCE, "i": order_item_id})
