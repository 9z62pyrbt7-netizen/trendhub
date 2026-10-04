"""0014 - Ajan çalışma zamanı: görev dağıtımı, araç çağrıları, kanıt, doğrulama, bütçe yönetimi (yalnızca ekleme)

Mevcut yapı GENİŞLETİLİR, ikinci bir sistem kurulmaz:
  ai_agents         + unit (10 çalışma birimi), role_source (açık kaynak rol kaynağı)
                    + finance, analytics, product_trend, creative, operations, reality_checker
  ai_agent_runs     + request_id, task_id (görev kaynaklı çalışmalar; döngü çalışmaları NULL)
  ai_proposals      = agent_approvals (HIGH/CRITICAL yazma işlemleri buradan onaylanır); + request_id, task_id, tool_call_id
  ai_decisions      = agent_decisions / hafıza; + request_id, lesson, lesson_source
  ai_activity       + request_id, task_id (canlı akış satırının görev bağlantısı)

Yeni tablolar:
  ai_requests       kullanıcı isteği → CEO planı → sonuç (orkestrasyon kaydı)
  ai_agent_tasks    CEO'nun uzman ajana verdiği görev (task_uid benzersiz)
  ai_agent_messages ajanlar arası mesajlar (görevlendirme, sonuç, doğrulama, itiraz, cevap)
  ai_tool_calls     her araç çağrısı: argüman, süre, durum, özet, hata, kanıt bağlantısı
  ai_evidence       iddia + kanıt (araç çağrısı, dönem, değer, temel) + Reality Checker sonucu
  ai_agent_errors   ajan/araç hataları (yeniden denenebilir mi, kaçıncı deneme)
  ai_budget_ledger  Bütçe Yöneticisi: onaylı harcamaların rezervasyonu (reserved → committed / released)
  ai_incidents      Operasyon ajanının CEO'ya açtığı olaylar (tekilleştirilmiş, açık/çözüldü)
  ai_experiments    Büyüme deneyleri (hipotez, beklenen etki, maliyet, risk, süre, başarı ölçütü, ölçülen sonuç)
  ai_creatives      Kreatif taslakları (hook, başlık, metin, CTA, video/görsel konsepti, A/B varyantı)

Revision ID: 0014_agent_runtime
Revises: 0013_agent_operations
Create Date: 2026-10-04
"""
from alembic import op

revision = "0014_agent_runtime"
down_revision = "0013_agent_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE ai_agents ADD COLUMN IF NOT EXISTS unit TEXT")
    op.execute("ALTER TABLE ai_agents ADD COLUMN IF NOT EXISTS role_source TEXT")
    op.execute("""
    INSERT INTO ai_agents(code, name, available, unavailable_reason) VALUES
      ('finance', 'Finans', TRUE, NULL),
      ('analytics', 'Analitik', TRUE, NULL),
      ('product_trend', 'Ürün & Trend', TRUE, NULL),
      ('creative', 'Kreatif', TRUE, NULL),
      ('operations', 'Operasyon', TRUE, NULL),
      ('reality_checker', 'Gerçeklik Denetçisi', TRUE, NULL)
    ON CONFLICT (code) DO NOTHING""")
    # 10 çalışma birimi: döngü ajanları ilgili birimin alt işçisidir (ayrı sistem değil)
    op.execute("""
    UPDATE ai_agents SET unit = m.unit FROM (VALUES
      ('ceo', 'ceo'), ('finance', 'finance'), ('capital', 'finance'),
      ('analytics', 'analytics'), ('risk', 'analytics'),
      ('product_trend', 'product_trend'), ('product_profit', 'product_trend'), ('product_tracking', 'product_trend'),
      ('pricing', 'product_trend'),
      ('marketing', 'marketing'), ('campaign', 'marketing'),
      ('advertising', 'advertising'), ('creative', 'creative'), ('social_media', 'social_media'),
      ('operations', 'operations'), ('inventory', 'operations'), ('customer_experience', 'operations'),
      ('reality_checker', 'reality_checker')
    ) AS m(code, unit) WHERE ai_agents.code = m.code AND ai_agents.unit IS NULL""")

    op.execute("""
    CREATE TABLE IF NOT EXISTS ai_requests(
        id BIGSERIAL PRIMARY KEY,
        request_uid TEXT NOT NULL UNIQUE,
        user_id BIGINT REFERENCES users(id),
        source TEXT NOT NULL DEFAULT 'api',
        conversation_id TEXT,
        message TEXT NOT NULL,
        intent TEXT,
        plan JSONB NOT NULL DEFAULT '[]'::jsonb,
        status TEXT NOT NULL DEFAULT 'planning'
            CHECK (status IN ('planning', 'running', 'verifying', 'completed', 'partial', 'failed', 'blocked')),
        ceo_run_id BIGINT REFERENCES ai_agent_runs(id),
        answer TEXT,
        verification JSONB NOT NULL DEFAULT '{}'::jsonb,
        error TEXT,
        started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        finished_at TIMESTAMPTZ,
        duration_ms INTEGER
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_requests_time ON ai_requests(started_at DESC)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS ai_agent_tasks(
        id BIGSERIAL PRIMARY KEY,
        task_uid TEXT NOT NULL UNIQUE,
        request_id BIGINT NOT NULL REFERENCES ai_requests(id),
        parent_task_id BIGINT REFERENCES ai_agent_tasks(id),
        agent_code TEXT NOT NULL REFERENCES ai_agents(code),
        delegated_by TEXT NOT NULL DEFAULT 'ceo',
        task_type TEXT NOT NULL,
        objective TEXT NOT NULL,
        input JSONB NOT NULL DEFAULT '{}'::jsonb,
        status TEXT NOT NULL DEFAULT 'queued'
            CHECK (status IN ('queued', 'running', 'completed', 'failed', 'blocked', 'waiting_approval', 'no_evidence')),
        attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 3,
        result JSONB NOT NULL DEFAULT '{}'::jsonb,
        verification_status TEXT CHECK (verification_status IN ('VERIFIED', 'PARTIALLY_VERIFIED', 'UNVERIFIED', 'FAILED')),
        run_id BIGINT REFERENCES ai_agent_runs(id),
        error TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        started_at TIMESTAMPTZ,
        finished_at TIMESTAMPTZ,
        duration_ms INTEGER
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_agent_tasks_req ON ai_agent_tasks(request_id, id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_agent_tasks_agent ON ai_agent_tasks(agent_code, created_at DESC)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS ai_agent_messages(
        id BIGSERIAL PRIMARY KEY,
        request_id BIGINT REFERENCES ai_requests(id),
        task_id BIGINT REFERENCES ai_agent_tasks(id),
        from_agent TEXT NOT NULL,
        to_agent TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('request', 'delegation', 'result', 'verification', 'objection', 'answer', 'incident', 'note')),
        content TEXT NOT NULL,
        data JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_agent_messages_req ON ai_agent_messages(request_id, id)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS ai_tool_calls(
        id BIGSERIAL PRIMARY KEY,
        call_uid TEXT NOT NULL UNIQUE,
        request_id BIGINT REFERENCES ai_requests(id),
        task_id BIGINT REFERENCES ai_agent_tasks(id),
        run_id BIGINT REFERENCES ai_agent_runs(id),
        agent_code TEXT NOT NULL,
        tool TEXT NOT NULL,
        access TEXT NOT NULL CHECK (access IN ('READ', 'WRITE')),
        risk_level TEXT NOT NULL CHECK (risk_level IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
        arguments JSONB NOT NULL DEFAULT '{}'::jsonb,
        status TEXT NOT NULL DEFAULT 'running'
            CHECK (status IN ('running', 'succeeded', 'failed', 'timeout', 'denied', 'blocked', 'pending_approval', 'not_connected')),
        attempts INTEGER NOT NULL DEFAULT 1,
        started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        completed_at TIMESTAMPTZ,
        duration_ms INTEGER,
        result_summary TEXT,
        result JSONB,
        result_hash TEXT,
        error TEXT,
        proposal_id BIGINT REFERENCES ai_proposals(id),
        external_ref TEXT
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_tool_calls_task ON ai_tool_calls(task_id, id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_tool_calls_time ON ai_tool_calls(started_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_tool_calls_agent ON ai_tool_calls(agent_code, started_at DESC)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS ai_evidence(
        id BIGSERIAL PRIMARY KEY,
        request_id BIGINT REFERENCES ai_requests(id),
        task_id BIGINT REFERENCES ai_agent_tasks(id),
        tool_call_id BIGINT REFERENCES ai_tool_calls(id),
        agent_code TEXT NOT NULL,
        claim TEXT NOT NULL,
        metric TEXT,
        value JSONB,
        baseline JSONB,
        period_start DATE,
        period_end DATE,
        source TEXT,
        verification TEXT CHECK (verification IN ('VERIFIED', 'PARTIALLY_VERIFIED', 'UNVERIFIED', 'FAILED')),
        verified_value JSONB,
        verifier_note TEXT,
        verified_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_evidence_task ON ai_evidence(task_id, id)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS ai_agent_errors(
        id BIGSERIAL PRIMARY KEY,
        request_id BIGINT REFERENCES ai_requests(id),
        task_id BIGINT REFERENCES ai_agent_tasks(id),
        tool_call_id BIGINT REFERENCES ai_tool_calls(id),
        run_id BIGINT REFERENCES ai_agent_runs(id),
        agent_code TEXT,
        error_type TEXT NOT NULL,
        message TEXT NOT NULL,
        retryable BOOLEAN NOT NULL DEFAULT FALSE,
        attempt INTEGER,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_agent_errors_time ON ai_agent_errors(created_at DESC)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS ai_budget_ledger(
        id BIGSERIAL PRIMARY KEY,
        proposal_id BIGINT NOT NULL UNIQUE REFERENCES ai_proposals(id),
        agent_code TEXT NOT NULL,
        category TEXT,
        campaign_key TEXT,
        amount NUMERIC(14,2) NOT NULL CHECK (amount >= 0),
        status TEXT NOT NULL CHECK (status IN ('reserved', 'committed', 'released')),
        note TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_budget_ledger_status ON ai_budget_ledger(status, created_at)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS ai_incidents(
        id BIGSERIAL PRIMARY KEY,
        dedupe_key TEXT NOT NULL,
        severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
        category TEXT NOT NULL,
        title TEXT NOT NULL,
        details JSONB NOT NULL DEFAULT '{}'::jsonb,
        status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
        opened_by TEXT NOT NULL DEFAULT 'operations',
        occurrences INTEGER NOT NULL DEFAULT 1,
        first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        resolved_at TIMESTAMPTZ
    )""")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_incidents_open ON ai_incidents(dedupe_key) WHERE status = 'open'")

    op.execute("""
    CREATE TABLE IF NOT EXISTS ai_experiments(
        id BIGSERIAL PRIMARY KEY,
        dedupe_key TEXT NOT NULL,
        agent_code TEXT NOT NULL DEFAULT 'marketing',
        product_id BIGINT REFERENCES products(id),
        title TEXT NOT NULL,
        hypothesis TEXT NOT NULL,
        expected_impact JSONB NOT NULL DEFAULT '{}'::jsonb,
        cost NUMERIC(14,2) NOT NULL DEFAULT 0,
        risk TEXT NOT NULL CHECK (risk IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
        duration_days INTEGER NOT NULL CHECK (duration_days BETWEEN 1 AND 90),
        success_metric TEXT NOT NULL,
        success_threshold NUMERIC(14,4),
        status TEXT NOT NULL DEFAULT 'proposed'
            CHECK (status IN ('proposed', 'running', 'measured', 'cancelled')),
        baseline JSONB,
        started_at TIMESTAMPTZ,
        measure_after DATE,
        result JSONB,
        outcome TEXT CHECK (outcome IN ('SUCCESS', 'FAILED', 'INCONCLUSIVE')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_experiments_open ON ai_experiments(dedupe_key) WHERE status IN ('proposed', 'running')")

    op.execute("""
    CREATE TABLE IF NOT EXISTS ai_creatives(
        id BIGSERIAL PRIMARY KEY,
        product_id BIGINT NOT NULL REFERENCES products(id),
        campaign_id BIGINT REFERENCES ad_campaigns(id),
        variant TEXT NOT NULL,
        concept TEXT NOT NULL,
        hook TEXT NOT NULL,
        headline TEXT NOT NULL,
        primary_text TEXT NOT NULL,
        cta TEXT NOT NULL,
        video_concept TEXT,
        image_concept TEXT,
        generator TEXT NOT NULL DEFAULT 'template',
        basis JSONB NOT NULL DEFAULT '{}'::jsonb,
        status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'in_use', 'retired')),
        tool_call_id BIGINT REFERENCES ai_tool_calls(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_creatives_product ON ai_creatives(product_id, created_at DESC)")

    for col in ("request_id BIGINT REFERENCES ai_requests(id)", "task_id BIGINT REFERENCES ai_agent_tasks(id)"):
        op.execute(f"ALTER TABLE ai_agent_runs ADD COLUMN IF NOT EXISTS {col}")
        op.execute(f"ALTER TABLE ai_activity ADD COLUMN IF NOT EXISTS {col}")
        op.execute(f"ALTER TABLE ai_proposals ADD COLUMN IF NOT EXISTS {col}")
    op.execute("ALTER TABLE ai_proposals ADD COLUMN IF NOT EXISTS tool_call_id BIGINT REFERENCES ai_tool_calls(id)")
    for col in ("request_id BIGINT REFERENCES ai_requests(id)", "lesson TEXT", "lesson_source TEXT"):
        op.execute(f"ALTER TABLE ai_decisions ADD COLUMN IF NOT EXISTS {col}")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_activity_request ON ai_activity(request_id) WHERE request_id IS NOT NULL")


def downgrade() -> None:
    raise RuntimeError("Geri alma desteklenmez: migration'lar yalnızca ekleme yapar.")
