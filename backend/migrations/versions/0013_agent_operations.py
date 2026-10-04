"""0013 - Ajan operasyonları: aksiyon kaydı (EXECUTED/PROPOSED/BLOCKED/FAILED/SKIPPED), yeni ajanlar, yönetici özeti
(yalnızca ekleme)

  ai_actions        her ajan çıktısının denetim kaydı: ajan, zaman, gerekçe, girdi, beklenen etki, gerçek aksiyon,
                    durum, hata, sonuç. EXECUTED yalnızca gerçek API/DB işlemi başarılıysa yazılır.
  ai_briefs         + summary (CEO'nun Türkçe yönetici özeti, yapılandırılmış)
  ai_agents         + pricing, campaign, product_tracking; marketing / social_media / customer_experience gerçek veriyle
                    çalışır hale getirildi (yayın/gönderim YOK, yalnızca öneri). experiments / opportunity_radar veri
                    kaynağı olmadığı için emekliye ayrıldı (satır silinmez, kapalı ve kullanılamaz).

Revision ID: 0013_agent_operations
Revises: 0012_platform_data
Create Date: 2026-10-04
"""
from alembic import op

revision = "0013_agent_operations"
down_revision = "0012_platform_data"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE IF NOT EXISTS ai_actions (
        id              BIGSERIAL PRIMARY KEY,
        cycle_id        BIGINT REFERENCES ai_agent_runs(id),
        agent_code      TEXT NOT NULL REFERENCES ai_agents(code),
        action_type     TEXT NOT NULL,
        entity_type     TEXT,
        entity_id       BIGINT,
        status          TEXT NOT NULL CHECK (status IN ('EXECUTED', 'PROPOSED', 'BLOCKED', 'FAILED', 'SKIPPED')),
        reason          TEXT NOT NULL,
        reason_code     TEXT,                    -- ör. needs_data, stock_risk, budget_limit, conflict
        input_data      JSONB NOT NULL DEFAULT '{}'::jsonb,
        expected_impact JSONB NOT NULL DEFAULT '{}'::jsonb,
        actual_action   TEXT,                    -- gerçekten ne yapıldı (yoksa NULL)
        error           TEXT,
        result          JSONB NOT NULL DEFAULT '{}'::jsonb,
        proposal_id     BIGINT REFERENCES ai_proposals(id),
        dedupe_key      TEXT,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    # dedupe_key: öneri durumu "proposal:<id>" (yerinde güncellenir), atlama "skip:...:<gün>", CEO çatışması "conflict:<id>".
    # Uygulama denemeleri anahtarsızdır: her deneme ayrı satır (FAILED → EXECUTED geçmişi korunur).
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_actions_dedupe ON ai_actions(dedupe_key) WHERE dedupe_key IS NOT NULL")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_actions_time ON ai_actions(created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_actions_status ON ai_actions(status, created_at DESC)")
    op.execute("ALTER TABLE ai_briefs ADD COLUMN IF NOT EXISTS summary JSONB")
    op.execute("""
    INSERT INTO ai_agents(code, name, available, unavailable_reason) VALUES
      ('pricing', 'Fiyat', TRUE, NULL),
      ('campaign', 'Kampanya', TRUE, NULL),
      ('product_tracking', 'Ürün Takibi', TRUE, NULL)
    ON CONFLICT (code) DO NOTHING""")
    op.execute("""UPDATE ai_agents SET available = TRUE, unavailable_reason = NULL, enabled = TRUE
                   WHERE code IN ('marketing', 'social_media', 'customer_experience') AND NOT available""")
    op.execute("""UPDATE ai_agents SET available = FALSE, enabled = FALSE,
                         unavailable_reason = 'Kaldırıldı: güvenilir veri kaynağı yok (öneri üretemez); işlevi Ürün Takibi ve Kampanya ajanlarında.'
                   WHERE code IN ('experiments', 'opportunity_radar')""")


def downgrade() -> None:
    raise RuntimeError("Geri alma desteklenmez: migration'lar yalnızca ekleme yapar.")
