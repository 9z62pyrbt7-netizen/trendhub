"""Kartla ödeme soyutlaması: PayTR (iFrame API) ve iyzico (Ödeme Formu).

Başarılı ödeme taklidi yapılmaz. Kart seçeneği yalnızca `STOREFRONT_PAYMENT_PROVIDER` bir sağlayıcıyı
seçtiğinde VE o sağlayıcının anahtarlarının tamamı sunucu ortam değişkenlerinde tanımlıyken gösterilir.
Anahtarlar repoda/panelde/tarayıcıda tutulmaz.

Akış:
  1. Sipariş `pending_payment` + 30 dk stok ayırma ile oluşur (checkout.place_order).
  2. `/odeme/kart/<kod>` → `start()` sağlayıcıda ödeme oturumu açar (PayTR: iframe token, iyzico: ödeme sayfası).
  3. Sağlayıcı geri bildirimi `POST /odeme/geri-donus/<kod>` → `verify_callback()` imzayı / sorguyu SUNUCU
     tarafında doğrular; tutar ve sipariş kodu eşleşmezse ödeme onaylanmaz.
  4. Yalnızca doğrulanmış başarılı ödemede TrendHub siparişi (orders) oluşturulur.

PayTR: bildirim adresi (Bildirim URL) PayTR mağaza panelinde `https://<alan-adı>/odeme/geri-donus/paytr`
olarak tanımlanmalıdır. iyzico: geri dönüş adresi her istekte gönderilir.

Uyarı: İki entegrasyon da sağlayıcıların yayımlanmış API belgelerine göre yazılmış ve sahte HTTP yanıtlarıyla
test edilmiştir; canlıya almadan önce sağlayıcının TEST anahtarlarıyla uçtan uca denenmelidir.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

import httpx

from ..config import get_settings

log = logging.getLogger("trendhub.storefront.payments")
TIMEOUT = httpx.Timeout(20.0, connect=10.0)


def _post(url: str, **kwargs) -> httpx.Response:
    """Sağlayıcıya HTTP isteği (testlerde değiştirilir)."""
    with httpx.Client(timeout=TIMEOUT) as client:
        return client.post(url, **kwargs)


@dataclass
class PaymentStart:
    redirect_url: str | None = None      # sağlayıcının ödeme sayfası (iyzico)
    iframe_url: str | None = None        # sayfaya gömülecek ödeme çerçevesi (PayTR)
    html: str | None = None              # sağlayıcının 3D form HTML'i (varsa)
    reference: str | None = None         # sağlayıcı işlem/token kimliği


@dataclass
class PaymentResult:
    verified: bool                        # imza/sorgu ile SUNUCU tarafında doğrulandı mı
    paid: bool
    reference: str | None = None
    amount: Decimal | None = None
    message: str = ""
    details: dict = field(default_factory=dict)


class PaymentNotConfigured(RuntimeError):
    pass


class CardPaymentProvider:
    code = ""
    name = ""

    def is_configured(self) -> bool:
        raise NotImplementedError

    # Sağlayıcının sayfalarına izin verilmesi gereken CSP kaynakları
    frame_src: tuple[str, ...] = ()
    form_action: tuple[str, ...] = ()

    def start(self, order: dict, *, ok_url: str, fail_url: str, callback_url: str, user_ip: str) -> PaymentStart:
        """Ödeme oturumu başlatır. `order`: storefront_orders satırı (public_code, total, lines, müşteri bilgileri)."""
        raise NotImplementedError

    def order_code(self, form: dict) -> str | None:
        """Geri bildirimdeki sipariş kodu (henüz doğrulanmamış)."""
        raise NotImplementedError

    def verify_callback(self, form: dict, order: dict) -> PaymentResult:
        """Sağlayıcı geri dönüşünü doğrular. Tutar ve sipariş kodu mutlaka sağlayıcıdan teyit edilmeli."""
        raise NotImplementedError


def kurus(amount) -> int:
    return int((Decimal(str(amount)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _money(amount) -> str:
    """iyzico fiyat biçimi: '1299.9' (gereksiz sıfırsız)."""
    d = Decimal(str(amount)).quantize(Decimal("0.01"))
    s = format(d, "f").rstrip("0").rstrip(".")
    return s if "." in s else s + ".0"


def _split_name(full: str) -> tuple[str, str]:
    parts = (full or "").strip().split()
    if len(parts) < 2:
        return (parts[0] if parts else "Müşteri"), "-"
    return " ".join(parts[:-1]), parts[-1]


def _basket(order: dict) -> list[dict]:
    """Sipariş satırları + kargo/hizmet bedeli. Toplamı her zaman order.total'a eşittir."""
    out = [{"id": f"{line['product_id']}", "name": str(line["title"])[:100], "quantity": int(line["quantity"]),
            "unit_price": Decimal(str(line["unit_price"])), "line_total": Decimal(str(line["line_total"]))}
           for line in order["lines"]]
    shipping = Decimal(str(order.get("shipping_fee") or 0))
    if shipping > 0:
        out.append({"id": "kargo", "name": "Kargo / hizmet bedeli", "quantity": 1, "unit_price": shipping, "line_total": shipping})
    return out


# ------------------------------------------------------------------ PayTR (iFrame API)
class PayTRProvider(CardPaymentProvider):
    code = "paytr"
    name = "PayTR"
    frame_src = ("https://www.paytr.com",)

    def __init__(self) -> None:
        s = get_settings()
        self.merchant_id, self.key, self.salt = s.paytr_merchant_id.strip(), s.paytr_merchant_key.strip(), s.paytr_merchant_salt.strip()
        self.test_mode = bool(s.paytr_test_mode)
        self.api = s.paytr_api_url.rstrip("/")

    def is_configured(self) -> bool:
        return bool(self.merchant_id and self.key and self.salt)

    def _sign(self, payload: str) -> str:
        return base64.b64encode(hmac.new(self.key.encode(), payload.encode(), hashlib.sha256).digest()).decode()

    def start(self, order, *, ok_url, fail_url, callback_url, user_ip):
        basket = base64.b64encode(json.dumps(
            [[b["name"], f"{b['unit_price']:.2f}", b["quantity"]] for b in _basket(order)], ensure_ascii=False).encode()).decode()
        amount = str(kurus(order["total"]))
        no_inst, max_inst, currency, test = "1", "0", "TL", "1" if self.test_mode else "0"
        token = self._sign(self.merchant_id + user_ip + order["public_code"] + order["email"] + amount + basket
                           + no_inst + max_inst + currency + test + self.salt)
        data = {"merchant_id": self.merchant_id, "user_ip": user_ip, "merchant_oid": order["public_code"], "email": order["email"],
                "payment_amount": amount, "paytr_token": token, "user_basket": basket, "debug_on": "0",
                "no_installment": no_inst, "max_installment": max_inst, "user_name": order["full_name"][:60],
                "user_address": f"{order['address']} {order['district']}/{order['city']}"[:400], "user_phone": order["phone"],
                "merchant_ok_url": ok_url, "merchant_fail_url": fail_url, "timeout_limit": "30", "currency": currency,
                "test_mode": test, "lang": "tr"}
        resp = _post(f"{self.api}/odeme/api/get-token", data=data)
        body = resp.json() if resp.headers.get("content-type", "").startswith(("application/json", "text/")) else {}
        if resp.status_code != 200 or body.get("status") != "success" or not body.get("token"):
            log.warning("PayTR token alınamadı: %s", str(body.get("reason") or resp.status_code)[:200])
            raise PaymentNotConfigured("Ödeme sayfası şu anda açılamadı. Lütfen biraz sonra tekrar deneyin.")
        return PaymentStart(iframe_url=f"{self.api}/odeme/guvenli/{body['token']}", reference=None)

    def order_code(self, form):
        return form.get("merchant_oid")

    def verify_callback(self, form, order):
        expected = self._sign(f"{form.get('merchant_oid', '')}{self.salt}{form.get('status', '')}{form.get('total_amount', '')}")
        if not hmac.compare_digest(expected, str(form.get("hash", ""))):
            return PaymentResult(verified=False, paid=False, message="PayTR imzası geçersiz")
        details = {k: form.get(k) for k in ("status", "total_amount", "payment_amount", "payment_type", "test_mode",
                                            "failed_reason_code", "failed_reason_msg", "currency")}
        if form.get("merchant_oid") != order["public_code"]:
            return PaymentResult(verified=False, paid=False, message="Sipariş kodu eşleşmiyor", details=details)
        if str(form.get("test_mode", "0")) == "1" and not self.test_mode:
            return PaymentResult(verified=False, paid=False, message="Canlı modda test ödemesi reddedildi", details=details)
        if form.get("status") != "success":
            return PaymentResult(verified=True, paid=False, message=str(form.get("failed_reason_msg") or "Ödeme başarısız"),
                                 details=details)
        # Taksit farkı total_amount'a eklenebilir; sipariş tutarı payment_amount ile karşılaştırılır.
        if str(form.get("payment_amount", "")) != str(kurus(order["total"])):
            return PaymentResult(verified=False, paid=False, message="Ödeme tutarı sipariş tutarıyla eşleşmiyor", details=details)
        return PaymentResult(verified=True, paid=True, reference=f"paytr:{form.get('merchant_oid')}",
                             amount=Decimal(str(form.get("payment_amount"))) / 100, details=details)


# ------------------------------------------------------------------ iyzico (Checkout Form)
class IyzicoProvider(CardPaymentProvider):
    code = "iyzico"
    name = "iyzico"

    def __init__(self) -> None:
        s = get_settings()
        self.api_key, self.secret = s.iyzico_api_key.strip(), s.iyzico_secret_key.strip()
        self.base = s.iyzico_base_url.rstrip("/")
        self.form_action = (self.base,)

    def is_configured(self) -> bool:
        return bool(self.api_key and self.secret and self.base.startswith("https://"))

    def _headers(self, path: str, body: str) -> dict:
        rnd = f"{int(time.time() * 1000)}{secrets.token_hex(4)}"
        sig = hmac.new(self.secret.encode(), (rnd + path + body).encode(), hashlib.sha256).hexdigest()
        auth = base64.b64encode(f"apiKey:{self.api_key}&randomKey:{rnd}&signature:{sig}".encode()).decode()
        return {"Authorization": f"IYZWSv2 {auth}", "x-iyzi-rnd": rnd, "Content-Type": "application/json",
                "Accept": "application/json"}

    def _call(self, path: str, payload: dict) -> dict:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        resp = _post(self.base + path, content=body.encode(), headers=self._headers(path, body))
        try:
            return resp.json()
        except ValueError:
            return {"status": "failure", "errorMessage": f"HTTP {resp.status_code}"}

    def start(self, order, *, ok_url, fail_url, callback_url, user_ip):
        name, surname = _split_name(order["full_name"])
        addr = {"contactName": order["full_name"], "city": order["city"], "country": "Turkey",
                "address": f"{order['address']} {order['district']}"[:500]}
        if order.get("postal_code"):
            addr["zipCode"] = order["postal_code"]
        basket = _basket(order)
        total = _money(order["total"])
        payload = {
            "locale": "tr", "conversationId": order["public_code"], "price": total, "paidPrice": total, "currency": "TRY",
            "basketId": order["public_code"], "paymentGroup": "PRODUCT", "callbackUrl": callback_url, "enabledInstallments": [1],
            "buyer": {"id": f"sfo-{order['id']}", "name": name, "surname": surname, "gsmNumber": "+90" + re.sub(r"\D", "", order["phone"])[-10:],
                      "email": order["email"],
                      # TC kimlik no toplanmıyor; iyzico bu durumda yer tutucu değeri kabul eder.
                      "identityNumber": "11111111111", "registrationAddress": addr["address"], "ip": user_ip,
                      "city": order["city"], "country": "Turkey"},
            "shippingAddress": addr, "billingAddress": addr,
            "basketItems": [{"id": b["id"], "name": b["name"], "category1": "Çanta", "itemType": "PHYSICAL",
                             "price": _money(b["line_total"])} for b in basket],
        }
        res = self._call("/payment/iyzipos/checkoutform/initialize/auth/ecom", payload)
        if res.get("status") != "success" or not res.get("paymentPageUrl"):
            log.warning("iyzico ödeme formu açılamadı: %s", str(res.get("errorMessage") or res.get("errorCode"))[:200])
            raise PaymentNotConfigured("Ödeme sayfası şu anda açılamadı. Lütfen biraz sonra tekrar deneyin.")
        return PaymentStart(redirect_url=res["paymentPageUrl"], reference=res.get("token"))

    def order_code(self, form):
        return form.get("_code")

    def verify_callback(self, form, order):
        token = str(form.get("token") or "")
        if not token:
            return PaymentResult(verified=False, paid=False, message="iyzico token yok")
        res = self._call("/payment/iyzipos/checkoutform/auth/ecom/detail",
                         {"locale": "tr", "conversationId": order["public_code"], "token": token})
        details = {k: res.get(k) for k in ("status", "paymentStatus", "paidPrice", "price", "basketId", "paymentId",
                                           "fraudStatus", "errorCode", "errorMessage", "currency")}
        if res.get("status") != "success":
            return PaymentResult(verified=False, paid=False, message=str(res.get("errorMessage") or "Sorgu başarısız"),
                                 details=details)
        if res.get("basketId") != order["public_code"] or res.get("conversationId") not in (None, order["public_code"]):
            return PaymentResult(verified=False, paid=False, message="Sipariş kodu eşleşmiyor", details=details)
        if res.get("paymentStatus") != "SUCCESS" or res.get("fraudStatus") not in (None, 1):
            return PaymentResult(verified=True, paid=False, message=str(res.get("errorMessage") or "Ödeme başarısız"),
                                 details=details)
        if kurus(res.get("price") or 0) != kurus(order["total"]) or (res.get("currency") or "TRY") != "TRY":
            return PaymentResult(verified=False, paid=False, message="Ödeme tutarı sipariş tutarıyla eşleşmiyor", details=details)
        return PaymentResult(verified=True, paid=True, reference=f"iyzico:{res.get('paymentId')}",
                             amount=Decimal(str(res.get("paidPrice"))), details=details)


PROVIDERS: dict[str, type[CardPaymentProvider]] = {"paytr": PayTRProvider, "iyzico": IyzicoProvider}


def card_provider() -> CardPaymentProvider | None:
    code = get_settings().storefront_payment_provider.strip().lower()
    cls = PROVIDERS.get(code)
    if cls is None:
        return None
    provider = cls()
    return provider if provider.is_configured() else None
