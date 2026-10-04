# V3 → production birleştirme planı (BLOCKED: production koduna erişim yok)

## Neden durdu
- Bu çalışma ortamında `/opt/trendhub` yok ve sunucuya SSH erişimi yok.
- Production'daki değişikliklerin hiçbiri GitHub'daki bir dalda değil: `0013_ad_attribution.py`, `trendyol_ads_import.py`, `ad_performance.direct_*/indirect_*`, `product_content_id`, finans apply düzeltmesi.
- Görmediğim kodu birleştirmek veya migration'ı tahminle yeniden zincirlemek, canlı düzeltmeleri ezebilir. Bu yüzden birleştirme ve deploy YAPILMADI.

## Bu dalda hazırlananlar
- **Doğrudan/dolaylı reklam atfı koruması** (`ai/data.py`, `ai/agents.py:ad_verdict`):
  - `ad_performance` tablosunda `direct_orders/indirect_orders/direct_revenue/indirect_revenue` varsa okunur, yoksa davranış değişmez.
  - Doğrudan satış 0 ise karar INSUFFICIENT_DATA olur; PAUSE, artış veya azaltma önerilmez.
  - Karma atıfta kâr kanıtı yalnızca doğrudan cirodan hesaplanır; ROAS toplam atıfla gösterilir.
  - Testler canlıdaki gerçek kampanyanın rakamlarıyla yazıldı (2.270,76 TL / 5.795 TL / 0 doğrudan / 5 dolaylı).

## Engeli kaldırmak için (sunucuda, salt okuma + git push; servis RESTART YOK)

**1. Yedek (yalnızca TrendHub):**
```
cd /opt/trendhub && ./deploy/backup.sh
```

**2. Production kodunun anlık görüntüsünü GitHub'a gönder.** `.env`, yedekler ve loglar gönderilmez.
```
cd /opt/trendhub
git status --short                      # önce listeyi kontrol et: .env / *.dump / secret OLMAMALI
git checkout -b production-snapshot-20261004
git add -A -- . ':!.env' ':!backups' ':!deploy/logs'
git commit -m "Production anlık görüntüsü (2026-10-04)"
git push origin production-snapshot-20261004
git checkout -                          # çalışan dala geri dön (dosyalar değişmez)
```

**3. Migration grafiği:**
```
docker compose exec -T api alembic heads
docker compose exec -T api alembic history | head -20
```
Çıktıyı bana ilet.

## Dal gelince yapılacak birleştirme
- `production-snapshot` + `e29800e` (+ bu hazırlık) birleştirilir. Çakışmalarda production'ın veri entegrasyonları ve reklam atıf düzeltmesi esas alınır.
- V3 migration'ı `0013_agent_operations` → `0014_agent_operations`, V4 ajan çalışma zamanı `0014_agent_runtime` → `0015_agent_runtime` olarak yeniden adlandırılır. `0014_agent_operations`'ın `down_revision`'ı production'daki `0013_ad_attribution` revizyon kimliği, `0015_agent_runtime`'ınki `0014_agent_operations` olur. Production migration'larına dokunulmaz.
- Testler çalıştırılır: production testleri + V3 testleri + migration testi (production yedeğinin geri yüklenmiş kopyası üzerinde) + atıf regresyonu.
- Deploy etmeden önce sayım karşılaştırması: order, listing, cost, finance ve ad kayıt sayıları migration öncesi ve sonrası birebir aynı olmalı.
- Deploy sunucuda yapılır; bu ortamdan yapılamaz. Sunucuda Claude Code (Remote Control) oturumu açılırsa adımları oradan birlikte yürütürüz.
- Platform yazma kapalı kalır: `CONNECTOR_WRITE_ENABLED=false`, Meta bağlı değil.
