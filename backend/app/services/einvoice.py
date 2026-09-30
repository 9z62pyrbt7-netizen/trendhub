"""E-fatura / e-arşiv fatura entegrasyon katmanı (Trendçantanız web siparişleri).

KAPALI-VARSAYILAN: Repoda uygulanmış bir e-fatura sağlayıcısı (entegratör) YOKTUR. Aşağıdakilerin tümü
sağlanmadıkça hiçbir kayıt oluşturulmaz ve hiçbir dış servise istek yapılmaz:
  1. `EInvoiceProvider` alt sınıfı yazılıp `PROVIDERS`'a eklenmesi (ör. entegratörün REST/SOAP API'si),
  2. sunucu .env'inde `EINVOICE_PROVIDER=<kod>` ve sağlayıcının kimlik bilgileri,
  3. panelde Web Sitesi → Ayarlar → "E-fatura" anahtarının açılması.

Etkinleştirildiğinde web kanalında oluşan her TrendHub siparişi için `einvoice_records` satırı ('queued')
açılır; bakım işi sağlayıcıya iletir ve belge numarasını kaydeder. Pazaryeri siparişleri (Trendyol vb.)
KAPSAM DIŞIDIR; onların faturaları mevcut süreçlerle kesilmeye devam eder.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import get_settings
from . import app_settings

log = logging.getLogger("trendhub.einvoice")
MAX_ATTEMPTS = 5


@dataclass
class InvoiceResult:
    external_id: str
    document_number: str | None = None
    document_type: str | None = None   # "e-fatura" | "e-arsiv"


class EInvoiceProvider:
    code = ""
    name = ""

    def is_configured(self) -> bool:
        raise NotImplementedError

    def issue(self, invoice: dict) -> InvoiceResult:
        """`invoice`: build_invoice() çıktısı. Belgeyi oluşturur; hata durumunda istisna fırlatır."""
        raise NotImplementedError


PROVIDERS: dict[str, type[EInvoiceProvider]] = {}


def provider() -> EInvoiceProvider | None:
    cls = PROVIDERS.get(get_settings().einvoice_provider.strip().lower())
    if cls is None:
        return None
    p = cls()
    return p if p.is_configured() else None


def enabled(conn: Connection) -> bool:
    return provider() is not None and bool(app_settings.get(conn, "storefront.einvoice_enabled", False))


def status(conn: Connection) -> dict:
    p = provider()
    return {"provider": p.name if p else None, "provider_configured": p is not None,
            "setting_enabled": bool(app_settings.get(conn, "storefront.einvoice_enabled", False)),
            "active": enabled(conn)}


def build_invoice(conn: Connection, order_id: int) -> dict:
    """Sağlayıcıdan bağımsız fatura verisi (web siparişi + fatura bilgileri + KDV'li satırlar)."""
    o = conn.execute(text("""
        SELECT o.id, o.external_order_id, o.order_date, so.full_name, so.email, so.phone, so.city, so.district,
               so.address, so.postal_code, so.billing, so.shipping_fee, so.total
          FROM orders o JOIN storefront_orders so ON so.order_id = o.id WHERE o.id = :id"""), {"id": order_id}).mappings().first()
    if o is None:
        raise ValueError("Yalnızca web siparişleri için e-fatura hazırlanır")
    lines = [dict(r) for r in conn.execute(text("""
        SELECT product_name AS name, sku, quantity, unit_price, COALESCE(vat_rate, 20) AS vat_rate
          FROM order_items WHERE order_id = :id ORDER BY id"""), {"id": order_id}).mappings()]
    return {"order_code": o["external_order_id"], "date": o["order_date"], "buyer": {
        "name": o["full_name"], "email": o["email"], "phone": o["phone"], "city": o["city"], "district": o["district"],
        "address": o["address"], "postal_code": o["postal_code"], **(o["billing"] or {})},
        "lines": lines, "shipping_fee": o["shipping_fee"], "total_incl_vat": o["total"], "currency": "TRY"}


def queue(conn: Connection, order_id: int) -> bool:
    if not enabled(conn):
        return False
    return bool(conn.execute(text("""
        INSERT INTO einvoice_records(order_id, provider, status) VALUES (:o, :p, 'queued')
        ON CONFLICT (order_id) DO NOTHING RETURNING id"""), {"o": order_id, "p": provider().code}).scalar())


def process(conn: Connection, limit: int = 20) -> dict:
    """Bakım işi: kuyruktaki faturaları sağlayıcıya iletir. Sağlayıcı yoksa hiçbir şey yapmaz."""
    p = provider()
    if p is None or not enabled(conn):
        return {"issued": 0, "failed": 0}
    issued = failed = 0
    for r in conn.execute(text("""SELECT id, order_id, attempts FROM einvoice_records
                                    WHERE status IN ('queued', 'failed') AND attempts < :m ORDER BY id LIMIT :l
                                    FOR UPDATE SKIP LOCKED"""), {"m": MAX_ATTEMPTS, "l": limit}).mappings().all():
        try:
            res = p.issue(build_invoice(conn, r["order_id"]))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            conn.execute(text("""UPDATE einvoice_records SET status = 'failed', attempts = attempts + 1, last_error = :e,
                                 updated_at = NOW() WHERE id = :id"""), {"e": f"{exc.__class__.__name__}: {str(exc)[:300]}", "id": r["id"]})
            continue
        issued += 1
        conn.execute(text("""UPDATE einvoice_records SET status = 'issued', attempts = attempts + 1, external_id = :x,
                             document_number = :n, document_type = :t, last_error = NULL, issued_at = NOW(), updated_at = NOW()
                             WHERE id = :id"""), {"x": res.external_id, "n": res.document_number, "t": res.document_type, "id": r["id"]})
    return {"issued": issued, "failed": failed}
