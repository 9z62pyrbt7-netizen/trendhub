# AI ajan çalışma zamanı (V4) — görev dağıtımı, araçlar, kanıt, doğrulama

## Akış
```
USER → CEO (plan: deterministik niyet kuralları) → uzman ajan görevleri (ai_agent_tasks, task_uid)
     → tipli araçlar (ai_tool_calls) → ajan sonucu + iddialar (ai_evidence)
     → Gerçeklik Denetçisi (araçları bağımsız yeniden çalıştırır) → CEO cevabı (yalnızca doğrulanmış rakamlar) → USER
```
- Giriş noktaları:
  - `POST /api/agents/requests`
  - Panel → AI Control Center → **Ajan Kontrol Merkezi**
  - CEO sohbeti (`/api/ai/chat`): durum analizleri ve açık aksiyon talimatları orkestre edilir; diğer sorular mevcut sohbet motoruna gider. O yoldaki araç çağrıları da `ai_tool_calls`'a yazılır.
- Kod: `backend/app/services/ai/`
  - `orchestrator.py`, `specialists.py`, `tools.py`, `reality.py`
  - `governor.py`, `profit_guard.py`, `memory.py`, `operations.py`, `experiments.py`, `creative.py`
  - `metrics.py`, `sanitize.py`, `roles.py`
- API: `backend/app/api/agents.py`

## 10 çalışma birimi
Döngüdeki V3 ajanları, bağlı oldukları birimin alt işçisidir (`ai_agents.unit`):

| Birim | Görev(ler) | Kullandığı araçlar |
|---|---|---|
| CEO | plan, sentez, itiraz, hafıza | `get_decision_history`; yazma: yetkili birim üzerinden |
| Finans (+ sermaye) | kârlılık, bütçe kontrolü | `get_profitability` (net kâr, sipariş/SKU başı kâr, katkı marjı, başabaş ROAS/CPA; maliyet eksikse UNKNOWN), `get_budget_status` |
| Analitik (+ risk) | sipariş/satış, anomali | `get_orders`, `get_anomalies` |
| Ürün & Trend (+ ürün kârı, takip, fiyat) | portföy, ürün kontrolü, fiyat önerisi | `get_products` (WINNER/PROMISING/NORMAL/WEAK/LOSS_MAKING/UNKNOWN), `get_profit_guard`, `update_price` |
| Pazarlama/Büyüme (+ kampanya) | deney | `propose_experiment` (hipotez/etki/maliyet/risk/süre/ölçüt); ölçülmeden başarılı sayılmaz |
| Reklam | performans, kampanya önerisi | `get_ad_performance`, `get_ad_platforms` (bağlı değilse NOT_CONNECTED), `create_campaign`, `pause_campaign`, `set_campaign_budget` |
| Kreatif | A/B taslakları | `create_ad_draft` (taslak; yayın yok) |
| Sosyal medya | içerik takvimi | `publish_social_post` (bağlantı yok → NOT_CONNECTED; yayın ancak platform ID'si ile sayılır) |
| Operasyon (+ stok, müşteri deneyimi) | stok/kuyruk/sipariş/entegrasyon sağlığı | `get_inventory`, `get_operations_health`, `check_marketplace_connection`; döngüde olay açar/kapatır (`ai_incidents`) |
| Gerçeklik Denetçisi | iddia doğrulama | tüm READ araçları; WRITE araç kullanamaz |

Rol davranışları msitarzewski/agency-agents (MIT) projesinden uyarlandı. Ayrıntı: `docs/THIRD_PARTY_NOTICES.md`.

## READ / WRITE ve risk
| Risk | Örnek | Davranış |
|---|---|---|
| LOW (READ) | tüm okuma araçları | otomatik |
| LOW (WRITE) | kreatif taslağı, deney önerisi | yalnızca TrendHub içine yazılır |
| MEDIUM | kampanya durdurma | öneri + onay (varsayılan) |
| HIGH | fiyat, stok, bütçe artışı, sosyal yayın | öneri + onay |
| CRITICAL | yeni reklam kampanyası | öneri + onay + Bütçe Yöneticisi + Kâr Koruması |

- Para, fiyat veya reklam araçları işlemi yapmaz; risk motorundan geçen bir öneri (`ai_proposals` = onay kaydı) açar.
- Onaydan sonra uygulama mevcut executor'dan geçer. `CONNECTOR_WRITE_ENABLED=false` ve bağlı reklam platformu yok; bu yüzden sonuç SKIPPED olur ve manuel adım verilir.
- Stok yazma bilinçli olarak yok: stok, tedarikçi XML otomasyonu tarafından yönetiliyor.

## Bütçe Yöneticisi / Kâr Koruması
- **Bütçe:** `PUT /api/agents/budget` (yönetici) ile şu alanlar tanımlanır:
  - `total_budget`
  - `daily_limit`, `weekly_limit`
  - `per_agent_limit`, `per_campaign_limit`
  - `max_single_action_amount`
  - `max_daily_ad_spend`
- `total_budget` tanımlı değilse sermaye motorunun kullanılabilir kasası esas alınır; o da yoksa harcama BLOCKED olur.
- Rezervasyon defteri (`ai_budget_ledger`): onayda `reserved`, uygulamada `committed`, ret/başarısız/süresi dolmuşta `released`.
- **Kâr Koruması** ürün durumları:

  | Durum | Koşul | Sonuç |
  |---|---|---|
  | UNKNOWN | maliyet/fiyat yok veya < 3 satış | `profit_guard_unknown_max_spend` (1.000 TL) üstü harcama BLOCKED |
  | DANGER | net zarar / birim kâr ≤ 0 | reklam açma, bütçe artırma ve indirim BLOCKED |
  | WARNING | — | uyarı |
  | SAFE | — | engel yok |

- Kampanya düzeyinde ürünün reklam öncesi kârı esas alınır; reklamın kendi kârlılığı ayrıca kontrol edilir.

## Gerçeklik Denetçisi kuralları
| Durum | Sonuç |
|---|---|
| Araç çağrısı olmayan iddia | UNVERIFIED |
| Ajanın değeri kaydedilmiş araç sonucundan farklı | FAILED |
| Bağımsız yeniden çalıştırmada aynı değer, veri gerçek | VERIFIED |
| Bağımsız yeniden çalıştırmada aynı değer, veri tahmini/kısmi | PARTIALLY_VERIFIED |
| Değişim iddiası, temel dönem/tarih aralığı yok | UNVERIFIED |
| "Reklam başarılı" türü değerlendirme | yalnızca dayandığı net kâr metriği doğrulanırsa |
| "Uygulandı" iddiası | araç/onay kaydı birebir aynı olmalı; dış platform için yanıt kimliği şart |

Araç çağırmadan sonuç üreten görev `no_evidence` olur ve sonucu CEO cevabında kullanılmaz.

## Hata yönetimi
- Araç katmanı:
  - Argüman doğrulama.
  - Birim izni (yetkisizse `denied`).
  - DB sorgu zaman aşımı (`statement_timeout` 15 sn).
  - Dış çağrıda thread zaman aşımı ve üstel geri çekilmeli yeniden deneme.
- Görev geçici hatada en fazla 3 kez denenir.
- Tüm hatalar `ai_agent_errors`'a yazılır. Ajan sonucu (`result.agent_output`) ile araç sonucu (`ai_tool_calls.result`) ayrı tutulur.

## Gözlemlenebilirlik
- `GET /api/agents/metrics` (JSON) ve `GET /api/agents/metrics/prometheus` (metin) şu metrikleri verir:
  - `agent_runs_total`, `agent_failures_total`
  - `tool_calls_total`, `tool_failures_total`
  - `approval_requests`, `actions_blocked`
  - LLM gecikmesi, araç gecikmesi
- Yapılandırılmış log satırları:
  - `trendhub.agents.tools` → `{"event":"tool_call",...}`
  - `trendhub.agents` → `{"event":"agent_request",...}`

## Güvenlik
- Plan, kullanıcı metnindeki talimatlardan çıkmaz.
- Talimat benzeri kalıplar işaretlenir (`injection_suspected`).
- Ürün adı, müşteri sorusu ve iade sebebi veri olarak temizlenir.
- Secret değerleri ve secret benzeri anahtarlar araç kayıtlarına, mesajlara ve özetlere yazılmaz (maskelenir).
- Aksiyon isteği (öneri açma) operatör yetkisi ister; bütçe ayarı yönetici yetkisi ister.

## Migration ve geri alma
- `0014_agent_runtime` (zincir: `0013_agent_operations` → `0014_agent_runtime`) yalnızca ekleme yapar:
  - Yeni tablolar.
  - Mevcut tablolara NULL kabul eden kolonlar.
  - 6 yeni `ai_agents` satırı.
- Hiçbir satır silinmez veya değiştirilmez; yalnızca yeni kolon `unit` doldurulur.
- **Production'a geçerken:** production'da kendi `0013_ad_attribution` migration'ı var. Bu yüzden V3 ve V4 migration'ları `0014_agent_operations` ve `0015_agent_runtime` olarak yeniden numaralanır ve production'ın 0013'üne zincirlenir. Ayrıntı: `docs/PRODUCTION_MERGE_V3.md`.
- **Geri alma:**
  1. Kod: önceki imaj/commit'e dönülür (`deploy/deploy.sh` önceki commit).
  2. Şema: yeni tablolar ve kolonlar eski kod tarafından okunmaz; şemayı geri almak gerekmez. `downgrade` bilinçli olarak desteklenmez (veri kaybını önlemek için).
  3. Tam geri dönüş gerekirse deploy öncesi alınan yedek (`deploy/backup.sh` → `backups/*.dump`) `pg_restore` ile yüklenir.

## Kanıt (bu ortamda)
- Testler:
  - `backend/tests/test_agent_runtime.py`: 32 test.
  - Tam paket: 354 geçti.
- Yerel Docker yığını:
  - deploy → `0014_agent_runtime`, tüm servisler healthy.
  - "Mağazanın durumunu analiz et" → 5 uzman görev + doğrulama. Sonuç: VERIFIED 12, PARTIALLY_VERIFIED 6, FAILED 0.
  - Kontrollü yazma: fiyat önerisi onaylandı, uygulama SKIPPED (yazma kapalı), fiyat değişmedi.
  - Ekran görüntüleri: `docs/screenshots/agent-runtime/`.
- Yığındaki veri yerel test verisidir; gerçek Trendyol kimlik bilgisi bu ortamda yok.
