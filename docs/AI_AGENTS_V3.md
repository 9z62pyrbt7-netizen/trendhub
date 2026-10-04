# AI ajanları V3 — çalışan zincir, guardrail'ler, aksiyon kaydı

## Döngü (her saat + elle "Ajanları şimdi çalıştır")
1. **Veri normalizasyonu:** sipariş kalemi ↔ ürün eşleştirmesi tamamlanır, maliyeti sonradan girilen kalemler yeniden hesaplanır (`ai/reconcile.py`).
2. Stok → Ürün & Kâr → Ürün Takibi → Fiyat → Kampanya → Reklam → Pazarlama → Sosyal Medya → Müşteri Deneyimi → Sermaye.
3. **CEO hakemliği** (`ai/ceo_review.resolve_conflicts`): stok kritik / ürün zararda / fiyat artışı bekliyorsa reklam ve indirim önerilerini engeller. Onaylanmış ama uygulanmamış öneriler de bu kontrolden geçer.
4. Her öneri risk motorundan geçer: guardrail'ler, veri tazeliği ve sermaye.
5. Aksiyon kaydı (`ai_actions`), yönetici özeti (`ai_briefs.summary`).
6. Sonuçlar 1/3/7/30 gün sonra ölçülür (geri bildirim).

**Zamanlama:** `ai.cycle` saatlik. `ai.daily_review` her gün 08:00 (TR) sonrasında bir kez, `ai.weekly_review` pazartesi.
- İşler tarih anahtarlıdır: aynı gün ikinci kez oluşmaz, worker yeniden başlasa da kaybolmaz.
- İki AI döngüsü aynı anda çalışamaz (PostgreSQL advisory lock).

## Aksiyon durumları
| Durum | Anlamı |
|---|---|
| EXECUTED | Yalnızca gerçek platform/DB işlemi başarılı olduysa. Sahibin "Uyguladım" kaydı da EXECUTED'dır ama "manuel, TrendHub doğrulamadı" olarak işaretlenir. |
| PROPOSED | Öneri; onay veya uygulama bekliyor. |
| BLOCKED | Guardrail, risk veya CEO çatışma kuralı engelledi. `reason_code` örnekleri: needs_data, stock_risk, budget_limit, negative_margin, campaign_loss, finance_stale, conflict. |
| FAILED | Uygulama denendi, dış servis hata verdi; hata metni kaydedilir, fiyat ve bütçe değişmez. |
| SKIPPED | Bağlantı yok, pazaryerine yazma kapalı veya döngü limiti doldu. |

## Guardrail'ler (AI → Ayarlar → eşikler)
| Eşik | Varsayılan |
|---|---|
| `daily_ad_budget_limit` | 1.500 TL (aktif günlük bütçeler + açık artış önerileri) |
| `advertising_max_capital_per_proposal` | 5.000 TL |
| `inventory_max_capital_per_proposal` | 20.000 TL |
| `min_unit_profit` | 20 TL |
| `min_net_margin` | %10 |
| `max_price_change_pct` | %10 |
| `max_price_changes_per_cycle` | 5 |
| `stock_safety_units` | 2 |

- Diğer ajanlar para isteyemez (`unauthorized_spend`).
- Maliyeti bilinmeyen ürüne fiyat, kampanya veya reklam kararı verilmez (NEEDS_DATA).

## Uygulama (executor)
- **Fiyat:** yalnızca onaylı öneri için ve `CONNECTOR_WRITE_ENABLED=true` iken `connector.update_price` çağrılır. Trendyol fiyat yazma bu repoda UYGULANMADI, dolayısıyla deneme FAILED olur. Yazma kapalıysa SKIPPED; uygulama adımları Trendyol panelinde manuel yapılır.
- **Reklam:** `ai/ads_platforms.py` adaptörleri (Meta, Trendyol Reklam). İkisi de bağlı değil, öneriler SKIPPED ve manuel adım verilir. Meta bağlanacağı zaman yalnızca `MetaAdsAdapter` metotları yazılır ve `WRITE_IMPLEMENTED=True` yapılır.

## Sınıflandırmanın boş çıkmasının kök nedeni
- Satışlar ürünle yalnızca birebir SKU/barkodla eşleşiyordu: harf/boşluk farkı eşleşmeyi bozuyordu, ilan ve tedarikçi barkodu kullanılmıyordu.
- Maliyet sonradan girildiğinde eski siparişler yeniden hesaplanmıyordu.
- Sonuç: eşleşmeyen satışlar analizde görünmüyordu, maliyeti sonradan gelen ürünler ise sürekli NO_DATA kalıyordu.
- Artık her döngü bunu onarıyor. "Veri kapsamı" kartı neden NO_DATA olduğunu sayılarla gösteriyor (eşleşmeyen kalem, maliyetsiz kalem, az satış).
