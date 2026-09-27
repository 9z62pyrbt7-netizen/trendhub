"""0001 - mevcut şemanın baseline'ı

Bu migration, eski `backend/main.py:init_db()` fonksiyonunun oluşturduğu
şemanın birebir kopyasıdır. Tüm ifadeler `IF NOT EXISTS` / `ON CONFLICT DO
NOTHING` olduğu için hem boş veritabanında hem de hâlihazırda veri içeren
mevcut TrendHub veritabanında güvenle çalışır ve hiçbir veriyi değiştirmez.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-09-27
"""
from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

STATEMENTS = [
    """
            CREATE TABLE IF NOT EXISTS marketplaces(
                id BIGSERIAL PRIMARY KEY,
                code TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                enabled BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
    """,
    """
            CREATE TABLE IF NOT EXISTS stores(
                id BIGSERIAL PRIMARY KEY,
                marketplace_id BIGINT REFERENCES marketplaces(id),
                name TEXT NOT NULL,
                external_id TEXT,
                enabled BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
    """,
    """
            CREATE TABLE IF NOT EXISTS products(
                id BIGSERIAL PRIMARY KEY,
                sku TEXT,
                barcode TEXT,
                name TEXT NOT NULL,
                cost NUMERIC(14,2) DEFAULT 0,
                sale_price NUMERIC(14,2) DEFAULT 0,
                stock INTEGER DEFAULT 0,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
    """,
    """
            CREATE TABLE IF NOT EXISTS orders(
                id BIGSERIAL PRIMARY KEY,
                store_id BIGINT REFERENCES stores(id),
                external_order_id TEXT NOT NULL,
                external_package_id TEXT,
                status TEXT,
                gross_revenue NUMERIC(14,2) DEFAULT 0,
                commission NUMERIC(14,2) DEFAULT 0,
                service_fee NUMERIC(14,2) DEFAULT 0,
                shipping_cost NUMERIC(14,2) DEFAULT 0,
                product_cost NUMERIC(14,2) DEFAULT 0,
                refund_cost NUMERIC(14,2) DEFAULT 0,
                net_profit NUMERIC(14,2) DEFAULT 0,
                order_date TIMESTAMPTZ,
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(store_id, external_order_id)
            )
    """,
    """
            CREATE TABLE IF NOT EXISTS order_items(
                id BIGSERIAL PRIMARY KEY,
                order_id BIGINT REFERENCES orders(id) ON DELETE CASCADE,
                product_id BIGINT REFERENCES products(id),
                sku TEXT,
                barcode TEXT,
                product_name TEXT,
                quantity INTEGER DEFAULT 1,
                unit_price NUMERIC(14,2) DEFAULT 0,
                unit_cost NUMERIC(14,2) DEFAULT 0
            )
    """,
    """
            CREATE TABLE IF NOT EXISTS shipments(
                id BIGSERIAL PRIMARY KEY,
                order_id BIGINT REFERENCES orders(id) ON DELETE CASCADE,
                carrier TEXT,
                tracking_number TEXT,
                status TEXT,
                shipped_at TIMESTAMPTZ,
                delivered_at TIMESTAMPTZ,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
    """,
    """
            CREATE TABLE IF NOT EXISTS supplier_orders(
                id BIGSERIAL PRIMARY KEY,
                order_id BIGINT REFERENCES orders(id),
                supplier TEXT,
                external_supplier_order_id TEXT,
                cost NUMERIC(14,2) DEFAULT 0,
                status TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
    """,
    """
            CREATE TABLE IF NOT EXISTS expenses(
                id BIGSERIAL PRIMARY KEY,
                store_id BIGINT REFERENCES stores(id),
                category TEXT,
                description TEXT,
                amount NUMERIC(14,2) NOT NULL,
                expense_date DATE DEFAULT CURRENT_DATE,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
    """,
    """
            CREATE TABLE IF NOT EXISTS sync_jobs(
                id BIGSERIAL PRIMARY KEY,
                marketplace TEXT,
                job_type TEXT,
                status TEXT,
                message TEXT,
                started_at TIMESTAMPTZ DEFAULT NOW(),
                finished_at TIMESTAMPTZ
            )
    """,
    """
            INSERT INTO marketplaces(code,name,enabled)
            VALUES
                ('trendyol','Trendyol',TRUE),
                ('hepsiburada','Hepsiburada',FALSE),
                ('amazon_tr','Amazon.com.tr',FALSE)
            ON CONFLICT(code) DO NOTHING
    """
]


def upgrade() -> None:
    for sql in STATEMENTS:
        op.execute(sql)


def downgrade() -> None:
    raise RuntimeError("Geri alma desteklenmez: migration'lar yalnızca ekleme yapar.")
