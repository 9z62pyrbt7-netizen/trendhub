"""Hepsiburada connector.

Durum: kimlik bilgisi algılama, statü eşlemesi ve bağlantı testi iskeleti
hazır; sipariş senkronizasyonu HENÜZ UYGULANMADI (canlı hesapla alan
eşlemesi doğrulanmadan sahte/tahmini veri üretmemek için bilinçli olarak
boş bırakıldı). Bilgiler girilmemişse entegrasyon "Bağlı değil" görünür.
"""
from __future__ import annotations

from ..domain import order_status as S
from .base import ConnectionCheck, CredentialField, MarketplaceConnector

STATUS_MAP = {
    "open": S.NEW,
    "unpacked": S.PREPARING,
    "packed": S.AWAITING_SHIPMENT,
    "readytoship": S.AWAITING_SHIPMENT,
    "intransit": S.SHIPPED,
    "shipped": S.SHIPPED,
    "delivered": S.DELIVERED,
    "undelivered": S.NEEDS_REVIEW,
    "cancelledbymerchant": S.CANCELLED,
    "cancelledbycustomer": S.CANCELLED,
    "cancelledbysap": S.CANCELLED,
    "cancelled": S.CANCELLED,
    "returned": S.RETURNED,
}


def map_status(raw: str | None) -> str:
    return STATUS_MAP.get((raw or "").replace("_", "").strip().lower(), S.NEEDS_REVIEW)


class HepsiburadaConnector(MarketplaceConnector):
    code = "hepsiburada"
    name = "Hepsiburada"
    capabilities = frozenset()
    implementation_note = "Sipariş senkronizasyonu henüz uygulanmadı."
    credential_fields = [
        CredentialField("HEPSIBURADA_MERCHANT_ID", "Merchant ID", secret=False),
        CredentialField("HEPSIBURADA_USERNAME", "Entegrasyon kullanıcı adı", secret=False),
        CredentialField("HEPSIBURADA_PASSWORD", "Entegrasyon şifresi"),
    ]

    def credential_values(self) -> dict[str, str]:
        s = self.settings
        return {
            "HEPSIBURADA_MERCHANT_ID": s.hepsiburada_merchant_id,
            "HEPSIBURADA_USERNAME": s.hepsiburada_username,
            "HEPSIBURADA_PASSWORD": s.hepsiburada_password,
        }

    def store_external_id(self) -> str:
        return str(self.settings.hepsiburada_merchant_id).strip()

    def test_connection(self) -> ConnectionCheck:
        if not self.is_configured():
            return ConnectionCheck(False, "Bağlı değil: eksik bilgiler " + ", ".join(self.missing_credentials()))
        return ConnectionCheck(False, "Bilgiler tanımlı ancak Hepsiburada bağlantı testi henüz uygulanmadı.")
