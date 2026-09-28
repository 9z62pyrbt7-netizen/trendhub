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

    def _clean_arg(self, a):
        if isinstance(a, str):
            return self._clean(a)
        if isinstance(a, (int, float, bool)) or a is None:
            return a
        text = str(a)
        cleaned = self._clean(text)
        return cleaned if cleaned != text else a

    def filter(self, record: logging.LogRecord) -> bool:
        """Mesajı ve argümanları yerinde maskeler; args'ın yapısını (tuple/dict) KORUR.

        Bazı formatter'lar (ör. uvicorn access log) args'ı tuple olarak açar; args'ı
        None yapmak her istekte 'Logging error' üretir.
        """
        if not self.values:
            return True
        if isinstance(record.msg, str):
            record.msg = self._clean(record.msg)
        elif record.msg is not None:
            record.msg = self._clean_arg(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(self._clean_arg(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: self._clean_arg(v) for k, v in record.args.items()}
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self._clean(record.exc_text)
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
