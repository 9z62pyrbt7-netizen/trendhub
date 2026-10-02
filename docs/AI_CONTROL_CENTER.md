# AI Control Center — durum raporu, V1 mimarisi ve uygulama planı

Bu belge iki şeyi içerir: (A) AI Control Center'a başlamadan önce mevcut repo analizi, (B) V1 tasarımı
ve bilinçli olarak V1 dışında bırakılanlar. Amaç ajan sayısı değil, **sermayeyi kontrollü riskle kullanarak
sürdürülebilir net kârı artırmak**.

## A. Mevcut durum analizi

### 1. Mevcut mimari
| Katman | Teknoloji | Not |
|---|---|---|
| API | FastAPI (`backend/app/main.py`), SQLAlchemy Core + ham SQL | Tek süreç, `/api/*` |
| Worker | `app/worker.py`, `sync_jobs` kuyruğu (SKIP LOCKED, idempotency, advisory-lock lider) | Zamanlanmış işler buradan |
| Mağaza | `app/storefront` (ayrı süreç, Jinja2) | Trendçantanız web kanalı |
| DB | PostgreSQL 16, Alembic (yalnızca ekleme; `test_migrations` DROP'u engeller) | 0001–0009 |
| Panel | `web/assets/app.js` (bağımlılıksız SPA, hash router, `PAGES.*`) + nginx | Mobil uyumlu bileşenler mevcut |
| Kimlik | Oturum çerezi (argon2), roller `viewer / accountant / operator / admin`, CSRF başlığı `X-Requested-With: TrendHub` | `deps.require()` |
| Denetim | `audit_logs` + `services/audit.log_audit` | Tüm yazma uçlarında kullanılıyor |
| Ayarlar | `app_settings` (JSONB, anahtar bazlı, doğrulamalı) | Eşikler buraya |
| Deploy | docker compose (db, migrate, api, worker, storefront, web), `deploy/deploy.sh` (yedek → migration → sağlık) | |

### 2. Çalışan özellikler (AI katmanının üzerine kurulacağı veriler)
* **Siparişler:** Trendyol / Hepsiburada / Amazon TR **salt okunur** sipariş senkronu; web mağazası kendi kanalı. `orders`, `order_items`, statü geçmişi, kargo.
* **Finans (tek kaynak):** `services/finance_view.py` — sipariş ve kalem bazında ciro, iade, ürün maliyeti, komisyon (+KDV modu), hizmet bedeli, kargo, reklam, diğer → KDV öncesi/sonrası kâr, marj, markup. Dönem giderleri `expenses`, `analytics.summary` dönem net kârı.
* **Reklam merkezi:** `ad_accounts / ad_campaigns / ad_campaign_products / ad_spend / ad_performance` — **elle giriş veya CSV**; ürün bazında reklam sonrası kâr (eşit bölüşüm, tahmini olarak etiketli).
* **Ürün & stok:** `products`, `supplier_products` (Çanta Bayim beslemesi), kanallar arası kullanılabilir stok (`services/stock_availability.py`).
* **Uyarı merkezi:** `services/alerts.py` (durum/olay tabanlı, tekilleştirilmiş).
* **Kontrollü yayın:** `publish_requests` + kapılar; pazaryerine yazma **sunucu genelinde kapalı** (`CONNECTOR_WRITE_ENABLED=false`).

### 3. Yeniden kullanılacak kodlar
`finance_view.item_columns/totals`, `analytics.summary`, `ads.spend_total`, `stock_availability.available_map`,
`app_settings`, `log_audit`, `jobs.enqueue`, worker zamanlayıcısı, panelin `kpi/card/table/openModal/toast` bileşenleri.
AI katmanı **yeni bir kâr formülü yazmaz**; dashboard ile aynı SQL'i kullanır (iki ekran farklı kâr gösteremez).

### 4. Teknik borç / riskler (dürüst değerlendirme)
1. **Reklam verisi elle giriliyor.** Trendyol reklam API'si bağlı değil; `ad_performance` yoksa reklamın gerçek kâr etkisi hesaplanamaz. Advertising Agent bu durumda `INSUFFICIENT_DATA` döner — tahmin uydurmaz.
2. **Kampanyalarda günlük bütçe alanı yoktu.** "100 → 130 TL/gün" önerisi için `ad_campaigns.daily_budget` eklendi (elle girilir).
3. **Maliyet eksikliği** kârı olduğundan yüksek gösterir. Maliyeti olmayan ürün `NO_DATA` sınıfına düşer; AI bu ürünler için ölçekleme önermez.
4. **Hakediş (payout) verisi yok:** Trendyol finans/hakediş API'si bağlı değil. Nakit akışı için kasa, bekleyen hakediş, tedarikçi/reklam borcu **elle girilir**; bekleyen hakediş kullanılabilir sermaye sayılmaz.
5. **İş modeli dropshipping ağırlıklı** (Çanta Bayim stoğu). Bu modelde stok sermayesi ihtiyacı ~0'dır; "50.000 TL'nin bir kısmını stoka koy" önerisi çoğu zaman yanlış olur. Ayar `ai.inventory_model = dropship | own_stock` ile yönetilir (varsayılan dropship).
6. Ürün yorumları, sorular, kategori trendleri, rakip fiyatları için **erişilebilir resmi veri kaynağı bağlı değil** → Customer Experience Analyzer ve Market Radar V1'de çalıştırılmaz (aşağıda).
7. Reklam harcaması dönem toplamında `ad_spend`'ten düşülür; sipariş kalemindeki `advertising_cost` ile çift sayım olmaması için AI katmanı ürün bazında yalnızca `ad_spend` bölüşümünü + SKU'ya bağlı `expenses` reklam kayıtlarını kullanır.

### 5. Eksik API erişimleri
| İhtiyaç | Durum | Sonuç |
|---|---|---|
| Trendyol Reklam (bütçe, tıklama, ROAS) | Bağlı değil | Reklam verisi elle/CSV; bütçe değişikliği **manuel uygulanır** |
| Trendyol ürün yorumu / soru / iade nedeni | Bağlı değil | CX Analyzer devre dışı |
| Trendyol hakediş / cari | Bağlı değil | Nakit pozisyonu elle |
| Trendyol fiyat/stok güncelleme | Kod var, **bilinçli kapalı** (Çanta Bayim otomasyonuyla çakışmamak için) | Fiyat önerileri manuel |
| Meta / Instagram Graph API | Bağlı değil (OAuth uygulaması + App Review gerekir) | Social Media Agent V2 |
| Claude API (CEO sohbeti için) | `ANTHROPIC_API_KEY` ile isteğe bağlı | Anahtar yoksa CEO sohbeti kural tabanlı, yine gerçek veriyle cevap verir |

## B. V1 mimarisi

```
OWNER (panel, onay yetkisi: admin)
  │
CEO AGENT ── günlük özet, önerileri sahibin geçmiş kararları + sonuçlarıyla karşılaştırır, sohbet
  │
  ├─ Product & Profit Agent   (SKU birim ekonomisi, STAR/PROFITABLE/WATCH/LOSS/NO_DATA)
  ├─ Advertising Agent        (kampanya net kâr etkisi; INCREASE/DECREASE/PAUSE/CONTINUE/TEST/INSUFFICIENT_DATA)
  ├─ Inventory/Supplier Agent (satış hızı, stok günü, tükenme riski → DO_NOT_SCALE_ADS sinyali, ölü stok)
  └─ Capital Engine           (kasa ≠ kâr; gerekçelendirilmiş sermaye vs. kullanılmayan sermaye)
  │
RISK & AUDIT ENGINE ── her öneriyi bağımsız olarak yeniden hesaplar; BLOKE edebilir; kimse bypass edemez
  │
APPROVAL (LOW/MEDIUM/HIGH/CRITICAL; para/müşteri etkili her şey onaylı)
  │
ACTION ENGINE ── executor kaydı; V1'de platforma yazan executor YOK → onaylanan öneri "manuel uygulanacak"
  │                olarak işaretlenir, sahip "Uyguladım" der, sonuç ölçümü başlar
DECISION JOURNAL + OUTCOME EVALUATOR (1/3/7/30 gün) → OWNER INTELLIGENCE + AI SCORECARD
```

**Önemli tasarım kararları**
* **Sayısal kararlar deterministiktir, LLM üretmez.** Bütçe, sınıflandırma, risk kontrolleri kural + eşik (ayarlardan) ile yapılır; böylece "halüsinasyon" kâr rakamı oluşamaz ve her karar tekrar üretilebilir. LLM yalnızca CEO sohbetinde, **salt okunur araçlarla** çekilmiş gerçek veriyi Türkçe anlatmak için kullanılır ve hiçbir aksiyon alamaz.
* **Platform bağımsız çekirdek:** öneriler `channel` + `action_type` + `entity` taşır; uygulama `executors` kaydı üzerinden yapılır (`trendyol_ads.set_daily_budget` gibi). Hepsiburada/Shopify/Meta/Google Ads eklemek yeni executor + veri kaynağı yazmaktır; çekirdeğe dokunulmaz.
* **Sahip tercihi ≠ iş kanıtı.** `ai_owner_preferences` (açık tercihler) ve karar günlüğünden hesaplanan kanıt ayrı tutulur. CEO, aynı tür kararın geçmiş sonuçları kötüyse bunu öneriye not olarak yazar ("Bu yaklaşımı 8 kez kullandın, 6'sında net kâr düştü").
* **Acil durdurma** (`ai.emergency_stop`): onay/uygulama, pazaryerine yayın işleri ve tedarikçiye otomatik sipariş gönderimi durur; analiz çalışmaya devam eder. Durdurmayı operatör, yeniden başlatmayı yalnızca yönetici yapabilir.

### V1'de bilinçli olarak OLMAYANLAR
Social Media Agent, Marketing Agent, Experiment Engine, Market/Opportunity Radar, Customer Experience Analyzer:
ajan kaydında `not_available` durumunda görünür ve **neden çalışmadığı** (eksik veri kaynağı) yazılır. Veri kaynağı
olmadan çalıştırılırlarsa ya boş ya da uydurma çıktı üretirler; ikisi de kâra hizmet etmez. Tablo şeması da
eklenmedi (kullanılmayan tablo teknik borçtur); veri kaynağı bağlandığında kendi migration'larıyla eklenecek.

## Veritabanı (migration 0010, yalnızca ekleme)
`ai_agents`, `ai_agent_runs`, `ai_proposals` (öneri + onay + uygulama durumu), `ai_risk_events`, `ai_activity`,
`ai_decisions`, `ai_decision_outcomes`, `ai_owner_preferences`, `ai_capital_accounts`, `ai_chat_messages`,
`ai_briefs`, `ai_inventory_snapshots`; `ad_campaigns.daily_budget`; `ai.*` ayar varsayılanları.
Mevcut `audit_logs`, `app_settings`, `ad_*`, `expenses` yeniden kullanılır (aynı amaçlı yeni tablo açılmadı).

## Güvenlik modeli
| İzin | Rol | Kapsam |
|---|---|---|
| READ | viewer, accountant, operator, admin | Tüm AI ekranları, sohbet |
| PROPOSE | operator, admin + ajanlar | Ajan çalıştırma, öneri üretme |
| APPROVE | admin | Onay / ret |
| EXECUTE | admin | "Uygulandı" işaretleme, executor tetikleme |
| ADMIN | admin | Eşikler, ajan aç/kapa, acil durdurmayı kaldırma |

Ajanlar yalnızca READ + PROPOSE yetkisine sahiptir; hiçbir ajan onay veremez. Her öneri, onay, ret, uygulama,
blokaj, acil durdurma `audit_logs`'a yazılır. API anahtarları yalnızca ortam değişkenlerinde.

## Uygulama sırası
1. Paylaşılan veri katmanı (`services/ai/data.py`) — finance_view üzerine ürün ekonomisi, kampanya, stok, dönem
2. Product & Profit Agent  3. Inventory Agent  4. Advertising Agent  5. Capital Engine + nakit pozisyonu
6. Risk Engine  7. Onay + Action Engine + acil durdurma  8. Karar günlüğü + sonuç değerlendirici + sahip analizi
9. CEO (günlük özet, öneri incelemesi, sohbet)  10. Worker döngüsü (`ai.cycle`)  11. Panel: AI Control Center
12. Testler (gerçek finans formülleriyle; LLM çağrısı test edilmez, kural tabanlı sohbet test edilir)

## C. Doğrulama turu (V1 davranış kanıtları)

Kanıt testleri: `backend/tests/test_ai_proof.py` (gerçek kod yolları, kontrollü veri; sonuçlar `evaluate_outcomes` ile
gerçek sipariş/reklam verisinden hesaplanır, elle yazılmaz).

**Öğrenme modeli (migration 0011):**
* `situation`: kararın verildiği durum anahtarı (`ads:early` = reklam az veriyle, `ads:mature`, `product:<sınıf>`).
* OWNER_PREFERENCE (`decisions.owner_preference`): sahibin kendi kararları (`decision='owner_action'`), ajan önerilerini
  onay/ret oranı, açık tercihler (`ai_owner_preferences`). Sonuçtan bağımsızdır.
* BUSINESS_EVIDENCE (`decisions._evidence`): uygulanmış kararların ölçülmüş sonuçları; benzer durumda ≥3 sonuç yoksa
  aynı karar türünün tüm sonuçları. Sahip kaynaklı ve AI kaynaklı sonuçlar ayrıca raporlanır (AI isabeti).
* CEO duruşu (`decisions.assess`): ≥3 sonuçta ≥%60 kötüleşme **ve** toplam net kâr etkisi negatifse `oppose`;
  ≥%60 iyileşme ve toplam pozitifse `support`; aksi `neutral`. Tercih duruşu değiştirmez, yalnızca gerekçede söylenir.
* Owner override: CEO `oppose` + risk LOW/MEDIUM → en az 10 karakterlik gerekçe ile onay, `override` olarak kaydedilir
  ve `ai.owner_override` denetim kaydı yazılır. CEO `oppose` + HIGH/CRITICAL → onaylanamaz. Risk motoru bloğu
  (`blocked`) hiçbir koşulda (override dahil) aşılamaz.
* Ürün kârı tek kaynak: `finance_view.item_columns` + `finance_view.campaign_ad_allocation` + `finance_view.sku_ad_expenses`;
  SKU raporu, Reklam merkezi, AI Kâr Merkezi, ajanlar, CEO sohbeti ve karar günlüğü aynı fonksiyonları kullanır.
