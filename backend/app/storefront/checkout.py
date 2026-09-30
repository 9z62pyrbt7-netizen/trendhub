"""Web siparişi: doğrulama, stok kilidi, ödeme yöntemine göre akış ve TrendHub siparişi oluşturma.

TrendHub'daki `orders` satırı (kanal: marketplaces.code = 'storefront', orders.source = 'storefront')
yalnızca GERÇEK sipariş için oluşur:
  * Kapıda ödeme: sipariş verildiği anda (iç statü 'new').
  * Havale/EFT: panelde "Ödeme alındı" onayıyla. O zamana kadar stok süreli ayrılır
    (`stock_reservations`, süre = storefront.bank_transfer_days); süre dolarsa sipariş 'expired' olur.
  * Kart: ödeme sağlayıcısı (PayTR / iyzico) geri dönüşü sunucuda doğrulandığında.
Böylece ödenmemiş siparişler ciro/kâr raporlarına girmez ama stok diğer kanallara karşı korunur.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..services import einvoice, order_notifications, stock_availability
from ..services.finance_service import recalculate_order
from ..services.orders_sync import ensure_store
from . import catalog, store_config

CHANNEL_CODE = "storefront"
CHANNEL_NAME = "Trendçantanız Web"
CARD_RESERVATION_MINUTES = 30

CITIES = ["Adana", "Adıyaman", "Afyonkarahisar", "Ağrı", "Aksaray", "Amasya", "Ankara", "Antalya", "Ardahan", "Artvin",
          "Aydın", "Balıkesir", "Bartın", "Batman", "Bayburt", "Bilecik", "Bingöl", "Bitlis", "Bolu", "Burdur", "Bursa",
          "Çanakkale", "Çankırı", "Çorum", "Denizli", "Diyarbakır", "Düzce", "Edirne", "Elazığ", "Erzincan", "Erzurum",
          "Eskişehir", "Gaziantep", "Giresun", "Gümüşhane", "Hakkari", "Hatay", "Iğdır", "Isparta", "İstanbul", "İzmir",
          "Kahramanmaraş", "Karabük", "Karaman", "Kars", "Kastamonu", "Kayseri", "Kilis", "Kırıkkale", "Kırklareli",
          "Kırşehir", "Kocaeli", "Konya", "Kütahya", "Malatya", "Manisa", "Mardin", "Mersin", "Muğla", "Muş", "Nevşehir",
          "Niğde", "Ordu", "Osmaniye", "Rize", "Sakarya", "Samsun", "Şanlıurfa", "Siirt", "Sinop", "Şırnak", "Sivas",
          "Tekirdağ", "Tokat", "Trabzon", "Tunceli", "Uşak", "Van", "Yalova", "Yozgat", "Zonguldak"]

STATUS_LABELS = {
    "pending_payment": "Ödeme bekleniyor",
    "awaiting_payment": "Havale/EFT ödemesi bekleniyor",
    "paid": "Ödeme alındı",
    "cash_on_delivery": "Kapıda ödeme",
    "payment_failed": "Ödeme başarısız",
    "cancelled": "İptal edildi",
    "expired": "Süresi doldu (ödeme alınmadı)",
}
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,}$")


def norm_space(v):
    return re.sub(r"\s+", " ", v).strip() if isinstance(v, str) else v


def valid_name(v: str) -> str:
    v = norm_space(v)
    if len(v.split(" ")) < 2:
        raise ValueError("Ad ve soyadınızı girin")
    return v


def valid_email(v: str) -> str:
    v = (v or "").strip()
    if not EMAIL_RE.match(v):
        raise ValueError("Geçerli bir e-posta adresi girin")
    return v.lower()


def valid_phone(v: str) -> str:
    d = re.sub(r"\D", "", v or "")
    if d.startswith("90") and len(d) == 12:
        d = d[2:]
    if d.startswith("0"):
        d = d[1:]
    if not re.match(r"^5\d{9}$", d):
        raise ValueError("Cep telefonunuzu 05xx xxx xx xx biçiminde girin")
    return f"0{d[:3]} {d[3:6]} {d[6:8]} {d[8:]}"


def valid_city(v: str) -> str:
    if v not in CITIES:
        raise ValueError("İl seçin")
    return v


def valid_zip(v):
    v = (v or "").strip()
    if v and not re.match(r"^\d{5}$", v):
        raise ValueError("Posta kodu 5 haneli olmalı")
    return v or None


class CheckoutError(ValueError):
    def __init__(self, message: str, fields: dict | None = None):
        super().__init__(message)
        self.fields = fields or {}


class CheckoutIn(BaseModel):
    full_name: str = Field(min_length=3, max_length=120)
    email: str = Field(max_length=254)
    phone: str = Field(max_length=20)
    city: str
    district: str = Field(min_length=2, max_length=80)
    address: str = Field(min_length=10, max_length=500)
    postal_code: str | None = Field(None, max_length=5)
    billing_type: str = "individual"
    company: str | None = Field(None, max_length=200)
    tax_office: str | None = Field(None, max_length=100)
    tax_number: str | None = Field(None, max_length=11)
    note: str | None = Field(None, max_length=500)
    payment_method: str
    accept_terms: bool = False
    accept_kvkk: bool = False

    @field_validator("full_name", "district", "address", "company", "tax_office", "note")
    @classmethod
    def _strip(cls, v):
        return norm_space(v)

    @field_validator("full_name")
    @classmethod
    def _name(cls, v):
        return valid_name(v)

    @field_validator("email")
    @classmethod
    def _email(cls, v):
        return valid_email(v)

    @field_validator("phone")
    @classmethod
    def _phone(cls, v):
        return valid_phone(v)

    @field_validator("city")
    @classmethod
    def _city(cls, v):
        return valid_city(v)

    @field_validator("postal_code")
    @classmethod
    def _zip(cls, v):
        return valid_zip(v)

    @model_validator(mode="after")
    def _billing(self):
        if self.billing_type not in ("individual", "corporate"):
            raise ValueError("Fatura türü geçersiz")
        if self.billing_type == "corporate":
            if not (self.company and self.tax_office and self.tax_number and re.match(r"^\d{10,11}$", self.tax_number)):
                raise ValueError("Kurumsal fatura için firma unvanı, vergi dairesi ve 10 haneli vergi numarası gerekli")
        return self


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _new_code(conn: Connection) -> str:
    alphabet = "ABCDEFGHJKLMNPRSTUVYZ23456789"
    today = datetime.now(timezone.utc).strftime("%y%m%d")
    for _ in range(20):
        code = f"TC{today}" + "".join(secrets.choice(alphabet) for _ in range(5))
        if not conn.execute(text("SELECT 1 FROM storefront_orders WHERE public_code = :c"), {"c": code}).first():
            return code
    raise RuntimeError("Sipariş numarası üretilemedi")


def place_order(conn: Connection, cart_id: int | None, data: CheckoutIn, ip: str | None,
                customer_id: int | None = None) -> dict:
    cfg = store_config.load(conn)
    blockers = cfg.checkout_blockers()
    if blockers:
        raise CheckoutError("Web mağazası şu anda sipariş kabul etmiyor. " + " ".join(blockers))
    methods = {m["code"] for m in cfg.payment_methods()}
    if data.payment_method not in methods:
        raise CheckoutError("Seçilen ödeme yöntemi kullanılamıyor.", {"payment_method": "Ödeme yöntemi seçin"})
    missing = {}
    if not data.accept_terms:
        missing["accept_terms"] = "Ön bilgilendirme formu ve mesafeli satış sözleşmesini onaylayın"
    if not data.accept_kvkk:
        missing["accept_kvkk"] = "KVKK aydınlatma metnini okuduğunuzu onaylayın"
    if missing:
        raise CheckoutError("Devam etmek için onay gerekli.", missing)
    if not cart_id:
        raise CheckoutError("Sepetiniz boş.")

    items = conn.execute(text("SELECT product_id, quantity FROM storefront_cart_items WHERE cart_id = :c ORDER BY added_at"),
                         {"c": cart_id}).all()
    if not items:
        raise CheckoutError("Sepetiniz boş.")
    ids = [i.product_id for i in items]
    # Ürün satırları kilitlenir: aynı ürün için eşzamanlı web siparişleri sırayla işlenir.
    available = stock_availability.available_map(conn, ids, lock=True)
    variants = catalog.variants_by_ids(conn, ids)
    problems = []
    lines = []
    for it in items:
        v = variants.get(it.product_id)
        if v is None:
            problems.append(f"Bir ürün artık satışta değil (#{it.product_id}).")
            continue
        avail = available.get(v.id, 0)
        if it.quantity > avail:
            problems.append(f"{v.title}: stokta {avail} adet var." if avail else f"{v.title}: tükendi.")
            continue
        lines.append({"product_id": v.id, "sku": v.sku, "barcode": v.barcode, "title": v.title, "color": v.color,
                      "size": v.size, "quantity": int(it.quantity), "unit_price": str(v.price),
                      "line_total": str(v.price * it.quantity), "image": v.images[0] if v.images else None})
    if problems:
        raise CheckoutError("Sepetinizdeki bazı ürünlerin stoğu değişti. " + " ".join(problems))

    items_total = sum((Decimal(line["line_total"]) for line in lines), Decimal("0"))
    shipping = cfg.shipping_for(items_total)
    if data.payment_method == "cash_on_delivery":
        shipping += cfg.cash_on_delivery_fee
    total = items_total + shipping

    code = _new_code(conn)
    token = secrets.token_urlsafe(24)
    status = {"bank_transfer": "awaiting_payment", "cash_on_delivery": "cash_on_delivery", "card": "pending_payment"}[data.payment_method]
    billing = {"type": data.billing_type}
    if data.billing_type == "corporate":
        billing.update({"company": data.company, "tax_office": data.tax_office, "tax_number": data.tax_number})
    now = datetime.now(timezone.utc).isoformat()
    sfo_id = conn.execute(text("""
        INSERT INTO storefront_orders(public_code, access_token_hash, status, payment_method, full_name, email, phone,
                                      city, district, address, postal_code, billing, customer_note, lines, items_total,
                                      shipping_fee, total, consents, ip, customer_id)
        VALUES (:code, :th, :status, :pm, :name, :email, :phone, :city, :district, :address, :zip, CAST(:billing AS JSONB),
                :note, CAST(:lines AS JSONB), :items_total, :shipping, :total, CAST(:consents AS JSONB), :ip, :cust)
        RETURNING id"""), {"cust": customer_id,
        "code": code, "th": _hash(token), "status": status, "pm": data.payment_method, "name": data.full_name,
        "email": data.email, "phone": data.phone, "city": data.city, "district": data.district, "address": data.address,
        "zip": data.postal_code, "billing": json.dumps(billing, ensure_ascii=False), "note": data.note,
        "lines": json.dumps(lines, ensure_ascii=False), "items_total": items_total, "shipping": shipping, "total": total,
        "consents": json.dumps({"terms": now, "kvkk": now}), "ip": ip}).scalar()

    if data.payment_method == "cash_on_delivery":
        create_trendhub_order(conn, sfo_id, raw_status="CashOnDelivery")
    else:
        minutes = CARD_RESERVATION_MINUTES if data.payment_method == "card" else cfg.bank_transfer_days * 24 * 60
        for line in lines:
            conn.execute(text("""INSERT INTO stock_reservations(product_id, storefront_order_id, quantity, expires_at)
                                 VALUES (:p, :o, :q, NOW() + make_interval(mins => :m))"""),
                         {"p": line["product_id"], "o": sfo_id, "q": line["quantity"], "m": minutes})
    conn.execute(text("DELETE FROM storefront_cart_items WHERE cart_id = :c"), {"c": cart_id})
    catalog.invalidate()
    return {"id": sfo_id, "public_code": code, "access_token": token, "status": status, "total": total}


def create_trendhub_order(conn: Connection, sfo_id: int, *, raw_status: str) -> int:
    """Web siparişini TrendHub `orders`/`order_items` tablolarına ayrı satış kanalı olarak yazar (idempotent)."""
    sfo = conn.execute(text("SELECT * FROM storefront_orders WHERE id = :id FOR UPDATE"), {"id": sfo_id}).mappings().first()
    if sfo["order_id"]:
        return sfo["order_id"]
    store_id = ensure_store(conn, CHANNEL_CODE, "web", CHANNEL_NAME)
    order_id = conn.execute(text("""
        INSERT INTO orders(store_id, external_order_id, status, internal_status, gross_revenue, order_date,
                           customer_name, customer_city, currency, source, finance_is_estimate, created_at, updated_at)
        VALUES (:s, :code, :raw, 'new', :rev, NOW(), :name, :city, 'TRY', 'storefront', TRUE, NOW(), NOW())
        ON CONFLICT (store_id, external_order_id) DO NOTHING RETURNING id"""),
        {"s": store_id, "code": sfo["public_code"], "raw": raw_status, "rev": sfo["items_total"],
         "name": sfo["full_name"], "city": sfo["city"]}).scalar()
    if order_id is None:
        order_id = conn.execute(text("SELECT id FROM orders WHERE store_id = :s AND external_order_id = :c"),
                                {"s": store_id, "c": sfo["public_code"]}).scalar()
    for n, line in enumerate(sfo["lines"], start=1):
        conn.execute(text("""
            INSERT INTO order_items(order_id, product_id, external_line_id, sku, barcode, product_name, quantity,
                                    unit_price, vat_rate, line_status, created_at)
            VALUES (:o, :p, :ext, :sku, :barcode, :name, :q, :price,
                    (SELECT vat_rate FROM products WHERE id = :p), :raw, NOW())
            ON CONFLICT (order_id, external_line_id) WHERE external_line_id IS NOT NULL DO NOTHING"""),
            {"o": order_id, "p": line["product_id"], "ext": f"{sfo['public_code']}-{n}", "sku": line.get("sku"),
             "barcode": line.get("barcode"), "q": line["quantity"], "price": Decimal(line["unit_price"]), "raw": raw_status,
             "name": " · ".join(x for x in (line["title"], line.get("color"), line.get("size")) if x)})
    conn.execute(text("""INSERT INTO order_status_history(order_id, from_status, to_status, marketplace_status, source, note)
                         VALUES (:o, NULL, 'new', :raw, 'storefront', :note)"""),
                 {"o": order_id, "raw": raw_status, "note": f"Web siparişi {sfo['public_code']} ({STATUS_LABELS.get(sfo['status'], sfo['status'])})"})
    # Web kanalında pazaryeri hizmet bedeli yoktur: tahmini hizmet bedeli yerine gerçek 0 kaydı.
    conn.execute(text("""INSERT INTO financial_transactions(store_id, order_id, source, external_ref, kind, amount, occurred_at, description)
                         VALUES (:s, :o, 'storefront', :ref, 'service_fee', 0, NOW(), 'Web kanalı: pazaryeri hizmet bedeli yok')
                         ON CONFLICT DO NOTHING"""),
                 {"s": store_id, "o": order_id, "ref": f"{sfo['public_code']}:service_fee"})
    conn.execute(text("UPDATE storefront_orders SET order_id = :o, updated_at = NOW() WHERE id = :id"), {"o": order_id, "id": sfo_id})
    # Ayrılan stok, sipariş satırı oluştuğu anda serbest bırakılır (artık sipariş olarak düşülür).
    conn.execute(text("UPDATE stock_reservations SET released_at = NOW() WHERE storefront_order_id = :id AND released_at IS NULL"),
                 {"id": sfo_id})
    recalculate_order(conn, order_id)
    einvoice.queue(conn, order_id)  # sağlayıcı/ayar kapalıysa hiçbir şey yapmaz
    return order_id


def confirm_payment(conn: Connection, sfo_id: int, *, reference: str | None = None, provider: str | None = None,
                    allow_expired: bool = False) -> int:
    """Ödemeyi onaylar. `allow_expired`: sağlayıcının doğruladığı ödeme, ayırma süresi dolduktan sonra geldiyse
    para tahsil edilmiştir; sipariş yine oluşturulur (panelde stok/iade kararı verilir)."""
    sfo = conn.execute(text("SELECT id, status FROM storefront_orders WHERE id = :id FOR UPDATE"), {"id": sfo_id}).mappings().first()
    if sfo is None:
        raise CheckoutError("Web siparişi bulunamadı")
    allowed = ("awaiting_payment", "pending_payment") + (("expired", "payment_failed") if allow_expired else ())
    if sfo["status"] not in allowed:
        raise CheckoutError(f"Bu siparişin ödemesi onaylanamaz (durum: {STATUS_LABELS.get(sfo['status'], sfo['status'])}).")
    conn.execute(text("""UPDATE storefront_orders SET status = 'paid', paid_at = NOW(), payment_reference = COALESCE(:ref, payment_reference),
                         payment_provider = COALESCE(:prov, payment_provider), updated_at = NOW() WHERE id = :id"""),
                 {"id": sfo_id, "ref": reference, "prov": provider})
    order_id = create_trendhub_order(conn, sfo_id, raw_status="Paid")
    # Kartla ödemede "sipariş alındı" ödeme doğrulanınca gönderilir; havalede ödeme onayı ayrı bildirimdir.
    order_notifications.notify_order(conn, sfo_id, "payment_confirmed" if sfo["status"] == "awaiting_payment" else "order_received")
    catalog.invalidate()
    return order_id


def cancel_unpaid(conn: Connection, sfo_id: int, *, status: str = "cancelled") -> None:
    sfo = conn.execute(text("SELECT id, status FROM storefront_orders WHERE id = :id FOR UPDATE"), {"id": sfo_id}).mappings().first()
    if sfo is None:
        raise CheckoutError("Web siparişi bulunamadı")
    if sfo["status"] not in ("awaiting_payment", "pending_payment"):
        raise CheckoutError("Yalnızca ödemesi beklenen web siparişleri buradan iptal edilir; diğerleri Siparişler ekranından yönetilir.")
    conn.execute(text("UPDATE storefront_orders SET status = :s, cancelled_at = NOW(), updated_at = NOW() WHERE id = :id"),
                 {"s": status, "id": sfo_id})
    conn.execute(text("UPDATE stock_reservations SET released_at = NOW() WHERE storefront_order_id = :id AND released_at IS NULL"),
                 {"id": sfo_id})
    if sfo["status"] == "awaiting_payment":
        # Havale/EFT siparişine "alındı" bildirimi gitmişti; iptal de bildirilir. Tamamlanmamış kart denemesine gönderilmez.
        order_notifications.notify_order(conn, sfo_id, "cancelled")
    catalog.invalidate()


def expire_stale(conn: Connection) -> dict:
    """Süresi dolan ödeme beklemelerini kapatır, stok ayırmalarını bırakır, eski sepetleri siler (worker)."""
    expired = [r.id for r in conn.execute(text("""
        SELECT DISTINCT o.id FROM storefront_orders o JOIN stock_reservations r ON r.storefront_order_id = o.id
         WHERE o.status IN ('awaiting_payment', 'pending_payment') AND r.released_at IS NULL AND r.expires_at <= NOW()"""))]
    for sfo_id in expired:
        cancel_unpaid(conn, sfo_id, status="expired")
    carts = conn.execute(text("DELETE FROM storefront_carts WHERE expires_at < NOW()")).rowcount
    return {"expired_orders": len(expired), "deleted_carts": carts}


def find_public(conn: Connection, code: str, token: str | None) -> dict | None:
    if not token or not re.match(r"^TC\d{6}[A-Z0-9]{5}$", code or ""):
        return None
    sfo = conn.execute(text("""
        SELECT so.*, o.internal_status FROM storefront_orders so LEFT JOIN orders o ON o.id = so.order_id
         WHERE so.public_code = :c"""), {"c": code}).mappings().first()
    if sfo is None or not secrets.compare_digest(sfo["access_token_hash"], _hash(token)):
        return None
    return dict(sfo)
