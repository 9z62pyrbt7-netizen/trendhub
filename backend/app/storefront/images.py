"""Ürün görseli optimizasyonu: imzalı, boyutlandıran, WebP'ye çeviren ve disk önbelleğine yazan proxy.

Ürün görselleri tedarikçi/pazaryeri CDN'lerinde (ör. XML beslemesindeki adresler) durur ve genellikle
büyük JPEG'lerdir. Mobil LCP için görsel, istenen genişlikte WebP olarak bir kez üretilip diskten servis
edilir (Cache-Control: 1 yıl, immutable).

Güvenlik: adres HMAC ile imzalanır (APP_SECRET'tan türetilen anahtar), yani bu uç nokta açık proxy
değildir; yalnızca sitenin kendi ürettiği adresler çalışır. İç ağ adresleri engellenir (SSRF),
boyut sınırı 15 MB, yalnızca GET.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import logging
import os
import tempfile
from pathlib import Path

import httpx

from ..config import get_settings, is_set

log = logging.getLogger("trendhub.storefront.images")
WIDTHS = (160, 320, 480, 640, 800, 1000, 1200, 1600)
MAX_BYTES = 15 * 1024 * 1024
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_transport: httpx.BaseTransport | None = None  # testler için


def _key() -> bytes | None:
    secret = get_settings().app_secret
    if not is_set(secret):
        return None
    return hashlib.sha256(("storefront-img:" + secret).encode()).digest()


def _sig(src: str, w: int) -> str:
    return hmac.new(_key(), f"{w}:{src}".encode(), hashlib.sha256).hexdigest()[:24]


def nearest_width(w: int) -> int:
    for x in WIDTHS:
        if x >= w:
            return x
    return WIDTHS[-1]


def img_url(src: str | None, w: int) -> str:
    """Şablonlarda kullanılan görsel adresi. APP_SECRET yoksa özgün adres döner."""
    if not src:
        return ""
    if not src.startswith(("http://", "https://")) or _key() is None:
        return src
    w = nearest_width(w)
    b = base64.urlsafe_b64encode(src.encode()).decode().rstrip("=")
    return f"/img/{w}/{_sig(src, w)}/{b}"


def srcset(src: str | None, widths=(320, 480, 640, 800, 1000, 1200)) -> str:
    if not src:
        return ""
    return ", ".join(f"{img_url(src, w)} {w}w" for w in widths)


def decode(w: int, sig: str, b: str) -> str | None:
    if w not in WIDTHS or _key() is None:
        return None
    try:
        src = base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)).decode()
    except Exception:  # noqa: BLE001
        return None
    if not hmac.compare_digest(_sig(src, w), sig):
        return None
    return src


def cache_path(src: str, w: int) -> Path:
    h = hashlib.sha256(src.encode()).hexdigest()
    return Path(get_settings().storefront_image_cache_dir) / h[:2] / f"{h}-{w}.webp"


def _download(src: str) -> bytes:
    from ..suppliers.fetch import check_url
    url = src
    with httpx.Client(timeout=TIMEOUT, follow_redirects=False, transport=_transport) as client:
        for _ in range(4):
            if _transport is None:
                check_url(url)
            with client.stream("GET", url, headers={"User-Agent": "Trendcantaniz-Image/1.0",
                                                     "Accept": "image/*"}) as resp:
                if resp.is_redirect and resp.headers.get("location"):
                    url = str(resp.url.join(resp.headers["location"]))
                    continue
                resp.raise_for_status()
                data, size = [], 0
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ValueError("görsel çok büyük")
                    data.append(chunk)
                return b"".join(data)
    raise ValueError("çok fazla yönlendirme")


def render(src: str, w: int) -> Path:
    """Önbellekte yoksa indirir, boyutlandırır, WebP olarak yazar; dosya yolunu döner."""
    path = cache_path(src, w)
    if path.exists():
        return path
    from PIL import Image, ImageOps
    raw = _download(src)
    with Image.open(io.BytesIO(raw)) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA" if "transparency" in im.info or im.mode in ("LA", "P") else "RGB")
        if im.width > w:
            im = im.resize((w, max(1, round(im.height * w / im.width))), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "WEBP", quality=80, method=4)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "wb") as f:
        f.write(buf.getvalue())
    os.replace(tmp, path)
    return path
