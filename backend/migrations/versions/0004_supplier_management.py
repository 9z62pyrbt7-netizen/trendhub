"""0004 - çoklu tedarikçi yönetimi + ürün aktarımı (yalnızca ekleme)

Genel tedarikçi mimarisi: herhangi bir tedarikçiye özel tablo/kolon YOKTUR.

  suppliers                 (mevcut)  + öncelik, stok kuralları, senkron sıklığı, son durum
  supplier_connections      (yeni)    bağlantı türü (xml/api/csv/manual), şifreli URL ve secret
  supplier_field_mappings   (yeni)    tedarikçi alanı -> TrendHub alanı eşleştirmesi
  supplier_products         (mevcut)  tedarikçi teklifi; supplier_id zorunlu, katalog ürünü opsiyonel
  supplier_sync_runs        (yeni)    senkron çalıştırma geçmişi
  supplier_product_changes  (yeni)    yeni / fiyat / stok / kaynağında bulunamadı değişiklik günlüğü
  products                  (mevcut)  + tercih edilen tedarikçi ve seçim stratejisi
  marketplace_rules         (yeni)    pazaryeri bazında fiyat/komisyon/stok/zorunlu alan kuralları
  marketplace_category_mappings (yeni) kaynak kategori -> pazaryeri kategorisi + özellikler
  listing_drafts            (yeni)    (katalog ürünü, pazaryeri) başına TEK taslak ilan

Kurallar (0002 ile aynı): DROP / TRUNCATE / DELETE yok, kolon tipi değişmez.
Tek gevşetme: `supplier_products.product_id` NOT NULL kısıtı kaldırılır; tedarikçi
ürünü artık kataloğa bağlanmadan havuzda durabilir. Mevcut satırlar değişmez.

Revision ID: 0004_supplier_management
Revises: 0003_listing_details
Create Date: 2026-09-28
"""
from alembic import op

revision = "0004_supplier_management"
down_revision = "0003_listing_details"
branch_labels = None
depends_on = None


def safe_unique_index(name: str, table: str, columns: str, where: str | None = None) -> None:
    where_sql = f" WHERE {where}" if where else ""
    op.execute(f"""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = '{name}') THEN
            BEGIN
                EXECUTE 'CREATE UNIQUE INDEX {name} ON {table} ({columns}){where_sql}';
            EXCEPTION WHEN unique_violation THEN
                RAISE NOTICE 'TrendHub: {table} ({columns}) üzerinde tekrar eden kayıtlar var; unique yerine normal index oluşturuldu.';
                EXECUTE 'CREATE INDEX {name} ON {table} ({columns}){where_sql}';
            END;
        END IF;
    END $$;
    """)


def upgrade() -> None:
    # ------------------------------------------------------------- suppliers
    for col in (
        "priority INTEGER NOT NULL DEFAULT 100",
        "sync_interval_minutes INTEGER NOT NULL DEFAULT 0",
        "stock_rules JSONB NOT NULL DEFAULT '{}'::jsonb",
        "last_sync_at TIMESTAMPTZ",
        "last_sync_status TEXT",
        "last_sync_error TEXT",
    ):
        op.execute(f"ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS {col}")

    op.execute("""
    CREATE TABLE IF NOT EXISTS supplier_connections(
        supplier_id BIGINT PRIMARY KEY REFERENCES suppliers(id),
        integration_type TEXT NOT NULL DEFAULT 'manual'
            CHECK (integration_type IN ('xml','api','csv','manual')),
        -- URL token içerebilir: şifreli saklanır, arayüze yalnızca maskeli hâli döner.
        source_url_enc TEXT,
        source_url_display TEXT,
        auth_type TEXT NOT NULL DEFAULT 'none'
            CHECK (auth_type IN ('none','basic','bearer','header','query')),
        auth_username TEXT,
        auth_param_name TEXT,
        secret_enc TEXT,
        -- Alternatif: secret'ı sunucu ortam değişkeninden oku (yalnızca SUPPLIER_* adları).
        secret_env TEXT,
        record_path TEXT,
        options JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")

    op.execute("""
    CREATE TABLE IF NOT EXISTS supplier_field_mappings(
        id BIGSERIAL PRIMARY KEY,
        supplier_id BIGINT NOT NULL REFERENCES suppliers(id),
        target_field TEXT NOT NULL,
        source_path TEXT,
        default_value TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (supplier_id, target_field)
    )""")

    # ------------------------------------------------------ supplier_products
    op.execute("ALTER TABLE supplier_products ALTER COLUMN product_id DROP NOT NULL")
    for col in (
        "barcode TEXT",
        "model_code TEXT",
        "name TEXT",
        "category TEXT",
        "brand TEXT",
        "sale_price NUMERIC(14,2)",
        "currency TEXT DEFAULT 'TRY'",
        "stock INTEGER",
        "vat_rate NUMERIC(5,2)",
        "desi NUMERIC(8,2)",
        "description TEXT",
        "images JSONB DEFAULT '[]'::jsonb",
        "status TEXT DEFAULT 'active'",
        "content_hash TEXT",
        "first_seen_at TIMESTAMPTZ DEFAULT NOW()",
        "last_seen_at TIMESTAMPTZ",
        "missing_since TIMESTAMPTZ",
        "price_changed_at TIMESTAMPTZ",
        "stock_changed_at TIMESTAMPTZ",
        "created_at TIMESTAMPTZ DEFAULT NOW()",
    ):
        op.execute(f"ALTER TABLE supplier_products ADD COLUMN IF NOT EXISTS {col}")
    # Tedarikçi SKU'su yalnızca kendi tedarikçisi içinde tekildir (global kimlik değildir).
    safe_unique_index("ux_supplier_products_sku", "supplier_products", "supplier_id, supplier_sku",
                      "supplier_sku IS NOT NULL")
    op.execute("CREATE INDEX IF NOT EXISTS ix_supplier_products_barcode ON supplier_products(barcode)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_supplier_products_product ON supplier_products(product_id)")

    # --------------------------------------------------------------- products
    for col in (
        "preferred_supplier_id BIGINT REFERENCES suppliers(id)",
        "supplier_strategy TEXT DEFAULT 'manual'",
        "model_code TEXT",
        "description TEXT",
        "images JSONB DEFAULT '[]'::jsonb",
    ):
        op.execute(f"ALTER TABLE products ADD COLUMN IF NOT EXISTS {col}")

    # ------------------------------------------------------------ sync runs
    op.execute("""
    CREATE TABLE IF NOT EXISTS supplier_sync_runs(
        id BIGSERIAL PRIMARY KEY,
        supplier_id BIGINT NOT NULL REFERENCES suppliers(id),
        job_id BIGINT,
        trigger TEXT NOT NULL DEFAULT 'manual',
        status TEXT NOT NULL DEFAULT 'running'
            CHECK (status IN ('running','success','partial','failed')),
        started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        finished_at TIMESTAMPTZ,
        records_total INTEGER NOT NULL DEFAULT 0,
        created_count INTEGER NOT NULL DEFAULT 0,
        updated_count INTEGER NOT NULL DEFAULT 0,
        price_changed INTEGER NOT NULL DEFAULT 0,
        stock_changed INTEGER NOT NULL DEFAULT 0,
        missing_count INTEGER NOT NULL DEFAULT 0,
        reactivated_count INTEGER NOT NULL DEFAULT 0,
        linked_count INTEGER NOT NULL DEFAULT 0,
        error_count INTEGER NOT NULL DEFAULT 0,
        errors JSONB NOT NULL DEFAULT '[]'::jsonb,
        message TEXT
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_supplier_sync_runs ON supplier_sync_runs(supplier_id, started_at DESC)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS supplier_product_changes(
        id BIGSERIAL PRIMARY KEY,
        supplier_id BIGINT NOT NULL REFERENCES suppliers(id),
        supplier_product_id BIGINT NOT NULL REFERENCES supplier_products(id),
        run_id BIGINT REFERENCES supplier_sync_runs(id),
        kind TEXT NOT NULL CHECK (kind IN ('new','price','stock','missing','reactivated','content')),
        old_value TEXT,
        new_value TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    op.execute("""CREATE INDEX IF NOT EXISTS ix_supplier_changes
                  ON supplier_product_changes(supplier_id, created_at DESC)""")

    # ------------------------------------------------------ pazaryeri kuralları
    op.execute("""
    CREATE TABLE IF NOT EXISTS marketplace_rules(
        marketplace_id BIGINT PRIMARY KEY REFERENCES marketplaces(id),
        commission_rate NUMERIC(6,4),
        markup_rate NUMERIC(6,4) NOT NULL DEFAULT 0.30,
        fixed_cost NUMERIC(14,2) NOT NULL DEFAULT 0,
        shipping_cost NUMERIC(14,2) NOT NULL DEFAULT 0,
        min_margin_rate NUMERIC(6,4) NOT NULL DEFAULT 0.05,
        rounding TEXT NOT NULL DEFAULT 'x.90' CHECK (rounding IN ('none','integer','x.90','x.99')),
        stock_buffer INTEGER NOT NULL DEFAULT 0,
        min_stock INTEGER NOT NULL DEFAULT 1,
        max_stock INTEGER,
        title_max_length INTEGER,
        required_fields JSONB NOT NULL DEFAULT '["barcode","brand","category","images"]'::jsonb,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    op.execute("""INSERT INTO marketplace_rules(marketplace_id)
                  SELECT id FROM marketplaces ON CONFLICT (marketplace_id) DO NOTHING""")

    op.execute("""
    CREATE TABLE IF NOT EXISTS marketplace_category_mappings(
        id BIGSERIAL PRIMARY KEY,
        marketplace_id BIGINT NOT NULL REFERENCES marketplaces(id),
        source_category TEXT NOT NULL,
        target_category_id TEXT NOT NULL,
        target_category_name TEXT,
        attributes JSONB NOT NULL DEFAULT '{}'::jsonb,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (marketplace_id, source_category)
    )""")

    # ------------------------------------------------------------ taslak ilanlar
    op.execute("""
    CREATE TABLE IF NOT EXISTS listing_drafts(
        id BIGSERIAL PRIMARY KEY,
        product_id BIGINT NOT NULL REFERENCES products(id),
        marketplace_id BIGINT NOT NULL REFERENCES marketplaces(id),
        supplier_product_id BIGINT REFERENCES supplier_products(id),
        price NUMERIC(14,2),
        price_is_manual BOOLEAN NOT NULL DEFAULT FALSE,
        stock INTEGER,
        cost_basis NUMERIC(14,2),
        commission_rate NUMERIC(6,4),
        estimated_profit NUMERIC(14,2),
        estimated_margin NUMERIC(8,4),
        category_id TEXT,
        category_name TEXT,
        attributes JSONB NOT NULL DEFAULT '{}'::jsonb,
        status TEXT NOT NULL DEFAULT 'draft'
            CHECK (status IN ('draft','invalid','ready','cancelled')),
        errors JSONB NOT NULL DEFAULT '[]'::jsonb,
        warnings JSONB NOT NULL DEFAULT '[]'::jsonb,
        existing_listing_id BIGINT REFERENCES marketplace_listings(id),
        created_by BIGINT REFERENCES users(id),
        validated_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        -- Bir katalog ürünü bir pazaryerinde tek ilan: iki tedarikçide olsa bile.
        UNIQUE (product_id, marketplace_id)
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_listing_drafts_status ON listing_drafts(marketplace_id, status)")


def downgrade() -> None:
    raise RuntimeError("Geri alma desteklenmez: migration'lar yalnızca ekleme yapar.")
