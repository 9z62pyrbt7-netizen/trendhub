"""Test altyapısı.

Testler GERÇEK bir PostgreSQL'e karşı çalışır (SQLite değil) çünkü kod
ON CONFLICT, SKIP LOCKED, advisory lock gibi PostgreSQL özelliklerine dayanır.

TEST_DATABASE_URL: yönetici bağlantısı (ör. postgresql://postgres@localhost:5432/postgres).
Testler bu sunucuda `trendhub_test*` adlı geçici veritabanları oluşturup siler;
başka hiçbir veritabanına dokunmaz. Production veritabanına ASLA yöneltmeyin.
"""
import os
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ADMIN_URL = os.environ.get("TEST_DATABASE_URL")
if not ADMIN_URL:
    pytest.exit("TEST_DATABASE_URL tanımlı değil (bkz. tests/conftest.py)", returncode=2)


def _sa(url: str) -> str:
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


def create_database(prefix: str) -> str:
    name = f"trendhub_test_{prefix}_{uuid.uuid4().hex[:8]}"
    eng = create_engine(_sa(ADMIN_URL), isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        c.execute(text(f'CREATE DATABASE "{name}"'))
    eng.dispose()
    return make_url(_sa(ADMIN_URL)).set(database=name).render_as_string(hide_password=False).replace(
        "postgresql+psycopg://", "postgresql://", 1)


def drop_database(url: str) -> None:
    name = make_url(url).database
    assert name.startswith("trendhub_test_"), "Güvenlik: yalnızca test veritabanları silinebilir"
    eng = create_engine(_sa(ADMIN_URL), isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    eng.dispose()


def run_migrations(url: str) -> None:
    from alembic import command
    from alembic.config import Config
    from app.config import get_settings

    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    get_settings.cache_clear()
    try:
        cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
        cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "migrations"))
        command.upgrade(cfg, "head")
    finally:
        if old is not None:
            os.environ["DATABASE_URL"] = old
        get_settings.cache_clear()


# Uygulama testleri için tek bir migrate edilmiş veritabanı
_APP_DB = create_database("app")
os.environ.update({
    "DATABASE_URL": _APP_DB,
    "ADMIN_USER": "admin",
    "ADMIN_PASSWORD": "Admin-Password-123",
    "COOKIE_SECURE": "false",
    "APP_ENV": "test",
    # Testlerde pazaryeri bilgileri kesinlikle boş
    "TRENDYOL_SELLER_ID": "", "TRENDYOL_API_KEY": "", "TRENDYOL_API_SECRET": "",
})
run_migrations(_APP_DB)

DATA_TABLES = ["financial_transactions", "order_status_history", "supplier_orders", "shipments", "order_items",
               "orders", "marketplace_listings", "product_costs", "supplier_products", "suppliers", "products",
               "expenses", "sync_state", "sync_jobs", "system_events", "worker_heartbeats", "audit_logs",
               "user_sessions", "users", "stores"]


def pytest_sessionfinish(session, exitstatus):
    from app.db import get_engine
    get_engine().dispose()
    drop_database(_APP_DB)


@pytest.fixture
def engine():
    from app.db import get_engine
    eng = get_engine()
    with eng.begin() as c:
        c.execute(text("TRUNCATE " + ", ".join(DATA_TABLES) + " RESTART IDENTITY CASCADE"))
        # users CASCADE, app_settings'i de boşaltır: migration'daki varsayılanları geri yükle
        c.execute(text("""INSERT INTO app_settings(key, value) VALUES
            ('finance.commission_rate.trendyol', '0.20'), ('finance.commission_rate.hepsiburada', '0.20'),
            ('finance.commission_rate.amazon_tr', '0.15'), ('finance.service_fee_per_order', '0'),
            ('finance.default_shipping_cost', '0'), ('finance.include_vat', 'true'),
            ('finance.return_product_cost_is_loss', 'false'),
            ('stock.low_stock_threshold', '3') ON CONFLICT (key) DO NOTHING"""))
        c.execute(text("UPDATE marketplaces SET last_check_at = NULL, last_check_ok = NULL, last_check_message = NULL, last_sync_at = NULL"))
    from app.security import login_limiter
    login_limiter.hits.clear()
    return eng


@pytest.fixture
def conn(engine):
    with engine.begin() as c:
        yield c
