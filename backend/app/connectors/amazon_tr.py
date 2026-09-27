"""Amazon.com.tr connector (Selling Partner API) — SALT OKUNUR.

Akış:
  1. LWA: refresh token ile erişim belirteci alınır
     (`POST https://api.amazon.com/auth/o2/token`). Bu, pazaryeri verisini
     değiştirmeyen tek POST isteğidir ve ayrı bir istemciyle yapılır.
  2. Orders API v0 (yalnızca GET):
       GET /orders/v0/orders?MarketplaceIds=…&LastUpdatedAfter=…   (NextToken ile sayfalama)
       GET /orders/v0/orders/{AmazonOrderId}/orderItems
  3. Senkronizasyon `LastUpdatedAfter` ile artımlıdır (incremental): yalnızca
     son senkrondan sonra değişen siparişler okunur.

Kargo takip bilgisi, müşteri adı (PII) ve komisyon Orders API'den gelmez;
bu yüzden sevkiyat oluşturulmaz, komisyon tahmini hesaplanır.

Not: Alan eşlemesi SP-API dokümantasyonuna göre yapılmıştır; canlı hesapla
doğrulanmamıştır.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx

from ..domain import order_status as S
from .base import (CAP_ORDERS_READ, AuthError, ConnectionCheck, ConnectorError, CredentialField,
                   MarketplaceConnector, NormalizedLine, NormalizedOrder)
from .http import RateLimiter, ResilientClient

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
LWA_URL = "https://api.amazon.com"
MAX_ORDER_PAGES = 200


def map_status(raw: str | None) -> str:
    return STATUS_MAP.get((raw or "").strip().lower(), S.NEEDS_REVIEW)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _amount(money: dict | None) -> Decimal | None:
    if not money or money.get("Amount") in (None, ""):
        return None
    return Decimal(str(money["Amount"]))


class AmazonTrConnector(MarketplaceConnector):
    code = "amazon_tr"
    name = "Amazon.com.tr"
    capabilities = frozenset({CAP_ORDERS_READ})
    incremental = True
    implementation_note = "Salt okunur sipariş senkronizasyonu; canlı hesapla henüz doğrulanmadı."
    credential_fields = [
        CredentialField("AMAZON_SP_SELLER_ID", "Seller ID", secret=False),
        CredentialField("AMAZON_SP_CLIENT_ID", "LWA Client ID", secret=False),
        CredentialField("AMAZON_SP_CLIENT_SECRET", "LWA Client Secret"),
        CredentialField("AMAZON_SP_REFRESH_TOKEN", "Refresh Token"),
    ]

    def __init__(self, settings, transport: httpx.BaseTransport | None = None, sleep=None,
                 clock=time.monotonic):
        super().__init__(settings)
        self._transport = transport
        self._sleep = sleep or time.sleep
        self._clock = clock
        self._token: str | None = None
        self._token_expires = 0.0
        self._api: ResilientClient | None = None
        self._orders_limiter = RateLimiter(1, burst=20, sleep=self._sleep)       # getOrders: 0.0167 rps, burst 20
        self._items_limiter = RateLimiter(30, burst=30, sleep=self._sleep)      # getOrderItems: 0.5 rps, burst 30

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

    # -- kimlik -----------------------------------------------------------------
    def _access_token(self) -> str:
        if self._token and self._clock() < self._token_expires - 60:
            return self._token
        s = self.settings
        auth = ResilientClient(LWA_URL, transport=self._transport, sleep=self._sleep, read_only=False)
        try:
            resp = auth.request("POST", "/auth/o2/token", data={
                "grant_type": "refresh_token", "refresh_token": s.amazon_sp_refresh_token,
                "client_id": s.amazon_sp_client_id, "client_secret": s.amazon_sp_client_secret,
            })
        except ConnectorError as exc:
            if "HTTP 400" in str(exc):
                raise AuthError("Amazon LWA: refresh token veya client bilgileri geçersiz") from None
            raise
        finally:
            auth.close()
        data = resp.json()
        if "access_token" not in data:
            raise AuthError("Amazon LWA yanıtında access_token yok")
        self._token = data["access_token"]
        self._token_expires = self._clock() + int(data.get("expires_in") or 3600)
        return self._token

    def _get(self, path: str, params: dict, limiter: RateLimiter) -> dict:
        if self._api is None:
            self._api = ResilientClient(self.settings.amazon_sp_endpoint, transport=self._transport,
                                        sleep=self._sleep, headers={"User-Agent": "TrendHub/2.0 (Language=Python)",
                                                                    "Accept": "application/json"})
        limiter.acquire()
        data = self._api.get_json(path, params=params, headers={"x-amz-access-token": self._access_token()})
        if not isinstance(data, dict) or "payload" not in data:
            raise ConnectorError("Amazon SP-API yanıtı beklenen formatta değil (payload yok)")
        return data["payload"]

    # -- işlemler ---------------------------------------------------------------
    def test_connection(self) -> ConnectionCheck:
        if not self.is_configured():
            return ConnectionCheck(False, "Bağlı değil: eksik bilgiler " + ", ".join(self.missing_credentials()))
        try:
            self._get("/orders/v0/orders", {
                "MarketplaceIds": self.settings.amazon_sp_marketplace_id,
                "LastUpdatedAfter": _iso(datetime.now(timezone.utc) - timedelta(days=1)),
                "MaxResultsPerPage": 1,
            }, self._orders_limiter)
        except ConnectorError as exc:
            return ConnectionCheck(False, str(exc))
        return ConnectionCheck(True, "Bağlantı başarılı (salt okunur)")

    def fetch_orders(self, since: datetime, until: datetime) -> list[NormalizedOrder]:
        raw_orders: list[dict] = []
        params = {"MarketplaceIds": self.settings.amazon_sp_marketplace_id,
                  "LastUpdatedAfter": _iso(since), "MaxResultsPerPage": 100}
        for _ in range(MAX_ORDER_PAGES):
            payload = self._get("/orders/v0/orders", params, self._orders_limiter)
            raw_orders.extend(payload.get("Orders") or [])
            token = payload.get("NextToken")
            if not token:
                break
            params = {"MarketplaceIds": self.settings.amazon_sp_marketplace_id, "NextToken": token}
        orders = []
        for o in raw_orders:
            items: list[dict] = []
            item_params: dict = {}
            for _ in range(20):
                p = self._get(f"/orders/v0/orders/{o['AmazonOrderId']}/orderItems", item_params, self._items_limiter)
                items.extend(p.get("OrderItems") or [])
                if not p.get("NextToken"):
                    break
                item_params = {"NextToken": p["NextToken"]}
            orders.append(self.normalize(o, items))
        return orders

    @staticmethod
    def normalize(o: dict, items: list[dict]) -> NormalizedOrder:
        raw = o.get("OrderStatus") or ""
        internal = map_status(raw)
        review = None
        lines = []
        for it in items:
            qty = int(it.get("QuantityOrdered") or 0)
            if qty <= 0:
                continue  # iptal edilmiş kalem
            total = _amount(it.get("ItemPrice"))
            discount = _amount(it.get("PromotionDiscount")) or Decimal("0")
            if total is None:
                unit = Decimal("0")
                if internal not in (S.NEW, S.CANCELLED):
                    review = "Amazon kalem fiyatı bildirmedi"
            else:
                unit = ((total - discount) / qty).quantize(Decimal("0.01"))
            lines.append(NormalizedLine(
                external_line_id=str(it.get("OrderItemId")),
                sku=it.get("SellerSKU"),
                barcode=None,
                product_name=it.get("Title") or it.get("ASIN") or "",
                quantity=qty,
                unit_price=unit,
                discount=discount,
            ))
        if internal == S.NEEDS_REVIEW:
            review = f"Amazon statüsü inceleme gerektiriyor: {raw or 'boş'}"
        address = o.get("ShippingAddress") or {}
        return NormalizedOrder(
            external_order_id=str(o["AmazonOrderId"]),
            marketplace_status=raw,
            internal_status=internal,
            order_date=_parse_dt(o.get("PurchaseDate")) or datetime.now(timezone.utc),
            currency=(o.get("OrderTotal") or {}).get("CurrencyCode") or "TRY",
            customer_city=address.get("City"),
            lines=lines,
            shipments=[],
            review_reason=review,
            last_modified=_parse_dt(o.get("LastUpdateDate")),
        )
