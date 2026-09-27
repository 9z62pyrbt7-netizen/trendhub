"""Pazaryeri connector arayüzü.

Her pazaryeri bu arayüzü bağımsız olarak uygular. Connector'lar yalnızca
pazaryeri API'si ile konuşur ve **normalize edilmiş** veri döner; veritabanına
yazma, idempotency ve statü kuralları `app.services` katmanındadır. Böylece
bir pazaryerinin arızası diğerlerini etkilemez ve yeni pazaryeri eklemek
yalnızca yeni bir connector sınıfı yazmaktan ibarettir.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal


class ConnectorError(Exception):
    """Genel connector hatası (tekrar denenebilir kabul edilir)."""
    retryable = True


class RetryableError(ConnectorError):
    """Geçici hata: zaman aşımı, 429, 5xx."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class AuthError(ConnectorError):
    """Kimlik bilgisi hatalı / yetkisiz. Tekrar denemek anlamsız."""
    retryable = False


class NotConfigured(ConnectorError):
    retryable = False


class NotSupported(ConnectorError):
    """Connector bu yeteneği (henüz) desteklemiyor."""
    retryable = False


class WriteDisabled(ConnectorError):
    """Pazaryerine yazma işlemleri CONNECTOR_WRITE_ENABLED ile kapalı."""
    retryable = False


# Yetenekler
CAP_ORDERS_READ = "orders.read"
CAP_PRODUCTS_READ = "products.read"
CAP_STOCK_WRITE = "stock.write"
CAP_PRICE_WRITE = "price.write"
CAP_SETTLEMENTS_READ = "settlements.read"


@dataclass
class NormalizedLine:
    external_line_id: str
    sku: str | None
    barcode: str | None
    product_name: str
    quantity: int
    unit_price: Decimal           # indirim sonrası birim satış fiyatı
    discount: Decimal = Decimal("0")
    vat_rate: Decimal | None = None
    commission: Decimal | None = None   # pazaryeri bildiriyorsa gerçek komisyon
    commission_rate: Decimal | None = None
    line_status: str | None = None


@dataclass
class NormalizedShipment:
    external_package_id: str
    carrier: str | None = None
    tracking_number: str | None = None
    tracking_url: str | None = None
    marketplace_status: str | None = None
    desi: Decimal | None = None
    shipped_at: datetime | None = None
    delivered_at: datetime | None = None


@dataclass
class NormalizedOrder:
    external_order_id: str
    marketplace_status: str           # ham statü -> orders.status
    internal_status: str              # iç model -> orders.internal_status
    order_date: datetime
    currency: str = "TRY"
    customer_name: str | None = None
    customer_city: str | None = None
    lines: list[NormalizedLine] = field(default_factory=list)
    shipments: list[NormalizedShipment] = field(default_factory=list)
    review_reason: str | None = None
    last_modified: datetime | None = None


@dataclass
class NormalizedListing:
    """Pazaryerindeki bir ürün ilanının salt okunur görüntüsü."""
    external_product_id: str
    barcode: str | None
    sku: str | None
    title: str
    price: Decimal | None = None
    list_price: Decimal | None = None
    stock: int | None = None
    status: str | None = None          # on_sale / not_on_sale / archived / pending / rejected
    brand: str | None = None
    category: str | None = None
    vat_rate: Decimal | None = None
    image_url: str | None = None


@dataclass
class CredentialField:
    env: str
    label: str
    secret: bool = True


@dataclass
class ConnectionCheck:
    ok: bool
    message: str


class MarketplaceConnector(abc.ABC):
    code: str
    name: str
    credential_fields: list[CredentialField]
    capabilities: frozenset[str] = frozenset()
    # True ise `fetch_orders(since, ...)` "since'ten sonra DEĞİŞEN" siparişleri
    # döner; servis son senkron zamanını (watermark) kullanır, derin tarama gerekmez.
    incremental: bool = False
    # Uygulama henüz doğrulanmadıysa arayüzde gösterilecek not.
    implementation_note: str | None = None

    def __init__(self, settings):
        self.settings = settings

    # -- yapılandırma -------------------------------------------------------
    @abc.abstractmethod
    def credential_values(self) -> dict[str, str]:
        """env adı -> değer."""

    def missing_credentials(self) -> list[str]:
        from ..config import is_set
        return [k for k, v in self.credential_values().items() if not is_set(v)]

    def is_configured(self) -> bool:
        return not self.missing_credentials()

    def store_external_id(self) -> str:
        """Bu hesabın pazaryerindeki satıcı kimliği (stores.external_id)."""
        raise NotImplementedError

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    # -- işlemler -----------------------------------------------------------
    @abc.abstractmethod
    def test_connection(self) -> ConnectionCheck:
        ...

    def fetch_orders(self, since: datetime, until: datetime) -> list[NormalizedOrder]:
        raise NotSupported(f"{self.name}: sipariş senkronizasyonu henüz uygulanmadı")

    def fetch_listings(self) -> list[NormalizedListing]:
        raise NotSupported(f"{self.name}: ürün/ilan okuma henüz uygulanmadı")

    def update_stock(self, barcode: str, quantity: int) -> None:
        self._require_write()
        raise NotSupported(f"{self.name}: stok güncelleme henüz uygulanmadı")

    def update_price(self, barcode: str, price: Decimal) -> None:
        self._require_write()
        raise NotSupported(f"{self.name}: fiyat güncelleme henüz uygulanmadı")

    def _require_write(self) -> None:
        if not self.settings.connector_write_enabled:
            raise WriteDisabled(
                "Pazaryerine yazma işlemleri kapalı (CONNECTOR_WRITE_ENABLED=false)."
            )
