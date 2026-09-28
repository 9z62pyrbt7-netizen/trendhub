"""Hepsiburada connector (salt okunur).

Kaynak: developers.hepsiburada.com (bu ortamdan doğrudan erişilemiyor; resmi sayfaların
arama özetleriyle doğrulandı — canlı hesapla ayrıca doğrulanmalı):

  Kimlik doğrulama : HTTP Basic (entegrasyon kullanıcı adı + şifre) + zorunlu User-Agent
                     başlığı (resmi duyuru: Basic auth'taki kullanıcı adı, ör. "xxx_dev").
  Siparişler       : GET {oms}/packages/merchantid/{merchantId}
                     ?begindate=YYYY-MM-DD HH:mm&enddate=...&limit=..&offset=..
                     Yanıt limit / offset / pagecount / totalcount bilgisi içerir.
                     Paket alanları: id, status, customerName, orderDate, packageNumber,
                     cargoCompany, shippingCity, items[] (lineItemId, sku, name, quantity,
                     unitPrice/totalPrice {amount, currency}, vat, vatRate).
  Hız sınırı       : sipariş servisleri 1000 istek/sn; 429'da yeniden denenir.
  İlanlar          : GET {listing}/listings/merchantid/{merchantId}?offset&limit
                     Yanıt şeması tam doğrulanamadı -> varsayılan KAPALI
                     (HEPSIBURADA_LISTINGS_ENABLED=false).

Bu connector Hepsiburada'da HİÇBİR değişiklik yapmaz: paketleme, statü, stok, fiyat,
ürün yazma metotları yoktur; HTTP istemcisi GET dışındaki isteği ağa çıkmadan reddeder.
Bilgiler tanımlı değilse entegrasyon "Bağlı değil" görünür; hiçbir veri üretilmez.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import httpx

from ..domain import order_status as S
from .base import (CAP_ORDERS_READ, CAP_PRODUCTS_READ, ConnectionCheck, ConnectorError, CredentialField,
                   MarketplaceConnector, NormalizedLine, NormalizedListing, NormalizedOrder,
                   NormalizedShipment)
from .http import RateLimiter, ResilientClient

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

TR_TZ = timezone(timedelta(hours=3))
WINDOW = timedelta(days=7)
MAX_LOOKBACK = timedelta(days=30)
PAGE_SIZE = 50
MAX_PAGES = 400          # güvenlik sınırı (pencere başına)


def map_status(raw: str | None) -> str:
    return STATUS_MAP.get((raw or "").replace("_", "").replace(" ", "").strip().lower(), S.NEEDS_REVIEW)


def _get(d, *keys):
    """Büyük/küçük harf duyarsız alan okuma (HB yanıtlarında her iki yazım görülebilir)."""
    if not isinstance(d, dict):
        return None
    lower = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        v = d.get(k)
        if v in (None, ""):
            v = lower.get(k.lower())
        if v not in (None, ""):
            return v
    return None


def _money(v) -> Decimal | None:
    if isinstance(v, dict):
        v = _get(v, "amount", "value")
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


def parse_date(v) -> datetime | None:
    """ISO tarih; saat dilimi yoksa Türkiye saati (UTC+3) kabul edilir."""
    if not v:
        return None
    s = str(v).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s[:16], "%Y-%m-%d %H:%M")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TR_TZ)
    return dt.astimezone(timezone.utc)


def _items(data, *keys) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            v = _get(data, k)
            if isinstance(v, list):
                return v
    raise ConnectorError("Hepsiburada yanıtı beklenen formatta değil (liste bulunamadı)")


def _is_true(v) -> bool:
    return v is True or str(v).lower() == "true"


class HepsiburadaConnector(MarketplaceConnector):
    code = "hepsiburada"
    name = "Hepsiburada"
    listing_publish_note = "Hepsiburada ürün/katalog oluşturma sözleşmesi doğrulanmadı; taslaklar CSV ile dışa aktarılır."
    capabilities = frozenset({CAP_ORDERS_READ})
    credential_fields = [
        CredentialField("HEPSIBURADA_MERCHANT_ID", "Merchant ID", secret=False),
        CredentialField("HEPSIBURADA_USERNAME", "Entegrasyon kullanıcı adı", secret=False),
        CredentialField("HEPSIBURADA_PASSWORD", "Entegrasyon şifresi"),
    ]

    def __init__(self, settings, transport: httpx.BaseTransport | None = None, sleep=None):
        super().__init__(settings)
        self._transport = transport
        self._sleep = sleep
        self._oms: ResilientClient | None = None
        self._listing: ResilientClient | None = None
        if getattr(settings, "hepsiburada_listings_enabled", False):
            self.capabilities = frozenset({CAP_ORDERS_READ, CAP_PRODUCTS_READ})
        self.implementation_note = (
            "Salt okunur sipariş (paket) senkronu. Uç nokta ve parametreler resmi dokümanın özetleriyle "
            "doğrulandı; alan eşlemesi canlı hesapla henüz doğrulanmadı. İlan senkronu: "
            + ("AÇIK (şema doğrulanmadı)" if CAP_PRODUCTS_READ in self.capabilities else "kapalı — şema doğrulanmadı"))

    def optional_settings(self) -> list[dict]:
        s = self.settings
        return [{"env": "HEPSIBURADA_USER_AGENT", "label": "User-Agent başlığı",
                 "value": "tanımlı" if (s.hepsiburada_user_agent or "").strip() else "kullanıcı adı kullanılır"},
                {"env": "HEPSIBURADA_LISTINGS_ENABLED", "label": "İlan okuma (şema doğrulanmadı)",
                 "value": "açık" if CAP_PRODUCTS_READ in self.capabilities else "kapalı"}]

    def credential_values(self) -> dict[str, str]:
        s = self.settings
        return {
            "HEPSIBURADA_MERCHANT_ID": s.hepsiburada_merchant_id,
            "HEPSIBURADA_USERNAME": s.hepsiburada_username,
            "HEPSIBURADA_PASSWORD": s.hepsiburada_password,
        }

    def store_external_id(self) -> str:
        return str(self.settings.hepsiburada_merchant_id).strip()

    def _client(self, base_url: str) -> ResilientClient:
        s = self.settings
        sleep = self._sleep or time.sleep
        user_agent = (getattr(s, "hepsiburada_user_agent", "") or s.hepsiburada_username).strip()
        return ResilientClient(
            base_url, auth=(s.hepsiburada_username, s.hepsiburada_password),
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            rate_limiter=RateLimiter(getattr(s, "hepsiburada_rate_per_minute", 60), sleep=sleep),
            transport=self._transport, sleep=sleep)

    @property
    def oms(self) -> ResilientClient:
        if self._oms is None:
            self._oms = self._client(self.settings.hepsiburada_base_url)
        return self._oms

    @property
    def listing(self) -> ResilientClient:
        if self._listing is None:
            self._listing = self._client(getattr(self.settings, "hepsiburada_listing_base_url",
                                                 "https://listing-external.hepsiburada.com"))
        return self._listing

    def _packages_path(self) -> str:
        return f"/packages/merchantid/{self.store_external_id()}"

    @staticmethod
    def _fmt(dt: datetime) -> str:
        return dt.astimezone(TR_TZ).strftime("%Y-%m-%d %H:%M")

    def _fetch_packages(self, since: datetime, until: datetime) -> list[dict]:
        packages: list[dict] = []
        start = max(since, until - MAX_LOOKBACK)
        while start < until:
            end = min(start + WINDOW, until)
            offset = 0
            for _ in range(MAX_PAGES):
                data = self.oms.get_json(self._packages_path(), params={
                    "begindate": self._fmt(start), "enddate": self._fmt(end), "limit": PAGE_SIZE, "offset": offset})
                page = _items(data, "items", "data", "packages")
                packages.extend(page)
                total = _get(data, "totalcount", "totalCount")
                offset += len(page)
                if not page or len(page) < PAGE_SIZE or (total is not None and offset >= int(total)):
                    break
            start = end
        return packages

    def test_connection(self) -> ConnectionCheck:
        if not self.is_configured():
            return ConnectionCheck(False, "Bağlı değil: eksik bilgiler " + ", ".join(self.missing_credentials()))
        now = datetime.now(timezone.utc)
        try:
            data = self.oms.get_json(self._packages_path(), params={
                "begindate": self._fmt(now - timedelta(days=1)), "enddate": self._fmt(now), "limit": 1, "offset": 0})
            _items(data, "items", "data", "packages")
        except ConnectorError as exc:
            return ConnectionCheck(False, str(exc))
        return ConnectionCheck(True, "Bağlantı başarılı (salt okunur)")

    def fetch_orders(self, since: datetime, until: datetime) -> list[NormalizedOrder]:
        return self.normalize(self._fetch_packages(since, until))

    def fetch_listings(self) -> list[NormalizedListing]:
        path = f"/listings/merchantid/{self.store_external_id()}"
        out: list[dict] = []
        offset = 0
        for _ in range(MAX_PAGES):
            data = self.listing.get_json(path, params={"offset": offset, "limit": PAGE_SIZE})
            page = _items(data, "listings", "items", "data")
            out.extend(page)
            offset += len(page)
            total = _get(data, "totalCount", "totalcount")
            if not page or len(page) < PAGE_SIZE or (total is not None and offset >= int(total)):
                break
        return [self.normalize_listing(x) for x in out if _get(x, "hepsiburadaSku", "merchantSku")]

    @staticmethod
    def normalize_listing(x: dict) -> NormalizedListing:
        on_sale = not (_is_true(_get(x, "isSuspended")) or _is_true(_get(x, "isFrozen"))
                       or str(_get(x, "isSalable")).lower() == "false")
        stock = _get(x, "availableStock", "stock")
        return NormalizedListing(
            external_product_id=str(_get(x, "hepsiburadaSku", "merchantSku")),
            barcode=_get(x, "barcode"), sku=_get(x, "merchantSku"),
            title=str(_get(x, "productName", "name", "merchantSku") or ""),
            price=_money(_get(x, "price")), stock=int(stock) if stock is not None else None,
            status="on_sale" if on_sale else "not_on_sale")

    @staticmethod
    def normalize(packages: list[dict]) -> list[NormalizedOrder]:
        from .trendyol import aggregate_status

        grouped: dict[str, list[tuple[dict, dict | None]]] = {}
        for pkg in packages:
            items = _get(pkg, "items", "lineItems") or []
            pkg_order = _get(pkg, "orderNumber")
            if not items:
                key = str(pkg_order or _get(pkg, "packageNumber") or "")
                if key:
                    grouped.setdefault(key, []).append((pkg, None))
                continue
            for it in items:
                key = str(_get(it, "orderNumber") or pkg_order or _get(pkg, "packageNumber") or "")
                if key:
                    grouped.setdefault(key, []).append((pkg, it))

        orders: list[NormalizedOrder] = []
        for number, rows in grouped.items():
            pkgs: dict[str, dict] = {}
            for pkg, _ in rows:
                pkgs[str(_get(pkg, "packageNumber", "id") or number)] = pkg
            raw_statuses = [str(_get(p, "status") or "") for p in pkgs.values()]
            internal = [map_status(r) for r in raw_statuses]
            first = next(iter(pkgs.values()))
            reasons = [f"Hepsiburada statüsü eşlenemedi: {r or 'boş'}" for r, st in zip(raw_statuses, internal)
                       if st == S.NEEDS_REVIEW]
            lines: list[NormalizedLine] = []
            seen: set[str] = set()
            for pkg, it in rows:
                if it is None:
                    continue
                qty = int(_get(it, "quantity") or 1)
                unit = _money(_get(it, "unitPrice", "price"))
                if unit is None:
                    total = _money(_get(it, "totalPrice"))
                    unit = (total / qty).quantize(Decimal("0.01")) if total is not None else Decimal("0")
                line_id = str(_get(it, "lineItemId", "id") or f"{_get(pkg, 'packageNumber')}:{_get(it, 'sku')}")
                if line_id in seen:
                    continue
                seen.add(line_id)
                lines.append(NormalizedLine(
                    external_line_id=line_id,
                    sku=_get(it, "merchantSku", "merchantSKU", "sku"),
                    barcode=_get(it, "barcode"),
                    product_name=str(_get(it, "name", "productName") or ""),
                    quantity=qty, unit_price=unit,
                    vat_rate=_money(_get(it, "vatRate")),
                    line_status=_get(it, "status") or _get(pkg, "status"),
                ))
            shipments = [NormalizedShipment(
                external_package_id=pid, carrier=_get(p, "cargoCompany", "cargoCompanyName"),
                tracking_number=_get(p, "trackingNumber", "barcode"), marketplace_status=_get(p, "status"))
                for pid, p in pkgs.items()]
            orders.append(NormalizedOrder(
                external_order_id=number,
                marketplace_status=",".join(sorted({r for r in raw_statuses if r})),
                internal_status=aggregate_status(internal),
                order_date=parse_date(_get(first, "orderDate")) or datetime.now(timezone.utc),
                customer_name=_get(first, "customerName", "recipientName"),
                customer_city=_get(first, "shippingCity"),
                lines=lines, shipments=shipments, review_reason="; ".join(reasons) or None))
        return orders
