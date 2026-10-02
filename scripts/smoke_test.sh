#!/usr/bin/env sh
# Çalışan bir TrendHub yığınına karşı uçtan uca duman testi.
# Kullanım: BASE_URL=http://localhost:8081 ADMIN_USER=admin ADMIN_PASSWORD=... scripts/smoke_test.sh
#           SMOKE_ANONYMOUS=1 BASE_URL=... scripts/smoke_test.sh   (giriş gerektirmeyen kontroller)
# Yalnızca okuma + giriş/çıkış yapar; pazaryerine hiçbir istek göndermez.
set -eu
BASE_URL="${BASE_URL:-http://localhost:8081}"
JAR="$(mktemp)"
trap 'rm -f "$JAR"' EXIT
H="X-Requested-With: TrendHub"

step() { printf '• %s\n' "$1"; }
fail() { printf '✗ %s\n' "$1" >&2; exit 1; }

step "health"
curl -fsS "$BASE_URL/api/health" | grep -q '"healthy"' || fail "/api/health sağlıklı değil"

step "kimlik doğrulamasız erişim reddedilir"
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE_URL/api/dashboard")
[ "$code" = "401" ] || fail "/api/dashboard beklenen 401, gelen $code"

step "güvenlik başlıkları"
curl -fsSI "$BASE_URL/" | grep -qi 'content-security-policy' || fail "CSP başlığı yok"

if [ -n "${STOREFRONT_URL:-}" ]; then
  step "web mağazası (storefront)"
  curl -fsS "$STOREFRONT_URL/api/store/health" | grep -q '"healthy"' || fail "storefront /api/store/health"
  curl -fsS "$STOREFRONT_URL/" | grep -q 'TRENDÇANTANIZ' || fail "mağaza ana sayfası"
  curl -fsSI "$STOREFRONT_URL/" | grep -qi 'content-security-policy' || fail "mağaza CSP başlığı yok"
  curl -fsS "$STOREFRONT_URL/robots.txt" | grep -q 'Sitemap:' || fail "robots.txt"
  curl -fsS "$STOREFRONT_URL/sitemap.xml" | grep -q '<urlset' || fail "sitemap.xml"
  code=$(curl -s -o /dev/null -w '%{http_code}' "$STOREFRONT_URL/api/orders")
  [ "$code" = "404" ] || fail "yönetim API'si mağaza adresinden erişilebilir olmamalı (gelen $code)"
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d '{"product_id":1}' "$STOREFRONT_URL/api/store/cart/add")
  [ "$code" = "403" ] || fail "özel başlıksız sepet isteği reddedilmeli (gelen $code)"
  curl -fsS "$STOREFRONT_URL/hesap/giris" | grep -q 'Üye ol' || fail "müşteri giriş sayfası"
  code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -d '{}' "$STOREFRONT_URL/api/store/account/login")
  [ "$code" = "403" ] || fail "özel başlıksız hesap isteği reddedilmeli (gelen $code)"
fi

if [ "${SMOKE_ANONYMOUS:-0}" = "1" ]; then
  step "panel dosyaları"
  curl -fsS "$BASE_URL/" | grep -q 'TrendHub' || fail "panel HTML"
  curl -fsS "$BASE_URL/assets/app.js" >/dev/null || fail "app.js"
  printf '✓ Anonim duman testi başarılı (giriş adımları atlandı)\n'
  exit 0
fi

step "giriş"
curl -fsS -c "$JAR" -H "$H" -H 'Content-Type: application/json' \
  -d "{\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASSWORD\"}" "$BASE_URL/api/auth/login" >/dev/null \
  || fail "giriş başarısız"

step "sistem sağlığı (şema + worker)"
sys=$(curl -fsS -b "$JAR" "$BASE_URL/api/system/health")
echo "$sys" | grep -q '"worker_alive":true' || fail "worker canlı değil: $sys"
echo "$sys" | grep -q '"connector_write_enabled":false' || fail "pazaryeri yazma kapalı olmalı"

step "credential yokken entegrasyonlar 'Bağlı değil'"
integ=$(curl -fsS -b "$JAR" "$BASE_URL/api/integrations")
n=$(echo "$integ" | grep -o '"state":"not_connected"' | wc -l)
[ "$n" -eq 3 ] || fail "3 entegrasyon da not_connected olmalı: $integ"

step "credential yokken hiçbir senkron işi planlanmaz"
# Yalnızca iç (dışarıya istek atmayan) uyarı taraması ve web mağazası bakımı beklenir; pazaryeri/tedarikçi işi olmamalı.
jobs=$(curl -fsS -b "$JAR" "$BASE_URL/api/system/jobs?page_size=200")
others=$(echo "$jobs" | grep -o '"job_type":"[^"]*"' | grep -v -e '"job_type":"alerts.scan"' -e '"job_type":"storefront.maintenance"' -e '"job_type":"storefront.notify"' -e '"job_type":"ai.cycle"' || true)
[ -z "$others" ] || fail "beklenmeyen iş var: $others"

step "dashboard ve panel"
curl -fsS -b "$JAR" "$BASE_URL/api/dashboard?period=7d" | grep -q '"statuses"' || fail "dashboard"
curl -fsS -b "$JAR" "$BASE_URL/api/ai/overview" | grep -q '"emergency_stop"' || fail "AI Control Center"
curl -fsS "$BASE_URL/" | grep -q 'TrendHub' || fail "panel HTML"
curl -fsS "$BASE_URL/assets/app.js" >/dev/null || fail "app.js"

step "çıkış"
curl -fsS -b "$JAR" -H "$H" -X POST "$BASE_URL/api/auth/logout" >/dev/null || fail "çıkış"
printf '✓ Duman testi başarılı\n'
