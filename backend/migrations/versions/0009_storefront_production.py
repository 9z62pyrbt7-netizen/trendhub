"""0009 - web mağazası üretim özellikleri (yalnızca ekleme)

  storefront_products        + color, size, group_code (varyant/renk düzeltmesi), seo_title, seo_description
  storefront_customers       müşteri hesabı (argon2 parola özeti)
  storefront_customer_sessions  müşteri oturumları (DB'de yalnızca SHA-256 özeti)
  storefront_customer_addresses kayıtlı teslimat adresleri
  storefront_password_resets tek kullanımlık, süreli şifre sıfırlama anahtarları (özet)
  storefront_orders          + customer_id, notified (gönderilen bildirimlerin kaydı)
  notification_outbox        e-posta/SMS kuyruğu (sağlayıcı yoksa 'skipped')
  einvoice_records           e-fatura kayıtları (sağlayıcı yoksa oluşturulmaz)
  supplier_orders            + channel, payload, sent_at, method (web siparişlerinin tedarikçiye aktarımı)
  app_settings               storefront.supplier_forwarding.* , notifications.* , einvoice.* varsayılanları

Mevcut satırlar değiştirilmez/silinmez. Pazaryeri ve Trendyol → Çanta Bayim otomasyonuyla ilgili hiçbir tabloya
yazılan veri yoktur (supplier_orders'a yalnızca 'storefront' kanalı kayıtları eklenir).

Revision ID: 0009_storefront_production
Revises: 0008_storefront
Create Date: 2026-09-30
"""
from alembic import op

revision = "0009_storefront_production"
down_revision = "0008_storefront"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for col in ("color TEXT", "size TEXT", "group_code TEXT", "seo_title TEXT", "seo_description TEXT"):
        op.execute(f"ALTER TABLE storefront_products ADD COLUMN IF NOT EXISTS {col}")

    op.execute("""
    CREATE TABLE IF NOT EXISTS storefront_customers(
        id BIGSERIAL PRIMARY KEY,
        email TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        full_name TEXT NOT NULL,
        phone TEXT,
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        marketing_consent_at TIMESTAMPTZ,
        kvkk_consent_at TIMESTAMPTZ NOT NULL,
        failed_login_count INTEGER NOT NULL DEFAULT 0,
        locked_until TIMESTAMPTZ,
        last_login_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_storefront_customers_email ON storefront_customers(lower(email))")
    op.execute("""
    CREATE TABLE IF NOT EXISTS storefront_customer_sessions(
        id BIGSERIAL PRIMARY KEY,
        customer_id BIGINT NOT NULL REFERENCES storefront_customers(id),
        token_hash TEXT NOT NULL UNIQUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        expires_at TIMESTAMPTZ NOT NULL,
        revoked_at TIMESTAMPTZ
    )""")
    op.execute("""
    CREATE TABLE IF NOT EXISTS storefront_customer_addresses(
        id BIGSERIAL PRIMARY KEY,
        customer_id BIGINT NOT NULL REFERENCES storefront_customers(id),
        title TEXT NOT NULL DEFAULT 'Adresim',
        full_name TEXT NOT NULL,
        phone TEXT NOT NULL,
        city TEXT NOT NULL,
        district TEXT NOT NULL,
        address TEXT NOT NULL,
        postal_code TEXT,
        is_default BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_storefront_addresses_customer ON storefront_customer_addresses(customer_id)")
    op.execute("""
    CREATE TABLE IF NOT EXISTS storefront_password_resets(
        id BIGSERIAL PRIMARY KEY,
        customer_id BIGINT NOT NULL REFERENCES storefront_customers(id),
        token_hash TEXT NOT NULL UNIQUE,
        expires_at TIMESTAMPTZ NOT NULL,
        used_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")

    op.execute("ALTER TABLE storefront_orders ADD COLUMN IF NOT EXISTS customer_id BIGINT REFERENCES storefront_customers(id)")
    op.execute("ALTER TABLE storefront_orders ADD COLUMN IF NOT EXISTS notified JSONB NOT NULL DEFAULT '{}'::jsonb")
    op.execute("CREATE INDEX IF NOT EXISTS ix_storefront_orders_customer ON storefront_orders(customer_id)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS notification_outbox(
        id BIGSERIAL PRIMARY KEY,
        channel TEXT NOT NULL CHECK (channel IN ('email', 'sms')),
        recipient TEXT NOT NULL,
        template TEXT NOT NULL,
        subject TEXT,
        body TEXT NOT NULL,
        storefront_order_id BIGINT REFERENCES storefront_orders(id),
        dedupe_key TEXT,
        status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'sent', 'failed', 'skipped')),
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        provider TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        sent_at TIMESTAMPTZ
    )""")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_notification_outbox_dedupe ON notification_outbox(dedupe_key) WHERE dedupe_key IS NOT NULL")
    op.execute("CREATE INDEX IF NOT EXISTS ix_notification_outbox_status ON notification_outbox(status, created_at)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS einvoice_records(
        id BIGSERIAL PRIMARY KEY,
        order_id BIGINT NOT NULL UNIQUE REFERENCES orders(id),
        provider TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('queued', 'issued', 'failed', 'cancelled')),
        document_type TEXT,
        external_id TEXT,
        document_number TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        issued_at TIMESTAMPTZ
    )""")

    for col in ("channel TEXT", "payload JSONB", "sent_at TIMESTAMPTZ", "method TEXT"):
        op.execute(f"ALTER TABLE supplier_orders ADD COLUMN IF NOT EXISTS {col}")

    op.execute("""
    INSERT INTO app_settings(key, value) VALUES
        ('storefront.supplier_forwarding_mode', '"manual"'),
        ('storefront.notify_email', 'true'),
        ('storefront.notify_sms', 'false'),
        ('storefront.einvoice_enabled', 'false')
    ON CONFLICT (key) DO NOTHING""")


def downgrade() -> None:
    raise RuntimeError("Geri alma desteklenmez: migration'lar yalnızca ekleme yapar.")
