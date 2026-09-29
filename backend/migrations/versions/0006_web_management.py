"""0006 - web üzerinden yönetim: mağaza bağlantıları, uyarı merkezi, kontrollü yayın,
reklam merkezi, muhasebe rolü (yalnızca ekleme)

  marketplace_connections    pazaryeri bağlantısı; secret'lar ŞİFRELİ (Fernet), diğer alanlar JSONB
  alerts                     merkezi uyarı/sorun kaydı; açık uyarı fingerprint başına tekil
  publish_requests           yayın önizleme + tek kullanımlık onay (token hash'i)
  listing_publications       yayın denemeleri; idempotency_key tekil (aynı ürün iki kez gönderilmez)
  ad_accounts / ad_campaigns / ad_campaign_products / ad_spend / ad_performance   reklam merkezi
  supplier_products          + color, variant
  users.role                 + 'accountant' (CHECK kısıtı genişletilir; veri değişmez)
  app_settings               iş ayarı varsayılanları (varsa dokunulmaz)

Revision ID: 0006_web_management
Revises: 0005_listing_attributes
Create Date: 2026-09-28
"""
from alembic import op

revision = "0006_web_management"
down_revision = "0005_listing_attributes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------ mağaza bağlantıları
    op.execute("""
    CREATE TABLE IF NOT EXISTS marketplace_connections(
        marketplace_id BIGINT PRIMARY KEY REFERENCES marketplaces(id),
        settings JSONB NOT NULL DEFAULT '{}'::jsonb,
        secrets_enc TEXT,
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        status TEXT NOT NULL DEFAULT 'untested' CHECK (status IN ('untested','connected','failed','removed')),
        last_test_at TIMESTAMPTZ,
        last_test_ok BOOLEAN,
        last_test_message TEXT,
        -- Mağaza bazında yayın izni. Tek başına yeterli değildir: connector'ın yayın desteği
        -- doğrulanmış olmalı ve CONNECTOR_WRITE_ENABLED açık olmalıdır.
        write_enabled BOOLEAN NOT NULL DEFAULT FALSE,
        created_by BIGINT REFERENCES users(id),
        updated_by BIGINT REFERENCES users(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")

    # ----------------------------------------------------------- uyarı merkezi
    op.execute("""
    CREATE TABLE IF NOT EXISTS alerts(
        id BIGSERIAL PRIMARY KEY,
        category TEXT NOT NULL CHECK (category IN ('supplier','marketplace','order','shipping','product','system')),
        severity TEXT NOT NULL CHECK (severity IN ('critical','warning','info')),
        code TEXT NOT NULL,
        title TEXT NOT NULL,
        description TEXT,
        source TEXT,
        fingerprint TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved')),
        product_id BIGINT REFERENCES products(id),
        supplier_id BIGINT REFERENCES suppliers(id),
        supplier_product_id BIGINT REFERENCES supplier_products(id),
        marketplace_id BIGINT REFERENCES marketplaces(id),
        order_id BIGINT REFERENCES orders(id),
        link TEXT,
        details JSONB NOT NULL DEFAULT '{}'::jsonb,
        occurrences INTEGER NOT NULL DEFAULT 1,
        first_detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        resolved_at TIMESTAMPTZ,
        resolved_by BIGINT REFERENCES users(id),
        resolution TEXT
    )""")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_alerts_open_fingerprint ON alerts(fingerprint) WHERE status = 'open'")
    op.execute("CREATE INDEX IF NOT EXISTS ix_alerts_status ON alerts(status, severity, last_detected_at DESC)")

    # -------------------------------------------------------- kontrollü yayın
    op.execute("""
    CREATE TABLE IF NOT EXISTS publish_requests(
        id BIGSERIAL PRIMARY KEY,
        marketplace_id BIGINT NOT NULL REFERENCES marketplaces(id),
        token_hash TEXT NOT NULL UNIQUE,
        draft_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
        preview JSONB NOT NULL DEFAULT '{}'::jsonb,
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','confirmed','blocked','expired','cancelled')),
        requested_by BIGINT REFERENCES users(id),
        confirmed_by BIGINT REFERENCES users(id),
        expires_at TIMESTAMPTZ NOT NULL,
        confirmed_at TIMESTAMPTZ,
        result JSONB,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    op.execute("""
    CREATE TABLE IF NOT EXISTS listing_publications(
        id BIGSERIAL PRIMARY KEY,
        request_id BIGINT REFERENCES publish_requests(id),
        draft_id BIGINT NOT NULL REFERENCES listing_drafts(id),
        product_id BIGINT NOT NULL REFERENCES products(id),
        marketplace_id BIGINT NOT NULL REFERENCES marketplaces(id),
        idempotency_key TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL CHECK (status IN ('queued','sent','failed','blocked')),
        external_ref TEXT,
        message TEXT,
        attempts INTEGER NOT NULL DEFAULT 0,
        created_by BIGINT REFERENCES users(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")

    # --------------------------------------------------------------- reklamlar
    op.execute("""
    CREATE TABLE IF NOT EXISTS ad_accounts(
        id BIGSERIAL PRIMARY KEY,
        channel TEXT NOT NULL,
        name TEXT NOT NULL,
        api_status TEXT NOT NULL DEFAULT 'manual',
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (channel, name)
    )""")
    op.execute("""
    CREATE TABLE IF NOT EXISTS ad_campaigns(
        id BIGSERIAL PRIMARY KEY,
        account_id BIGINT NOT NULL REFERENCES ad_accounts(id),
        name TEXT NOT NULL,
        external_id TEXT,
        marketplace_id BIGINT REFERENCES marketplaces(id),
        status TEXT NOT NULL DEFAULT 'active',
        start_date DATE,
        end_date DATE,
        notes TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (account_id, name)
    )""")
    op.execute("""
    CREATE TABLE IF NOT EXISTS ad_campaign_products(
        campaign_id BIGINT NOT NULL REFERENCES ad_campaigns(id),
        product_id BIGINT NOT NULL REFERENCES products(id),
        PRIMARY KEY (campaign_id, product_id)
    )""")
    op.execute("""
    CREATE TABLE IF NOT EXISTS ad_spend(
        id BIGSERIAL PRIMARY KEY,
        campaign_id BIGINT NOT NULL REFERENCES ad_campaigns(id),
        spend_date DATE NOT NULL,
        amount NUMERIC(14,2) NOT NULL CHECK (amount >= 0),
        currency TEXT NOT NULL DEFAULT 'TRY',
        source TEXT NOT NULL DEFAULT 'manual',
        external_ref TEXT,
        note TEXT,
        created_by BIGINT REFERENCES users(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ad_spend_date ON ad_spend(spend_date)")
    op.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ux_ad_spend_external
                  ON ad_spend(source, external_ref) WHERE external_ref IS NOT NULL""")
    # Yalnızca platformun bildirdiği (veya kullanıcının girdiği) ölçümler; TrendHub tahmin ÜRETMEZ.
    op.execute("""
    CREATE TABLE IF NOT EXISTS ad_performance(
        id BIGSERIAL PRIMARY KEY,
        campaign_id BIGINT NOT NULL REFERENCES ad_campaigns(id),
        perf_date DATE NOT NULL,
        impressions BIGINT,
        clicks BIGINT,
        attributed_orders INTEGER,
        attributed_revenue NUMERIC(14,2),
        source TEXT NOT NULL DEFAULT 'manual',
        created_by BIGINT REFERENCES users(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (campaign_id, perf_date, source)
    )""")

    # ------------------------------------------------------ tedarikçi ürünleri
    for col in ("color TEXT", "variant TEXT"):
        op.execute(f"ALTER TABLE supplier_products ADD COLUMN IF NOT EXISTS {col}")

    # ---------------------------------------------------------- muhasebe rolü
    # CHECK kısıtı yalnızca GENİŞLETİLİR (mevcut tüm değerler geçerli kalır); satırlar değişmez.
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check")
    op.execute("""ALTER TABLE users ADD CONSTRAINT users_role_check
                  CHECK (role IN ('admin','operator','viewer','accountant'))""")

    # ------------------------------------------------------------ iş ayarları
    op.execute("""
    INSERT INTO app_settings(key, value) VALUES
        ('shipping.same_day_before', '"11:00"'),
        ('shipping.next_day_from', '"12:00"'),
        ('alerts.critical_stock_threshold', '2'),
        ('alerts.price_change_pct', '20'),
        ('alerts.shipping_overdue_hours', '24'),
        ('notifications.in_app', 'true')
    ON CONFLICT (key) DO NOTHING""")


def downgrade() -> None:
    raise RuntimeError("Geri alma desteklenmez: migration'lar yalnızca ekleme yapar.")
