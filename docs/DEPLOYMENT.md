# TrendHub — Production kurulum / güncelleme

Bu kılavuz TrendHub'ı sunucuda **ayrı bir Docker Compose projesi** olarak kurar veya günceller.
Canlı Trendyol → Çanta Bayim otomasyonu (`/opt/trendcantamiz-xml`) ile **hiçbir** ortak
container, volume, ağ veya dosya kullanılmaz; `deploy/deploy.sh` o klasörde çalışmayı reddeder
ve deploy öncesi/sonrası TrendHub dışındaki tüm container'ların kimliğini, durumunu ve başlama
zamanını karşılaştırır.

## Gereksinimler

* Docker Engine + Docker Compose v2 (`docker compose version`)
* En az 2 GB boş disk
* Açılışta Docker'ın başlaması: `sudo systemctl enable docker` (reboot sonrası stack otomatik gelir;
  tüm servisler `restart: unless-stopped`)

## 1. İlk kurulum veya güncelleme (tek komut)

**İlk kurulum** (TrendHub henüz yoksa; `/opt/trendcantamiz-xml` içinde DEĞİL):

```bash
sudo git clone https://github.com/9z62pyrbt7-netizen/trendhub.git /opt/trendhub && cd /opt/trendhub && sudo bash deploy/install.sh
```

**Güncelleme** (TrendHub zaten kuruluysa, kendi klasöründe):

```bash
cd /opt/trendhub && sudo git fetch origin main && sudo git checkout main && sudo git merge --ff-only origin/main && sudo bash deploy/install.sh
```

`deploy/install.sh`:
* `/opt/trendcantamiz-xml` içinde çalışmayı reddeder; yerel değişiklik varsa durur.
* `.env` **yoksa** `.env.example`'dan oluşturur, veritabanı parolasını ve `APP_SECRET`'ı rastgele
  üretir (değerler ekrana/loga yazılmaz, izin 600). `.env` **varsa** değerlerine dokunmaz; yalnızca
  eksik güvenlik anahtarlarını ekler (`CONNECTOR_WRITE_ENABLED=false`, `*_LISTINGS_ENABLED=false`, …).
* `deploy/deploy.sh`'i çalıştırır (aşağıda), sonra aktif yönetici yoksa parolayı gizli sorarak oluşturur.

Pazaryeri API bilgilerini (`TRENDYOL_*`, `HEPSIBURADA_*`, `AMAZON_SP_*`) daha sonra `.env`'e ekleyip
`sudo bash deploy/install.sh`'i tekrar çalıştırmanız yeterlidir. Bilgi girilmeyen pazaryeri
"Bağlı değil" görünür.

Yalnızca deploy adımı için:

```bash
./deploy/deploy.sh
```

Betik sırasıyla şunları yapar ve bir adım başarısız olursa **durur** (hiçbir veri silinmez):

1. Güvenlik: korunan klasör kontrolü, `.env` zorunlu değerleri (değerleri yazdırmaz),
   `CONNECTOR_WRITE_ENABLED=false` zorunluluğu, disk alanı.
2. Mevcut TrendHub container'larının compose projesini etiketlerinden bulur. Böylece aynı veri
   volume'u kullanılır.
3. Port: `TRENDHUB_HTTP_PORT` (varsayılan 8081) başka bir container/süreç tarafından
   kullanılıyorsa 8082–8099 arasında boş bir port seçer ve `.env`'e yazar.
4. `git pull --ff-only` (main).
5. Mevcut DB'nin `pg_dump` yedeği → `backups/trendhub-<zaman>-<commit>.dump` (izin 600,
   `pg_restore -l` ile doğrulanır). İlk kurulumda atlanır.
6. İmaj derleme → `migrate` (Alembic; yalnızca ekleme yapan, tekrar çalıştırılabilir migration'lar)
   → `api`, `worker`, `storefront`, `web` → nginx yapılandırma testi ve yeniden yükleme.
   Web mağazası portu `TRENDCANTANIZ_HTTP_PORT` (varsayılan 8090) doluysa boş bir port seçilir.
7. `db`, `api`, `worker`, `web` servislerinin `healthy` olmasını ve nginx üzerinden
   `/api/health = 200` dönmesini bekler; duman testi.
8. TrendHub dışındaki container'ların değişmediğini doğrular ve erişim adresini yazar.

## 2. Yönetici hesabı

```bash
./deploy/create-admin.sh            # kullanıcı adı: admin
./deploy/create-admin.sh ali        # başka bir kullanıcı adı
```

Parola ekranda görünmeden iki kez sorulur (en az 12 karakter). Komut satırına, shell geçmişine,
`.env`'e veya loglara yazılmaz. Parola sıfırlama:

```bash
docker compose -p <proje> exec api python -m app.cli reset-password --username admin
```

(`<proje>` deploy çıktısındaki "Compose projesi" değeridir; genellikle klasör adı.)

## 3. Giriş

Tarayıcıdan `http://SUNUCU_IP:8081` (veya deploy çıktısındaki port). Telefonda da aynı adres
çalışır.

* Düz HTTP'de oturum çerezi `COOKIE_SECURE=auto` ile çalışır. İnternete açık kullanımda HTTPS
  önerilir: önüne Caddy/Traefik/nginx + Let's Encrypt koyun ve `COOKIE_SECURE=true` yapın.
* Sunucu güvenlik duvarında yalnızca bu portu (veya TLS sonlandırıcının 443'ünü) açın; veritabanı
  ve API dışarıya port açmaz.

## 4. Trendyol bağlantısı (salt okunur)

`.env` içine `TRENDYOL_SELLER_ID`, `TRENDYOL_API_KEY`, `TRENDYOL_API_SECRET` yazıp tekrar
`./deploy/deploy.sh` çalıştırın. Ardından panelde **Entegrasyonlar → Bağlantıyı test et**.

* TrendHub yalnızca `GET` istekleri yapar (HTTP istemcisi GET/HEAD dışındaki her isteği ağa
  çıkmadan reddeder); sipariş, fiyat, stok, ürün veya kampanya **değiştirmez**.
* Canlı Çanta Bayim otomasyonuyla aynı API anahtarını paylaşabilir. TrendHub dakikada en fazla
  60 istek (Trendyol sınırı 1000/dk) ve 15 dakikada bir senkron yapar.
* İlan (ürün/stok/fiyat) okuma `TRENDYOL_LISTINGS_ENABLED=false` ile kapalıdır. Trendyol Ürün V1
  servisleri kapatılıyor ve V2 yanıt şeması doğrulanmadı.

## 4a. Tedarikçi ekleme (ör. Çanta Bayim)

1. Panel → **Tedarikçiler → + Yeni Tedarikçi Ekle** (yönetici). Şablon olarak “Çanta Bayim”i
   seçin, XML adresini girin. Adres ve şifre/token **şifreli** saklanır (anahtar `APP_SECRET`;
   `deploy.sh` boşsa üretir — sonradan değiştirmeyin). İsterseniz token'ı `.env` içinde
   `SUPPLIER_CANTABAYIM_TOKEN=...` olarak tutup panelde yalnızca adını yazabilirsiniz.
2. **Alan eşleştirme → Kaynaktan önizle**: alanlar ve örnek kayıtlar görünür, otomatik öneri
   gelir. Kontrol edip **Eşleştirmeyi kaydet**. Önizleme veritabanına yazmaz.
3. **Şimdi senkronize et** (veya seçtiğiniz sıklıkta worker otomatik yapar). Tedarikçiden
   kaybolan ürünler silinmez, “Kaynağında bulunamadı” olur.
4. Başka tedarikçiler aynı yolla eklenir (XML / JSON API / CSV URL veya manuel dosya yükleme).

TrendHub tedarikçiden yalnızca **okur** (GET); `/opt/trendcantamiz-xml` otomasyonuna dokunmaz.
Ürün Aktarımı pazaryerine gönderim yapmaz; hazır taslaklar CSV olarak indirilir.

## 5. Yedek ve geri dönüş

```bash
./deploy/backup.sh                         # anlık yedek -> backups/
```

Geri dönüş (yalnızca gerekirse, **dikkatle**):

1. Koda dönüş: `git checkout <önceki_commit> && TRENDHUB_GIT_PULL=0 ./deploy/deploy.sh`.
   Migration'lar yalnızca ekleme yaptığından eski kod yeni şemayla çalışır.
2. Veriye dönüş (mevcut veriyi yedektekiyle değiştirir; geri alınamaz, önce yeni bir yedek alın):
   `docker exec -i trendhub-db pg_restore -U <POSTGRES_USER> -d <POSTGRES_DB> --clean --if-exists < backups/<dosya>.dump`

## 6. Durum ve loglar

```bash
docker compose -p <proje> ps          # tüm servisler (healthy) olmalı
docker compose -p <proje> logs -f --tail 100 api worker
```

Panelde **Sistem / Hatalar**: worker heartbeat, kuyruk, başarısız işler, pazaryeri bağlantı
durumu, son senkron ve sistem olayları. Credential değerleri hiçbir ekranda veya logda görünmez.

## Yapılmayanlar (bilinçli)

`docker compose down -v`, `docker system prune`, `docker volume rm` ve TrendHub dışındaki herhangi
bir container'ı durdurma/yeniden başlatma **hiçbir betikte yoktur**.


## Trendçantanız web mağazası (storefront)

Aynı `deploy.sh` ile kurulur/güncellenir; `storefront` servisi (trendhub-storefront) de başlatılır ve
sağlık kontrolünden (`/api/store/health`, nginx üzerinden :8090) geçmeden deploy başarılı sayılmaz.

> **Düzeltilen hata:** Eski `deploy.sh`, 3. adımda `git pull` ile kendi dosyasını güncelliyordu; bash çalışan
> betiği diskten okumaya devam ettiği için eski adımlar (yalnızca `api worker web` başlatan satır) çalışıyor ve
> yeni `storefront` servisi başlamıyordu. Artık betik güncellendiyse yeni sürüm baştan çalıştırılır
> (`TRENDHUB_REEXEC`), başlatılacak servisler `docker compose config --services`'ten okunur ve herhangi biri
> çalışmıyorsa logu gösterilerek durulur. **Bu düzeltme sunucuya ilk kez geldiğinde** eski betik hâlâ eski
> adımları çalıştırabilir; bu yüzden ilk güncellemede `git pull` sonrası betiği bir kez daha çalıştırın
> (veya doğrudan `sudo bash deploy/install.sh`).

### Canlıya almadan önce

1. **Alan adı + HTTPS (trendcantaniz.com)** — mevcut 8081 (panel) ve 8090 (mağaza) erişimi değişmez:
   * DNS: `trendcantaniz.com` ve `www.trendcantaniz.com` için A kaydı → sunucu IP'si.
   * **Seçenek A (Caddy, önerilen; 80/443 boşsa):** `.env` içine `ACME_EMAIL=...` ekleyin, sonra
     `docker compose -f docker-compose.yml -f docker-compose.https.yml up -d --no-deps https`.
     Sertifika Let's Encrypt'ten otomatik alınır/yenilenir; www → apex 301 yönlendirilir.
   * **Seçenek B (sunucuda zaten nginx + certbot varsa):** `deploy/https/nginx-host.conf.example`.
   * Mağaza nginx bloğu yalnızca yerel/özel ağdan gelen `X-Forwarded-For`'a güvenir (hız sınırları ziyaretçi başına).
2. `.env`: `STOREFRONT_BASE_URL=https://trendcantaniz.com` (canonical, sitemap, OpenGraph, ödeme dönüş ve
   e-posta bağlantıları) ve `COOKIE_SECURE=auto`. Sonra `sudo bash deploy/install.sh`.
3. Panel → **Web Sitesi → Mağaza ayarları:** satıcı bilgileri, yasal metinler, ödeme yöntemleri, kargo,
   "Sipariş sonrası" (tedarikçiye aktarım modu, e-posta/SMS, e-fatura anahtarları).
4. Panel → **Web Sitesi → Ürün yayını:** filtre "Yayına hazır" → "Filtredeki tümünü yayınla" veya seçerek yayınlayın.
5. Google Search Console'a `https://trendcantaniz.com/sitemap.xml` gönderin.

### Harici servisler (hepsi `.env`; yoksa özellik kapalı kalır, hiçbir dış isteğe çıkılmaz)

| Özellik | .env | Not |
|---|---|---|
| Kart ödeme — PayTR | `STOREFRONT_PAYMENT_PROVIDER=paytr`, `PAYTR_MERCHANT_ID/KEY/SALT`, `PAYTR_TEST_MODE` | PayTR panelinde Bildirim URL: `https://trendcantaniz.com/odeme/geri-donus/paytr`. Önce `PAYTR_TEST_MODE=true` ile test kartıyla deneyin. |
| Kart ödeme — iyzico | `STOREFRONT_PAYMENT_PROVIDER=iyzico`, `IYZICO_API_KEY/SECRET_KEY`, `IYZICO_BASE_URL` | Önce sandbox (`https://sandbox-api.iyzipay.com`) anahtarlarıyla deneyin. |
| E-posta | `SMTP_HOST/PORT/USERNAME/PASSWORD/FROM`, `STOREFRONT_ORDER_ALERT_EMAILS` | Sipariş alındı / ödeme onayı / kargoya verildi / iptal + şifre sıfırlama. SPF/DKIM kayıtlarını ayarlayın. |
| SMS | `SMS_PROVIDER=netgsm`, `NETGSM_USERCODE/PASSWORD/HEADER` | Onaylı SMS başlığı gerekir. |
| E-fatura | `EINVOICE_PROVIDER` | Repoda uygulanmış entegratör yok; `app/services/einvoice.py` arayüzü hazır. |
| Tedarikçi sipariş API'si | — | Bağlantı yok; web siparişleri panelden taslak → "Manuel iletildi". `app/services/supplier_forwarding.py`. |

Durum: Panel → Web Sitesi → **Entegrasyonlar** (secret göstermez).

Görsel önbelleği `storefront_images` volume'undadır (WebP); silinirse kendiliğinden yeniden üretilir.

## AI Control Center

Aynı `deploy.sh` ile gelir (migration 0010, yalnızca ekleme). Kurulumdan sonra:
1. Panel → **AI Control Center → Sermaye:** kasa, bekleyen hakediş, borçlar ve "sisteme ayırdığım sermaye" limitini girin
   (bunlar girilmeden sermaye önerisi yapılmaz).
2. **Reklamlar** ekranında kampanyalara ürün bağlayın, harcama ve performans (tıklama, sipariş, ciro) girin; AI → Reklam'da
   kampanyanın günlük bütçesini girin. Performans verisi olmayan kampanya için karar "Veri yetersiz" olur.
3. Ürün maliyetlerinin eksiksiz olduğundan emin olun (maliyeti eksik ürün "Veri yok" sınıfına düşer).
4. İsteğe bağlı: `.env` → `ANTHROPIC_API_KEY=...` (CEO sohbeti doğal dille; yoksa kural motoru).
5. Acil durumda: AI Control Center → **TÜM AJANLARI DURDUR** (operatör açabilir, yalnızca yönetici kaldırabilir).

