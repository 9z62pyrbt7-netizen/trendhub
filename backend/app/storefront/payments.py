"""Ödeme soyutlaması.

Repoda gerçek bir sanal POS / ödeme sağlayıcısı entegrasyonu YOKTUR. Başarılı ödeme taklidi yapılmaz:
kartla ödeme seçeneği, `CardPaymentProvider` alt sınıfı yazılıp `PROVIDERS`'a eklenene ve
`STOREFRONT_PAYMENT_PROVIDER` ile seçilene kadar sitede gösterilmez.

Yeni sağlayıcı eklemek (ör. iyzico, PayTR, Param):
  1. `CardPaymentProvider` alt sınıfı yazın: `is_configured`, `start`, `verify_callback`.
     Anahtarlar YALNIZCA sunucu ortam değişkenlerinden okunur; tarayıcıya gönderilmez.
  2. `PROVIDERS` sözlüğüne kaydedin, `.env`'de `STOREFRONT_PAYMENT_PROVIDER=<kod>` yapın.
  3. Sağlayıcının geri dönüş adresi: `POST /odeme/geri-donus/<kod>` (imza doğrulaması sağlayıcıda).

Akış: sipariş `pending_payment` + süreli stok ayırma ile oluşur → `start()` yönlendirme adresi verir
→ sağlayıcı geri dönüşü `verify_callback()` ile SUNUCU tarafında doğrulanır → yalnızca doğrulanmış
başarılı ödemede TrendHub siparişi (orders) oluşturulur. Havale/EFT ve kapıda ödeme bir sağlayıcı
gerektirmez; panelden etkinleştirilir (bkz. store_config).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from ..config import get_settings


@dataclass
class PaymentStart:
    redirect_url: str | None = None      # sağlayıcının ödeme sayfası
    html: str | None = None              # sağlayıcının 3D form HTML'i (varsa)
    reference: str | None = None         # sağlayıcı işlem/token kimliği


@dataclass
class PaymentResult:
    verified: bool                        # imza/sorgu ile SUNUCU tarafında doğrulandı mı
    paid: bool
    reference: str | None = None
    amount: Decimal | None = None
    message: str = ""
    details: dict = field(default_factory=dict)


class PaymentNotConfigured(RuntimeError):
    pass


class CardPaymentProvider:
    code = ""
    name = ""

    def is_configured(self) -> bool:
        raise NotImplementedError

    def start(self, order: dict, *, return_url: str, callback_url: str) -> PaymentStart:
        """Ödeme oturumu başlatır. `order`: public_code, total (Decimal), currency, lines, müşteri bilgileri."""
        raise NotImplementedError

    def verify_callback(self, form: dict, order: dict) -> PaymentResult:
        """Sağlayıcı geri dönüşünü doğrular. Tutar ve sipariş kodu mutlaka sağlayıcıdan teyit edilmeli."""
        raise NotImplementedError


# Uygulanmış kart sağlayıcıları (şu an yok).
PROVIDERS: dict[str, type[CardPaymentProvider]] = {}


def card_provider() -> CardPaymentProvider | None:
    code = get_settings().storefront_payment_provider.strip().lower()
    cls = PROVIDERS.get(code)
    if cls is None:
        return None
    provider = cls()
    return provider if provider.is_configured() else None
