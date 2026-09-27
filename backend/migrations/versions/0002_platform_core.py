"""0002 - platform çekirdeği (yalnızca ekleme)

Kurallar:
  * DROP TABLE / DROP COLUMN / TRUNCATE / DELETE yok.
  * Mevcut kolonların tipi ve anlamı değiştirilmez. Örn. `orders.status`
    pazaryerinden gelen ham statü olarak kalır; iç statü için yeni
    `orders.internal_status` kolonu eklenir.
  * Mevcut veride çakışma olabilecek unique index'ler `safe_unique_index()`
    ile denenir; mevcut veride tekrar eden kayıt varsa index normal (unique
    olmayan) olarak oluşturulur ve NOTICE yazılır, veri silinmez.

Revision ID: 0002_platform_core
Revises: 0001_baseline
Create Date: 2026-09-27
"""
from alembic import op

revision = "0002_platform_core"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None

INTERNAL_STATUSES = (
    "new", "preparing", "sent_to_supplier", "awaiting_shipment", "shipped",
    "delivered", "cancelled", "returned", "needs_review",
)


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
    statuses = ",".join(f"'{s}'" for s in INTERNAL_STATUSES)

    # ------------------------------------------------------------------ auth
    op.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id BIGSERIAL PRIMARY KEY,
        username TEXT NOT NULL UNIQUE,
        full_name TEXT,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'viewer' CHECK (role IN ('admin','operator','viewer')),
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        failed_login_count INTEGER NOT NULL DEFAULT 0,
        locked_until TIMESTAMPTZ,
        last_login_at TIMESTAMPTZ,
        password_changed_at TIMESTAMPTZ DEFAULT NOW(),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    op.execute("""
    CREATE TABLE IF NOT EXISTS user_sessions(
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT NOT NULL REFERENCES users(id),
        token_hash TEXT NOT NULL UNIQUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        expires_at TIMESTAMPTZ NOT NULL,
        last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        revoked_at TIMESTAMPTZ,
        ip TEXT,
        user_agent TEXT
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_user_sessions_user ON user_sessions(user_id)")

    # ----------------------------------------------------------- audit / ops
    op.execute("""
    CREATE TABLE IF NOT EXISTS audit_logs(
        id BIGSERIAL PRIMARY KEY,
        occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        user_id BIGINT REFERENCES users(id),
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        entity_type TEXT,
        entity_id TEXT,
        ip TEXT,
        details JSONB NOT NULL DEFAULT '{}'::jsonb
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_audit_logs_time ON audit_logs(occurred_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_audit_logs_entity ON audit_logs(entity_type, entity_id)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS system_events(
        id BIGSERIAL PRIMARY KEY,
        occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        level TEXT NOT NULL CHECK (level IN ('info','warning','error','critical')),
        source TEXT NOT NULL,
        message TEXT NOT NULL,
        details JSONB NOT NULL DEFAULT '{}'::jsonb,
        fingerprint TEXT,
        occurrences INTEGER NOT NULL DEFAULT 1,
        last_occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        resolved_at TIMESTAMPTZ,
        resolved_by BIGINT REFERENCES users(id)
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_system_events_time ON system_events(last_occurred_at DESC)")
    op.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ux_system_events_open_fingerprint
                  ON system_events(fingerprint) WHERE resolved_at IS NULL AND fingerprint IS NOT NULL""")

    op.execute("""
    CREATE TABLE IF NOT EXISTS worker_heartbeats(
        worker_id TEXT PRIMARY KEY,
        hostname TEXT,
        started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        current_job_id BIGINT
    )""")

    op.execute("""
    CREATE TABLE IF NOT EXISTS app_settings(
        key TEXT PRIMARY KEY,
        value JSONB NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_by BIGINT REFERENCES users(id)
    )""")
    op.execute("""
    INSERT INTO app_settings(key, value) VALUES
        ('finance.commission_rate.trendyol', '0.20'),
        ('finance.commission_rate.hepsiburada', '0.20'),
        ('finance.commission_rate.amazon_tr', '0.15'),
        ('finance.service_fee_per_order', '0'),
        ('finance.default_shipping_cost', '0'),
        ('finance.include_vat', 'true'),
        ('stock.low_stock_threshold', '3')
    ON CONFLICT (key) DO NOTHING""")

    # ---------------------------------------------------------- marketplaces
    for col in (
        "last_check_at TIMESTAMPTZ",
        "last_check_ok BOOLEAN",
        "last_check_message TEXT",
        "last_sync_at TIMESTAMPTZ",
        "updated_at TIMESTAMPTZ DEFAULT NOW()",
    ):
        op.execute(f"ALTER TABLE marketplaces ADD COLUMN IF NOT EXISTS {col}")

    op.execute("ALTER TABLE stores ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW()")
    safe_unique_index("ux_stores_marketplace_external", "stores", "marketplace_id, external_id",
                      "external_id IS NOT NULL")

    # -------------------------------------------------------------- products
    for col in (
        "brand TEXT",
        "category TEXT",
        "image_url TEXT",
        "vat_rate NUMERIC(5,2) DEFAULT 20",
        "desi NUMERIC(8,2)",
        "is_active BOOLEAN DEFAULT TRUE",
        "stock_updated_at TIMESTAMPTZ",
        "updated_at TIMESTAMPTZ DEFAULT NOW()",
    ):
        op.execute(f"ALTER TABLE products ADD COLUMN IF NOT EXISTS {col}")
    safe_unique_index("ux_products_sku", "products", "sku", "sku IS NOT NULL")
    op.execute("CREATE INDEX IF NOT EXISTS ix_products_barcode ON products(barcode)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS product_costs(
        id BIGSERIAL PRIMARY KEY,
        product_id BIGINT NOT NULL REFERENCES products(id),
        cost NUMERIC(14,2) NOT NULL CHECK (cost >= 0),
        currency TEXT NOT NULL DEFAULT 'TRY',
        valid_from TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        source TEXT NOT NULL DEFAULT 'manual',
        note TEXT,
        created_by BIGINT REFERENCES users(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_product_costs_lookup ON product_costs(product_id, valid_from DESC)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS marketplace_listings(
        id BIGSERIAL PRIMARY KEY,
        product_id BIGINT REFERENCES products(id),
        store_id BIGINT NOT NULL REFERENCES stores(id),
        external_product_id TEXT NOT NULL,
        barcode TEXT,
        title TEXT,
        listed_price NUMERIC(14,2),
        listed_stock INTEGER,
        status TEXT,
        last_synced_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (store_id, external_product_id)
    )""")

    # ---------------------------------------------------------------- orders
    for col in (
        "internal_status TEXT",
        "currency TEXT DEFAULT 'TRY'",
        "customer_name TEXT",
        "customer_city TEXT",
        "advertising_cost NUMERIC(14,2) DEFAULT 0",
        "other_cost NUMERIC(14,2) DEFAULT 0",
        "finance_is_estimate BOOLEAN DEFAULT TRUE",
        "review_reason TEXT",
        "payload_hash TEXT",
        "source TEXT DEFAULT 'legacy'",
        "last_synced_at TIMESTAMPTZ",
        "created_at TIMESTAMPTZ DEFAULT NOW()",
    ):
        op.execute(f"ALTER TABLE orders ADD COLUMN IF NOT EXISTS {col}")

    # Mevcut kayıtlar için iç statüyü ham statüden türet (yalnızca boş olanlar).
    op.execute("""
    UPDATE orders SET internal_status = CASE lower(coalesce(status, ''))
        WHEN 'created' THEN 'new'
        WHEN 'awaiting' THEN 'new'
        WHEN 'picking' THEN 'preparing'
        WHEN 'unpacked' THEN 'preparing'
        WHEN 'invoiced' THEN 'awaiting_shipment'
        WHEN 'shipped' THEN 'shipped'
        WHEN 'atcollectionpoint' THEN 'shipped'
        WHEN 'delivered' THEN 'delivered'
        WHEN 'cancelled' THEN 'cancelled'
        WHEN 'unsupplied' THEN 'cancelled'
        WHEN 'returned' THEN 'returned'
        ELSE 'needs_review'
    END
    WHERE internal_status IS NULL""")
    op.execute("""UPDATE orders SET review_reason = 'Eski kayıt: ham statü eşlenemedi (' || coalesce(status, 'boş') || ')'
                  WHERE internal_status = 'needs_review' AND review_reason IS NULL""")
    op.execute("ALTER TABLE orders ALTER COLUMN internal_status SET DEFAULT 'new'")
    op.execute("ALTER TABLE orders ALTER COLUMN internal_status SET NOT NULL")
    op.execute(f"""
    DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_orders_internal_status') THEN
            ALTER TABLE orders ADD CONSTRAINT ck_orders_internal_status CHECK (internal_status IN ({statuses}));
        END IF;
    END $$""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_orders_internal_status ON orders(internal_status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_orders_order_date ON orders(order_date DESC)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS order_status_history(
        id BIGSERIAL PRIMARY KEY,
        order_id BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
        from_status TEXT,
        to_status TEXT NOT NULL,
        marketplace_status TEXT,
        source TEXT NOT NULL,
        note TEXT,
        user_id BIGINT REFERENCES users(id),
        changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_order_status_history_order ON order_status_history(order_id, changed_at)")

    # ----------------------------------------------------------- order_items
    for col in (
        "external_line_id TEXT",
        "discount NUMERIC(14,2) DEFAULT 0",
        "vat_rate NUMERIC(5,2)",
        "commission NUMERIC(14,2) DEFAULT 0",
        "commission_rate NUMERIC(6,4)",
        "service_fee NUMERIC(14,2) DEFAULT 0",
        "shipping_cost NUMERIC(14,2) DEFAULT 0",
        "advertising_cost NUMERIC(14,2) DEFAULT 0",
        "refund_amount NUMERIC(14,2) DEFAULT 0",
        "other_cost NUMERIC(14,2) DEFAULT 0",
        "line_status TEXT",
        "finance_is_estimate BOOLEAN DEFAULT TRUE",
        "created_at TIMESTAMPTZ DEFAULT NOW()",
    ):
        op.execute(f"ALTER TABLE order_items ADD COLUMN IF NOT EXISTS {col}")
    op.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ux_order_items_external_line
                  ON order_items(order_id, external_line_id) WHERE external_line_id IS NOT NULL""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_order_items_sku ON order_items(sku)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_order_items_order ON order_items(order_id)")

    # ------------------------------------------------------------- shipments
    for col in (
        "external_package_id TEXT",
        "tracking_url TEXT",
        "marketplace_status TEXT",
        "cost NUMERIC(14,2)",
        "desi NUMERIC(8,2)",
        "created_at TIMESTAMPTZ DEFAULT NOW()",
    ):
        op.execute(f"ALTER TABLE shipments ADD COLUMN IF NOT EXISTS {col}")
    op.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ux_shipments_package
                  ON shipments(order_id, external_package_id) WHERE external_package_id IS NOT NULL""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_shipments_order ON shipments(order_id)")

    # ------------------------------------------------------------- suppliers
    op.execute("""
    CREATE TABLE IF NOT EXISTS suppliers(
        id BIGSERIAL PRIMARY KEY,
        code TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        contact_name TEXT,
        phone TEXT,
        email TEXT,
        lead_time_days INTEGER,
        integration_type TEXT NOT NULL DEFAULT 'manual',
        is_active BOOLEAN NOT NULL DEFAULT TRUE,
        notes TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    op.execute("""
    CREATE TABLE IF NOT EXISTS supplier_products(
        id BIGSERIAL PRIMARY KEY,
        supplier_id BIGINT NOT NULL REFERENCES suppliers(id),
        product_id BIGINT NOT NULL REFERENCES products(id),
        supplier_sku TEXT,
        cost NUMERIC(14,2),
        is_primary BOOLEAN NOT NULL DEFAULT TRUE,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (supplier_id, product_id)
    )""")
    for col in (
        "supplier_id BIGINT REFERENCES suppliers(id)",
        "idempotency_key TEXT",
        "attempts INTEGER DEFAULT 0",
        "last_error TEXT",
        "updated_at TIMESTAMPTZ DEFAULT NOW()",
    ):
        op.execute(f"ALTER TABLE supplier_orders ADD COLUMN IF NOT EXISTS {col}")
    op.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ux_supplier_orders_idem
                  ON supplier_orders(idempotency_key) WHERE idempotency_key IS NOT NULL""")

    # --------------------------------------------------------------- finance
    for col in (
        "marketplace_id BIGINT REFERENCES marketplaces(id)",
        "product_id BIGINT REFERENCES products(id)",
        "sku TEXT",
        "created_by BIGINT REFERENCES users(id)",
        "source TEXT DEFAULT 'manual'",
        "external_ref TEXT",
    ):
        op.execute(f"ALTER TABLE expenses ADD COLUMN IF NOT EXISTS {col}")
    op.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ux_expenses_external_ref
                  ON expenses(source, external_ref) WHERE external_ref IS NOT NULL""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_expenses_date ON expenses(expense_date)")

    # Pazaryeri hakediş/ekstre satırları (ham defter). Gerçek komisyon ve
    # kesintiler geldikçe tahmini değerlerin yerini alır.
    op.execute("""
    CREATE TABLE IF NOT EXISTS financial_transactions(
        id BIGSERIAL PRIMARY KEY,
        store_id BIGINT REFERENCES stores(id),
        order_id BIGINT REFERENCES orders(id),
        order_item_id BIGINT REFERENCES order_items(id),
        source TEXT NOT NULL,
        external_ref TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('revenue','product_cost','commission','service_fee',
                                           'shipping','advertising','refund','other')),
        amount NUMERIC(14,2) NOT NULL,
        currency TEXT NOT NULL DEFAULT 'TRY',
        occurred_at TIMESTAMPTZ NOT NULL,
        description TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (source, external_ref)
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_fin_tx_order ON financial_transactions(order_id)")

    # ------------------------------------------------------------- sync jobs
    for col in (
        "store_id BIGINT REFERENCES stores(id)",
        "payload JSONB DEFAULT '{}'::jsonb",
        "result JSONB",
        "attempts INTEGER DEFAULT 0",
        "max_attempts INTEGER DEFAULT 6",
        "run_after TIMESTAMPTZ DEFAULT NOW()",
        "locked_by TEXT",
        "locked_at TIMESTAMPTZ",
        "last_error TEXT",
        "idempotency_key TEXT",
        "created_at TIMESTAMPTZ DEFAULT NOW()",
    ):
        op.execute(f"ALTER TABLE sync_jobs ADD COLUMN IF NOT EXISTS {col}")
    # Aynı iş kuyrukta iki kez bekleyemez.
    op.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ux_sync_jobs_pending_idem
                  ON sync_jobs(idempotency_key) WHERE status IN ('queued','running')""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_sync_jobs_queue ON sync_jobs(status, run_after)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS sync_state(
        store_id BIGINT NOT NULL REFERENCES stores(id),
        resource TEXT NOT NULL,
        synced_until TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (store_id, resource)
    )""")


def downgrade() -> None:
    raise RuntimeError("Geri alma desteklenmez: migration'lar yalnızca ekleme yapar.")
