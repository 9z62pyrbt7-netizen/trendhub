"""Log yapılandırması: credential değerleri hiçbir log satırında görünmez.

`RedactSecretsFilter`, ayarlardaki gizli değerleri (API anahtarları, parolalar,
token'lar, DB parolası) log mesajında ve hata izlerinde `***` ile değiştirir.
httpx/httpcore istek logları WARNING seviyesine çekilir (URL/başlık loglanmaz).
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

from .config import get_settings, is_set

SECRET_FIELDS = ("trendyol_api_key", "trendyol_api_secret", "hepsiburada_password", "amazon_sp_client_secret",
                 "amazon_sp_refresh_token", "admin_password", "app_secret")
MASK = "***"


def secret_values(settings=None) -> list[str]:
    s = settings or get_settings()
    values = [getattr(s, f, "") for f in SECRET_FIELDS]
    try:
        pw = urlsplit(s.database_url).password
        if pw:
            values.append(pw)
    except ValueError:
        pass
    # Çok kısa değerler maskelenirse log okunmaz hâle gelir; en az 4 karakter.
    return sorted({v for v in values if is_set(v) and len(v) >= 4}, key=len, reverse=True)


class RedactSecretsFilter(logging.Filter):
    def __init__(self, values: list[str]):
        super().__init__()
        self.values = values

    def _clean(self, text: str) -> str:
        for v in self.values:
            if v in text:
                text = text.replace(v, MASK)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.values:
            return True
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        cleaned = self._clean(msg)
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self._clean(record.exc_text)
        record.msg, record.args = cleaned, None
        return True


def _attach(logger: logging.Logger, redactor: RedactSecretsFilter) -> None:
    for handler in logger.handlers:
        if not any(isinstance(f, RedactSecretsFilter) for f in handler.filters):
            handler.addFilter(redactor)


def configure_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    redactor = RedactSecretsFilter(secret_values())
    _attach(root, redactor)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        _attach(logging.getLogger(name), redactor)
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
