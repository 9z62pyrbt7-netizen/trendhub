"""Web mağazası ayarları (app_settings tablosundaki `storefront.*` anahtarları).

Panelden (Web Sitesi → Ayarlar) düzenlenir. Secret içermez: ödeme sağlayıcısı anahtarları yalnızca
sunucu ortam değişkenlerindedir.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import get_settings

# Yasal sayfalar: slug -> (başlık, checkout için zorunlu mu)
LEGAL_PAGES: dict[str, tuple[str, bool]] = {
    "mesafeli-satis-sozlesmesi": ("Mesafeli Satış Sözleşmesi", True),
    "on-bilgilendirme-formu": ("Ön Bilgilendirme Formu", True),
    "kvkk-aydinlatma-metni": ("KVKK Aydınlatma Metni", True),
    "iade-ve-degisim": ("İade ve Değişim Koşulları", True),
    "gizlilik-politikasi": ("Gizlilik ve Çerez Politikası", False),
    "teslimat-bilgileri": ("Teslimat Bilgileri", False),
    "hakkimizda": ("Hakkımızda", False),
}
SELLER_FIELDS: dict[str, tuple[str, bool]] = {
    "title": ("Satıcı unvanı", True),
    "address": ("Adres", True),
    "phone": ("Telefon", True),
    "email": ("E-posta", True),
    "tax_office": ("Vergi dairesi", False),
    "tax_number": ("Vergi numarası", False),
    "mersis": ("MERSİS numarası", False),
    "kep": ("KEP adresi", False),
}
SOCIAL_FIELDS = ("instagram", "tiktok", "facebook", "pinterest", "youtube", "whatsapp")

KEYS = ("enabled", "auto_publish", "hero_product_id", "stock_buffer", "committed_window_hours", "shipping_fee",
        "free_shipping_threshold", "bank_transfer_enabled", "bank_transfer_iban", "bank_transfer_account_name",
        "bank_transfer_bank_name", "bank_transfer_days", "cash_on_delivery_enabled", "cash_on_delivery_fee",
        "seller", "legal", "announcement", "social")


def _dec(v, default: str = "0") -> Decimal:
    try:
        return Decimal(str(v if v not in (None, "") else default))
    except (InvalidOperation, ValueError):
        return Decimal(default)


@dataclass
class StoreConfig:
    enabled: bool = True
    auto_publish: bool = True
    hero_product_id: int | None = None
    stock_buffer: int = 0
    committed_window_hours: int = 48
    shipping_fee: Decimal = Decimal("0")
    free_shipping_threshold: Decimal | None = None
    bank_transfer_enabled: bool = False
    bank_transfer_iban: str = ""
    bank_transfer_account_name: str = ""
    bank_transfer_bank_name: str = ""
    bank_transfer_days: int = 3
    cash_on_delivery_enabled: bool = False
    cash_on_delivery_fee: Decimal = Decimal("0")
    seller: dict = field(default_factory=dict)
    legal: dict = field(default_factory=dict)
    announcement: str = ""
    social: dict = field(default_factory=dict)

    # ---- türetilmiş
    @property
    def bank_transfer_available(self) -> bool:
        return bool(self.bank_transfer_enabled and self.bank_transfer_iban and self.bank_transfer_account_name)

    @property
    def card_available(self) -> bool:
        from .payments import card_provider
        return card_provider() is not None

    def payment_methods(self) -> list[dict]:
        out = []
        if self.card_available:
            from .payments import card_provider
            out.append({"code": "card", "label": "Kredi / Banka Kartı", "note": f"{card_provider().name} güvenli ödeme sayfası"})
        if self.bank_transfer_available:
            out.append({"code": "bank_transfer", "label": "Havale / EFT",
                        "note": f"Siparişiniz ödemeniz hesabımıza ulaştığında hazırlanır ({self.bank_transfer_days} gün içinde)."})
        if self.cash_on_delivery_enabled:
            fee = f" (+{fmt_try(self.cash_on_delivery_fee)} hizmet bedeli)" if self.cash_on_delivery_fee > 0 else ""
            out.append({"code": "cash_on_delivery", "label": "Kapıda Ödeme", "note": "Ödemeyi teslimatta yaparsınız" + fee + "."})
        return out

    def shipping_for(self, items_total: Decimal) -> Decimal:
        if self.free_shipping_threshold is not None and items_total >= self.free_shipping_threshold:
            return Decimal("0")
        return self.shipping_fee

    def checkout_blockers(self) -> list[str]:
        """Sipariş alınabilmesi için eksik olanlar (panelde ve checkout'ta gösterilir)."""
        out = []
        if not self.enabled:
            out.append("Web mağazası kapalı.")
        if not self.payment_methods():
            out.append("Etkin ödeme yöntemi yok (kart sağlayıcısı, Havale/EFT veya kapıda ödeme).")
        missing_seller = [label for k, (label, req) in SELLER_FIELDS.items() if req and not str(self.seller.get(k) or "").strip()]
        if missing_seller:
            out.append("Satıcı bilgileri eksik: " + ", ".join(missing_seller) + ".")
        missing_legal = [title for slug, (title, req) in LEGAL_PAGES.items() if req and not str(self.legal.get(slug) or "").strip()]
        if missing_legal:
            out.append("Yasal metinler eksik: " + ", ".join(missing_legal) + ".")
        return out

    @property
    def checkout_ready(self) -> bool:
        return not self.checkout_blockers()


def load(conn: Connection) -> StoreConfig:
    raw = {r.key.removeprefix("storefront."): r.value
           for r in conn.execute(text("SELECT key, value FROM app_settings WHERE key LIKE 'storefront.%'"))}
    c = StoreConfig()
    c.enabled = bool(raw.get("enabled", True))
    c.auto_publish = bool(raw.get("auto_publish", True))
    hp = raw.get("hero_product_id")
    c.hero_product_id = int(hp) if isinstance(hp, (int, str)) and str(hp).isdigit() else None
    c.stock_buffer = max(0, int(raw.get("stock_buffer") or 0))
    c.committed_window_hours = max(1, int(raw.get("committed_window_hours") or 48))
    c.shipping_fee = _dec(raw.get("shipping_fee"))
    fst = raw.get("free_shipping_threshold")
    c.free_shipping_threshold = _dec(fst) if fst not in (None, "") else None
    c.bank_transfer_enabled = bool(raw.get("bank_transfer_enabled", False))
    c.bank_transfer_iban = str(raw.get("bank_transfer_iban") or "")
    c.bank_transfer_account_name = str(raw.get("bank_transfer_account_name") or "")
    c.bank_transfer_bank_name = str(raw.get("bank_transfer_bank_name") or "")
    c.bank_transfer_days = max(1, int(raw.get("bank_transfer_days") or 3))
    c.cash_on_delivery_enabled = bool(raw.get("cash_on_delivery_enabled", False))
    c.cash_on_delivery_fee = _dec(raw.get("cash_on_delivery_fee"))
    c.seller = raw.get("seller") if isinstance(raw.get("seller"), dict) else {}
    c.legal = raw.get("legal") if isinstance(raw.get("legal"), dict) else {}
    c.announcement = str(raw.get("announcement") or "")
    c.social = raw.get("social") if isinstance(raw.get("social"), dict) else {}
    return c


# ------------------------------------------------------------------ doğrulama (panel)
IBAN_RE = re.compile(r"^TR\d{24}$")


def validate(values: dict) -> dict:
    """Panelden gelen ayarları doğrular; yalnızca bilinen anahtarlar kabul edilir."""
    out: dict = {}
    for k, v in values.items():
        if k not in KEYS:
            raise ValueError(f"Bilinmeyen ayar: {k}")
        if k in ("enabled", "auto_publish", "bank_transfer_enabled", "cash_on_delivery_enabled"):
            if not isinstance(v, bool):
                raise ValueError(f"{k}: evet/hayır olmalı")
            out[k] = v
        elif k == "hero_product_id":
            out[k] = None if v in (None, "", 0) else int(v)
        elif k in ("stock_buffer", "committed_window_hours", "bank_transfer_days"):
            n = int(v)
            if n < 0 or n > 10000:
                raise ValueError(f"{k}: geçersiz sayı")
            out[k] = n
        elif k in ("shipping_fee", "cash_on_delivery_fee", "free_shipping_threshold"):
            if k == "free_shipping_threshold" and v in (None, ""):
                out[k] = None
                continue
            d = _dec(v, "-1")
            if d < 0 or d > Decimal("100000"):
                raise ValueError(f"{k}: tutar 0 veya pozitif olmalı")
            out[k] = str(d.quantize(Decimal("0.01")))
        elif k == "bank_transfer_iban":
            iban = re.sub(r"\s+", "", str(v or "")).upper()
            if iban and not IBAN_RE.match(iban):
                raise ValueError("IBAN TR ile başlayan 26 karakter olmalı")
            out[k] = iban
        elif k in ("bank_transfer_account_name", "bank_transfer_bank_name", "announcement"):
            s = str(v or "").strip()
            if len(s) > 200:
                raise ValueError(f"{k}: en fazla 200 karakter")
            out[k] = s
        elif k == "seller":
            if not isinstance(v, dict):
                raise ValueError("Satıcı bilgileri geçersiz")
            out[k] = {f: str(v.get(f) or "").strip()[:500] for f in SELLER_FIELDS}
        elif k == "legal":
            if not isinstance(v, dict):
                raise ValueError("Yasal metinler geçersiz")
            out[k] = {slug: str(v.get(slug) or "")[:60000] for slug in LEGAL_PAGES}
        elif k == "social":
            if not isinstance(v, dict):
                raise ValueError("Sosyal medya bilgileri geçersiz")
            soc = {}
            for f in SOCIAL_FIELDS:
                s = str(v.get(f) or "").strip()
                if f == "whatsapp":
                    s = re.sub(r"\D", "", s)
                elif s and not s.startswith("https://"):
                    raise ValueError(f"{f}: https:// ile başlayan adres girin")
                soc[f] = s[:300]
            out[k] = soc
    return out


def fmt_try(amount) -> str:
    """Türkçe para biçimi: 1.299,90 ₺"""
    d = _dec(amount).quantize(Decimal("0.01"))
    sign = "-" if d < 0 else ""
    whole, frac = f"{abs(d):.2f}".split(".")
    whole = f"{int(whole):,}".replace(",", ".")
    return f"{sign}{whole},{frac} ₺"


def base_url(request=None) -> str:
    configured = get_settings().storefront_base_url.strip().rstrip("/")
    if configured:
        return configured
    if request is not None:
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme
        host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
        return f"{proto}://{host}"
    return ""
