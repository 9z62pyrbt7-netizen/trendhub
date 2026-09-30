"""YALNIZCA YEREL TEST: Trendçantanız benzeri katalog ve Trendyol siparişleri (GERÇEK VERİ DEĞİLDİR).

Yalnızca adı "dev" veya "test" içeren bir geliştirme veritabanına yazar; ürün/sipariş tablolarını BOŞALTIR.
Production veritabanına ASLA yöneltmeyin. Görseller genimg.py ile üretilip yerel bir HTTP sunucusundan servis edilir.
"""
import json, os, random, sys
from sqlalchemy import create_engine, text
random.seed(3)
url = os.environ["DATABASE_URL"].replace("postgresql://", "postgresql+psycopg://")
from sqlalchemy.engine import make_url
assert any(x in (make_url(url).database or "") for x in ("dev", "test")), "yalnızca geliştirme veritabanı"
eng = create_engine(url)
IMG = os.environ.get("E2E_IMG_BASE", "http://127.0.0.1:8765")
files = sorted(os.listdir(os.environ.get("E2E_IMG_DIR") or os.path.join(os.path.dirname(__file__), ".img")))
MODELS = [
 ("Kapitone Zincir Askılı Omuz Çantası", "Kadın > Çanta > Omuz Çantası", 1299.90),
 ("Hobo Form Yumuşak Deri Omuz Çantası", "Kadın > Çanta > Omuz Çantası", 1149.90),
 ("Mini Kutu Çapraz Çanta", "Kadın > Çanta > Çapraz Çanta", 899.90),
 ("Geniş Hacimli Günlük Tote Çanta", "Kadın > Çanta > El Çantası", 1049.90),
 ("Metal Tokalı Satchel El Çantası", "Kadın > Çanta > El Çantası", 1399.90),
 ("Yarım Ay Baget Omuz Çantası", "Kadın > Çanta > Omuz Çantası", 979.90),
 ("Ayarlanabilir Askılı Çapraz Çanta", "Kadın > Çanta > Çapraz Çanta", 849.90),
 ("Hasır Detaylı Plaj Tote Çanta", "Kadın > Çanta > El Çantası", 749.90),
 ("Kroko Desenli Zarf Portföy Çanta", "Kadın > Çanta > Portföy Çanta", 689.90),
 ("Dokulu Sırt Çantası", "Kadın > Çanta > Sırt Çantası", 1189.90),
 ("Kilitli Kapaklı Omuz Çantası", "Kadın > Çanta > Omuz Çantası", 1249.90),
 ("Örgü Detaylı Çapraz Çanta", "Kadın > Çanta > Çapraz Çanta", 929.90),
 ("Minimal Laptop Tote Çanta", "Kadın > Çanta > El Çantası", 1499.90),
 ("Saten Abiye Clutch", "Kadın > Çanta > Portföy Çanta", 639.90),
]
with eng.begin() as c:
    c.execute(text("TRUNCATE storefront_cart_items, storefront_carts, stock_reservations, storefront_payment_events, storefront_orders, storefront_products, financial_transactions, order_status_history, order_items, orders, supplier_products, products, stores RESTART IDENTITY CASCADE"))
    sid = c.execute(text("""INSERT INTO suppliers(code, name, integration_type) VALUES ('canta_bayim','Çanta Bayim','xml')
                            ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name RETURNING id""")).scalar()
    pids = []
    for m, (name, cat, price) in enumerate(MODELS):
        colors = sorted({f.split("-")[1] for f in files if f.startswith(f"m{m}-")})
        for color in colors:
            imgs = [f"{IMG}/m{m}-{color}-{k}.jpg" for k in range(4)]
            barcode = f"86{random.randint(10**10, 10**11-1)}"
            stock = random.choice([0, 1, 2, 4, 8, 15, 22]) if m not in (0, 2) else 12
            desc = f"<p>{name}, {color} renk. Günlük kullanıma uygun, iç bölmeli ve fermuarlı.</p><ul><li>Ayarlanabilir askı</li><li>İç cep</li></ul>"
            pid = c.execute(text("""INSERT INTO products(sku, barcode, name, brand, category, model_code, description, images, cost,
                                     sale_price, stock, vat_rate, is_active, preferred_supplier_id, stock_updated_at, created_at, updated_at)
                                    VALUES (:sku, :bc, :name, 'Trendçantanız', :cat, :mc, :desc, CAST(:imgs AS JSONB), :cost, :price, :stock, 20, TRUE, :sid,
                                            NOW() - INTERVAL '3 hours', NOW() - make_interval(days => :age), NOW()) RETURNING id"""),
                            {"sku": f"TC-{m:03d}-{color[:3].upper()}", "bc": barcode, "name": f"{name}", "cat": cat, "mc": f"TCM{m:03d}",
                             "desc": desc, "imgs": json.dumps(imgs), "cost": round(price * .45, 2), "price": price, "stock": stock, "sid": sid,
                             "age": m * 3}).scalar()
            c.execute(text("""INSERT INTO supplier_products(supplier_id, product_id, supplier_sku, barcode, name, color, parent_code, cost, stock, status, images)
                              VALUES (:s, :p, :ssku, :bc, :n, :col, :pc, :cost, :stock, 'active', CAST(:imgs AS JSONB))"""),
                      {"s": sid, "p": pid, "ssku": f"CB{pid:05d}", "bc": barcode, "n": name, "col": color.capitalize(), "pc": f"CBM{m:03d}",
                       "cost": round(price * .45, 2), "stock": stock, "imgs": json.dumps(imgs)})
            pids.append((pid, price, name))
    ty = c.execute(text("SELECT id FROM marketplaces WHERE code='trendyol'")).scalar()
    store = c.execute(text("INSERT INTO stores(marketplace_id, name, external_id) VALUES (:m,'Trendyol Mağaza','123') RETURNING id"), {"m": ty}).scalar()
    weights = {1: 9, 4: 6, 0: 4, 7: 3, 10: 2}
    n = 0
    for idx, (pid, price, name) in enumerate(pids):
        for _ in range(weights.get(idx, 0)):
            n += 1
            oid = c.execute(text("""INSERT INTO orders(store_id, external_order_id, status, internal_status, gross_revenue, order_date, source, customer_name, customer_city)
                                    VALUES (:s, :e, 'Delivered', 'delivered', :p, NOW() - make_interval(days => :d), 'trendyol', 'Test Müşteri', 'İstanbul') RETURNING id"""),
                            {"s": store, "e": f"TY{n:06d}", "p": price, "d": random.randint(4, 60)}).scalar()
            c.execute(text("""INSERT INTO order_items(order_id, product_id, external_line_id, sku, product_name, quantity, unit_price)
                              VALUES (:o, :p, :l, 'x', :n, 1, :price)"""), {"o": oid, "p": pid, "l": f"L{n}", "n": name, "price": price})
    # web mağazası ayarları: test için hazır
    for k, v in {"storefront.bank_transfer_enabled": True, "storefront.bank_transfer_iban": "TR330006100519786457841326",
                 "storefront.bank_transfer_account_name": "Trendçantanız Test Ltd. Şti.", "storefront.bank_transfer_bank_name": "Test Bankası",
                 "storefront.cash_on_delivery_enabled": True, "storefront.cash_on_delivery_fee": "39.90",
                 "storefront.shipping_fee": "49.90", "storefront.free_shipping_threshold": "1000.00",
                 "storefront.seller": {"title": "Trendçantanız Test Ltd. Şti.", "address": "Test Mah. Örnek Sok. No:1 Kadıköy/İstanbul", "phone": "0850 000 00 00", "email": "destek@example.com"},
                 "storefront.legal": {s: f"{s} test metni.\nİkinci paragraf." for s in ["mesafeli-satis-sozlesmesi", "on-bilgilendirme-formu", "kvkk-aydinlatma-metni", "iade-ve-degisim", "teslimat-bilgileri"]},
                 "storefront.social": {"instagram": "https://instagram.com/example", "whatsapp": "908500000000"}}.items():
        c.execute(text("INSERT INTO app_settings(key, value) VALUES (:k, CAST(:v AS JSONB)) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"),
                  {"k": k, "v": json.dumps(v)})
print("products", len(pids), "orders", n)
