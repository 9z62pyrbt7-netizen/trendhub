"""0005 - kategori zorunlu özellikleri ve taslakta elle düzenleme bayrakları (yalnızca ekleme)

  marketplace_category_mappings.required_attributes  bu pazaryeri kategorisinde zorunlu özellik adları
  listing_drafts.category_is_manual                   kategori taslakta elle girildi (eşleştirme ezmez)
  listing_drafts.attributes_override                  taslakta elle girilen özellikler (eşleştirme varsayılanlarının üstüne)

Revision ID: 0005_listing_attributes
Revises: 0004_supplier_management
Create Date: 2026-09-28
"""
from alembic import op

revision = "0005_listing_attributes"
down_revision = "0004_supplier_management"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""ALTER TABLE marketplace_category_mappings
                  ADD COLUMN IF NOT EXISTS required_attributes JSONB NOT NULL DEFAULT '[]'::jsonb""")
    for col in ("category_is_manual BOOLEAN NOT NULL DEFAULT FALSE",
                "attributes_override JSONB NOT NULL DEFAULT '{}'::jsonb"):
        op.execute(f"ALTER TABLE listing_drafts ADD COLUMN IF NOT EXISTS {col}")
    # Önceki sürümde elle girilen kategori (eşleştirmesi olmayan) taslaklar: elle girilmiş sayılır.
    op.execute("""
        UPDATE listing_drafts d SET category_is_manual = TRUE
         WHERE d.category_id IS NOT NULL AND NOT EXISTS (
               SELECT 1 FROM marketplace_category_mappings m JOIN products p ON p.id = d.product_id
                WHERE m.marketplace_id = d.marketplace_id AND m.source_category = p.category)""")


def downgrade() -> None:
    raise RuntimeError("Geri alma desteklenmez: migration'lar yalnızca ekleme yapar.")
