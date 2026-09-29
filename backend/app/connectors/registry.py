"""Connector kayıt defteri. Yeni pazaryeri = yeni sınıf + buraya bir satır."""
from __future__ import annotations

from ..config import get_settings
from .amazon_tr import AmazonTrConnector
from .base import MarketplaceConnector
from .hepsiburada import HepsiburadaConnector
from .trendyol import TrendyolConnector

CONNECTOR_CLASSES: dict[str, type[MarketplaceConnector]] = {
    TrendyolConnector.code: TrendyolConnector,
    HepsiburadaConnector.code: HepsiburadaConnector,
    AmazonTrConnector.code: AmazonTrConnector,
}


def _settings(settings):
    # Panelden girilen bağlantılar (şifreli, DB) ortam değişkenlerinin üzerine uygulanır.
    from ..services.marketplace_credentials import effective_settings
    return effective_settings(settings or get_settings())


def get_connector(code: str, settings=None) -> MarketplaceConnector:
    try:
        cls = CONNECTOR_CLASSES[code]
    except KeyError:
        raise KeyError(f"Bilinmeyen pazaryeri: {code}") from None
    return cls(_settings(settings))


def all_connectors(settings=None) -> list[MarketplaceConnector]:
    s = _settings(settings)
    return [cls(s) for cls in CONNECTOR_CLASSES.values()]
