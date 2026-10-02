"""0011 - AI öğrenme ve override alanları (yalnızca ekleme)

  ai_proposals  + situation (benzer durum anahtarı), ceo_stance (support/neutral/oppose), ceo_assessment (kanıt),
                  owner_override (CEO itirazına rağmen sahip onayı)
  ai_decisions  + situation, ceo_stance, override, override_reason

Revision ID: 0011_ai_learning
Revises: 0010_ai_control_center
Create Date: 2026-10-02
"""
from alembic import op

revision = "0011_ai_learning"
down_revision = "0010_ai_control_center"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for col in ("situation TEXT", "ceo_stance TEXT", "ceo_assessment JSONB", "owner_override BOOLEAN NOT NULL DEFAULT FALSE"):
        op.execute(f"ALTER TABLE ai_proposals ADD COLUMN IF NOT EXISTS {col}")
    for col in ("situation TEXT", "ceo_stance TEXT", "override BOOLEAN NOT NULL DEFAULT FALSE", "override_reason TEXT"):
        op.execute(f"ALTER TABLE ai_decisions ADD COLUMN IF NOT EXISTS {col}")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_decisions_situation ON ai_decisions(decision_type, situation)")


def downgrade() -> None:
    raise RuntimeError("Geri alma desteklenmez: migration'lar yalnızca ekleme yapar.")
