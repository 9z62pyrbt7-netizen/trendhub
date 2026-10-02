"""0012 - Gerçek pazaryeri verisi: finans defteri, iadeler, müşteri soruları, platform olayları (yalnızca ekleme)

  sync_state                    + last_attempt_at, last_success_at, error_count, last_error, status, record_count,
                                  latest_record_at, meta  (her dış kaynak için tazelik / hata durumu)
  marketplace_finance_entries   pazaryeri cari hesap kayıtları (settlements / otherfinancials / kargo faturası kalemleri)
                                  — NAKİT tarafı (hakediş, ödeme talimatı, kesinti); kâr tarafına yalnızca eşleşen kalemler
                                  financial_transactions üzerinden aktarılır
  marketplace_returns           iade (claim) kalemleri: sipariş / paket / kalem / ürün / sebep / statü / tutar
  customer_questions            müşteri soruları (salt okunur; müşteri adı / müşteri id SAKLANMAZ)
  platform_events               normalize edilmiş iç olaylar (webhook + polling): doğrula → kaydet → kuyruk → worker

Mevcut tablolar yeniden kullanılır: financial_transactions (gerçek komisyon/iade/kargo → kâr motoru),
sync_state (tazelik), expenses/ai_capital_accounts (değişmez).

Revision ID: 0012_platform_data
Revises: 0011_ai_learning
Create Date: 2026-10-02
"""
from alembic import op

revision = "0012_platform_data"
down_revision = "0011_ai_learning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for col in ("last_attempt_at TIMESTAMPTZ", "last_success_at TIMESTAMPTZ", "error_count INTEGER NOT NULL DEFAULT 0",
                "last_error TEXT", "status TEXT", "record_count INTEGER", "latest_record_at TIMESTAMPTZ",
                "meta JSONB NOT NULL DEFAULT '{}'::jsonb"):
        op.execute(f"ALTER TABLE sync_state ADD COLUMN IF NOT EXISTS {col}")

    op.execute("""
    CREATE TABLE IF NOT EXISTS marketplace_finance_entries (
        id                  BIGSERIAL PRIMARY KEY,
        store_id            BIGINT NOT NULL REFERENCES stores(id),
        source              TEXT NOT NULL,              -- settlements | otherfinancials | cargo_invoice
        external_id         TEXT NOT NULL,
        transaction_type    TEXT NOT NULL,
        transaction_sub_type TEXT,
        transaction_date    TIMESTAMPTZ,
        order_number        TEXT,
        shipment_package_id TEXT,
        barcode             TEXT,
        description         TEXT,
        debt                NUMERIC(14,2) NOT NULL DEFAULT 0,
        credit              NUMERIC(14,2) NOT NULL DEFAULT 0,
        commission_rate     NUMERIC(8,4),
        commission_amount   NUMERIC(14,2),
        seller_revenue      NUMERIC(14,2),
        payment_order_id    TEXT,
        payment_date        TIMESTAMPTZ,
        receipt_id          TEXT,
        invoice_serial      TEXT,
        order_id            BIGINT REFERENCES orders(id),
        order_item_id       BIGINT REFERENCES order_items(id),
        applied_at          TIMESTAMPTZ,                -- kâr motoruna (financial_transactions) aktarıldığı an
        fetched_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (store_id, source, external_id, transaction_type)
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_mfe_order ON marketplace_finance_entries(order_number)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_mfe_unpaid ON marketplace_finance_entries(store_id) WHERE payment_order_id IS NULL")
    op.execute("CREATE INDEX IF NOT EXISTS ix_mfe_date ON marketplace_finance_entries(transaction_date DESC)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS marketplace_returns (
        id                  BIGSERIAL PRIMARY KEY,
        store_id            BIGINT NOT NULL REFERENCES stores(id),
        claim_id            TEXT NOT NULL,
        claim_item_id       TEXT NOT NULL,
        order_number        TEXT,
        order_id            BIGINT REFERENCES orders(id),
        order_item_id       BIGINT REFERENCES order_items(id),
        shipment_package_id TEXT,
        order_line_id       TEXT,
        barcode             TEXT,
        sku                 TEXT,
        product_id          BIGINT REFERENCES products(id),
        product_name        TEXT,
        amount              NUMERIC(14,2),
        reason_code         TEXT,
        reason_name         TEXT,
        trendyol_reason_code TEXT,
        trendyol_reason_name TEXT,
        status              TEXT,
        accepted_by_seller  BOOLEAN,
        customer_note       TEXT,                       -- kişisel veri temizlenmiş (telefon/e-posta maskeli)
        claim_date          TIMESTAMPTZ,
        last_modified_at    TIMESTAMPTZ,
        fetched_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (store_id, claim_item_id)
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_mreturns_product ON marketplace_returns(product_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_mreturns_order ON marketplace_returns(order_id)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS customer_questions (
        id                  BIGSERIAL PRIMARY KEY,
        store_id            BIGINT NOT NULL REFERENCES stores(id),
        external_id         TEXT NOT NULL,
        product_main_id     TEXT,
        barcode             TEXT,
        product_id          BIGINT REFERENCES products(id),
        product_name        TEXT,
        web_url             TEXT,
        question_text       TEXT NOT NULL,              -- kişisel veri temizlenmiş
        status              TEXT,
        is_public           BOOLEAN,
        asked_at            TIMESTAMPTZ,
        answer_text         TEXT,
        answered_at         TIMESTAMPTZ,
        category            TEXT,                       -- CX sınıflandırması (kural tabanlı)
        suggested_answer    TEXT,                       -- yalnızca ÖNERİ; otomatik gönderilmez
        fetched_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (store_id, external_id)
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_cq_product ON customer_questions(product_id, category)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_cq_asked ON customer_questions(asked_at DESC)")

    op.execute("""
    CREATE TABLE IF NOT EXISTS platform_events (
        id                  BIGSERIAL PRIMARY KEY,
        source              TEXT NOT NULL,              -- webhook | polling
        marketplace         TEXT NOT NULL,
        event_type          TEXT NOT NULL,              -- ORDER_CREATED, ORDER_UPDATED, RETURN_CREATED, ...
        dedupe_key          TEXT NOT NULL UNIQUE,
        entity_type         TEXT,
        entity_ref          TEXT,
        payload             JSONB NOT NULL DEFAULT '{}'::jsonb,   -- kişisel veri ayıklanmış
        status              TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending', 'processing', 'done', 'failed', 'ignored')),
        attempts            INTEGER NOT NULL DEFAULT 0,
        last_error          TEXT,
        actions             JSONB NOT NULL DEFAULT '[]'::jsonb,
        received_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        next_attempt_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        processed_at        TIMESTAMPTZ
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_platform_events_pending ON platform_events(next_attempt_at) "
               "WHERE status IN ('pending', 'failed')")
    op.execute("CREATE INDEX IF NOT EXISTS ix_platform_events_time ON platform_events(received_at DESC)")


def downgrade() -> None:
    raise RuntimeError("Geri alma desteklenmez: migration'lar yalnızca ekleme yapar.")
