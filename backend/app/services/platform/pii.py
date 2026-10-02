"""Kişisel veri (PII) koruması.

* Pazaryeri yanıtlarındaki müşteri adı, e-posta, telefon, adres, müşteri id alanları kalıcı olarak SAKLANMAZ
  (webhook/olay yükleri `strip_pii` ile ayıklanır).
* Serbest metinler (soru, iade notu) `scrub` ile maskelenir: e-posta, telefon, TCKN benzeri 11 hane, IBAN.
* LLM'e gönderilen her araç çıktısı `assert_no_pii` ile kontrol edilir (testlerde de doğrulanır).
"""
from __future__ import annotations

import re

PII_KEYS = frozenset(k.lower() for k in (
    "customerFirstName", "customerLastName", "customerEmail", "customerId", "customerName", "customer",
    "shipmentAddress", "invoiceAddress", "address", "address1", "address2", "fullAddress", "fullName", "firstName",
    "lastName", "email", "phone", "phoneNumber", "gsm", "identityNumber", "taxNumber", "tcIdentityNumber",
    "userName", "customerNote", "buyer", "recipient", "receiverName", "invoiceAddressId", "taxOffice",
    "company", "postalCode", "district", "neighborhood", "neighborhoodId", "districtId", "addressLines",
))

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
IBAN = re.compile(r"\bTR\s?\d{2}(?:\s?\d{4}){5}\s?\d{2}\b", re.I)
PHONE = re.compile(r"(?<!\d)(?:\+?90[\s-]?)?0?5\d{2}[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}(?!\d)")
TCKN = re.compile(r"(?<!\d)[1-9]\d{10}(?!\d)")


def scrub(text: str | None) -> str | None:
    if text is None:
        return None
    t = EMAIL.sub("[e-posta]", str(text))
    t = IBAN.sub("[iban]", t)
    t = PHONE.sub("[telefon]", t)
    return TCKN.sub("[kimlik no]", t)


def strip_pii(obj):
    """Sözlük/listeden kişisel veri alanlarını özyinelemeli olarak çıkarır (kopya döner)."""
    if isinstance(obj, dict):
        return {k: strip_pii(v) for k, v in obj.items() if str(k).lower() not in PII_KEYS}
    if isinstance(obj, list):
        return [strip_pii(v) for v in obj]
    return obj


def find_pii(text: str) -> list[str]:
    hits = []
    # TCKN kalıbı burada aranmaz: 11 haneli Trendyol sipariş numaralarıyla karışır (serbest metinde scrub maskeler).
    for name, rx in (("email", EMAIL), ("iban", IBAN), ("phone", PHONE)):
        if rx.search(text or ""):
            hits.append(name)
    return hits


class PIILeak(ValueError):
    pass


def assert_no_pii(text: str, forbidden: list[str] | None = None) -> str:
    """LLM'e gidecek metinde kişisel veri kalıbı veya bilinen müşteri adı varsa hata verir."""
    hits = find_pii(text)
    for f in forbidden or []:
        if f and f.lower() in (text or "").lower():
            hits.append("customer_name")
    if hits:
        raise PIILeak("LLM'e gönderilecek veride kişisel veri bulundu: " + ", ".join(sorted(set(hits))))
    return text
