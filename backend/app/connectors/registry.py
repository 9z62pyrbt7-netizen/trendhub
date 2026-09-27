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


def get_connector(code: str, settings=None) -> MarketplaceConnector:
    try:
        cls = CONNECTOR_CLASSES[code]
    except KeyError:
        raise KeyError(f"Bilinmeyen pazaryeri: {code}") from None
    return cls(settings or get_settings())


def all_connectors(settings=None) -> list[MarketplaceConnector]:
    return [cls(settings or get_settings()) for cls in CONNECTOR_CLASSES.values()]
