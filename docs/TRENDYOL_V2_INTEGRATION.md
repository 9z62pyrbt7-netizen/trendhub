# V2.1 — Trendyol gerçek veri entegrasyonu (salt okunur)

Durum: geliştirildi ve sahte HTTP katmanıyla test edildi. **Canlı Trendyol hesabıyla henüz doğrulanmadı**
(bu geliştirme ortamının ağ politikası `apigw.trendyol.com` ve `developers.trendyol.com` adreslerini engelliyor ve
ortamda mağaza kimlik bilgileri yok). Production'a deploy edilmedi.

## 1. Resmî dokümantasyondan doğrulanan uç noktalar

Kaynak: developers.trendyol.com. Doğrudan erişim engelli olduğu için sayfalar arama motoru üzerinden okundu.
Hepsinde kimlik doğrulama Basic Auth (API Key / API Secret) ve `User-Agent: {sellerId} - SelfIntegration`.

| Servis | Yöntem + yol | Sayfalama / limit | Kullanım |
|---|---|---|---|
| Siparişler (mevcut) | GET `/integration/order/sellers/{id}/v2/orders` | size ≤ 200, en fazla 1 ay geriye, 1000 istek/dk | değişmedi |
| Cari hesap — settlements | GET `/integration/finance/che/sellers/{id}/settlements?transactionType=…&startDate&endDate&page&size` | aralık ≤ 15 gün, size 500/1000, 100 istek/dk | satış/iade komisyonu, hakediş |
| Cari hesap — otherfinancials | GET `/integration/finance/che/sellers/{id}/otherfinancials` | aynı | PaymentOrder (ödeme), DeductionInvoices (kesinti) |
| Kargo faturası detayı | GET `/integration/finance/che/sellers/{id}/cargo-invoice/{invoiceSerialNumber}/items` | size 500 | gerçek kargo bedeli |
| İadeler (getClaims) | GET `/integration/order/sellers/{id}/claims` | tarih = lastModifiedDate, 1000 istek/dk | iade kalemi, sebep, statü |
| Soru filtreleme | GET `/integration/qna/sellers/{id}/questions/filter` | size ≤ 50, aralık ≤ 2 hafta, 1000 istek/dk | sorular (cevap servisi ÇAĞRILMAZ) |
| Satıcı adresleri | GET `/integration/sellers/{id}/addresses` | — | adres türleri özeti |
| Webhook listesi | GET `/integration/webhook/sellers/{id}/webhooks` | — | yapılandırma durumu (oluşturma YAPILMAZ) |
| Ürün V2 onaylı filtre | GET `/integration/product/sellers/{id}/products/approved` + `storeFrontCode` başlığı | size ≤ 1000 | yalnızca smoke testte |

Genel limit: aynı uç noktaya 10 saniyede en fazla 50 istek, aşılınca 429 döner.

**Webhook:** Yalnızca sipariş paketi statülerini gönderir: CREATED, PICKING, INVOICED, SHIPPED, CANCELLED, DELIVERED,
UNDELIVERED, RETURNED, UNSUPPLIED, AWAITING, UNPACKED, AT_COLLECTION_POINT, VERIFIED.
- Kimlik doğrulama BASIC_AUTHENTICATION veya API_KEY (`x-api-key` başlığı) ile yapılır.
- İmza ve zaman damgası yoktur, dolayısıyla klasik replay koruması uygulanamaz.
- Satıcı başına en fazla 15 webhook tanımlanabilir.
- Hata durumunda Trendyol bir süre yeniden dener, sonra webhook'u PASİF yapar.

İade, finans, soru ve stok için webhook **yoktur**; bunlar polling ile gelir.

**Reklam API'si:** developers.trendyol.com'da satıcı API'si kapsamında reklam / kampanya performansı servisi
bulunamadı. Reklam verisi elle veya CSV ile girilmeye devam eder. Erişim için Trendyol Satıcı Destek / partner
ekibinden resmî bir API olup olmadığı sorulmalıdır.

**Ürün V1:** V1 ürün servisleri 15.10.2026'da kapanıyor. Mevcut (varsayılan kapalı) ilan senkronu V1 kullanıyor.
`storeFrontCode` başlığının değeri ("TR" varsayıldı) canlı hesapla doğrulanmalıdır.

## 2. Veri modeli (migration 0012, yalnızca ekleme)

- `sync_state` tablosuna tazelik kolonları eklendi: last_attempt / last_success / error_count / status / record_count / latest_record / meta.
- `marketplace_finance_entries`: cari hesap defteri (nakit tarafı).
- `marketplace_returns`: iade kalemleri.
- `customer_questions`: müşteri soruları. Müşteri adı ve müşteri id saklanmaz; metin maskelenir.
- `platform_events`: iç olaylar (webhook + polling), `dedupe_key` UNIQUE.

Kâr tarafı için yeni tablo açılmadı; mevcut `financial_transactions` kullanılıyor:
- `source='trendyol_finance'`: gerçek komisyon, gerçek iade ve gerçek kargo.
- `source='trendyol_claims'`: finans kaydı gelene kadar kabul edilmiş iade.

**Geri alma:** Eski kod yeni tablo ve kolonları okumaz. Kodu geri almak güvenlidir; şemanın geri alınması gerekmez.

## 3. Kurallar

| Kural | Uygulama |
|---|---|
| Gerçek veri tahminin önüne geçer | Gerçek Trendyol finans kaydı varsa komisyon, iade ve kargo ondan alınır; yoksa tahmin kullanılır ve sipariş `finance_is_estimate` olarak kalır. Kârın kaynağı ACTUAL / PARTIAL / ESTIMATED olarak gösterilir. |
| Hakediş nakit değildir | Harcanabilir sermaye = kasa − borç − ayrılmış − rezerv. Bekleyen hakediş ve alacak bilgi olarak gösterilir, hiçbir hesapta harcanabilir sayılmaz. |
| Tazelik sınırı | Finans verisi 36 saatten eskiyse (STALE) veya alınamıyorsa (ERROR) para harcayan öneri bloke edilir: "Finans verisi güncel olmadığı için ek reklam bütçesi / harcama önermiyorum". DEGRADED durumunda yalnızca uyarı verilir. |
| Olay akışı | Webhook → doğrula → PII ayıkla → kaydet → kuyruk → worker → Event Router. HTTP isteği içinde ağır iş veya LLM çalışmaz. Yalnızca kritik risk (reklamdaki ürünün stoğu biterse) AI döngüsünü erkene alır. |
| Kişisel veri (PII) | LLM'e giden her araç çıktısında e-posta, telefon, IBAN ve müşteri adı aranır (`assert_no_pii`); bulunursa çıktı gönderilmez. |
| Güvenli devreye alma | Genişletilmiş okuma `TRENDYOL_EXTENDED_READ=false` ile varsayılan olarak kapalıdır. Smoke test başarılı olunca açılır. |

## 4. Canlıya geçiş adımları

1. Deploy (yalnızca onayla).
2. `docker compose exec api python -m app.cli trendyol-smoke`
   - Yalnızca GET isteği yapar, veritabanına yazmaz.
   - Çıktıda kişisel veri yoktur.
   - Her servis için şunları raporlar: HTTP sonucu, kayıt sayısı, en son kayıt zamanı, tazelik, alan eşlemesi, işaret kontrolü.
3. Rapor uygunsa `.env` içinde `TRENDYOL_EXTENDED_READ=true` yapın ve worker'ı yeniden başlatın.
4. İlk finans senkronundan sonra Finans / Nakit ekranındaki iki kontrolü inceleyin:
   - mutabakat farkı (son ödeme talimatı ile defter karşılaştırması),
   - işaret kontrolü (net tutar ile `sellerRevenue` karşılaştırması).
5. Webhook (isteğe bağlı) aşağıdaki sırayla kurulur. Webhook'u Trendyol tarafında oluşturmak bir yazma işlemidir; sahip yapar.
   1. `.env` içine `TRENDYOL_WEBHOOK_API_KEY` ekleyin.
   2. Trendyol tarafında URL'yi `https://<panel>/api/webhooks/trendyol` olarak tanımlayın.
   3. Kimlik doğrulama türünü API_KEY seçin.

## 5. Doğrulama turu (Docker + güvenlik)

**Kimlik bilgileri: NOT_FOUND.** Trendyol kimlik bilgileri şu sırayla okunur:
1. Panelden girilip şifreli olarak saklanan `marketplace_connections`.
2. Ortam değişkenleri (`TRENDYOL_SELLER_ID`, `TRENDYOL_API_KEY`, `TRENDYOL_API_SECRET`). docker-compose bunları `env_file: .env` ile tüm servislere verir.

Bu ortamda ikisi de boş. Ayrıca ağ politikası `apigw.trendyol.com` adresini engelliyor. Bu yüzden **gerçek Trendyol smoke testi çalıştırılamadı**; bütün servisler NOT_TESTED. Sunucuda çalıştırılacak komut: §4 madde 2.

**Docker zinciri:** yedek → migration → başlatma → sağlık → girişli duman testi BAŞARILI.
- Migration öncesi alınan yedek (0011) ayrı bir veritabanına geri yüklendi ve 0012 orada uygulandı. Yalnızca beklenen farklar çıktı: sürüm, 8 yeni `sync_state` kolonu, 4 yeni tablo. İkinci çalıştırma işlem yapmadı (no-op).
- Sipariş, ürün, kullanıcı, sermaye, audit ve ayar verileri birebir aynı kaldı.

**Bulunan ve düzeltilen hatalar** (her biri kök neden, düzeltme ve regresyon testiyle):

1. **nginx reload'dan sonra web portu kapandı (health 000).**
   - Kök neden: `./web` klasörü silinip yeniden oluşturulduğunda, çalışan nginx container'ı bind mount ile eski (silinmiş) klasörü, yani boş bir `conf.d` görüyor. `nginx -t` boş yapılandırmayı geçerli sayıyor, `nginx -s reload` tüm dinleyicileri kapatıyor. Kanıt: container içinde `conf.d` boş, nginx yalnızca DNS portunu dinliyor.
   - Düzeltme: `deploy.sh` reload'dan önce container'ın gördüğü `nginx.conf` ile diskteki dosyanın SHA-256 değerini karşılaştırıyor. Farklıysa web container'ını `--force-recreate` ile yeniden oluşturuyor; yine de göremezse reload yapmadan duruyor ve canlı panel korunuyor.
   - Regresyon: `tests/test_deploy_script.py`.
2. **Duman testi `events.process` işini "beklenmeyen iş" sayıyordu.**
   - Kök neden: iç olay kuyruğu işi, duman testinin izinli iş listesinde yoktu. Webhook açılınca her deploy bu noktada dururdu.
   - Düzeltme: iş listeye eklendi.
   - Regresyon: izinli liste ile koddaki iç iş tipleri birebir karşılaştırılıyor; dışarıya istek atan senkron işleri listede asla yer alamaz.
3. **Veri yok ≠ 0.**
   - Bekleyen hakediş: Trendyol finansı bağlı değilken ve elle de girilmemişken **0,00** gösteriliyordu. Artık `UNKNOWN` (tutar boş, "Bilinmiyor"). CEO "hakedişini bilmiyorum" diyor; toplam paranın eksik olduğu belirtiliyor.
   - Net kâr ve reklam harcaması: sipariş ya da reklam verisi yokken KPI'larda **₺0** görünüyordu. Artık "—  Veri yok" görünüyor.
   - Kâr kaynağı: sipariş yokken "Tahmin" yerine `NO_DATA` gösteriliyor.
   - Regresyon: `test_unknown_pending_payout_is_not_zero`, `test_no_data_is_not_reported_as_zero_or_estimate`.

**Güvenlik:**
- `git grep` ile repoda secret/anahtar kalıbı, `.env` veya `.dump` dosyası bulunmadı.
- Docker ve deploy loglarında admin parolası ve webhook anahtarı geçmiyor.
- 34 `/api/ai/*` uç noktasının tamamı oturumsuz istekte 401 dönüyor.
- Webhook: anahtarsız veya yanlış anahtarla 401, tekrar gönderimde `duplicates=1`, bozuk JSON'da 400.
- Webhook olayı kuyruktan gerçek worker'da işleniyor; kişisel veri saklanmıyor.
- LLM'e giden araç çıktılarında kişisel veri engeli ve acil durdurma testlerle kanıtlı.

**Reklam API'si (yeniden kontrol):**
- Trendyol, reklam raporlarını yalnızca satıcı panelinde sunuyor: kampanya, bütçe, harcama, gösterim, tıklama, sipariş, gelir ve ROAS; Excel olarak indirilebiliyor, veriler saatlik güncelleniyor.
- Satıcı API'sinde (developers.trendyol.com) reklam uç noktası bulunamadı. Mevcut kimlik bilgileriyle erişim yok.
- Panelden indirilen Excel → CSV içe aktarma ya da elle giriş devam ediyor.
