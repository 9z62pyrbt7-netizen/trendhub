"""0007 - tedarikçi içe aktarma onayı, maliyet KDV durumu, varyant alanları, finans KDV ayarları (yalnızca ekleme)

  suppliers                 + price_vat_mode ('included' | 'excluded' | NULL = belirtilmedi)
                            + mapping_approved_at / mapping_approved_by  (onaysız senkron/ilk import yok)
  supplier_field_mappings   + confirmed  (belirsiz alanın — ör. tek 'Price' — kullanıcı doğrulaması)
  supplier_products         + size, parent_code  (varyant bedeni, ana ürün kodu)
  app_settings              komisyon/gider KDV modu ve oranı, döviz kurları (varsa dokunulmaz)

Geriye uyumluluk: daha önce ürün getirmiş (senkron çalıştırmış) tedarikçilerin mevcut eşleştirmesi
"onaylı" kabul edilir; böylece çalışan senkronlar durmaz. Hiçbir satır silinmez.

Revision ID: 0007_supplier_import
Revises: 0006_web_management
Create Date: 2026-09-29
"""
from alembic import op

revision = "0007_supplier_import"
down_revision = "0006_web_management"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS price_vat_mode TEXT
                  CHECK (price_vat_mode IN ('included','excluded'))""")
    op.execute("ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS mapping_approved_at TIMESTAMPTZ")
    op.execute("ALTER TABLE suppliers ADD COLUMN IF NOT EXISTS mapping_approved_by BIGINT REFERENCES users(id)")
    op.execute("ALTER TABLE supplier_field_mappings ADD COLUMN IF NOT EXISTS confirmed BOOLEAN NOT NULL DEFAULT FALSE")
    for col in ("size TEXT", "parent_code TEXT"):
        op.execute(f"ALTER TABLE supplier_products ADD COLUMN IF NOT EXISTS {col}")

    # Çalışan tedarikçiler durmasın: daha önce senkron çalıştırmış olanların eşleştirmesi onaylı sayılır.
    op.execute("""
        UPDATE suppliers s SET mapping_approved_at = NOW()
         WHERE s.mapping_approved_at IS NULL
           AND (EXISTS (SELECT 1 FROM supplier_sync_runs r WHERE r.supplier_id = s.id)
                OR EXISTS (SELECT 1 FROM supplier_products p WHERE p.supplier_id = s.id))""")

    op.execute("""
    INSERT INTO app_settings(key, value) VALUES
        ('finance.commission_vat_mode', '"unset"'),
        ('finance.commission_vat_rate', '20'),
        ('finance.expense_vat_mode', '"unset"'),
        ('finance.expense_vat_rate', '20'),
        ('finance.fx_rates', '{}')
    ON CONFLICT (key) DO NOTHING""")


def downgrade() -> None:
    raise RuntimeError("Geri alma desteklenmez: migration'lar yalnızca ekleme yapar.")
