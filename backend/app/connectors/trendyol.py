"""Trendyol connector (salt okunur).

Sipariş paketleri `GET /integration/order/sellers/{sellerId}/orders` ile okunur.
Aynı `orderNumber`a ait birden çok paket tek siparişte birleştirilir; her
paket ayrı bir sevkiyat (shipment) kaydıdır.

ÖNEMLİ: Canlı Trendyol → Çanta Bayim otomasyonu aynı satıcı hesabını
kullanıyor. Bu connector pazaryerinde HİÇBİR değişiklik yapmaz (statü
güncelleme, stok/fiyat gönderme yok). Yazma metotları
CONNECTOR_WRITE_ENABLED=false iken hata verir ve henüz uygulanmamıştır.

Not: Alan eşlemesi Trendyol entegrasyon dokümantasyonuna göre yapılmıştır;
canlı hesapla doğrulanması gerekir (bkz. docs/ARCHITECTURE.md).
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx

from ..domain import order_status as S
from .base import (CAP_ORDERS_READ, ConnectionCheck, ConnectorError, CredentialField,
                   MarketplaceConnector, NormalizedLine, NormalizedOrder, NormalizedShipment)
from .http import RateLimiter, ResilientClient

STATUS_MAP = {
    "awaiting": S.NEW,
    "created": S.NEW,
    "picking": S.PREPARING,
    "unpacked": S.PREPARING,
    "invoiced": S.AWAITING_SHIPMENT,
    "shipped": S.SHIPPED,
    "atcollectionpoint": S.SHIPPED,
    "delivered": S.DELIVERED,
    "undelivered": S.NEEDS_REVIEW,
    "cancelled": S.CANCELLED,
    "unsupplied": S.CANCELLED,
    "returned": S.RETURNED,
}

MAX_WINDOW = timedelta(days=14)   # Trendyol tek sorguda en fazla ~2 hafta aralık kabul eder
PAGE_SIZE = 200


def map_status(raw: str | None) -> str:
    return STATUS_MAP.get((raw or "").strip().lower(), S.NEEDS_REVIEW)


def _ms_to_dt(value) -> datetime | None:
    if value in (None, ""):
        return None
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)


def _dec(value) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


_PRIORITY = [S.NEEDS_REVIEW, S.NEW, S.PREPARING, S.SENT_TO_SUPPLIER, S.AWAITING_SHIPMENT,
             S.SHIPPED, S.DELIVERED, S.RETURNED, S.CANCELLED]


def aggregate_status(statuses: list[str]) -> str:
    """Birden çok paketin statüsünden sipariş statüsü.

    Aktif (iptal/iade olmayan) paketler varsa en geride olanın statüsü alınır;
    hepsi iptal/iade ise iade > iptal önceliği uygulanır.
    """
    if S.NEEDS_REVIEW in statuses:
        return S.NEEDS_REVIEW
    active = [s for s in statuses if s not in (S.CANCELLED, S.RETURNED)]
    if active:
        return min(active, key=_PRIORITY.index)
    return S.RETURNED if S.RETURNED in statuses else S.CANCELLED


class TrendyolConnector(MarketplaceConnector):
    code = "trendyol"
    name = "Trendyol"
    capabilities = frozenset({CAP_ORDERS_READ})
    credential_fields = [
        CredentialField("TRENDYOL_SELLER_ID", "Satıcı ID (Supplier ID)", secret=False),
        CredentialField("TRENDYOL_API_KEY", "API Key"),
        CredentialField("TRENDYOL_API_SECRET", "API Secret"),
    ]

    def __init__(self, settings, transport: httpx.BaseTransport | None = None, sleep=None):
        super().__init__(settings)
        self._transport = transport
        self._sleep = sleep
        self._client: ResilientClient | None = None

    def credential_values(self) -> dict[str, str]:
        s = self.settings
        return {
            "TRENDYOL_SELLER_ID": s.trendyol_seller_id,
            "TRENDYOL_API_KEY": s.trendyol_api_key,
            "TRENDYOL_API_SECRET": s.trendyol_api_secret,
        }

    def store_external_id(self) -> str:
        return str(self.settings.trendyol_seller_id).strip()

    @property
    def client(self) -> ResilientClient:
        if self._client is None:
            s = self.settings
            sleep = self._sleep or time.sleep
            self._client = ResilientClient(
                s.trendyol_base_url,
                auth=(s.trendyol_api_key, s.trendyol_api_secret),
                headers={"User-Agent": f"{self.store_external_id()} - SelfIntegration",
                         "Accept": "application/json"},
                rate_limiter=RateLimiter(s.trendyol_rate_per_minute, sleep=sleep),
                transport=self._transport,
                sleep=sleep,
            )
        return self._client

    def _orders_path(self) -> str:
        return f"/integration/order/sellers/{self.store_external_id()}/orders"

    def _fetch_packages(self, since: datetime, until: datetime) -> list[dict]:
        packages: list[dict] = []
        window_start = since
        while window_start < until:
            window_end = min(window_start + MAX_WINDOW, until)
            page = 0
            while True:
                data = self.client.get_json(self._orders_path(), params={
                    "startDate": int(window_start.timestamp() * 1000),
                    "endDate": int(window_end.timestamp() * 1000),
                    "page": page,
                    "size": PAGE_SIZE,
                    "orderByField": "PackageLastModifiedDate",
                    "orderByDirection": "ASC",
                })
                if not isinstance(data, dict) or "content" not in data:
                    raise ConnectorError("Trendyol yanıtı beklenen formatta değil (content alanı yok)")
                packages.extend(data.get("content") or [])
                total_pages = int(data.get("totalPages") or 0)
                page += 1
                if page >= total_pages:
                    break
            window_start = window_end
        return packages

    def test_connection(self) -> ConnectionCheck:
        if not self.is_configured():
            return ConnectionCheck(False, "Bağlı değil: eksik bilgiler " + ", ".join(self.missing_credentials()))
        now = datetime.now(timezone.utc)
        try:
            data = self.client.get_json(self._orders_path(), params={
                "startDate": int((now - timedelta(days=1)).timestamp() * 1000),
                "endDate": int(now.timestamp() * 1000), "page": 0, "size": 1,
            })
        except ConnectorError as exc:
            return ConnectionCheck(False, str(exc))
        if not isinstance(data, dict) or "content" not in data:
            return ConnectionCheck(False, "Beklenmeyen yanıt formatı")
        return ConnectionCheck(True, "Bağlantı başarılı (salt okunur)")

    def fetch_orders(self, since: datetime, until: datetime) -> list[NormalizedOrder]:
        return self.normalize(self._fetch_packages(since, until))

    # -- eşleme ---------------------------------------------------------------
    @staticmethod
    def normalize(packages: list[dict]) -> list[NormalizedOrder]:
        grouped: dict[str, list[dict]] = {}
        for pkg in packages:
            number = str(pkg.get("orderNumber") or "").strip()
            if not number:
                continue
            grouped.setdefault(number, []).append(pkg)

        orders: list[NormalizedOrder] = []
        for number, pkgs in grouped.items():
            # Aynı paket birden çok pencerede gelebilir: paket id'ye göre tekilleştir,
            # en son değişeni tut.
            by_id: dict[str, dict] = {}
            for p in pkgs:
                pid = str(p.get("id") or p.get("shipmentPackageId") or number)
                prev = by_id.get(pid)
                if prev is None or int(p.get("lastModifiedDate") or 0) >= int(prev.get("lastModifiedDate") or 0):
                    by_id[pid] = p
            pkgs = list(by_id.values())

            raw_statuses = [str(p.get("shipmentPackageStatus") or p.get("status") or "") for p in pkgs]
            internal = [map_status(r) for r in raw_statuses]
            first = pkgs[0]
            address = first.get("shipmentAddress") or {}
            customer = " ".join(x for x in (first.get("customerFirstName"), first.get("customerLastName")) if x) or None

            lines: list[NormalizedLine] = []
            shipments: list[NormalizedShipment] = []
            review_reasons: list[str] = []
            for pkg, raw, st in zip(pkgs, raw_statuses, internal):
                if st == S.NEEDS_REVIEW:
                    review_reasons.append(f"Trendyol statüsü eşlenemedi/sorunlu: {raw or 'boş'}")
                pid = str(pkg.get("id") or pkg.get("shipmentPackageId") or number)
                shipments.append(NormalizedShipment(
                    external_package_id=pid,
                    carrier=pkg.get("cargoProviderName"),
                    tracking_number=str(pkg["cargoTrackingNumber"]) if pkg.get("cargoTrackingNumber") else None,
                    tracking_url=pkg.get("cargoTrackingLink"),
                    marketplace_status=raw or None,
                    desi=_dec(pkg.get("cargoDeci")),
                ))
                for ln in pkg.get("lines") or []:
                    qty = int(ln.get("quantity") or 1)
                    price = _dec(ln.get("price"))
                    if price is None:
                        price = _dec(ln.get("amount")) or Decimal("0")
                    lines.append(NormalizedLine(
                        external_line_id=str(ln.get("id") or f"{pid}:{ln.get('barcode')}"),
                        sku=ln.get("merchantSku") or ln.get("sku"),
                        barcode=ln.get("barcode"),
                        product_name=ln.get("productName") or "",
                        quantity=qty,
                        unit_price=price,
                        discount=_dec(ln.get("discount")) or Decimal("0"),
                        vat_rate=_dec(ln.get("vatRate")),
                        line_status=ln.get("orderLineItemStatusName") or raw or None,
                    ))

            modified = [_ms_to_dt(p.get("lastModifiedDate")) for p in pkgs]
            modified = [m for m in modified if m]
            orders.append(NormalizedOrder(
                external_order_id=number,
                marketplace_status=",".join(sorted(set(r for r in raw_statuses if r))) or "",
                internal_status=aggregate_status(internal),
                order_date=_ms_to_dt(first.get("orderDate")) or datetime.now(timezone.utc),
                currency=first.get("currencyCode") or "TRY",
                customer_name=customer,
                customer_city=address.get("city"),
                lines=lines,
                shipments=shipments,
                review_reason="; ".join(review_reasons) or None,
                last_modified=max(modified) if modified else None,
            ))
        return orders
