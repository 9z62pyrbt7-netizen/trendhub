"""Tedarikçi credential'larının güvenli saklanması.

* Secret'lar (parola, token, API anahtarı) ve token içerebilen kaynak URL'leri
  veritabanında Fernet (AES-128-CBC + HMAC-SHA256) ile ŞİFRELİ tutulur.
  Anahtar `APP_SECRET` ortam değişkeninden türetilir; repoda veya DB'de durmaz.
* Alternatif: secret'ı sunucu ortam değişkeninden okumak (`secret_env`).
  Yalnızca `SUPPLIER_` ile başlayan adlara izin verilir; aksi hâlde
  `DATABASE_URL` gibi bir değer tedarikçi sunucusuna gönderilebilirdi.
* API yanıtlarına hiçbir zaman çözülmüş değer dönmez; yalnızca "tanımlı mı"
  bilgisi ve maskeli URL döner.
* Çözülen her secret log maskeleyicisine kaydedilir.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from cryptography.fernet import Fernet, InvalidToken

from ..config import get_settings, is_set
from ..logging_setup import register_secret

ENV_NAME_RE = re.compile(r"^SUPPLIER_[A-Z0-9_]{1,64}$")
MASK = "***"


class SecretStoreError(Exception):
    """Secret saklanamadı/okunamadı (kullanıcıya gösterilebilir Türkçe mesaj)."""


def _fernet(settings=None) -> Fernet:
    secret = (settings or get_settings()).app_secret
    if not is_set(secret) or len(secret) < 16:
        raise SecretStoreError("Sunucuda APP_SECRET tanımlı değil (en az 16 karakter). Tedarikçi URL'si ve "
                               "şifreleri şifrelenerek saklandığı için önce .env içinde APP_SECRET belirleyin.")
    key = hashlib.sha256(b"trendhub/supplier-secrets/v1\x00" + secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(value: str | None, settings=None) -> str | None:
    if value is None or value == "":
        return None
    return _fernet(settings).encrypt(value.encode()).decode()


def decrypt(token: str | None, settings=None) -> str | None:
    if not token:
        return None
    try:
        value = _fernet(settings).decrypt(token.encode()).decode()
    except InvalidToken:
        raise SecretStoreError("Kayıtlı tedarikçi bilgisi çözülemedi (APP_SECRET değişmiş olabilir). "
                               "Bağlantı bilgilerini yeniden girin.") from None
    register_secret(value)
    return value


def read_env_secret(name: str | None) -> str | None:
    if not name:
        return None
    if not ENV_NAME_RE.match(name):
        raise SecretStoreError("Ortam değişkeni adı SUPPLIER_ ile başlamalı (ör. SUPPLIER_ACME_TOKEN).")
    value = os.environ.get(name)
    if not value:
        raise SecretStoreError(f"Sunucuda {name} ortam değişkeni tanımlı değil.")
    register_secret(value)
    return value


def mask_url(url: str | None) -> str | None:
    """Arayüzde gösterilecek maskeli URL: kullanıcı bilgisi ve tüm sorgu değerleri gizlenir.

    Uzun yol parçaları (token olabilir) da kısaltılır."""
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return MASK
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    segments = []
    for seg in parts.path.split("/"):
        segments.append(seg[:4] + MASK if len(seg) >= 20 else seg)
    query = urlencode([(k, MASK) for k, _ in parse_qsl(parts.query, keep_blank_values=True)], safe="*")
    return urlunsplit((parts.scheme, host, "/".join(segments), query, ""))
