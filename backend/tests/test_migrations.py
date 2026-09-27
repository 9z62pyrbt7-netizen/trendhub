"""Migration'ların mevcut (legacy) veriyi korumasını doğrular."""
import importlib.util
import os
import subprocess

from sqlalchemy import create_engine, text

from .conftest import _sa, create_database, drop_database, run_migrations

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
            assert c.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0003_listing_details"
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
        for bad in ("DROP ", "TRUNCATE", "DELETE", "ALTER COLUMN TYPE", " TYPE "):
            assert bad not in src, f"{name} içinde yıkıcı ifade: {bad}"
