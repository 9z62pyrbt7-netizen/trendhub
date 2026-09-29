# Trendçantanız — Shopify vitrin teması

Trendçantanız web mağazası için premium, mobil öncelikli bir Shopify Online Store 2.0 teması.
Tema yalnızca **görünümü** değiştirir. Ürün, fiyat, stok, varyant, sepet, ödeme (checkout), sipariş
ve uygulama/pixel entegrasyonları Shopify'ın kendi altyapısında çalışmaya devam eder.

```
storefront/
  theme/   ← Shopify'a yüklenen tema (layout, sections, snippets, templates, assets, config, locales)
  dev/     ← yalnızca geliştirme/test araçları (Shopify'a yüklenmez)
```

## Kurulum (yayınlamadan önce önizleme)

**Shopify CLI ile (önerilen):**

```bash
shopify theme push --path storefront/theme --unpublished --store zfdf1w-ij.myshopify.com
```

**Zip ile:** `storefront/theme` klasörünün *içeriğini* zipleyin
(`cd storefront/theme && zip -r ../trendcantaniz-theme.zip .`) ve
Shopify yönetici paneli → Online Mağaza → Temalar → **Tema ekle → Zip dosyası yükle** ile yükleyin.

Tema yayınlanmamış olarak eklenir. **Önizle** ile kontrol edin, hazır olduğunuzda **Yayınla**'ya basın.
Önceki tema (Flora) silinmez; tek tıkla geri dönülebilir.

## Yayından önce mağaza tarafında yapılması önerilenler

| Konu | Neden |
|---|---|
| Mağaza adı "My Store 2" → **Trendçantanız** (Ayarlar → Mağaza bilgileri) | Sayfa başlıkları, OpenGraph, telif satırı ve Organization şeması mağaza adını kullanır |
| Mağaza para birimi **USD** | Türkiye'de satış yapılacaksa Ayarlar → Pazarlar'dan TRY ayarlanmalı; tema fiyatları Shopify'ın para birimi biçimiyle gösterir |
| Ana menü (`main-menu`) şu an "Home / Catalog / Contact" (İngilizce) | Menüler → Ana menü'den Türkçe başlıklar ve koleksiyonlar eklenebilir; "Yeni Gelenler" ve "Çok Satanlar" bağlantılarını tema kendisi ekler |
| Ürün adları uzun İngilizce pazaryeri başlıkları | Ürün sayfası ve kartlar gerçek başlığı gösterir; hero ve kampanya bölümünde tema düzenleyicisinden kısa "Görünen ürün adı" verilebilir (şu an "Top Handle Satchel") |
| Ürün açıklaması ("Product description") ve SEO başlık/açıklamaları boş | Ürün sayfası ve arama motoru sonuçları için doldurulmalı |
| Mağaza açıklaması (Tercihler → Ana sayfa meta açıklaması) boş | Ana sayfa meta açıklaması ve OpenGraph açıklaması buradan gelir |
| "Face & Eye Care", "Lip & Oral Care", "Default example products" koleksiyonları | Boş oldukları için tema bunları hiçbir yerde göstermez; isterseniz silebilirsiniz |
| Kargo/iade metni | Ürün sayfasındaki "Kargo ve İade" bölümü içerik girilene kadar gizlidir (tema kendi başına kargo vaadi yazmaz) |
| Sosyal medya / WhatsApp | Tema ayarları → Sosyal medya. Boş bırakılanlar gösterilmez |

## Tasarım ve dönüşüm kararları

* **Hero ürünü:** Mağazada satış verisi yok (son 365 günde sipariş yok, ürün sayfası oturumu yok).
  Bu yüzden 3. öncelik uygulandı: stokta olan (10 adet), 9 yüksek çözünürlüklü görseli bulunan ve
  mağaza sahibi tarafından ana sayfa koleksiyonuna eklenmiş siyah **Top Handle Leather Satchel**
  seçildi. Diğer ürün stokta olmadığı için (0 adet) hero'ya konmadı. Tema düzenleyicisinden değiştirilebilir;
  ürün seçilmezse ilk stokta olan ürün kullanılır.
* **Çok Satanlar / Yeni Gelenler:** Koleksiyon seçilmediğinde Shopify'ın gerçek sıralaması
  (`sort_by=best-selling` / `created-descending`) kullanılır. "Yeni Gelenler" şeridi katalog en az
  6 ürüne ulaşınca görünür (2 ürünün iki şeritte tekrarlanmaması için).
* **Koleksiyonlar:** Yalnızca ürün içeren gerçek koleksiyonlar gösterilir. Şu an tek dolu koleksiyon
  "Home page" olduğundan tüm ürünlerin gerçek görünümleri (Tüm Çantalar / Yeni Gelenler / Çok Satanlar)
  kullanılır. Mağazada olmayan kategori (ör. "Omuz Çantaları") uydurulmaz.
* **Güven unsurları:** Varsayılan olarak yalnızca Shopify ödeme altyapısıyla gerçekten sağlanan
  "Güvenli Ödeme", "Kolay Sipariş" ve "Müşteri Desteği" (iletişim formu/e-posta) gösterilir.
  Sahte yorum, sipariş bildirimi, sayaç veya indirim yoktur. İndirim rozeti yalnızca gerçek
  "karşılaştırma fiyatı" satış fiyatından yüksekse, "Son X ürün" uyarısı yalnızca gerçek stok
  eşiğin altındaysa görünür.
* **Ürün görselleri:** Beyaz arka planlı ürün fotoğrafları `mix-blend-mode: multiply` ile krem
  zemine karışır; kutu görünümü oluşmaz.

## Performans, erişilebilirlik ve SEO

* Hero görseli `fetchpriority="high"` ve eager yüklenir, diğer tüm görseller lazy + `srcset`.
  Tüm görsellerde genişlik/yükseklik vardır (CLS = 0).
* Tek CSS (≈58 KB, sıkıştırmasız) ve tek, bağımlılıksız `defer` JS (≈29 KB). Yazı tipleri
  Shopify font CDN'inden `preload` + `font-display: swap`.
* Scroll animasyonları yalnızca `transform`/`opacity` kullanır, rAF ile ve yalnızca bölüm görünürken çalışır.
  Mobilde paralaks kapalıdır; `prefers-reduced-motion` ve tema ayarındaki "Kaydırma animasyonları"
  kapalıyken tüm hareketler durur.
* URL'ler Shopify'ın standart yapısıdır (`/products/…`, `/collections/…`); canonical, title/meta,
  OpenGraph/Twitter, Product + Offer + BreadcrumbList, Organization ve WebSite (SearchAction)
  yapılandırılmış verileri eklidir. Site haritası Shopify tarafından üretilmeye devam eder.
* Yerel ölçüm (emülatör, mobil Lighthouse): Erişilebilirlik 100, En iyi uygulamalar 100,
  Performans 93 (CDN/WebP olmadan).

## Test

```bash
cd storefront/dev
npm ci
node theme-check.mjs                 # Shopify Theme Check (0 hata olmalı)
node genimg.mjs                      # test görselleri
EXTRA=1 node server.mjs &            # yerel Shopify emülatörü (liquidjs + Cart/Section Rendering API)
node smoke.mjs                       # tüm şablonlar 200/404, Liquid/çeviri hatası yok
node e2e.mjs                         # Playwright: masaüstü + mobil satın alma akışı
```

`e2e.mjs`: karttan hızlı sepete ekleme, yan sepet, adet artır/azalt, varyant seçimi (URL, fiyat,
stok, tükenmiş/uygunsuz kombinasyon), stok limiti hatası, mobil yapışkan "Sepete Ekle", sepet
sayfasında kaldırma, **checkout yönlendirmesi**, mobil menü, anlık arama, hero'dan sepete ekleme,
sıralama ve filtre çekmecesi. `EXTRA=1` emülatöre çok varyantlı/indirimli test ürünleri ekler
(yalnızca yerel testte; mağazaya hiçbir şey yazılmaz). Aynı testler CI'daki `storefront` işinde çalışır.
