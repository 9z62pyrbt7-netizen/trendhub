"""0008 - Trendçantanız web mağazası (storefront) satış kanalı (yalnızca ekleme)

  marketplaces            + 'storefront' (Trendçantanız Web) satış kanalı; siparişleri orders tablosunda
                            diğer pazaryerlerinden ayrılır (stores.marketplace_id → marketplaces.code)
  storefront_products     ürün başına web yayını ayarı (yayında mı, başlık/açıklama, üstü çizili fiyat, öne çıkan)
  storefront_carts        sunucu tarafı sepet (çerezde yalnızca rastgele anahtar; DB'de SHA-256 özeti)
  storefront_cart_items
  storefront_orders       web siparişi: müşteri/teslimat/ödeme bilgisi, onay kayıtları, orders satırına bağlantı
  stock_reservations      ödeme bekleyen kart siparişleri için süreli stok ayırma
  storefront_payment_events  ödeme sağlayıcısı geri bildirimlerinin denetim kaydı
  app_settings            storefront.* varsayılanları (varsa dokunulmaz)

Pazaryeri tabloları, siparişler ve stok değerleri DEĞİŞTİRİLMEZ; hiçbir satır silinmez.

Revision ID: 0008_storefront
Revises: 0007_supplier_import
Create Date: 2026-09-30
"""
from alembic import op

revision = "0008_storefront"
down_revision = "0007_supplier_import"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""INSERT INTO marketplaces(code, name, enabled)
                  VALUES ('storefront', 'Trendçantanız Web', TRUE) ON CONFLICT (code) DO NOTHING""")

    op.execute("""
    CREATE TABLE IF NOT EXISTS storefront_products(
        product_id BIGINT PRIMARY KEY REFERENCES products(id),
        published BOOLEAN NOT NULL DEFAULT TRUE,
        title TEXT,
        description TEXT,
        compare_at_price NUMERIC(14,2) CHECK (compare_at_price IS NULL OR compare_at_price > 0),
        featured BOOLEAN NOT NULL DEFAULT FALSE,
        sort_order INTEGER NOT NULL DEFAULT 0,
        updated_by BIGINT REFERENCES users(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")

    op.execute("""
    CREATE TABLE IF NOT EXISTS storefront_carts(
        id BIGSERIAL PRIMARY KEY,
        token_hash TEXT NOT NULL UNIQUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        expires_at TIMESTAMPTZ NOT NULL
    )""")
    op.execute("""
    CREATE TABLE IF NOT EXISTS storefront_cart_items(
        cart_id BIGINT NOT NULL REFERENCES storefront_carts(id) ON DELETE CASCADE,
        product_id BIGINT NOT NULL REFERENCES products(id),
        quantity INTEGER NOT NULL CHECK (quantity > 0 AND quantity <= 99),
        added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (cart_id, product_id)
    )""")

    op.execute("""
    CREATE TABLE IF NOT EXISTS storefront_orders(
        id BIGSERIAL PRIMARY KEY,
        public_code TEXT NOT NULL UNIQUE,
        access_token_hash TEXT NOT NULL,
        order_id BIGINT UNIQUE REFERENCES orders(id),
        status TEXT NOT NULL CHECK (status IN ('pending_payment','awaiting_payment','paid','cash_on_delivery',
                                               'payment_failed','cancelled','expired')),
        payment_method TEXT NOT NULL CHECK (payment_method IN ('card','bank_transfer','cash_on_delivery')),
        payment_provider TEXT,
        payment_reference TEXT,
        full_name TEXT NOT NULL,
        email TEXT NOT NULL,
        phone TEXT NOT NULL,
        city TEXT NOT NULL,
        district TEXT NOT NULL,
        address TEXT NOT NULL,
        postal_code TEXT,
        billing JSONB NOT NULL DEFAULT '{}'::jsonb,
        customer_note TEXT,
        lines JSONB NOT NULL,
        items_total NUMERIC(14,2) NOT NULL,
        shipping_fee NUMERIC(14,2) NOT NULL DEFAULT 0,
        total NUMERIC(14,2) NOT NULL,
        currency TEXT NOT NULL DEFAULT 'TRY',
        consents JSONB NOT NULL DEFAULT '{}'::jsonb,
        ip TEXT,
        paid_at TIMESTAMPTZ,
        cancelled_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_storefront_orders_status ON storefront_orders(status, created_at)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS stock_reservations(
        id BIGSERIAL PRIMARY KEY,
        product_id BIGINT NOT NULL REFERENCES products(id),
        storefront_order_id BIGINT REFERENCES storefront_orders(id),
        quantity INTEGER NOT NULL CHECK (quantity > 0),
        expires_at TIMESTAMPTZ NOT NULL,
        released_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    op.execute("""CREATE INDEX IF NOT EXISTS ix_stock_reservations_active
                  ON stock_reservations(product_id) WHERE released_at IS NULL""")

    op.execute("""
    CREATE TABLE IF NOT EXISTS storefront_payment_events(
        id BIGSERIAL PRIMARY KEY,
        storefront_order_id BIGINT REFERENCES storefront_orders(id),
        provider TEXT NOT NULL,
        kind TEXT NOT NULL,
        verified BOOLEAN NOT NULL DEFAULT FALSE,
        details JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")

    # Kanallar arası kullanılabilir stok ve çok satanlar sorguları ürün bazında sipariş kalemi okur.
    op.execute("CREATE INDEX IF NOT EXISTS ix_order_items_product ON order_items(product_id)")

    # Web kanalında pazaryeri komisyonu / hizmet bedeli yoktur; ödeme sağlayıcısı komisyonu panelden girilir.
    op.execute("""
    INSERT INTO app_settings(key, value) VALUES
        ('finance.commission_rate.storefront', '0'),
        ('storefront.enabled', 'true'),
        ('storefront.auto_publish', 'true'),
        ('storefront.hero_product_id', 'null'),
        ('storefront.stock_buffer', '0'),
        ('storefront.committed_window_hours', '48'),
        ('storefront.shipping_fee', '0'),
        ('storefront.free_shipping_threshold', 'null'),
        ('storefront.bank_transfer_enabled', 'false'),
        ('storefront.bank_transfer_iban', '""'),
        ('storefront.bank_transfer_account_name', '""'),
        ('storefront.bank_transfer_bank_name', '""'),
        ('storefront.bank_transfer_days', '3'),
        ('storefront.cash_on_delivery_enabled', 'false'),
        ('storefront.cash_on_delivery_fee', '0'),
        ('storefront.seller', '{}'),
        ('storefront.legal', '{}'),
        ('storefront.announcement', '""'),
        ('storefront.social', '{}')
    ON CONFLICT (key) DO NOTHING""")


def downgrade() -> None:
    raise RuntimeError("Geri alma desteklenmez: migration'lar yalnızca ekleme yapar.")
