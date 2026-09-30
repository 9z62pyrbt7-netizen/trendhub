# Web mağazası uçtan uca testi (tarayıcı)

Masaüstü (1440×900) ve mobil (iPhone 13) görünümde gerçek bir tarayıcıyla satın alma akışını dener:
kartta hızlı sepete ekleme, yan sepet (adet artır/azalt), ürün sayfası (adet, sepete ekle, mobil yapışkan buton),
sepet sayfası, ödeme formu doğrulamaları ve onay zorunluluğu, Havale/EFT siparişi (IBAN'lı sipariş sayfası),
kapıda ödeme siparişi, CSRF koruması, mobil menü, anlık arama, sıralama.

**Yalnızca geliştirme veritabanıyla** çalıştırın (`seed_dev.py` ürün/sipariş tablolarını boşaltır ve
test verisi yazar; veritabanı adında `dev` veya `test` yoksa çalışmaz).

```bash
cd backend/tests/e2e
python genimg.py                                   # test görselleri → .img/
(cd .img && python -m http.server 8765 --bind 127.0.0.1 &)
export DATABASE_URL=postgresql://postgres:postgres@localhost:5432/trendhub_dev
(cd ../.. && alembic upgrade head)
python seed_dev.py
(cd ../.. && APP_SECRET=dev-secret-0123456789abcdef SUPPLIER_ALLOW_PRIVATE_URLS=true COOKIE_SECURE=false \
   uvicorn app.storefront.main:app --port 8090 &)
npm i playwright && npx playwright install chromium
node storefront.e2e.mjs                            # BASE=http://127.0.0.1:8090 (varsayılan)
```

`SUPPLIER_ALLOW_PRIVATE_URLS=true` yalnızca bu yerel testte, görsel proxy'sinin 127.0.0.1'deki test
görsellerini okuyabilmesi içindir; canlıda kullanmayın.
