"""Trendyol connector (salt okunur).

Sipariş paketleri getShipmentPackages servisiyle okunur:
  GET /integration/order/sellers/{sellerId}/v2/orders   (15.10.2026'dan itibaren zorunlu)
  GET /integration/order/sellers/{sellerId}/orders      (v1; v2 404 dönerse yedek)
Aynı `orderNumber`a ait birden çok paket tek siparişte birleştirilir; her
paket ayrı bir sevkiyat (shipment) kaydıdır.

Resmî dokümantasyon/changelog'dan (developers.trendyol.com) alınan kurallar:
  * En fazla 1 ay (30 gün) geriye sorgu; sorgu başına en fazla 10.000 kayıt,
    sayfa başına en fazla 200 paket; 1000 istek/dk üst sınırı.
  * `orderDate` GMT+3 zaman damgası (ms) olarak gelir -> UTC'ye çevrilirken 3 saat düşülür.
  * 6 Nisan 2026'da alan adları değişti; yeni adlar önceliklidir, eskiler yedek:
      paket: id -> shipmentPackageId, grossAmount -> packageGrossAmount
      satır: id -> lineId, merchantSku -> stockCode, amount -> lineGrossAmount,
             price -> lineUnitPrice, discount -> lineSellerDiscount (+ lineTyDiscount),
             vatBaseAmount -> vatRate, merchantId -> sellerId

Ürün/ilan okuma: Trendyol Ürün V1 servisleri kapatılıyor ve V2 filtre servisinin
yanıt şeması bu ortamdan doğrulanamadı. Bu yüzden ilan senkronu varsayılan olarak
KAPALIDIR (TRENDYOL_LISTINGS_ENABLED=false) ve doğrulanmamış olarak gösterilir.

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
from .base import (CAP_ORDERS_READ, CAP_PRODUCTS_READ, CAP_QUESTIONS_READ, CAP_RETURNS_READ, CAP_SELLER_READ,
                   CAP_SETTLEMENTS_READ, CAP_WEBHOOKS_READ, ConnectionCheck, ConnectorError, CredentialField,
                   MarketplaceConnector, NormalizedLine, NormalizedListing, NormalizedOrder,
                   NormalizedShipment)
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

MAX_WINDOW = timedelta(days=14)   # sorgu penceresi; tek sorgu 10.000 kayıt sınırının altında kalsın
MAX_LOOKBACK = timedelta(days=30)  # servis en fazla 1 ay geriye izin verir
ORDER_DATE_OFFSET = timedelta(hours=3)  # orderDate GMT+3 olarak gönderilir
PAGE_SIZE = 200
# Cari hesap ekstresi (settlements / otherfinancials): startDate–endDate en fazla 15 gün; size 500 veya 1000;
# 100 istek/dk. paymentOrderId ödeme yapıldıktan sonra oluşur (ödeme talimatı her çarşamba).
FINANCE_WINDOW = timedelta(days=15)
FINANCE_PAGE_SIZE = 500
SETTLEMENT_TYPES = ("Sale", "Return", "Discount", "DiscountCancel", "Coupon", "CouponCancel", "ProvisionPositive",
                    "ProvisionNegative", "ManualRefund", "ManualRefundCancel", "TyDiscount", "TyDiscountCancel", "TyCoupon",
                    "TyCouponCancel", "SellerRevenuePositive", "SellerRevenueNegative", "CommissionPositive",
                    "CommissionNegative", "SellerRevenuePositiveCancel", "SellerRevenueNegativeCancel",
                    "CommissionPositiveCancel", "CommissionNegativeCancel", "DeliveryFee", "DeliveryFeeCancel", "PayByLink")
OTHER_FINANCIAL_TYPES = ("PaymentOrder", "DeductionInvoices", "CreditNote", "CommissionInvoice")
# getClaims: startDate/endDate iade paketinin lastModifiedDate'ine göre çalışır; 1000 istek/dk.
CLAIMS_WINDOW = timedelta(days=14)
CLAIMS_PAGE_SIZE = 200
# Soru filtreleme: tarih verilirse aralık en fazla 2 hafta; sayfa en fazla 50.
QUESTIONS_WINDOW = timedelta(days=14)
QUESTIONS_PAGE_SIZE = 50
MAX_PAGES = 200              # her pencere için güvenlik sınırı
PRODUCT_PAGE_SIZE = 100
MAX_PRODUCT_PAGES = 500      # güvenlik sınırı: en fazla 50.000 ilan


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


def _first(d: dict, *keys):
    """Yeni alan adı önce, eski ad yedek (6 Nisan 2026 yeniden adlandırması)."""
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def order_date_to_utc(value) -> datetime | None:
    dt = _ms_to_dt(value)
    return dt - ORDER_DATE_OFFSET if dt else None


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
    listing_publish_note = "Trendyol Ürün V2 oluşturma sözleşmesi doğrulanmadı; taslaklar CSV ile dışa aktarılır."
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
        self._orders_version = "v2"
        caps = {CAP_ORDERS_READ}
        # İlan okuma yalnızca açıkça etkinleştirilirse (V2 şeması doğrulanmadı)
        if getattr(settings, "trendyol_listings_enabled", False):
            caps.add(CAP_PRODUCTS_READ)
        # Genişletilmiş salt okunur veri (finans, iade, soru, satıcı, webhook listesi)
        if getattr(settings, "trendyol_extended_read", False):
            caps |= {CAP_SETTLEMENTS_READ, CAP_RETURNS_READ, CAP_QUESTIONS_READ, CAP_SELLER_READ, CAP_WEBHOOKS_READ}
        self.capabilities = frozenset(caps)
        self.implementation_note = (
            "Salt okunur sipariş senkronu (Order V2 alan adlarıyla). Canlı hesapla henüz doğrulanmadı. "
            "İlan senkronu: " + ("AÇIK (doğrulanmamış V1 servisi)" if CAP_PRODUCTS_READ in self.capabilities
                                 else "kapalı — Ürün V2 şeması doğrulanmadı"))

    def optional_settings(self) -> list[dict]:
        return [{"env": "TRENDYOL_LISTINGS_ENABLED", "label": "İlan okuma (Ürün V2 doğrulanmadı)",
                 "value": "açık" if CAP_PRODUCTS_READ in self.capabilities else "kapalı"}]

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
        suffix = "v2/orders" if self._orders_version == "v2" else "orders"
        return f"/integration/order/sellers/{self.store_external_id()}/{suffix}"

    def _get_orders(self, params: dict):
        """v2 uç noktası; hesap/ortam henüz v2 sunmuyorsa (404) bir kez v1'e düşer."""
        try:
            return self.client.get_json(self._orders_path(), params=params)
        except ConnectorError as exc:
            if self._orders_version == "v2" and "HTTP 404" in str(exc):
                self._orders_version = "v1"
                return self.client.get_json(self._orders_path(), params=params)
            raise

    def _fetch_packages(self, since: datetime, until: datetime) -> list[dict]:
        packages: list[dict] = []
        window_start = max(since, until - MAX_LOOKBACK)
        while window_start < until:
            window_end = min(window_start + MAX_WINDOW, until)
            page = 0
            while True:
                data = self._get_orders({
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
            data = self._get_orders({
                "startDate": int((now - timedelta(days=1)).timestamp() * 1000),
                "endDate": int(now.timestamp() * 1000), "page": 0, "size": 1,
            })
        except ConnectorError as exc:
            return ConnectionCheck(False, str(exc))
        if not isinstance(data, dict) or "content" not in data:
            return ConnectionCheck(False, "Beklenmeyen yanıt formatı")
        return ConnectionCheck(True, "Bağlantı başarılı (salt okunur)")

    # -- genişletilmiş salt okunur servisler (ham kayıt döner; eşleme services/platform içinde) ---------------
    def _seller_path(self, prefix: str, suffix: str) -> str:
        return f"/integration/{prefix}/sellers/{self.store_external_id()}/{suffix}"

    def _paged(self, path: str, params: dict, *, page_size: int, content_key: str = "content",
               headers: dict | None = None, max_pages: int = MAX_PAGES) -> list[dict]:
        out: list[dict] = []
        page = 0
        while True:
            data = self.client.get_json(path, params={**params, "page": page, "size": page_size}, headers=headers)
            if isinstance(data, list):           # bazı servisler doğrudan dizi döner
                out.extend(data)
                break
            if not isinstance(data, dict) or content_key not in data:
                raise ConnectorError(f"Trendyol yanıtı beklenen formatta değil ({content_key} alanı yok): {path}")
            out.extend(data.get(content_key) or [])
            page += 1
            total_pages = int(data.get("totalPages") or 0)
            if page >= total_pages or page >= max_pages:
                break
        return out

    @staticmethod
    def _windows(since: datetime, until: datetime, width: timedelta):
        start = since
        while start < until:
            end = min(start + width, until)
            yield start, end
            start = end

    @staticmethod
    def _ms(dt: datetime) -> int:
        return int(dt.timestamp() * 1000)

    def fetch_settlements(self, since: datetime, until: datetime, types=SETTLEMENT_TYPES) -> list[dict]:
        """Cari hesap ekstresi — settlements (satış, iade, indirim, kupon, komisyon, ...). Her kayıt `_type` taşır."""
        return self._finance("settlements", since, until, types)

    def fetch_other_financials(self, since: datetime, until: datetime, types=OTHER_FINANCIAL_TYPES) -> list[dict]:
        """Cari hesap ekstresi — otherfinancials (ödeme talimatı, kesinti faturası, ...)."""
        return self._finance("otherfinancials", since, until, types)

    def _finance(self, kind: str, since: datetime, until: datetime, types) -> list[dict]:
        path = self._seller_path("finance/che", kind)
        out: list[dict] = []
        for start, end in self._windows(since, until, FINANCE_WINDOW):
            for t in types:
                for r in self._paged(path, {"startDate": self._ms(start), "endDate": self._ms(end), "transactionType": t},
                                     page_size=FINANCE_PAGE_SIZE):
                    out.append({**r, "_type": r.get("transactionType") or t})
        return out

    def fetch_cargo_invoice_items(self, invoice_serial: str) -> list[dict]:
        """Kargo faturası detayları (DeductionInvoices kaydındaki fatura seri numarasıyla)."""
        return self._paged(self._seller_path("finance/che", f"cargo-invoice/{invoice_serial}/items"), {},
                           page_size=FINANCE_PAGE_SIZE)

    def fetch_claims(self, since: datetime, until: datetime) -> list[dict]:
        """İadesi oluşturulan siparişler (getClaims). Tarihler iade paketinin lastModifiedDate'ine göre."""
        path = self._seller_path("order", "claims")
        out: list[dict] = []
        for start, end in self._windows(max(since, until - MAX_LOOKBACK), until, CLAIMS_WINDOW):
            out.extend(self._paged(path, {"startDate": self._ms(start), "endDate": self._ms(end)}, page_size=CLAIMS_PAGE_SIZE))
        return out

    def fetch_questions(self, since: datetime, until: datetime) -> list[dict]:
        """Müşteri soruları filtreleme servisi (salt okunur; cevaplama servisi ÇAĞRILMAZ)."""
        path = self._seller_path("qna", "questions/filter")
        out: list[dict] = []
        for start, end in self._windows(since, until, QUESTIONS_WINDOW):
            out.extend(self._paged(path, {"supplierId": self.store_external_id(), "startDate": self._ms(start),
                                          "endDate": self._ms(end)}, page_size=QUESTIONS_PAGE_SIZE))
        return out

    def fetch_seller_addresses(self) -> dict:
        """Satıcı adres bilgileri (getSuppliersAddresses): GET /integration/sellers/{sellerId}/addresses."""
        return self.client.get_json(f"/integration/sellers/{self.store_external_id()}/addresses")

    def fetch_webhooks(self) -> list[dict]:
        """Tanımlı webhook'lar (yalnızca listeleme; oluşturma/güncelleme YAPILMAZ)."""
        data = self.client.get_json(self._seller_path("webhook", "webhooks"))
        if isinstance(data, dict):
            data = data.get("content") or data.get("webhooks") or []
        return data if isinstance(data, list) else []

    def fetch_products_v2(self, max_pages: int = 1, page_size: int = 50) -> list[dict]:
        """Ürün filtreleme — onaylı ürün V2 (storeFrontCode başlığıyla). Varsayılan: yalnızca ilk sayfa (doğrulama)."""
        return self._paged(self._seller_path("product", "products/approved"), {}, page_size=page_size,
                           headers={"storeFrontCode": self.settings.trendyol_storefront_code or "TR"},
                           max_pages=max_pages)

    def fetch_orders(self, since: datetime, until: datetime) -> list[NormalizedOrder]:
        return self.normalize(self._fetch_packages(since, until))

    def fetch_listings(self) -> list[NormalizedListing]:
        """Ürün filtreleme servisi (GET). Sayfa sayfa tüm ilanları okur."""
        path = f"/integration/product/sellers/{self.store_external_id()}/products"
        items: list[dict] = []
        page = 0
        while True:
            data = self.client.get_json(path, params={"page": page, "size": PRODUCT_PAGE_SIZE})
            if not isinstance(data, dict) or "content" not in data:
                raise ConnectorError("Trendyol ürün yanıtı beklenen formatta değil (content alanı yok)")
            items.extend(data.get("content") or [])
            page += 1
            if page >= int(data.get("totalPages") or 0) or page >= MAX_PRODUCT_PAGES:
                break
        return [self.normalize_listing(p) for p in items if p.get("barcode") or p.get("id")]

    @staticmethod
    def normalize_listing(p: dict) -> NormalizedListing:
        if p.get("archived"):
            status = "archived"
        elif p.get("rejected") or p.get("blacklisted"):
            status = "rejected"
        elif p.get("approved") is False:
            status = "pending"
        elif p.get("onSale") is False:
            status = "not_on_sale"
        else:
            status = "on_sale"
        images = p.get("images") or []
        return NormalizedListing(
            external_product_id=str(p.get("barcode") or p.get("id")),
            barcode=p.get("barcode"),
            sku=p.get("stockCode") or None,
            title=p.get("title") or "",
            price=_dec(p.get("salePrice")),
            list_price=_dec(p.get("listPrice")),
            stock=int(p["quantity"]) if p.get("quantity") is not None else None,
            status=status,
            brand=p.get("brand"),
            category=p.get("categoryName"),
            vat_rate=_dec(p.get("vatRate")),
            image_url=(images[0] or {}).get("url") if images else None,
        )

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
                pid = str(_first(p, "shipmentPackageId", "id") or number)
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
                pid = str(_first(pkg, "shipmentPackageId", "id") or number)
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
                    price = _dec(_first(ln, "lineUnitPrice", "price"))
                    if price is None:
                        gross = _dec(_first(ln, "lineGrossAmount", "amount"))
                        price = (gross / qty).quantize(Decimal("0.01")) if gross is not None else Decimal("0")
                    line_id = _first(ln, "lineId", "id")
                    lines.append(NormalizedLine(
                        external_line_id=str(line_id if line_id is not None else f"{pid}:{ln.get('barcode')}"),
                        sku=_first(ln, "stockCode", "merchantSku", "sku"),
                        barcode=ln.get("barcode"),
                        product_name=ln.get("productName") or "",
                        quantity=qty,
                        unit_price=price,
                        # Satıcının üstlendiği indirim (Trendyol indirimi satıcı maliyeti değildir)
                        discount=_dec(_first(ln, "lineSellerDiscount", "discount")) or Decimal("0"),
                        vat_rate=_dec(_first(ln, "vatRate", "vatBaseAmount")),
                        line_status=ln.get("orderLineItemStatusName") or raw or None,
                    ))

            modified = [_ms_to_dt(p.get("lastModifiedDate")) for p in pkgs]
            modified = [m for m in modified if m]
            orders.append(NormalizedOrder(
                external_order_id=number,
                marketplace_status=",".join(sorted(set(r for r in raw_statuses if r))) or "",
                internal_status=aggregate_status(internal),
                order_date=order_date_to_utc(first.get("orderDate")) or datetime.now(timezone.utc),
                currency=first.get("currencyCode") or "TRY",
                customer_name=customer,
                customer_city=address.get("city"),
                lines=lines,
                shipments=shipments,
                review_reason="; ".join(review_reasons) or None,
                last_modified=max(modified) if modified else None,
            ))
        return orders
