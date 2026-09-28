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
   → `api`, `worker`, `web` → nginx yapılandırma testi ve yeniden yükleme.
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
