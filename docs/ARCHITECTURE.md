# TrendHub — Mimari, Kararlar ve Çalıştırma Kılavuzu

TrendHub; Trendyol, Hepsiburada, Amazon.com.tr ve ileride eklenecek pazaryerlerini tek
merkezden yöneten sipariş, stok ve kârlılık platformudur.

> **Kapsam dışı:** Canlı Trendyol → Çanta Bayim sipariş otomasyonu ayrı sunucuda
> (`/opt/trendcantamiz-xml`) çalışır. Bu repo o sistemi içermez, ona bağlanmaz ve onu
> tetiklemez. TrendHub pazaryeri API'lerini **salt okunur** kullanır
> (`CONNECTOR_WRITE_ENABLED=false`). Bu yüzden aynı Trendyol hesabı kullanılsa bile
> sipariş statüsü, stok veya fiyat değiştirmez.

## 1. Başlangıç analizi (commit `a574635`)

| Alan | Durum | Risk |
|---|---|---|
| Kimlik doğrulama | Yoktu; tüm `/api/*` herkese açıktı, CORS `*` | **Yüksek**: finans verisi internete açık |
| Şema yönetimi | API açılışında `CREATE TABLE IF NOT EXISTS` | Kolon eklemenin/değiştirmenin güvenli yolu yoktu |
| Çalışma zamanı | `python:3.12-slim` her açılışta `pip install` yapıyordu | Yavaş/kırılgan açılış, sürüm kayması |
| DB erişimi | Her istekte yeni bağlantı | Yük altında bağlantı tükenmesi |
| Sipariş statüsü | Ham pazaryeri statüsü (`Created`, `Picking`…) | Pazaryerleri arası ortak model yoktu |
| Finans | Sipariş başına tek `net_profit` | SKU kârı, maliyet geçmişi, reklam/iade ayrımı yoktu |
| Worker / retry / rate limit / audit | Yoktu | Senkronizasyon, hata takibi ve denetim yapılamıyordu |
| Seed | Trendyol `enabled=TRUE` (credential olmasa da) | Arayüz "bağlı" izlenimi verebilirdi |
| `ADMIN_*`, `APP_SECRET` | `.env.example`'da vardı ama kullanılmıyordu | Yanlış güvenlik algısı |
| Healthcheck | `pg_isready -U trendhub` sabit | Farklı kullanıcı adıyla bozulur |

## 2. Hedef mimari

```
             ┌────────────── nginx (web, :8081) ───────────────┐
Tarayıcı ──▶ │ statik SPA (/)  ·  CSP / güvenlik başlıkları     │
             │ /api/*  ──────────────▶  api (FastAPI, 2 worker)  │
             └───────────────────────────────┬─────────────────┘
                                             │ SQLAlchemy havuzu
  worker (python -m app.worker) ─────────────┤
    · zamanlayıcı (advisory-lock lider)      ▼
    · iş kuyruğu (sync_jobs, SKIP LOCKED) ─▶ PostgreSQL 16 (trendhub_db volume)
    · connector'lar ──HTTP (rate limit + retry)──▶ Trendyol / HB / Amazon API
  migrate (tek seferlik: alembic upgrade head)
```

* **Tek veritabanı, ek altyapı yok.** Kuyruk, kilit ve zamanlayıcı PostgreSQL üzerinde
  (`FOR UPDATE SKIP LOCKED`, `pg_try_advisory_lock`). Redis/Celery ihtiyacı yok.
* **Katmanlar:** `app/connectors` (yalnızca API ↔ normalize veri) → `app/services`
  (idempotent yazma, finans, kuyruk) → `app/api` (HTTP, yetki, audit). `app/domain` saf
  iş kuralı içerir (statü makinesi, kâr hesabı) ve DB'siz test edilir.

### Dizinler

```
backend/
  app/config.py          ortam değişkenleri (secret'lar yalnızca env'den)
  app/db.py              bağlantı havuzu, transaction yardımcıları
  app/security.py        argon2, oturum, kilitleme, ilk admin
  app/deps.py            oturum/rol bağımlılıkları
  app/domain/            order_status.py, finance.py (saf mantık)
  app/connectors/        base.py (arayüz), http.py (salt okunur istemci, rate limit+retry), trendyol.py, hepsiburada.py, amazon_tr.py, registry.py
  app/services/          orders_sync.py, listings_sync.py, finance_service.py, jobs.py, sync_service.py, audit.py, events.py, app_settings.py
  app/api/               auth, analytics (dashboard/finans/rapor), orders, catalog (ürün/kargo/tedarikçi), integrations, system
  app/worker.py          worker + zamanlayıcı
  app/cli.py             healthcheck (worker/db), create-user, reset-password
  migrations/versions/   0001_baseline, 0002_platform_core, 0003_listing_details
  tests/                 gerçek PostgreSQL ile testler
scripts/smoke_test.sh    çalışan yığına karşı uçtan uca duman testi
web/                     index.html, assets/app.js, assets/app.css, nginx.conf
```

## 3. Veritabanı ve migration politikası

* **Alembic.** Migration'lar API açılışında değil, `migrate` servisinde çalışır.
* **`0001_baseline`**, eski `init_db()` şemasının birebir kopyasıdır (`IF NOT EXISTS`). Mevcut
  veritabanında hiçbir şey değiştirmez, yalnızca Alembic'in takibini başlatır.
* **`0003_listing_details`**: `marketplace_listings` tablosuna ilan kolonları ekler (yalnızca ekleme).
* **`0002_platform_core`** yalnızca ekleme yapar. `DROP`, `TRUNCATE`, `DELETE` ve tip
  değişikliği yoktur. Bu kural `tests/test_migrations.py` tarafından otomatik denetlenir.
  * `orders.status` **ham pazaryeri statüsü olarak kalır**. Yeni `orders.internal_status`
    eklenir ve mevcut kayıtlar için ham statüden türetilir. Eşlenemeyenler
    `needs_review` olur ve `review_reason` doldurulur.
  * Mevcut veride çakışma olabilecek unique index'ler (`products.sku`,
    `stores(marketplace_id, external_id)`) önce unique olarak denenir. Tekrarlı kayıt
    varsa normal index kurulur, NOTICE yazılır ve **veri silinmez**.
  * Tüm migration'lar tek transaction'da ve advisory lock altında çalışır.
* Legacy veriyle test: eski `init_db()` ile oluşturulan ve veri içeren bir DB'ye
  migration uygulanıp satır sayıları ve değerler karşılaştırılır.

Yeni tablolar: `users`, `user_sessions`, `audit_logs`, `system_events`, `worker_heartbeats`,
`app_settings`, `product_costs`, `marketplace_listings`, `order_status_history`, `suppliers`,
`supplier_products`, `financial_transactions`, `sync_state`.

## 4. Kimlik doğrulama ve yetki

* Parolalar **argon2** ile hash'lenir. Oturum belirteci 256 bit rastgele değerdir ve DB'de
  yalnızca SHA-256 özeti tutulur. Çerez `HttpOnly` + `SameSite=Strict` (+ `Secure`) ayarlıdır.
* **CSRF:** değiştirici isteklerde `X-Requested-With: TrendHub` başlığı zorunludur
  (başka bir site bu başlığı ekleyemez).
* **Kaba kuvvet koruması:** 5 hatalı denemede hesap 15 dk kilitlenir. Buna ek olarak
  uygulama içinde IP başına pencere limiti ve nginx `limit_req` vardır.
* **Roller:** `viewer` (okuma), `operator` (sipariş statüsü, maliyet, gider, senkron
  tetikleme), `admin` (ayarlar, kullanıcılar, audit log).
* **İlk admin:** `users` tablosu boşsa ve `ADMIN_PASSWORD` en az 12 karakterse API
  açılışında oluşturulur.
* Parola değişince diğer oturumlar kapatılır. Pasifleştirilen kullanıcının oturumları
  da iptal edilir.

## 5. API

| Grup | Uç noktalar |
|---|---|
| Auth | `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me`, `POST /api/auth/change-password` |
| Dashboard | `GET /api/dashboard?period=today\|7d\|30d\|90d` |
| Siparişler | `GET /api/orders` (filtre, arama, sıralama, sayfalama), `GET /api/orders/{id}`, `POST /api/orders/{id}/status`, `POST /api/orders/{id}/adjustments`, `GET /api/orders/meta` |
| Ürün & Stok | `GET/POST /api/products`, `PATCH /api/products/{id}`, `POST /api/products/{id}/cost`, `GET /api/products/{id}/costs`, `GET /api/listings` (salt okunur pazaryeri ilanları), `POST /api/listings/import-products` |
| Kargo | `GET /api/shipments`, `PATCH /api/shipments/{id}` |
| Tedarikçiler | `GET/POST /api/suppliers`, `PUT /api/suppliers/{id}`, `GET /api/supplier-orders` |
| Finans | `GET /api/finance/summary`, `GET/POST /api/finance/expenses`, `DELETE /api/finance/expenses/{id}` (yalnızca panelden girilenler) |
| Raporlar | `GET /api/reports/sku`, `GET /api/reports/sku.csv`, `GET /api/reports/statuses`, `GET /api/reports/top-loss` |
| Entegrasyonlar | `GET /api/integrations`, `POST /api/integrations/{code}/check`, `POST /api/integrations/{code}/sync` (`{"kind": "orders"\|"listings", "lookback_days"?}`) |
| Sistem | `GET /api/health` (herkese açık, minimal), `GET /api/system/health`, `GET /api/system/jobs`, `POST /api/system/jobs/{id}/retry`, `GET /api/system/events`, `POST /api/system/events/{id}/resolve`, `GET /api/system/audit` |
| Ayarlar | `GET/PUT /api/settings`, `GET/POST /api/users`, `PATCH /api/users/{id}` |

Tüm değiştirici işlemler `audit_logs` tablosuna yazılır: kim, ne zaman, hangi IP, önce ve
sonra. OpenAPI dokümanı yalnızca `APP_ENV` production değilken `/api/docs` altında açılır.

## 6. Sipariş statüleri

| Kod | Etiket | Trendyol ham statüleri |
|---|---|---|
| `new` | Yeni | Awaiting, Created |
| `preparing` | Hazırlanıyor | Picking, UnPacked |
| `sent_to_supplier` | Tedarikçiye Aktarıldı | (manuel; pazaryerinde karşılığı yok) |
| `awaiting_shipment` | Kargoya Verilmeyi Bekliyor | Invoiced |
| `shipped` | Kargoda | Shipped, AtCollectionPoint |
| `delivered` | Teslim Edildi | Delivered |
| `cancelled` | İptal | Cancelled, UnSupplied |
| `returned` | İade | Returned |
| `needs_review` | Hata / İnceleme Gerekiyor | UnDelivered, bilinmeyen statüler |

Senkronizasyon kuralları (`domain/order_status.resolve_sync_status`):

* İlerleme statülerinde **geri gidilmez**. Örneğin panelde "Tedarikçiye Aktarıldı"
  olarak işaretlenmiş bir sipariş için pazaryeri "Picking" dese de iç statü korunur.
* İptal, iade ve inceleme her zaman uygulanır.
* İncelemedeki sipariş, bir kullanıcı çözene kadar incelemede kalır.
* İptal/iade edilmiş bir sipariş pazaryerinde yeniden ilerlerse inceleme durumuna alınır.
* Birden çok paketli siparişte aktif paketlerin en geride olanı esas alınır.

Manuel geçişler `MANUAL_TRANSITIONS` ile sınırlıdır ve her değişiklik
`order_status_history` ile audit log'a yazılır. Manuel statü değişikliği pazaryerine
**gönderilmez**.

## 7. Finans modeli (sipariş / SKU seviyesi)

```
net_kar   = ciro − ürün_maliyeti − komisyon − hizmet_bedeli − kargo − reklam − iade − diğer
kar_marji = net_kar / ciro
```

* Hesap `order_items` (SKU) seviyesinde yapılır. Sipariş toplamı kalemlerin toplamıdır, bu
  yüzden sipariş ve SKU raporları aynı sayıları verir.
* Siparişe ait ortak giderler (kargo, hizmet bedeli, siparişe girilmiş reklam/diğer)
  kalemlere **ciro payı oranında** dağıtılır. Kuruş farkı son kaleme eklenir.
* **Kaynak önceliği:** bileşen başına
  1. `financial_transactions` (gerçek hakediş veya manuel giriş)
  2. bilinen tutar (ör. `shipments.cost`)
  3. `app_settings` içindeki tahmini oran/tutar

  Tahmin kullanılan sipariş `finance_is_estimate=TRUE` olarak işaretlenir ve arayüzde
  "Tahmini" rozetiyle gösterilir.
* **Ürün maliyeti:** `product_costs` geçmişinden, sipariş tarihinde geçerli olan değer
  kalem üzerine snapshot olarak yazılır. Maliyet değişikliği geçmiş siparişleri değiştirmez.
  Yalnızca maliyeti hiç girilmemiş kalemler yeniden hesaplanır.
* **İptal:** ne ciro ne gider oluşur.
* **İade:** iade tutarı ciroya eşittir. Komisyon iade edilmiş kabul edilir (gerçek tutar
  ledger'dan gelirse o kullanılır). Ürün varsayılan olarak stoğa döner. Stoğa dönmüyorsa
  `finance.return_product_cost_is_loss=true` yapılabilir. Kargo ve hizmet bedeli
  kayıp olarak kalır.
* **Dönem giderleri** (`expenses`): reklam, ambalaj, personel gibi giderler dönem
  kârından düşülür. `sku` girilmiş reklam giderleri SKU raporuna yansır.
* **Legacy kayıtlar:** kalemi olmayan eski siparişlerin tutarlarına **dokunulmaz**.

## 8. Connector mimarisi

`MarketplaceConnector` arayüzü (`app/connectors/base.py`):

* `credential_fields` / `credential_values()` / `missing_credentials()` / `is_configured()`
* `capabilities` (`orders.read`, `products.read`) ve `incremental` (değişen siparişleri döndürür mü)
* `test_connection()`, `fetch_orders(since, until) -> list[NormalizedOrder]`, `fetch_listings() -> list[NormalizedListing]`
* `update_stock()` / `update_price()`: `CONNECTOR_WRITE_ENABLED=false` iken `WriteDisabled` verir. `true` olsa bile **hiçbir connector'da yazma uygulanmadı**.

**Salt okunurluk iki katmanda garanti edilir:**
1. Connector'larda yazma metodu uygulanmadı.
2. `ResilientClient` varsayılan olarak `read_only=True` çalışır. GET/HEAD dışındaki her istek ağa hiç çıkmadan reddedilir.

Tek istisna Amazon LWA token isteğidir (`POST api.amazon.com/auth/o2/token`). Bu istek oturum belirteci alır, pazaryeri verisini değiştirmez ve ayrı bir istemciyle yapılır. Testler, tam bir senkron boyunca giden tüm pazaryeri isteklerinin GET olduğunu doğrular.

Her connector bağımsızdır. Birinin hatası diğerini etkilemez ve her iş ayrı kuyruk kaydıdır. Yeni bir pazaryeri eklemek için bir sınıf yazıp `registry.py`'ye bir satır eklemek yeterlidir.

| Connector | Durum |
|---|---|
| Trendyol | **Sipariş okuma:** sayfalama, 14 günlük pencereler, paket birleştirme, günlük 60 günlük derin tarama. **İlan okuma:** ürün filtreleme servisi → `marketplace_listings`. Alan eşlemesi dokümantasyona göre yapıldı; **canlı hesapla doğrulanmadı.** |
| Amazon.com.tr | **Sipariş okuma:** LWA token + Orders API v0 (`getOrders`, `getOrderItems`), SP-API rate limitleri, `LastUpdatedAfter` ile artımlı (watermark − 1 saat). Kargo takibi, müşteri adı ve komisyon bu API'de yok. **Canlı hesapla doğrulanmadı.** |
| Hepsiburada | Credential algılama ve statü eşlemesi hazır. **Sipariş/ilan okuma uygulanmadı.** API sözleşmesi doğrulanmadan tahminle kod yazılmadı. |

Credential yoksa entegrasyon **"Bağlı değil"** görünür, hiçbir iş planlanmaz ve hiçbir veri üretilmez. Credential değerleri API'den hiçbir zaman dönmez; yalnızca "Tanımlı / Eksik" bilgisi gösterilir.

### Zamanlayıcı

Yalnızca bilgisi tanımlı ve ilgili yeteneği olan connector'lar için çalışır. Lider worker
`pg_try_advisory_lock` ile seçilir.

| İş | Aralık | Not |
|---|---|---|
| `orders.sync` | `SYNC_INTERVAL_MINUTES` (15 dk) | Son 14 gün; artımlı connector'da watermark |
| `orders.deep_sync` | 24 saat | Son 60 gün; geç iade/teslim statüleri için. Artımlı connector'da atlanır |
| `listings.sync` | 6 saat | Yalnızca `products.read` yeteneği olanlar |
| `integration.check` | 60 dk | Entegrasyon ekranındaki bağlantı durumu |

## 9. Güvenilirlik

* **Idempotency**
  * Sipariş: `(store_id, external_order_id)` (mevcut UNIQUE)
  * Kalem: `(order_id, external_line_id)`
  * Paket: `(order_id, external_package_id)`
  * `payload_hash` değişmemişse sipariş satırı güncellenmez
  * İş kuyruğunda aynı `idempotency_key` ile ikinci bekleyen iş oluşamaz
  * `supplier_orders.idempotency_key` ve `financial_transactions(source, external_ref)` unique
* **Hata izolasyonu:** her sipariş kendi savepoint'inde işlenir. Bozuk bir sipariş diğerlerini geri almaz.
* **Retry/backoff:**
  * HTTP katmanında 408/425/429/5xx ve ağ hatalarında jitter'lı üstel bekleme yapılır; `Retry-After` dikkate alınır.
  * 401/403 tekrar denenmez (`AuthError`).
  * İş katmanında `max_attempts`'a kadar üstel geri çekilme uygulanır; sonra `dead` olur ve sistem olayı yazılır.
* **Rate limit:** connector başına token bucket (`TRENDYOL_RATE_PER_MINUTE`, varsayılan 60/dk). Bunun dışında nginx'te giriş limiti vardır.
* **Kurtarma:**
  * Çalışan iş boyunca bir arka plan thread'i her 30 sn'de iş kilidini (`locked_at`) ve worker heartbeat'ini tazeler.
  * 5 dakikadan uzun süre tazelenmeyen `running` iş yeniden kuyruğa alınır.
  * Sonuç (`complete`/`fail`) yalnızca işi hâlâ tutan worker tarafından yazılabilir.
  * Worker SIGTERM ile mevcut işi bitirip temiz kapanır.
* **Sağlık izleme:**
  * `/api/health`: liveness
  * `/api/system/health`: DB, şema sürümü, worker heartbeat, kuyruk, açık hatalar
  * compose healthcheck: api (`/api/health`), worker (`python -m app.cli healthcheck worker`), web (nginx üzerinden)
  * `python -m app.cli healthcheck db`: şema en güncel sürümde mi
  * `system_events`: fingerprint ile tekrar sayacı tutar, ekranı doldurmaz

## 10. Kurulum / yükseltme (production)

> Aşağıdaki adımlar TrendHub sunucusu içindir; `/opt/trendcantamiz-xml` ile ilgisi yoktur.

1. **Yedek alın (zorunlu):**
   `docker compose exec db pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB" > trendhub-$(date +%F).dump`
2. `.env` dosyasını `.env.example`'daki yeni değişkenlerle tamamlayın. Özellikle:
   * `ADMIN_PASSWORD`: en az 12 karakter
   * `COOKIE_SECURE`: HTTPS yoksa `false`
   * `CONNECTOR_WRITE_ENABLED=false`
3. `docker compose build && docker compose up -d`. Önce `migrate` çalışır, başarılı olursa `api` ve `worker` açılır.
4. `docker compose logs migrate` çıktısında `0002_platform_core` görülmelidir.
5. `docker compose ps` çıktısında `api`, `worker` ve `web` servisleri `healthy` görünmelidir.
6. `ADMIN_USER=… ADMIN_PASSWORD=… BASE_URL=http://sunucu:8081 scripts/smoke_test.sh` çalıştırılır.
7. Panele admin ile girin → Entegrasyonlar → "Bağlantıyı test et".

Parola sıfırlama:
`docker compose exec api python -m app.cli reset-password --username admin`
(parola etkileşimli sorulur).

Geri dönüş: migration'lar yalnızca ekleme yaptığı için eski imaj yeni şemayla çalışmaya
devam eder. Gerekirse 1. adımdaki yedekten `pg_restore` ile dönülebilir.

## 11. Test ve CI

* `backend/tests`: gerçek PostgreSQL 16 ile çalışır.
  * migration (legacy veri korunumu, yıkıcı ifade taraması)
  * domain (statü, finans)
  * connector'lar: mock HTTP, salt okunurluk garantisi, retry/401/429, Amazon LWA + sayfalama
  * idempotent sipariş/ilan senkronu
  * iş kuyruğu (sahiplik, stale kurtarma, keepalive)
  * uçtan uca worker
  * API (auth, CSRF, RBAC, kilitleme, finans, raporlar, CSV)
  * CLI
* **CI (`.github/workflows/ci.yml`):**
  * pytest
  * `node --check`
  * `nginx -t`
  * `.env` commit koruması
  * Docker işi: `compose config`, imaj build, tüm yığını ayağa kaldırma, duman testi, ikinci `migrate` çalıştırması

## 12. Bilinen sınırlamalar / kalan işler

* Trendyol ve Amazon alan eşlemeleri **canlı veriyle doğrulanmalı**. Özellikle Trendyol `price`/`discount` anlamı ve `orderDate` saat dilimi, Amazon `ItemPrice` ve `PromotionDiscount` KDV davranışı.
* 60 günden eski siparişlerin statü değişiklikleri Trendyol'da yakalanmaz.
* Trendyol hakediş (settlement) API'si bağlanmadı; komisyon şu an **tahmini**. Gerçek tutar manuel girilebilir.
* Amazon Finances API (gerçek ücretler) ve Amazon ilan okuma (Reports API) yok.
* Hepsiburada sipariş/ilan okuma yok.
* Stok/fiyat gönderimi bilinçli olarak kapalı ve uygulanmadı.
* Tedarikçi (Çanta Bayim) aktarım kayıtları production sisteminden okunmuyor. Entegrasyon yöntemi (salt okunur DB/replika, dosya veya API) kararlaştırılmalı.
* KDV ayrımı: tutarlar KDV dahil tutuluyor, KDV hariç kâr raporu yok.
* Rate limit süreç içindedir. Birden fazla worker çalıştırılırsa limit worker başına uygulanır.
* Credential'lar yalnızca env'de, tek mağaza/hesap destekleniyor. Çoklu mağaza için şifreli credential deposu gerekir.
* HTTPS nginx önünde bir TLS sonlandırıcı (ör. Caddy, Traefik, certbot) ile sağlanmalıdır.
* Docker imajı ve CI Docker işi bu geliştirme ortamında (Docker yok) çalıştırılamadı. Yığın Docker'sız olarak (uvicorn + worker + nginx 1.24 + PostgreSQL 16) duman testinden geçti.
