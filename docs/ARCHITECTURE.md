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
* **`0004_supplier_management`**: çoklu tedarikçi + ürün aktarımı tabloları (bkz. §8a). Tek gevşetme:
  `supplier_products.product_id` NOT NULL kısıtı kaldırılır (tedarikçi ürünü kataloğa bağlanmadan
  havuzda durabilsin); veri değişmez. Bu istisna `test_migrations.py` içinde açık izin listesiyle
  sınırlıdır; başka hiçbir `DROP` kabul edilmez.
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
* **Tahmini KDV (vergi):** kalem bazında `(net satış − ürün maliyeti)` içindeki KDV,
  yani `tutar × r / (100 + r)`. Oran önceliği: kalem `vat_rate` > ürün `vat_rate` > %20.
  Tutarlar KDV dahil kabul edilir; iptal/iade edilen satışta 0 alınır. "Net kâr (KDV sonrası)" =
  net kâr − tahmini KDV. Beyanname yerine geçmez; arayüzde **TAHMİNİ** olarak işaretlenir.
* **İndirim:** satıcının üstlendiği indirim (`lineSellerDiscount`) bilgi olarak gösterilir.
  Birim fiyata zaten yansıdığı için ayrıca düşülmez. Trendyol'un üstlendiği indirim satıcı gideri değildir.
* **Decimal:** tüm hesaplar `Decimal`/PostgreSQL `NUMERIC` ile yapılır; dashboard, finans ve
  rapor toplamları da Decimal olarak taşınır. Sayıya dönüşüm yalnızca JSON çıktısında olur.
* Pazaryeri hakediş verisi bağlanmadığı sürece finans yanıtları `is_estimate: true` taşır ve
  panel tüm tahmini değerleri **TAHMİNİ** rozetiyle gösterir.

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
| Trendyol | **Sipariş okuma (Order V2):** `GET /integration/order/sellers/{id}/v2/orders` (15.10.2026'dan itibaren zorunlu; 404 dönerse bir kez v1'e düşer). Sayfalama, 14 günlük pencereler, en fazla 30 gün geriye (servis sınırı: 1 ay, 10.000 kayıt, 1000 istek/dk), paket birleştirme. 6 Nisan 2026'da yeniden adlandırılan alanlar (`shipmentPackageId`, `lineId`, `stockCode`, `lineUnitPrice`, `lineGrossAmount`, `lineSellerDiscount`, `vatRate`) önceliklidir; eski adlar yedektir. `orderDate` GMT+3 olarak gelir ve UTC'ye çevrilir. Kaynak: developers.trendyol.com changelog ve servis dokümanları. **Canlı hesapla doğrulanmadı.** **İlan okuma:** Ürün V1 kapatılıyor, V2 filtre şeması doğrulanamadı → `TRENDYOL_LISTINGS_ENABLED=false` (varsayılan kapalı). |
| Amazon.com.tr | **Sipariş okuma:** LWA token + Orders API v0 (`getOrders`, `getOrderItems`), SP-API rate limitleri, `LastUpdatedAfter` ile artımlı (watermark − 1 saat). Kargo takibi, müşteri adı ve komisyon bu API'de yok. **Canlı hesapla doğrulanmadı.** |
| Hepsiburada | **Sipariş (paket) okuma:** `GET https://oms-external.hepsiburada.com/packages/merchantid/{merchantId}?begindate&enddate&limit&offset` (tarih `YYYY-MM-DD HH:mm`, Türkiye saati), HTTP Basic (entegrasyon kullanıcı adı/şifre) + zorunlu `User-Agent` (varsayılan kullanıcı adı), offset sayfalama (`totalcount`), 7 günlük pencereler, en fazla 30 gün geriye; paket kalemleri `orderNumber`'a göre siparişte birleşir; bilinmeyen statü → `needs_review`. Kaynak: developers.hepsiburada.com (arama özetleri; portal bu ortamdan engelli). **Canlı hesapla doğrulanmadı.** **İlan okuma:** `listing-external` `/listings/merchantid/{id}` — şema doğrulanmadı → `HEPSIBURADA_LISTINGS_ENABLED=false`. Yazma yok. |

Credential yoksa entegrasyon **"Bağlı değil"** görünür, hiçbir iş planlanmaz ve hiçbir veri üretilmez. Credential değerleri API'den hiçbir zaman dönmez; yalnızca "Tanımlı / Eksik" bilgisi gösterilir.

### Zamanlayıcı

Yalnızca bilgisi tanımlı ve ilgili yeteneği olan connector'lar için çalışır. Lider worker
`pg_try_advisory_lock` ile seçilir.

| İş | Aralık | Not |
|---|---|---|
| `orders.sync` | `SYNC_INTERVAL_MINUTES` (15 dk) | Son 14 gün; artımlı connector'da watermark |
| `orders.deep_sync` | 24 saat | Son 30 gün (Trendyol sınırı); geç iade/teslim statüleri için. Artımlı connector'da atlanır |
| `listings.sync` | 6 saat | Yalnızca `products.read` yeteneği olanlar (Trendyol'da varsayılan kapalı) |
| `integration.check` | 60 dk | Entegrasyon ekranındaki bağlantı durumu |

## 8a. Tedarikçi yönetimi (çoklu tedarikçi)

Sistem hiçbir tedarikçiye özel değildir; **Çanta Bayim yalnızca ilk tedarikçidir** ve bir şablon
(`app/suppliers/fields.py → PRESETS`) olarak gelir. Tablo/kolon adları geneldir:

| Tablo | Amaç |
|---|---|
| `suppliers` | Tedarikçi kartı + öncelik, stok kuralları (`buffer`, `min_stock`, `max_stock`), senkron sıklığı, son senkron durumu |
| `supplier_connections` | Entegrasyon türü (xml/api/csv/manual), **şifreli** kaynak URL'si + maskeli gösterimi, kimlik doğrulama türü, **şifreli** secret veya `SUPPLIER_*` ortam değişkeni adı, kayıt yolu |
| `supplier_field_mappings` | Tedarikçi alanı (yol) → TrendHub alanı eşleştirmesi |
| `supplier_products` | Tedarikçi teklifi: `supplier_id` zorunlu, `supplier_sku` tedarikçi içinde tekil, `product_id` (global katalog) opsiyonel |
| `supplier_sync_runs`, `supplier_product_changes` | Senkron geçmişi; yeni / fiyat / stok / kaynağında bulunamadı / yeniden göründü günlüğü |
| `products` (+) | `preferred_supplier_id`, `supplier_strategy` |
| `marketplace_rules`, `marketplace_category_mappings` | Pazaryeri başına fiyat/komisyon/stok/zorunlu alan kuralı ve kategori eşleştirmesi |
| `listing_drafts` | (katalog ürünü, pazaryeri) başına **tek** taslak ilan |

**Connector sınıfları** (`app/suppliers/connectors.py`): `XmlFeedConnector`, `JsonApiConnector`
(sayfa/limit parametreli sayfalama; sayfalamayı yok sayan API'de döngüye girmez), `CsvFeedConnector`,
`ManualUploadConnector`; `CONNECTORS` kayıt defteri. Yeni tür = yeni sınıf + bir satır. Her connector
`fetch()`, `from_content()` ve `test_connection()` (panelde **Bağlantıyı test et**, DB'ye yazmaz) sunar.
Çanta Bayim `XmlFeedConnector` kullanan bir şablondur.

**Katman:** `app/suppliers/` — `parsing.py` (XML `defusedxml` ile, JSON, CSV; kayıt yolu otomatik
tespit), `mapping.py` (Türkçe sayı biçimleri, Decimal), `fields.py` (hedef alanlar + eşanlamlılardan
otomatik öneri), `fetch.py` (yalnızca GET, SSRF koruması, boyut sınırı), `secrets.py` (Fernet;
anahtar `APP_SECRET`'tan türetilir). Servisler: `supplier_sync.py`, `supplier_catalog.py`,
`listing_drafts.py`. Saf alan mantığı: `domain/suppliers.py` (stok kuralı, seçim stratejileri),
`domain/pricing.py` (fiyat formülü, tahmini kâr, taslak doğrulama).

**Kimlikler:** tedarikçi SKU'su yalnızca kendi tedarikçisinde tekildir; global katalog kimliği
`products.id`'dir ve tedarikçiler arası eşleşme **barkod** ile yapılır. Aynı barkod iki tedarikçide
varsa tek katalog ürünü altında iki teklif olur; pazaryerinde tek taslak/ilan açılır. Pazaryerinde
zaten aynı barkodlu ilan varsa taslak hata verir (duplicate ilan açılmaz).

**Senkron:** worker işi `supplier.sync` (zamanlayıcı `sync_interval_minutes`'e göre, idempotent)
veya panelden dosya yükleme. Kaynaktan kaybolan ürün **silinmez**, `status='missing'` olur. Kaynak
birden boşalırsa / önceki aktif ürünlerin yarısından azını döndürürse (≥20 ürün) toplu "kayıp"
işareti yapılmaz, çalıştırma `partial` olur ve sistem uyarısı yazılır.

**Seçim stratejileri:** `manual` (tercih edilen), `cheapest`, `highest_stock`, `priority`.
Yenisi `domain/suppliers.STRATEGIES`'e bir fonksiyon eklenerek tanımlanır. Seçilen teklif katalog
ürününün stok ve maliyetini belirler (maliyet değişikliği `product_costs`'a `source='supplier'`
ile yazılır; geçmiş siparişler değişmez). Strateji `manual` ve tercih yoksa ürüne dokunulmaz.

**Ürün aktarımı:** Tedarikçiler → Ürün Havuzu → Ürünleri Seç → Pazaryerini Seç → Fiyatlandır →
Validate → Yayına Hazırla. Fiyat = (maliyet × (1 + kâr oranı) + kargo + sabit gider) ÷ (1 − komisyon),
yuvarlanır. **Pazaryerine gönderim yoktur** (`CONNECTOR_WRITE_ENABLED=false`); hazır taslaklar
yalnızca CSV olarak indirilebilir.

**Tedarikçi karşılaştırma** (`GET /api/supplier-comparison`, panel: Tedarikçiler → Tedarikçi
karşılaştırma): birden çok teklifli ürünlerde teklifler, seçili / en ucuz / en yüksek stoklu tedarikçi
ve adet başı olası tasarruf.

**Çoklu pazaryeri:** pazaryerleri tablo tabanlıdır; her birinin `marketplace_rules` kuralı ve
`marketplace_category_mappings` (kategori ID + özellikler) eşleştirmesi ayrıdır. Panelden yeni pazaryeri
eklenebilir (`POST /api/marketplaces`); connector'ı yoksa "Connector yok" görünür. Her connector
`publish_status()` ile yayın durumunu bildirir; `publish_listing()` hiçbir connector'da uygulanmadı ve
`CONNECTOR_WRITE_ENABLED=false` iken `WriteDisabled` verir.

**Güvenlik:** URL ve secret'lar API yanıtlarında asla dönmez (yalnızca maskeli URL ve
"tanımlı mı"); denetim kaydına değer yazılmaz; çözülen secret'lar log maskeleyicisine eklenir;
iç ağ adresleri engellidir (`SUPPLIER_ALLOW_PRIVATE_URLS`); XML DTD/dış varlık reddedilir;
bağlantı değişikliği yalnızca yönetici, eşleştirme/senkron/yükleme operatör yetkisindedir.

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

> Adım adım kılavuz: **[DEPLOYMENT.md](DEPLOYMENT.md)**. `/opt/trendcantamiz-xml` ile ilgisi yoktur.

Tek komut: `./deploy/deploy.sh`. Sırasıyla şunları yapar:
1. Güvenlik kontrolleri.
2. Mevcut compose projesini ve volume'u tespit eder.
3. Port çakışması varsa 8082–8099 arasından boş port seçer.
4. `pg_dump` yedeği alır ve doğrular.
5. `migrate`, ardından `api`/`worker`/`web` servislerini başlatır; nginx yeniden yüklenir.
6. 4 servisin `healthy` olmasını ve nginx üzerinden `/api/health`'in 200 dönmesini bekler.
7. Duman testini çalıştırır.
8. TrendHub dışındaki container'ların değişmediğini doğrular.

Yönetici hesabı: `./deploy/create-admin.sh` (parola etkileşimli sorulur).

Geri dönüş: migration'lar yalnızca ekleme yaptığı için eski imaj yeni şemayla çalışmaya devam eder.
Veri gerekirse `backups/` altındaki yedekten `pg_restore` ile geri yüklenir.

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
  * Docker işi: `compose config`, imaj build, tüm yığını ayağa kaldırma, duman testi, ikinci `migrate` çalıştırması, api yeniden oluşturulduktan sonra nginx→api erişimi
* Güvenlik testleri:
  * tüm GET uç noktalarında credential sızıntısı taraması
  * log maskeleme
  * çerez bayrakları
  * SQL injection girdilerinin etkisiz kaldığı

## 12. Bilinen sınırlamalar / kalan işler

* Trendyol ve Amazon alan eşlemeleri **canlı veriyle doğrulanmalı**. Özellikle Trendyol `lineUnitPrice`'ın indirim sonrası birim fiyat olduğu varsayımı, Amazon `ItemPrice` ve `PromotionDiscount` KDV davranışı.
* Trendyol servis sınırı nedeniyle 30 günden eski siparişlerin statü değişiklikleri yakalanmaz. Ayrı bir iade (claims) servisi ile genişletilebilir.
* Trendyol ilan (ürün/stok/fiyat) okuma: Ürün V2 filtre servisinin yanıt şeması doğrulanınca açılmalı.
* Trendyol hakediş (settlement) API'si bağlanmadı; komisyon şu an **tahmini**. Gerçek tutar manuel girilebilir.
* Amazon Finances API (gerçek ücretler) ve Amazon ilan okuma (Reports API) yok.
* Hepsiburada sipariş (paket) okuma: uç nokta, kimlik doğrulama ve sayfalama resmi dokümanın arama özetleriyle doğrulandı (portal bu ortamdan erişilemiyor); alan eşlemesi canlı hesapla doğrulanmalı. HB ilan okuma şeması doğrulanmadı (`HEPSIBURADA_LISTINGS_ENABLED=false`). Paket statü değişikliklerini (ör. teslim) yakalamak için özel statü uç noktaları eklenmedi.
* Stok/fiyat gönderimi bilinçli olarak kapalı ve uygulanmadı. Ürün aktarımı taslak + CSV ile sınırlıdır; pazaryeri ürün oluşturma API'leri resmi dokümantasyonla doğrulanmadan yazılmayacak.
* Çanta Bayim'in gerçek XML alan adları bu ortamdan doğrulanamadı: şablon alan adı içermez, önizlemede eşanlamlılardan öneri üretir; ilk kurulumda eşleştirme panelden kontrol edilmeli.
* Pazaryeri kategori ağaçları ve zorunlu özellikler (attributes) API'den çekilmiyor; kategori ID'si elle girilir, varsayılan zorunlu alanlar düzenlenebilir varsayımlardır.
* Otomatik tedarikçi seçimi stratejileri katalog stok/maliyetini günceller; sipariş anında tedarikçiye yönlendirme yapılmaz (canlı otomasyon ayrı sistemde).
* Tedarikçi (Çanta Bayim) aktarım kayıtları production sisteminden okunmuyor. Entegrasyon yöntemi (salt okunur DB/replika, dosya veya API) kararlaştırılmalı.
* KDV: tahmini KDV ve KDV sonrası net kâr var; gerçek beyanname/muhasebe entegrasyonu yok.
* Rate limit süreç içindedir. Birden fazla worker çalıştırılırsa limit worker başına uygulanır.
* Credential'lar yalnızca env'de, tek mağaza/hesap destekleniyor. Çoklu mağaza için şifreli credential deposu gerekir.
* HTTPS nginx önünde bir TLS sonlandırıcı (ör. Caddy, Traefik, certbot) ile sağlanmalıdır.
* Canlı sunucuya bu geliştirme ortamından erişim yoktur. Deploy, eski sürümün çalıştığı ve korunan bir "canlı" stack'in bulunduğu bir provada (Docker 29, compose v5) uçtan uca denendi:
  * yedek alındı, migration uygulandı
  * 4 servis healthy oldu, duman testi geçti
  * api yeniden oluşturulduğunda nginx erişimi korundu
  * Docker yeniden başlatıldığında (reboot) stack kendiliğinden geri geldi
  * korunan stack değişmedi
