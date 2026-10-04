"""Reklam platformu adaptörleri (temiz arayüz).

Ajanlar platforma doğrudan bağlanmaz; yalnızca bu arayüzü çağıran executor kullanılır. Bağlı olmayan platformda
SAHTE işlem yapılmaz: `connected()` False ise executor aksiyonu SKIPPED (manuel uygulama talimatıyla) kaydeder.

  * meta          Meta Marketing API — kimlik bilgisi (META_ACCESS_TOKEN + META_AD_ACCOUNT_ID) ve yazma uygulaması yok.
                  Bağlanacağı zaman yalnızca bu sınıfın metotları yazılır; ajan/CEO/risk motoru değişmez.
  * trendyol_ads  Trendyol satıcı API'sinde reklam uç noktası yok (resmî dokümantasyon, 2026-10).
"""
from __future__ import annotations

from decimal import Decimal

from ...config import get_settings, is_set


class AdsPlatformError(Exception):
    pass


class NotConnected(AdsPlatformError):
    pass


class AdsPlatformAdapter:
    code = "base"
    name = "Reklam platformu"

    def connected(self) -> bool:
        return False

    def not_connected_reason(self) -> str:
        return f"{self.name} bağlı değil"

    def set_daily_budget(self, external_campaign_id: str, amount: Decimal) -> dict:
        raise NotConnected(self.not_connected_reason())

    def pause(self, external_campaign_id: str) -> dict:
        raise NotConnected(self.not_connected_reason())

    def create_campaign(self, spec: dict) -> dict:
        raise NotConnected(self.not_connected_reason())


class MetaAdsAdapter(AdsPlatformAdapter):
    code = "meta"
    name = "Meta (Facebook/Instagram) Ads"
    # Yazma metotları (bütçe, durdurma, kampanya oluşturma) uygulanana kadar False: kimlik bilgisi olsa bile bağlı sayılmaz.
    WRITE_IMPLEMENTED = False

    def __init__(self, settings=None):
        self.settings = settings or get_settings()

    def connected(self) -> bool:
        s = self.settings
        has_creds = is_set(getattr(s, "meta_access_token", "")) and is_set(getattr(s, "meta_ad_account_id", ""))
        return bool(has_creds and self.WRITE_IMPLEMENTED)

    def not_connected_reason(self) -> str:
        s = self.settings
        if not (is_set(getattr(s, "meta_access_token", "")) and is_set(getattr(s, "meta_ad_account_id", ""))):
            return "Meta Marketing API bağlı değil (META_ACCESS_TOKEN / META_AD_ACCOUNT_ID yok)"
        return "Meta Marketing API yazma işlemleri henüz uygulanmadı (App Review + ads_management izni gerekir)"


class TrendyolAdsAdapter(AdsPlatformAdapter):
    code = "trendyol_ads"
    name = "Trendyol Reklam"

    def not_connected_reason(self) -> str:
        return "Trendyol satıcı API'sinde reklam uç noktası yok; reklam paneli üzerinden manuel uygulanır"


ADAPTERS = {"meta": MetaAdsAdapter, "instagram": MetaAdsAdapter, "facebook": MetaAdsAdapter,
            "trendyol_ads": TrendyolAdsAdapter, "trendyol": TrendyolAdsAdapter}


def adapter_for(channel: str | None) -> AdsPlatformAdapter:
    cls = ADAPTERS.get((channel or "").lower())
    return cls() if cls else AdsPlatformAdapter()


def status() -> list[dict]:
    out = []
    for code, cls in (("meta", MetaAdsAdapter), ("trendyol_ads", TrendyolAdsAdapter)):
        a = cls()
        out.append({"code": code, "name": a.name, "connected": a.connected(),
                    "reason": None if a.connected() else a.not_connected_reason()})
    return out
