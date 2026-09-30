"""Uygulama ayarları.

Tüm gizli bilgiler (DB parolası, pazaryeri API anahtarları) yalnızca ortam
değişkenlerinden okunur; repoya hiçbir secret yazılmaz. Bkz. `.env.example`.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

PLACEHOLDER_VALUES = {"", "CHANGE_ME", "changeme", "change_me"}


def is_set(value: str | None) -> bool:
    return value is not None and value.strip() not in PLACEHOLDER_VALUES


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", case_sensitive=False)

    database_url: str
    app_secret: str = ""
    app_env: str = "production"

    # İlk admin hesabı; yalnızca kullanıcı tablosu boşsa kullanılır.
    admin_user: str = ""
    admin_password: str = ""

    # Oturum çerezi
    session_ttl_hours: int = 12
    # auto: çerez yalnızca HTTPS isteklerinde Secure işaretlenir (nginx X-Forwarded-Proto);
    # true: her zaman Secure (HTTPS zorunlu); false: hiçbir zaman (önerilmez).
    cookie_secure: str = "auto"
    # Boş bırakılırsa CORS kapalıdır (nginx ile aynı origin'den servis edilir).
    cors_origins: str = ""

    # Worker
    worker_poll_seconds: float = 5.0
    sync_interval_minutes: int = 15
    job_max_attempts: int = 6

    # Pazaryerine yazma işlemleri (stok/fiyat/statü güncelleme) varsayılan
    # olarak KAPALI. Canlı Trendyol -> Çanta Bayim otomasyonuyla çakışmamak için
    # TrendHub yalnızca okuma yapar.
    connector_write_enabled: bool = False

    # Trendyol
    trendyol_seller_id: str = ""
    trendyol_api_key: str = ""
    trendyol_api_secret: str = ""
    trendyol_base_url: str = "https://apigw.trendyol.com"
    trendyol_rate_per_minute: int = 60
    # Ürün V1 servisi kapatılıyor, V2 şeması doğrulanmadı: varsayılan KAPALI.
    trendyol_listings_enabled: bool = False

    # Hepsiburada
    hepsiburada_merchant_id: str = ""
    hepsiburada_username: str = ""
    hepsiburada_password: str = ""
    hepsiburada_base_url: str = "https://oms-external.hepsiburada.com"
    hepsiburada_listing_base_url: str = "https://listing-external.hepsiburada.com"
    # Zorunlu User-Agent başlığı; boşsa entegrasyon kullanıcı adı kullanılır.
    hepsiburada_user_agent: str = ""
    hepsiburada_rate_per_minute: int = 60
    # İlan okuma: yanıt şeması doğrulanmadı, varsayılan KAPALI (açılırsa yine yalnızca GET).
    hepsiburada_listings_enabled: bool = False

    # Amazon SP-API (Türkiye pazaryeri: A33AVAJ2PDY3EV, EU bölgesi)
    amazon_sp_client_id: str = ""
    amazon_sp_client_secret: str = ""
    amazon_sp_refresh_token: str = ""
    amazon_sp_seller_id: str = ""
    amazon_sp_marketplace_id: str = "A33AVAJ2PDY3EV"
    amazon_sp_endpoint: str = "https://sellingpartnerapi-eu.amazon.com"

    # Trendçantanız web mağazası (storefront)
    # Kanonik adres (ör. https://www.trendcantaniz.com). Boşsa isteğin adresi kullanılır;
    # canlıda mutlaka doldurun (canonical, sitemap, OpenGraph bu adresi kullanır).
    storefront_base_url: str = ""
    # Ürün görselleri bu klasörde WebP olarak önbelleklenir (compose'da kalıcı volume).
    storefront_image_cache_dir: str = "/tmp/trendcantaniz-img"
    # Kartla ödeme sağlayıcısı kodu (app/storefront/payments.py). Hiçbir sağlayıcı uygulanmadığı
    # için boş kalır; kartla ödeme seçeneği sağlayıcı eklenene kadar gösterilmez.
    storefront_payment_provider: str = ""
    # PayTR iFrame API (STOREFRONT_PAYMENT_PROVIDER=paytr). Anahtarlar yalnızca sunucu .env'inde tutulur.
    paytr_merchant_id: str = ""
    paytr_merchant_key: str = ""
    paytr_merchant_salt: str = ""
    # 1: PayTR test modu (yalnızca test kartları). Canlıda 0 olmalı.
    paytr_test_mode: bool = False
    paytr_api_url: str = "https://www.paytr.com"
    # iyzico Ödeme Formu (STOREFRONT_PAYMENT_PROVIDER=iyzico). Sandbox: https://sandbox-api.iyzipay.com
    iyzico_api_key: str = ""
    iyzico_secret_key: str = ""
    iyzico_base_url: str = "https://api.iyzipay.com"

    # Bildirimler: SMTP (e-posta). SMTP_HOST boşsa e-posta gönderilmez (kayıtlar 'skipped' olur).
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    # SMS sağlayıcısı: "" (kapalı) | "netgsm". Netgsm bilgileri yalnızca .env'de.
    sms_provider: str = ""
    netgsm_usercode: str = ""
    netgsm_password: str = ""
    netgsm_header: str = ""
    netgsm_api_url: str = "https://api.netgsm.com.tr/sms/send/get"
    # Mağaza sahibine yeni sipariş bildirimi (virgülle ayrılmış e-posta adresleri; boşsa gönderilmez).
    storefront_order_alert_emails: str = ""

    # E-fatura/e-arşiv sağlayıcısı: "" (kapalı). Uygulanmış sağlayıcı yoktur; bkz. app/services/einvoice.py
    einvoice_provider: str = ""

    @property
    def sqlalchemy_url(self) -> str:
        url = self.database_url
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        if url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        return url


def cookie_secure_for(settings: "Settings", scheme: str) -> bool:
    v = str(settings.cookie_secure).strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return scheme == "https"


@lru_cache
def get_settings() -> Settings:
    return Settings()
