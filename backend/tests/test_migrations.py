"""Migration'ların mevcut (legacy) veriyi korumasını doğrular."""
import importlib.util
import os
import re
import subprocess

from sqlalchemy import create_engine, text

from .conftest import _sa, create_database, drop_database, run_migrations

# Tedarikçi ürünü kataloğa bağlanmadan havuzda durabilsin (0004).
ALLOWED_NOT_NULL_RELAXATIONS = {("SUPPLIER_PRODUCTS", "PRODUCT_ID")}
# CHECK kısıtını GENİŞLETMEK için aynı adla yeniden eklemek (0006: users.role + 'accountant').
# Kısıt aynı migration'da aynı adla yeniden eklenmelidir; veri silinmez.
ALLOWED_CONSTRAINT_WIDENINGS = {("USERS", "USERS_ROLE_CHECK")}

LEGACY_INIT = os.path.join(os.path.dirname(__file__), "fixtures", "legacy_main.py")


def _legacy_init(url: str) -> None:
    spec = importlib.util.spec_from_file_location("legacy_main", LEGACY_INIT)
    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.init_db()
    finally:
        os.environ["DATABASE_URL"] = old


def test_fresh_database_migrates():
    url = create_database("fresh")
    try:
        run_migrations(url)
        run_migrations(url)  # ikinci çalıştırma no-op olmalı
        eng = create_engine(_sa(url))
        with eng.connect() as c:
            assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0006_web_management"
            assert c.execute(text("SELECT COUNT(*) FROM marketplaces")).scalar() == 3
        eng.dispose()
    finally:
        drop_database(url)


def test_legacy_database_data_is_preserved():
    url = create_database("legacy")
    try:
        _legacy_init(url)  # eski main.py'nin oluşturduğu şema
        eng = create_engine(_sa(url))
        with eng.begin() as c:
            c.execute(text("UPDATE marketplaces SET enabled = FALSE WHERE code = 'trendyol'"))
            c.execute(text("INSERT INTO stores(marketplace_id, name) VALUES (1, 'Mağaza')"))
            c.execute(text("INSERT INTO products(sku, name, cost) VALUES ('A1','x',10),('A1','kopya',11)"))
            c.execute(text("""INSERT INTO orders(store_id, external_order_id, status, gross_revenue, net_profit, order_date)
                              VALUES (1,'1001','Created',100,20,NOW()), (1,'1002','Shipped',50,5,NOW()),
                                     (1,'1003','Bilinmeyen',1,0,NOW())"""))
            c.execute(text("INSERT INTO order_items(order_id, sku, quantity, unit_price) VALUES (1,'A1',1,100)"))
            c.execute(text("INSERT INTO expenses(amount, category) VALUES (15, 'reklam')"))
            c.execute(text("INSERT INTO sync_jobs(marketplace, job_type, status, message) VALUES ('trendyol','orders','ok','eski')"))
            before = {t: c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
                      for t in ("stores", "products", "orders", "order_items", "expenses", "sync_jobs")}
        eng.dispose()

        run_migrations(url)
        run_migrations(url)

        eng = create_engine(_sa(url))
        with eng.connect() as c:
            after = {t: c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() for t in before}
            assert after == before
            rows = c.execute(text("SELECT external_order_id, status, internal_status, gross_revenue, net_profit "
                                  "FROM orders ORDER BY id")).all()
            assert [(r[0], r[1], r[2]) for r in rows] == [
                ("1001", "Created", "new"), ("1002", "Shipped", "shipped"), ("1003", "Bilinmeyen", "needs_review")]
            assert float(rows[0][3]) == 100 and float(rows[0][4]) == 20
            # Mevcut seed değeri ezilmedi
            assert c.execute(text("SELECT enabled FROM marketplaces WHERE code='trendyol'")).scalar() is False
            # Tekrarlı SKU yüzünden unique index yerine normal index kuruldu, veri silinmedi
            idx = c.execute(text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ux_products_sku'")).scalar()
            assert "UNIQUE" not in idx
            assert c.execute(text("SELECT status FROM sync_jobs")).scalar() == "ok"
        eng.dispose()
    finally:
        drop_database(url)


def test_migrations_contain_no_destructive_statements():
    here = os.path.join(os.path.dirname(__file__), "..", "migrations", "versions")
    for name in os.listdir(here):
        if not name.endswith(".py"):
            continue
        src = open(os.path.join(here, name), encoding="utf-8").read()
        # Yalnızca çalıştırılan kod (upgrade fonksiyonu ve SQL sabitleri), açıklamalar hariç
        src = src[src.index('"""', src.index('"""') + 3) + 3:].upper().replace("ON DELETE CASCADE", "")
        # Tek izinli istisna: NOT NULL kısıtını gevşetmek (veri silmez/değiştirmez). Her kullanım
        # aşağıdaki listede açıkça onaylı olmalı; DROP TABLE/COLUMN/INDEX/CONSTRAINT hâlâ yasak.
        relaxed = re.findall(r"ALTER TABLE (\w+) ALTER COLUMN (\w+) DROP NOT NULL", src)
        assert set(relaxed) <= ALLOWED_NOT_NULL_RELAXATIONS, f"{name}: onaysız NOT NULL gevşetme {relaxed}"
        src = re.sub(r"ALTER TABLE \w+ ALTER COLUMN \w+ DROP NOT NULL", "", src)
        widened = re.findall(r"ALTER TABLE (\w+) DROP CONSTRAINT IF EXISTS (\w+)", src)
        assert set(widened) <= ALLOWED_CONSTRAINT_WIDENINGS, f"{name}: onaysız kısıt kaldırma {widened}"
        for table, cons in widened:
            assert f"ALTER TABLE {table} ADD CONSTRAINT {cons}" in src, f"{name}: {cons} yeniden eklenmemiş"
        src = re.sub(r"ALTER TABLE \w+ DROP CONSTRAINT IF EXISTS \w+", "", src)
        for bad in ("DROP ", "TRUNCATE", "DELETE", "ALTER COLUMN TYPE", " TYPE "):
            assert bad not in src, f"{name} içinde yıkıcı ifade: {bad}"


def test_supplier_migration_preserves_existing_supplier_rows():
    """0004 öncesi tedarikçi/ürün bağları korunur; product_id artık boş olabilir."""
    from alembic import command
    from alembic.config import Config

    from app.config import get_settings
    url = create_database("sup")
    try:
        old = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = url
        get_settings.cache_clear()
        cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
        cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "migrations"))
        try:
            command.upgrade(cfg, "0003_listing_details")
        finally:
            os.environ["DATABASE_URL"] = old
            get_settings.cache_clear()
        eng = create_engine(_sa(url))
        with eng.begin() as c:
            c.execute(text("INSERT INTO suppliers(code, name, integration_type) VALUES ('eski', 'Eski Tedarikçi', 'external')"))
            c.execute(text("INSERT INTO products(sku, name, cost) VALUES ('P1', 'Ürün', 10)"))
            c.execute(text("INSERT INTO supplier_products(supplier_id, product_id, supplier_sku, cost) VALUES (1, 1, 'E-1', 9.5)"))
        run_migrations(url)
        with eng.begin() as c:
            r = c.execute(text("SELECT supplier_id, product_id, supplier_sku, cost, status FROM supplier_products")).one()
            assert (r[0], r[1], r[2], float(r[3]), r[4]) == (1, 1, "E-1", 9.5, "active")
            assert c.execute(text("SELECT integration_type, priority FROM suppliers")).one() == ("external", 100)
            nullable = c.execute(text("""SELECT is_nullable FROM information_schema.columns
                                         WHERE table_name = 'supplier_products' AND column_name = 'product_id'""")).scalar()
            assert nullable == "YES"
            # Her pazaryeri için varsayılan kural satırı
            assert c.execute(text("SELECT COUNT(*) FROM marketplace_rules")).scalar() == 3
        eng.dispose()
    finally:
        drop_database(url)


def test_0006_widens_roles_without_touching_users_and_seeds_settings():
    """Mevcut kullanıcılar ve ayarlar korunur; 'accountant' rolü eklenir; geçersiz rol hâlâ reddedilir."""
    import pytest as _pytest
    from alembic import command
    from alembic.config import Config
    from sqlalchemy.exc import IntegrityError

    from app.config import get_settings
    url = create_database("roles")
    try:
        old = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = url
        get_settings.cache_clear()
        cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
        cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "migrations"))
        try:
            command.upgrade(cfg, "0005_listing_attributes")
        finally:
            os.environ["DATABASE_URL"] = old
            get_settings.cache_clear()
        eng = create_engine(_sa(url))
        with eng.begin() as c:
            c.execute(text("""INSERT INTO users(username, password_hash, role) VALUES
                              ('a', 'h', 'admin'), ('o', 'h', 'operator'), ('v', 'h', 'viewer')"""))
            c.execute(text("""INSERT INTO app_settings(key, value) VALUES ('shipping.same_day_before', '"10:00"')"""))
        run_migrations(url)
        with eng.begin() as c:
            assert c.execute(text("SELECT username, role FROM users ORDER BY username")).all() == [
                ("a", "admin"), ("o", "operator"), ("v", "viewer")]
            c.execute(text("INSERT INTO users(username, password_hash, role) VALUES ('m', 'h', 'accountant')"))
            # Kullanıcının mevcut ayarı ezilmedi, eksik varsayılanlar eklendi
            assert c.execute(text("SELECT value FROM app_settings WHERE key = 'shipping.same_day_before'")).scalar() == "10:00"
            assert c.execute(text("SELECT value FROM app_settings WHERE key = 'shipping.next_day_from'")).scalar() == "12:00"
        with _pytest.raises(IntegrityError):
            with eng.begin() as c:
                c.execute(text("INSERT INTO users(username, password_hash, role) VALUES ('x', 'h', 'superuser')"))
        eng.dispose()
    finally:
        drop_database(url)
