"""Web siparişlerinin tedarikçiye aktarımı (yalnızca Trendçantanız web kanalı).

KAPSAM: Yalnızca `orders.source = 'storefront'` siparişleri. Trendyol ve diğer pazaryeri siparişlerine,
mevcut Trendyol → Çanta Bayim otomasyonuna ve /opt/trendcantamiz-xml sistemine DOKUNULMAZ; bu modül o
siparişler için hiçbir kayıt oluşturmaz ve hiçbir dış servise istek yapmaz.

Akış (panel ayarı `storefront.supplier_forwarding_mode`):
  off     Hiçbir şey yapılmaz.
  manual  Ödemesi alınmış / kapıda ödemeli web siparişi için tedarikçi siparişi TASLAĞI hazırlanır
          (supplier_orders, channel='storefront', status='draft'). Operatör panelde onaylar:
          bağlantı (connector) varsa API ile gönderilir, yoksa "manuel iletildi" olarak işaretlenir.
  auto    Taslak hazırlanır ve tedarikçinin sipariş bağlantısı tanımlıysa bakım işinde otomatik gönderilir;
          bağlantı yoksa taslak panelde bekler (manuel iletim).

Tedarikçi, ürünün tercih edilen tedarikçisinden (products.preferred_supplier_id) veya birincil
supplier_products kaydından belirlenir. Birden fazla tedarikçiye dağılan sipariş için her tedarikçiye ayrı
taslak açılır. Tedarikçi bulunamayan kalemler taslakta "tedarikçi yok" olarak raporlanır.

Tedarikçi sipariş API'si bağlantısı (ör. Çanta Bayim'in web siparişleri için sunacağı bir uç nokta) henüz
yoktur: `SupplierOrderConnector` alt sınıfı yazılıp `CONNECTORS`'a eklenene kadar gönderim manueldir.
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from . import app_settings

log = logging.getLogger("trendhub.supplier_forwarding")
CHANNEL = "storefront"
MODES = ("off", "manual", "auto")
STATUS_LABELS = {"draft": "Onay bekliyor", "sent": "Tedarikçiye gönderildi", "manual_sent": "Manuel iletildi",
                 "failed": "Gönderilemedi", "cancelled": "İptal"}


class ForwardingError(ValueError):
    pass


class SupplierOrderConnector:
    """Tedarikçiye sipariş gönderen bağlantı. Kimlik bilgileri yalnızca sunucu ortam değişkenlerinden okunur."""
    supplier_code = ""
    name = ""

    def is_configured(self) -> bool:
        raise NotImplementedError

    def submit(self, payload: dict) -> str:
        """Siparişi gönderir, tedarikçinin sipariş numarasını döndürür. Hata durumunda istisna fırlatır."""
        raise NotImplementedError


# supplier code -> bağlantı sınıfı. Şu an uygulanmış tedarikçi sipariş API'si yoktur.
CONNECTORS: dict[str, type[SupplierOrderConnector]] = {}


def connector_for(code: str | None) -> SupplierOrderConnector | None:
    cls = CONNECTORS.get(code or "")
    if cls is None:
        return None
    c = cls()
    return c if c.is_configured() else None


def mode(conn: Connection) -> str:
    m = app_settings.get(conn, "storefront.supplier_forwarding_mode", "manual")
    return m if m in MODES else "manual"


def _storefront_order(conn: Connection, order_id: int) -> dict:
    o = conn.execute(text("""
        SELECT o.id, o.external_order_id, o.internal_status, so.full_name, so.phone, so.city, so.district, so.address,
               so.postal_code, so.customer_note, so.status AS web_status, so.payment_method
          FROM orders o JOIN storefront_orders so ON so.order_id = o.id
         WHERE o.id = :id AND o.source = 'storefront'"""), {"id": order_id}).mappings().first()
    if o is None:
        raise ForwardingError("Yalnızca Trendçantanız web siparişleri tedarikçiye buradan aktarılabilir.")
    return dict(o)


def prepare(conn: Connection, order_id: int) -> list[int]:
    """Web siparişi için tedarikçi başına taslak oluşturur (idempotent). Oluşan/var olan kayıt id'lerini döndürür."""
    o = _storefront_order(conn, order_id)
    if o["internal_status"] in ("cancelled", "returned"):
        raise ForwardingError("İptal/iade edilmiş sipariş tedarikçiye aktarılmaz.")
    if o["web_status"] not in ("paid", "cash_on_delivery"):
        raise ForwardingError("Ödemesi onaylanmamış web siparişi tedarikçiye aktarılmaz.")
    items = [dict(r) for r in conn.execute(text("""
        SELECT i.id, i.product_id, i.sku, i.barcode, i.product_name, i.quantity,
               COALESCE(p.preferred_supplier_id, sp.supplier_id) AS supplier_id,
               sp.supplier_sku, COALESCE(sp.cost, NULLIF(i.unit_cost, 0)) AS cost
          FROM order_items i
          LEFT JOIN products p ON p.id = i.product_id
          LEFT JOIN LATERAL (SELECT x.supplier_id, x.supplier_sku, x.cost FROM supplier_products x
                              WHERE x.product_id = i.product_id
                              ORDER BY (x.supplier_id = p.preferred_supplier_id) DESC NULLS LAST, x.is_primary DESC, x.id
                              LIMIT 1) sp ON TRUE
         WHERE i.order_id = :o ORDER BY i.id"""), {"o": order_id}).mappings()]
    by_supplier: dict[int | None, list[dict]] = {}
    for it in items:
        by_supplier.setdefault(it["supplier_id"], []).append(it)
    ids = []
    ship_to = {k: o[k] for k in ("full_name", "phone", "city", "district", "address", "postal_code")}
    for supplier_id, lines in by_supplier.items():
        if supplier_id is None:
            continue
        payload = {
            "order_code": o["external_order_id"], "channel": CHANNEL, "ship_to": ship_to, "note": o["customer_note"],
            "lines": [{"sku": ln["supplier_sku"] or ln["sku"], "barcode": ln["barcode"], "name": ln["product_name"],
                       "quantity": ln["quantity"]} for ln in lines],
        }
        cost = sum((Decimal(str(ln["cost"] or 0)) * ln["quantity"] for ln in lines), Decimal("0"))
        key = f"{CHANNEL}:{order_id}:{supplier_id}"
        new_id = conn.execute(text("""
            INSERT INTO supplier_orders(order_id, supplier_id, supplier, status, cost, idempotency_key, channel, payload,
                                        method, attempts, created_at, updated_at)
            VALUES (:o, :s, (SELECT code FROM suppliers WHERE id = :s), 'draft', :cost, :k, :ch, CAST(:p AS JSONB),
                    'manual', 0, NOW(), NOW())
            ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING RETURNING id"""),
            {"o": order_id, "s": supplier_id, "cost": cost, "k": key, "ch": CHANNEL,
             "p": json.dumps(payload, ensure_ascii=False)}).scalar()
        if new_id is None:
            new_id = conn.execute(text("SELECT id FROM supplier_orders WHERE idempotency_key = :k"), {"k": key}).scalar()
        ids.append(new_id)
    return ids


def _get(conn: Connection, so_id: int) -> dict:
    so = conn.execute(text("""SELECT so.*, s.code AS supplier_code FROM supplier_orders so
                              LEFT JOIN suppliers s ON s.id = so.supplier_id
                              WHERE so.id = :id AND so.channel = :ch FOR UPDATE OF so"""),
                      {"id": so_id, "ch": CHANNEL}).mappings().first()
    if so is None:
        raise ForwardingError("Web kanalı tedarikçi siparişi bulunamadı.")
    return dict(so)


def send(conn: Connection, so_id: int) -> dict:
    """Taslağı tedarikçinin sipariş bağlantısıyla gönderir. Bağlantı yoksa hata verir (manuel iletim gerekir)."""
    so = _get(conn, so_id)
    if so["status"] not in ("draft", "failed"):
        raise ForwardingError(f"Bu kayıt gönderilemez (durum: {STATUS_LABELS.get(so['status'], so['status'])}).")
    from .ai.config import emergency_stop
    if emergency_stop(conn):
        raise ForwardingError("Acil durdurma aktif: tedarikçiye gönderim yapılamaz.")
    connector = connector_for(so["supplier_code"])
    if connector is None:
        raise ForwardingError("Bu tedarikçi için sipariş aktarım bağlantısı tanımlı değil; siparişi tedarikçiye "
                              "manuel iletip 'Manuel iletildi' olarak işaretleyin.")
    try:
        external_id = connector.submit(so["payload"])
    except Exception as exc:  # noqa: BLE001
        conn.execute(text("""UPDATE supplier_orders SET status = 'failed', attempts = COALESCE(attempts, 0) + 1,
                             last_error = :e, updated_at = NOW() WHERE id = :id"""),
                     {"e": f"{exc.__class__.__name__}: {str(exc)[:300]}", "id": so_id})
        return {"ok": False, "error": str(exc)[:300]}
    conn.execute(text("""UPDATE supplier_orders SET status = 'sent', external_supplier_order_id = :x, method = :m,
                         sent_at = NOW(), attempts = COALESCE(attempts, 0) + 1, last_error = NULL, updated_at = NOW()
                         WHERE id = :id"""), {"x": external_id, "m": connector.name or so["supplier_code"], "id": so_id})
    return {"ok": True, "external_id": external_id}


def mark_manual(conn: Connection, so_id: int, external_id: str | None) -> None:
    so = _get(conn, so_id)
    if so["status"] not in ("draft", "failed"):
        raise ForwardingError("Bu kayıt zaten iletilmiş veya iptal edilmiş.")
    conn.execute(text("""UPDATE supplier_orders SET status = 'manual_sent', method = 'manual', sent_at = NOW(),
                         external_supplier_order_id = COALESCE(:x, external_supplier_order_id), last_error = NULL,
                         updated_at = NOW() WHERE id = :id"""), {"x": (external_id or "").strip() or None, "id": so_id})


def cancel(conn: Connection, so_id: int) -> None:
    so = _get(conn, so_id)
    if so["status"] not in ("draft", "failed"):
        raise ForwardingError("Gönderilmiş tedarikçi siparişi buradan iptal edilemez; tedarikçiyle iletişime geçin.")
    conn.execute(text("UPDATE supplier_orders SET status = 'cancelled', updated_at = NOW() WHERE id = :id"), {"id": so_id})


def run_auto(conn: Connection, limit: int = 50) -> dict:
    """Bakım işi: moda göre taslak hazırlar; 'auto' modunda bağlantısı olan tedarikçilere gönderir."""
    m = mode(conn)
    if m == "off":
        return {"mode": m, "prepared": 0, "sent": 0}
    pending = [r.order_id for r in conn.execute(text("""
        SELECT so.order_id FROM storefront_orders so JOIN orders o ON o.id = so.order_id
         WHERE so.status IN ('paid', 'cash_on_delivery') AND o.source = 'storefront'
           AND o.internal_status NOT IN ('cancelled', 'returned')
           AND so.created_at > NOW() - INTERVAL '14 days'
           AND NOT EXISTS (SELECT 1 FROM supplier_orders x WHERE x.order_id = o.id AND x.channel = :ch)
         ORDER BY so.id LIMIT :l"""), {"ch": CHANNEL, "l": limit})]
    prepared = sent = 0
    for order_id in pending:
        try:
            prepared += len(prepare(conn, order_id))
        except ForwardingError as exc:
            log.info("Web siparişi #%s tedarikçi taslağı hazırlanmadı: %s", order_id, exc)
    from .ai.config import emergency_stop
    if m == "auto" and not emergency_stop(conn):
        drafts = [r.id for r in conn.execute(text("""
            SELECT so.id FROM supplier_orders so JOIN suppliers s ON s.id = so.supplier_id
             WHERE so.channel = :ch AND so.status = 'draft' AND s.code = ANY(:codes) ORDER BY so.id LIMIT :l"""),
            {"ch": CHANNEL, "codes": list(CONNECTORS), "l": limit})] if CONNECTORS else []
        for so_id in drafts:
            if connector_for(conn.execute(text("SELECT s.code FROM supplier_orders so JOIN suppliers s ON s.id = so.supplier_id "
                                                "WHERE so.id = :id"), {"id": so_id}).scalar()) is None:
                continue
            sent += bool(send(conn, so_id).get("ok"))
    return {"mode": m, "prepared": prepared, "sent": sent}
