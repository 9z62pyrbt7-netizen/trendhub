"""Amazon.com.tr connector (Selling Partner API).

Durum: kimlik bilgisi algılama ve statü eşlemesi hazır; LWA token alma ve
Orders API senkronizasyonu HENÜZ UYGULANMADI. Bilgiler girilmemişse
entegrasyon "Bağlı değil" görünür.
"""
from __future__ import annotations

from ..domain import order_status as S
from .base import ConnectionCheck, CredentialField, MarketplaceConnector

# SP-API OrderStatus değerleri
STATUS_MAP = {
    "pendingavailability": S.NEW,
    "pending": S.NEW,
    "unshipped": S.PREPARING,
    "partiallyshipped": S.SHIPPED,
    "shipped": S.SHIPPED,
    "invoiceunconfirmed": S.NEEDS_REVIEW,
    "canceled": S.CANCELLED,
    "unfulfillable": S.NEEDS_REVIEW,
}


def map_status(raw: str | None) -> str:
    return STATUS_MAP.get((raw or "").strip().lower(), S.NEEDS_REVIEW)


class AmazonTrConnector(MarketplaceConnector):
    code = "amazon_tr"
    name = "Amazon.com.tr"
    capabilities = frozenset()
    implementation_note = "SP-API sipariş senkronizasyonu henüz uygulanmadı."
    credential_fields = [
        CredentialField("AMAZON_SP_SELLER_ID", "Seller ID", secret=False),
        CredentialField("AMAZON_SP_CLIENT_ID", "LWA Client ID", secret=False),
        CredentialField("AMAZON_SP_CLIENT_SECRET", "LWA Client Secret"),
        CredentialField("AMAZON_SP_REFRESH_TOKEN", "Refresh Token"),
    ]

    def credential_values(self) -> dict[str, str]:
        s = self.settings
        return {
            "AMAZON_SP_SELLER_ID": s.amazon_sp_seller_id,
            "AMAZON_SP_CLIENT_ID": s.amazon_sp_client_id,
            "AMAZON_SP_CLIENT_SECRET": s.amazon_sp_client_secret,
            "AMAZON_SP_REFRESH_TOKEN": s.amazon_sp_refresh_token,
        }

    def store_external_id(self) -> str:
        return str(self.settings.amazon_sp_seller_id).strip()

    def test_connection(self) -> ConnectionCheck:
        if not self.is_configured():
            return ConnectionCheck(False, "Bağlı değil: eksik bilgiler " + ", ".join(self.missing_credentials()))
        return ConnectionCheck(False, "Bilgiler tanımlı ancak Amazon SP-API bağlantı testi henüz uygulanmadı.")
