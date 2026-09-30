# TrendHub

Trendyol, Hepsiburada, Amazon.com.tr ve ileride eklenecek pazaryerleri için sipariş, stok,
kargo, tedarikçi ve **sipariş/SKU seviyesinde kârlılık** yönetim paneli.

Aynı altyapı üzerinde **Trendçantanız web mağazası** (bağımsız satış kanalı) da çalışır: ürün,
fiyat ve stok TrendHub kataloğundan gelir; web siparişleri TrendHub'da "Trendçantanız Web" kanalı
olarak diğer pazaryerleriyle aynı panelden yönetilir (bkz. ARCHITECTURE.md §8d).

Mimari, kararlar, migration politikası ve kurulum adımları için:
**[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

## Hızlı başlangıç

```bash
cp .env.example .env         # değerleri doldurun; .env commit edilmez
docker compose build
docker compose up -d         # migrate → api + worker + storefront → web
                             # panel: http://sunucu:8081 · mağaza: http://sunucu:8090
```

> Mevcut bir kurulumu yükseltmeden önce veritabanı yedeği alın
> (bkz. docs/ARCHITECTURE.md §10).

## Geliştirme ve test

Testler gerçek bir PostgreSQL 16 ister. Test altyapısı `trendhub_test_*` adlı geçici
veritabanları oluşturup siler.

```bash
cd backend
pip install -r requirements-dev.txt
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres pytest
```

Web mağazasını yerelde çalıştırmak (yalnızca geliştirme veritabanına karşı):

```bash
cd backend
DATABASE_URL=postgresql://…/trendhub_dev APP_SECRET=dev-secret-0123456789 uvicorn app.storefront.main:app --port 8090
```

Tarayıcı ile uçtan uca satın alma testi: `backend/tests/e2e/README.md`.

## Güvenlik notları

* API anahtarları yalnızca `.env` içinde durur. Panel ve API credential değerlerini göstermez.
* Pazaryerine yazma işlemleri varsayılan olarak kapalıdır (`CONNECTOR_WRITE_ENABLED=false`).
* Canlı Trendyol → Çanta Bayim otomasyonu (`/opt/trendcantamiz-xml`) bu repoda değildir
  ve TrendHub tarafından tetiklenmez.
