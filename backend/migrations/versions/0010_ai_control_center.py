"""0010 - AI Control Center (yalnızca ekleme)

  ai_agents               ajan kaydı (aç/kapa, durum, son çalışma)
  ai_agent_runs           her çalışmanın başlangıç/bitiş/süre/girdi/çıktı/hata/kullanım kaydı
  ai_proposals            ajan önerileri + risk sonucu + onay + uygulama durumu
  ai_risk_events          risk motorunun bulguları (bloke dahil)
  ai_activity             "Ajan hareketleri" akışı
  ai_decisions            karar günlüğü (sahip ve CEO kararları, bağlam anlık görüntüsüyle)
  ai_decision_outcomes    1/3/7/30 gün sonra ölçülen gerçek sonuç
  ai_owner_preferences    sahibin açık tercihleri (kanıttan ayrı tutulur)
  ai_capital_accounts     kasa, bekleyen hakediş, borçlar, rezerv (elle; nakit ≠ kâr)
  ai_chat_messages        CEO sohbeti
  ai_briefs               günlük "Bugün bilmen gerekenler"
  ai_inventory_snapshots  günlük stok anlık görüntüsü (stoksuz kalma ölçümü için)
  ad_campaigns            + daily_budget (bütçe önerileri için; elle girilir)
  app_settings            ai.* varsayılanları

Mevcut satırlara dokunulmaz.

Revision ID: 0010_ai_control_center
Revises: 0009_storefront_production
Create Date: 2026-10-02
"""
from alembic import op

revision = "0010_ai_control_center"
down_revision = "0009_storefront_production"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE IF NOT EXISTS ai_agents(
        code TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        available BOOLEAN NOT NULL DEFAULT TRUE,
        unavailable_reason TEXT,
        last_run_at TIMESTAMPTZ,
        last_status TEXT,
        last_error TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    op.execute("""
    INSERT INTO ai_agents(code, name, available, unavailable_reason) VALUES
      ('ceo', 'CEO', TRUE, NULL),
      ('product_profit', 'Ürün & Kâr', TRUE, NULL),
      ('advertising', 'Reklam', TRUE, NULL),
      ('inventory', 'Stok / Tedarikçi', TRUE, NULL),
      ('capital', 'Sermaye & Nakit', TRUE, NULL),
      ('risk', 'Risk & Denetim', TRUE, NULL),
      ('social_media', 'Sosyal Medya', FALSE, 'Meta/Instagram resmi API (OAuth + App Review) bağlı değil.'),
      ('marketing', 'Pazarlama', FALSE, 'Trendyol kampanya/promosyon API''si bağlı değil; kampanya maliyet verisi yok.'),
      ('experiments', 'Deney Motoru', FALSE, 'Fiyat/bütçe değişikliklerini uygulayan bağlantı yok; kontrollü test kurulamaz.'),
      ('opportunity_radar', 'Fırsat Radarı', FALSE, 'Trendyol trend/arama/rakip fiyat verisi için erişilebilir resmi kaynak yok.'),
      ('customer_experience', 'Müşteri Deneyimi', FALSE, 'Trendyol yorum/soru/iade nedeni API''si bağlı değil.')
    ON CONFLICT (code) DO NOTHING""")

    op.execute("""
    CREATE TABLE IF NOT EXISTS ai_agent_runs(
        id BIGSERIAL PRIMARY KEY,
        agent_code TEXT NOT NULL REFERENCES ai_agents(code),
        trigger TEXT NOT NULL DEFAULT 'schedule',
        status TEXT NOT NULL CHECK (status IN ('running', 'ok', 'degraded', 'error', 'skipped')),
        started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        finished_at TIMESTAMPTZ,
        duration_ms INTEGER,
        input_sources JSONB NOT NULL DEFAULT '[]'::jsonb,
        output JSONB NOT NULL DEFAULT '{}'::jsonb,
        warnings JSONB NOT NULL DEFAULT '[]'::jsonb,
        error TEXT,
        usage JSONB NOT NULL DEFAULT '{}'::jsonb
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_agent_runs_agent ON ai_agent_runs(agent_code, started_at DESC)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS ai_proposals(
        id BIGSERIAL PRIMARY KEY,
        agent_code TEXT NOT NULL REFERENCES ai_agents(code),
        run_id BIGINT REFERENCES ai_agent_runs(id),
        channel TEXT,
        action_type TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        entity_id BIGINT,
        title TEXT NOT NULL,
        reason TEXT NOT NULL,
        evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
        params JSONB NOT NULL DEFAULT '{}'::jsonb,
        expected_result JSONB NOT NULL DEFAULT '{}'::jsonb,
        required_capital NUMERIC(14,2) NOT NULL DEFAULT 0,
        capital_category TEXT,
        risk_level TEXT NOT NULL DEFAULT 'medium' CHECK (risk_level IN ('low', 'medium', 'high', 'critical')),
        confidence NUMERIC(4,3),
        requires_approval BOOLEAN NOT NULL DEFAULT TRUE,
        status TEXT NOT NULL DEFAULT 'pending_approval' CHECK (status IN (
            'pending_approval', 'blocked', 'approved', 'rejected', 'executed', 'failed', 'expired', 'superseded', 'info')),
        risk_checks JSONB NOT NULL DEFAULT '[]'::jsonb,
        ceo_note TEXT,
        dedupe_key TEXT,
        decided_by BIGINT REFERENCES users(id),
        decided_at TIMESTAMPTZ,
        decision_note TEXT,
        executed_by BIGINT REFERENCES users(id),
        executed_at TIMESTAMPTZ,
        execution_result JSONB,
        expires_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    op.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_proposals_open_dedupe ON ai_proposals(dedupe_key)
                  WHERE dedupe_key IS NOT NULL AND status IN ('pending_approval', 'blocked', 'approved')""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_proposals_status ON ai_proposals(status, created_at DESC)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS ai_risk_events(
        id BIGSERIAL PRIMARY KEY,
        proposal_id BIGINT REFERENCES ai_proposals(id),
        code TEXT NOT NULL,
        severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'block')),
        message TEXT NOT NULL,
        details JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_risk_events_created ON ai_risk_events(created_at DESC)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS ai_activity(
        id BIGSERIAL PRIMARY KEY,
        agent_code TEXT,
        kind TEXT NOT NULL,
        level TEXT NOT NULL DEFAULT 'info' CHECK (level IN ('info', 'success', 'warning', 'error')),
        message TEXT NOT NULL,
        proposal_id BIGINT REFERENCES ai_proposals(id),
        run_id BIGINT REFERENCES ai_agent_runs(id),
        user_id BIGINT REFERENCES users(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_activity_created ON ai_activity(created_at DESC)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS ai_decisions(
        id BIGSERIAL PRIMARY KEY,
        proposal_id BIGINT REFERENCES ai_proposals(id),
        actor TEXT NOT NULL CHECK (actor IN ('owner', 'ceo', 'agent', 'system')),
        agent_code TEXT,
        user_id BIGINT REFERENCES users(id),
        decision TEXT NOT NULL,
        decision_type TEXT NOT NULL,
        entity_type TEXT,
        entity_id BIGINT,
        context_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
        reason TEXT,
        expected_result JSONB NOT NULL DEFAULT '{}'::jsonb,
        risk_level TEXT,
        confidence NUMERIC(4,3),
        requires_approval BOOLEAN NOT NULL DEFAULT TRUE,
        approved_by BIGINT REFERENCES users(id),
        executed_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_decisions_type ON ai_decisions(decision_type, created_at DESC)")
    op.execute("""
    CREATE TABLE IF NOT EXISTS ai_decision_outcomes(
        id BIGSERIAL PRIMARY KEY,
        decision_id BIGINT NOT NULL REFERENCES ai_decisions(id),
        horizon_days INTEGER NOT NULL CHECK (horizon_days IN (1, 3, 7, 30)),
        evaluated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
        final_result TEXT NOT NULL CHECK (final_result IN ('improved', 'worsened', 'neutral', 'insufficient_data')),
        UNIQUE (decision_id, horizon_days)
    )""")

    op.execute("""
    CREATE TABLE IF NOT EXISTS ai_owner_preferences(
        key TEXT PRIMARY KEY,
        value JSONB NOT NULL,
        source TEXT NOT NULL DEFAULT 'explicit' CHECK (source IN ('explicit', 'inferred')),
        note TEXT,
        updated_by BIGINT REFERENCES users(id),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")

    op.execute("""
    CREATE TABLE IF NOT EXISTS ai_capital_accounts(
        id BIGSERIAL PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('cash', 'pending_payout', 'supplier_liability', 'ad_liability',
                                           'opex_liability', 'reserved', 'investable_limit')),
        name TEXT NOT NULL,
        amount NUMERIC(14,2) NOT NULL CHECK (amount >= 0),
        as_of DATE NOT NULL DEFAULT CURRENT_DATE,
        note TEXT,
        updated_by BIGINT REFERENCES users(id),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (kind, name)
    )""")

    op.execute("""
    CREATE TABLE IF NOT EXISTS ai_chat_messages(
        id BIGSERIAL PRIMARY KEY,
        user_id BIGINT REFERENCES users(id),
        conversation_id TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
        content TEXT NOT NULL,
        engine TEXT,
        tools_used JSONB NOT NULL DEFAULT '[]'::jsonb,
        usage JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_chat_conv ON ai_chat_messages(conversation_id, id)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS ai_briefs(
        id BIGSERIAL PRIMARY KEY,
        brief_date DATE NOT NULL UNIQUE,
        items JSONB NOT NULL DEFAULT '[]'::jsonb,
        data_quality JSONB NOT NULL DEFAULT '[]'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")

    op.execute("""
    CREATE TABLE IF NOT EXISTS ai_inventory_snapshots(
        product_id BIGINT NOT NULL REFERENCES products(id),
        snap_date DATE NOT NULL,
        stock INTEGER,
        available INTEGER,
        supplier_stock INTEGER,
        units_7d INTEGER,
        PRIMARY KEY (product_id, snap_date)
    )""")

    op.execute("ALTER TABLE ad_campaigns ADD COLUMN IF NOT EXISTS daily_budget NUMERIC(14,2)")

    op.execute("""
    INSERT INTO app_settings(key, value) VALUES
        ('ai.enabled', 'true'),
        ('ai.emergency_stop', 'false'),
        ('ai.cycle_minutes', '60'),
        ('ai.inventory_model', '"dropship"'),
        ('ai.thresholds', '{"analysis_days": 30, "min_units_for_data": 3, "star_margin": 0.20, "star_min_profit": 1000,
                            "profitable_margin": 0.08, "ads_window_days": 7, "ads_min_clicks": 100, "ads_min_spend": 300,
                            "ads_target_margin_after_ads": 0.10, "ads_max_budget_step": 0.30, "ads_pause_loss": 500,
                            "stockout_days": 7, "dead_stock_days": 60, "reserve_months_opex": 1,
                            "data_stale_hours": 24, "ads_data_stale_days": 3}')
    ON CONFLICT (key) DO NOTHING""")


def downgrade() -> None:
    raise RuntimeError("Geri alma desteklenmez: migration'lar yalnızca ekleme yapar.")
