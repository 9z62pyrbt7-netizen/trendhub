"""Tedarikçi kaynağını indirir (yalnızca GET, salt okunur).

Güvenlik:
  * Yalnızca http/https; kullanıcı panelden URL girdiği için iç ağ adresleri
    (localhost, 10/8, 172.16/12, 192.168/16, link-local, docker servisleri) varsayılan
    olarak ENGELLİDİR (SSRF). Gerekirse `SUPPLIER_ALLOW_PRIVATE_URLS=true`.
  * Yönlendirmeler en fazla 5 kez izlenir ve her adımda aynı kontrol yapılır.
  * Yanıt boyutu sınırlıdır (varsayılan 50 MB).
  * Hata mesajlarında gerçek URL yerine maskeli URL kullanılır; secret loglanmaz.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl

import httpx

from ..connectors.base import ConnectorError, RetryableError
from .secrets import mask_url

MAX_BYTES = int(os.environ.get("SUPPLIER_MAX_FEED_MB", "50")) * 1024 * 1024
TIMEOUT = httpx.Timeout(60.0, connect=15.0)
USER_AGENT = "TrendHub-SupplierSync/1.0 (+read-only)"


class FetchError(ConnectorError):
    """Kalıcı indirme hatası (yeniden denemek düzeltmez)."""
    retryable = False


@dataclass
class SourceAuth:
    auth_type: str = "none"          # none | basic | bearer | header | query
    username: str | None = None
    param_name: str | None = None    # header adı veya query parametre adı
    secret: str | None = None


def _private_allowed() -> bool:
    return os.environ.get("SUPPLIER_ALLOW_PRIVATE_URLS", "").lower() in ("1", "true", "yes")


def check_url(url: str) -> None:
    try:
        parts = urlsplit(url)
    except ValueError:
        raise FetchError("Geçersiz URL") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FetchError("URL http:// veya https:// ile başlamalı")
    if _private_allowed():
        return
    host = parts.hostname
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80),
                                   type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise RetryableError(f"Tedarikçi sunucusu çözümlenemedi: {host}") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast \
                or ip.is_unspecified:
            raise FetchError(f"İç ağ adresine istek engellendi ({host}). Tedarikçi URL'si internetten "
                             "erişilebilir olmalı.")


def build_request(url: str, auth: SourceAuth) -> tuple[str, dict, tuple | None]:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/xml, application/json, text/csv, */*"}
    basic = None
    if auth.auth_type == "basic":
        basic = (auth.username or "", auth.secret or "")
    elif auth.auth_type == "bearer" and auth.secret:
        headers["Authorization"] = f"Bearer {auth.secret}"
    elif auth.auth_type == "header" and auth.secret and auth.param_name:
        headers[auth.param_name] = auth.secret
    elif auth.auth_type == "query" and auth.secret and auth.param_name:
        parts = urlsplit(url)
        q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != auth.param_name]
        q.append((auth.param_name, auth.secret))
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), ""))
    return url, headers, basic


def fetch(url: str, auth: SourceAuth | None = None, *, transport: httpx.BaseTransport | None = None) -> bytes:
    auth = auth or SourceAuth()
    shown = mask_url(url)
    req_url, headers, basic = build_request(url, auth)
    with httpx.Client(timeout=TIMEOUT, follow_redirects=False, transport=transport, auth=basic) as client:
        for _ in range(6):
            if transport is None:
                check_url(req_url)
            try:
                with client.stream("GET", req_url, headers=headers) as resp:
                    if resp.is_redirect:
                        loc = resp.headers.get("location")
                        if not loc:
                            raise FetchError(f"Yönlendirme adresi yok ({shown})")
                        req_url = str(resp.url.join(loc))
                        continue
                    if resp.status_code in (401, 403):
                        raise FetchError(f"Tedarikçi erişimi reddetti (HTTP {resp.status_code}). "
                                         "Kimlik bilgilerini kontrol edin.")
                    if resp.status_code in (408, 425, 429) or resp.status_code >= 500:
                        raise RetryableError(f"Tedarikçi sunucusu geçici hata verdi (HTTP {resp.status_code})")
                    if resp.status_code >= 400:
                        raise FetchError(f"Tedarikçi kaynağı bulunamadı/okunamadı (HTTP {resp.status_code}, {shown})")
                    chunks, size = [], 0
                    for chunk in resp.iter_bytes():
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise FetchError(f"Kaynak çok büyük (> {MAX_BYTES // (1024 * 1024)} MB)")
                        chunks.append(chunk)
                    return b"".join(chunks)
            except httpx.TimeoutException:
                raise RetryableError(f"Tedarikçi zaman aşımı ({shown})") from None
            except httpx.TransportError as exc:
                raise RetryableError(f"Tedarikçiye bağlanılamadı ({shown}): {exc.__class__.__name__}") from None
        raise FetchError("Çok fazla yönlendirme")
