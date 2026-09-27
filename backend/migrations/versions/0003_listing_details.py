"""0003 - pazaryeri ilan detayları (yalnızca ekleme)

`marketplace_listings` tablosuna salt okunur ilan senkronizasyonu için
kolonlar eklenir. Mevcut veri değiştirilmez.

Revision ID: 0003_listing_details
Revises: 0002_platform_core
Create Date: 2026-09-28
"""
from alembic import op

revision = "0003_listing_details"
down_revision = "0002_platform_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for col in (
        "sku TEXT",
        "brand TEXT",
        "category TEXT",
        "list_price NUMERIC(14,2)",
        "vat_rate NUMERIC(5,2)",
        "image_url TEXT",
        "updated_at TIMESTAMPTZ DEFAULT NOW()",
    ):
        op.execute(f"ALTER TABLE marketplace_listings ADD COLUMN IF NOT EXISTS {col}")
    op.execute("CREATE INDEX IF NOT EXISTS ix_listings_barcode ON marketplace_listings(barcode)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_listings_sku ON marketplace_listings(sku)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_listings_product ON marketplace_listings(product_id)")


def downgrade() -> None:
    raise RuntimeError("Geri alma desteklenmez: migration'lar yalnızca ekleme yapar.")
