#!/usr/bin/env bash
# TrendHub güvenli production deploy betiği.
#
# Kullanım (TrendHub repo klasöründe, sunucuda):
#     ./deploy/deploy.sh
#
# Bu betik YALNIZCA TrendHub compose projesine dokunur:
#   * /opt/trendcantamiz-xml (canlı Trendyol -> Çanta Bayim otomasyonu) ve başka
#     hiçbir container/volume/ağ durdurulmaz, yeniden başlatılmaz, silinmez.
#   * Veritabanı silinmez; migration öncesi pg_dump yedeği alınır ve doğrulanır.
#   * `docker compose down -v`, `docker system prune`, `docker volume rm` KULLANILMAZ.
#
# Ortam değişkenleri (isteğe bağlı):
#   TRENDHUB_BUILD=0          imajı derleme (önceden derlenmiş trendhub-backend kullan)
#   TRENDHUB_GIT_PULL=0       git pull yapma (mevcut checkout'u deploy et)
#   TRENDHUB_BRANCH=main      çekilecek dal
#   TRENDHUB_HEALTH_TIMEOUT=240
#   ADMIN_USER / ADMIN_PASSWORD  verilirse giriş dahil tam duman testi çalışır (loglanmaz)
set -Eeuo pipefail

PROTECTED_DIR="/opt/trendcantamiz-xml"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$REPO_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
LOG_DIR="$REPO_DIR/deploy/logs"
BACKUP_DIR="${TRENDHUB_BACKUP_DIR:-$REPO_DIR/backups}"
mkdir -p "$LOG_DIR" "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
LOG="$LOG_DIR/deploy-$TS.log"
exec > >(tee -a "$LOG") 2>&1

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31m✗ DURDU: %s\033[0m\n' "$*" >&2; exit 1; }
trap 'die "Beklenmeyen hata (satır $LINENO). Hiçbir veri silinmedi. Log: $LOG"' ERR

# ----------------------------------------------------------------- 0. korumalar
say "0. Güvenlik kontrolleri"
case "$REPO_DIR/" in
  "$PROTECTED_DIR"/*) die "Bu betik $PROTECTED_DIR içinde çalıştırılamaz." ;;
esac
ok "Çalışma klasörü: $REPO_DIR (korunan klasör dışında)"
command -v docker >/dev/null || die "docker bulunamadı"
docker compose version >/dev/null 2>&1 || die "docker compose v2 gerekli"
docker info >/dev/null 2>&1 || die "docker daemon'a erişilemiyor (sudo/grup yetkisi?)"
[ -f .env ] || die ".env yok. 'cp .env.example .env' yapıp değerleri doldurun (bkz. docs/DEPLOYMENT.md)."
[ "$(stat -c '%a' .env)" = "600" ] || { chmod 600 .env; warn ".env izinleri 600 yapıldı"; }

envget() { # .env'den değer okur; değeri ASLA yazdırmaz
  local v
  v="$(grep -E "^$1=" .env | tail -n1 | cut -d= -f2- || true)"
  v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
  printf '%s' "$v"
}
for k in POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD DATABASE_URL; do
  v="$(envget "$k")"
  [ -n "$v" ] && [ "$v" != "CHANGE_ME" ] || die ".env içinde $k tanımlı değil veya CHANGE_ME"
done
case "$(envget DATABASE_URL)" in *CHANGE_ME*) die "DATABASE_URL hâlâ CHANGE_ME içeriyor" ;; esac
[ "$(envget CONNECTOR_WRITE_ENABLED | tr 'A-Z' 'a-z')" = "false" ] || [ -z "$(envget CONNECTOR_WRITE_ENABLED)" ] \
  || die "CONNECTOR_WRITE_ENABLED=false olmalı (pazaryerine yazma kapalı kalmalı)"
ok ".env zorunlu değerler tanımlı; pazaryeri yazma kapalı (değerler gösterilmez)"

# ------------------------------------------------ 1. mevcut durum ve proje adı
say "1. Sunucu durumu"
df -h "$REPO_DIR" | tail -n1 | awk '{print "  disk: "$4" boş / "$2" ("$5" dolu)"}'
avail_kb="$(df -Pk "$REPO_DIR" | tail -n1 | awk '{print $4}')"
[ "$avail_kb" -gt 2097152 ] || die "En az 2 GB boş disk gerekli"
if command -v systemctl >/dev/null 2>&1; then
  if systemctl is-enabled docker >/dev/null 2>&1; then ok "docker servisi açılışta başlıyor (reboot sonrası stack geri gelir)"
  else warn "docker servisi açılışta etkin değil: 'sudo systemctl enable docker' önerilir"; fi
fi

PROJECT="${COMPOSE_PROJECT_NAME:-$(basename "$REPO_DIR" | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9_-')}"
for c in trendhub-db trendhub-api trendhub-web trendhub-worker; do
  if docker inspect "$c" >/dev/null 2>&1; then
    p="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$c" 2>/dev/null || true)"
    wd="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$c" 2>/dev/null || true)"
    case "$wd/" in "$PROTECTED_DIR"/*) die "$c container'ı $PROTECTED_DIR projesine ait görünüyor; dokunulmayacak." ;; esac
    if [ -n "$p" ] && [ "$p" != "$PROJECT" ]; then
      warn "$c mevcut '$p' compose projesine ait; aynı proje adı kullanılacak (veri volume'u korunur)"
      PROJECT="$p"
    fi
  fi
done
export COMPOSE_PROJECT_NAME="$PROJECT"
DC=(docker compose -p "$PROJECT")
ok "Compose projesi: $PROJECT"

# Diğer (TrendHub dışı) container'ların anlık görüntüsü: deploy sonrası değişmemeli
others_snapshot() {
  docker ps -a --format '{{.ID}} {{.Names}} {{.Label "com.docker.compose.project"}}' \
    | awk -v p="$PROJECT" '$3 != p {print $1}' \
    | xargs -r docker inspect -f '{{.Name}} {{.Id}} {{.State.Status}} {{.State.StartedAt}} {{.RestartCount}}' | sort
}
BEFORE="$(others_snapshot || true)"
echo "  TrendHub dışı container sayısı: $(printf '%s' "$BEFORE" | grep -c . || true) (dokunulmayacak)"
docker ps --format '  {{.Names}}\t{{.Ports}}' || true

# ----------------------------------------------------------------- 2. port
say "2. Port kontrolü"
port_owner() { # porta bağlı container adı (yoksa boş)
  docker ps --format '{{.Names}} {{.Ports}}' | grep -E "(:|\s)$1->" | awk '{print $1}' | head -n1 || true
}
port_listening() { (command -v ss >/dev/null && ss -ltnH "sport = :$1" | grep -q .) ; }
PORT="$(envget TRENDHUB_HTTP_PORT)"; PORT="${PORT:-8081}"
pick_port() {
  local p="$1"
  while [ "$p" -le 8099 ]; do
    owner="$(port_owner "$p")"
    if [ -z "$owner" ] && ! port_listening "$p"; then echo "$p"; return; fi
    if [ "$owner" = "trendhub-web" ]; then
      wp="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' trendhub-web)"
      [ "$wp" = "$PROJECT" ] && { echo "$p"; return; }
    fi
    p=$((p + 1))
  done
  return 1
}
NEW_PORT="$(pick_port "$PORT")" || die "8081-8099 arasında boş port yok"
if [ "$NEW_PORT" != "$PORT" ]; then
  warn "Port $PORT başka bir süreç/container tarafından kullanılıyor; TrendHub için $NEW_PORT seçildi"
  if grep -qE '^TRENDHUB_HTTP_PORT=' .env; then sed -i "s/^TRENDHUB_HTTP_PORT=.*/TRENDHUB_HTTP_PORT=$NEW_PORT/" .env
  else printf '\nTRENDHUB_HTTP_PORT=%s\n' "$NEW_PORT" >> .env; fi
fi
export TRENDHUB_HTTP_PORT="$NEW_PORT"
ok "TrendHub web portu: $NEW_PORT"

# ------------------------------------------------------------------ 3. kod
say "3. Kod"
if [ "${TRENDHUB_GIT_PULL:-1}" = "1" ] && [ -d .git ]; then
  [ -z "$(git status --porcelain --untracked-files=no)" ] || die "Repoda commit edilmemiş değişiklik var; önce inceleyin"
  git fetch --quiet origin "${TRENDHUB_BRANCH:-main}"
  git checkout --quiet "${TRENDHUB_BRANCH:-main}"
  git pull --quiet --ff-only origin "${TRENDHUB_BRANCH:-main}"
fi
COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo bilinmiyor)"
ok "Deploy edilecek commit: $COMMIT"
"${DC[@]}" config -q || die "docker compose config geçersiz"
ok "compose yapılandırması geçerli"

# ---------------------------------------------------------------- 4. yedek
say "4. Veritabanı yedeği"
PGU="$(envget POSTGRES_USER)"; PGD="$(envget POSTGRES_DB)"
DB_CID="$("${DC[@]}" ps -q db 2>/dev/null || true)"
[ -n "$DB_CID" ] || DB_CID="$(docker ps -aq -f name='^trendhub-db$' || true)"
if [ -n "$DB_CID" ]; then
  if [ "$(docker inspect -f '{{.State.Running}}' "$DB_CID")" != "true" ]; then
    warn "Mevcut TrendHub DB durdurulmuş; yedek için yalnızca db servisi başlatılıyor"
    "${DC[@]}" up -d db
    sleep 5
  fi
  for i in $(seq 1 30); do docker exec "$DB_CID" pg_isready -U "$PGU" -d "$PGD" >/dev/null 2>&1 && break; sleep 2; done
  BACKUP="$BACKUP_DIR/trendhub-$TS-$COMMIT.dump"
  docker exec "$DB_CID" pg_dump -U "$PGU" -d "$PGD" -Fc > "$BACKUP"
  chmod 600 "$BACKUP"
  [ -s "$BACKUP" ] || die "Yedek dosyası boş: $BACKUP"
  docker exec -i "$DB_CID" pg_restore -l < "$BACKUP" > /dev/null || die "Yedek doğrulanamadı: $BACKUP"
  ok "Yedek alındı ve doğrulandı: $BACKUP ($(du -h "$BACKUP" | cut -f1))"
else
  ok "Mevcut TrendHub veritabanı yok (ilk kurulum); yedek gerekmiyor"
fi

# --------------------------------------------------------------- 5. deploy
say "5. Derleme ve başlatma (yalnızca TrendHub servisleri)"
if [ "${TRENDHUB_BUILD:-1}" = "1" ]; then "${DC[@]}" build; else warn "TRENDHUB_BUILD=0: derleme atlandı"; fi
"${DC[@]}" up -d db
"${DC[@]}" run --rm migrate || die "Migration başarısız. Yedek: ${BACKUP:-yok}. Uygulama container'ları değiştirilmedi."
ok "Migration tamam"
"${DC[@]}" up -d --no-deps api worker web
ok "api, worker, web başlatıldı"

# ------------------------------------------------------------ 6. sağlık
say "6. Sağlık kontrolü"
deadline=$(( $(date +%s) + ${TRENDHUB_HEALTH_TIMEOUT:-240} ))
while :; do
  all=1; line=""
  for s in db api worker web; do
    cid="$("${DC[@]}" ps -q "$s")"
    h="$( [ -n "$cid" ] && docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" || echo yok)"
    line="$line $s=$h"
    [ "$h" = "healthy" ] || all=0
  done
  echo " $line"
  [ "$all" = 1 ] && break
  [ "$(date +%s)" -lt "$deadline" ] || { "${DC[@]}" ps; "${DC[@]}" logs --tail 80 api worker web; die "Servisler zamanında sağlıklı olmadı"; }
  sleep 5
done
ok "db, api, worker, web: healthy"

BASE_URL="http://127.0.0.1:$NEW_PORT"
curl -fsS "$BASE_URL/api/health" | grep -q '"healthy"' || die "$BASE_URL/api/health sağlıklı değil"
if [ -n "${ADMIN_USER:-}" ] && [ -n "${ADMIN_PASSWORD:-}" ]; then
  BASE_URL="$BASE_URL" scripts/smoke_test.sh
else
  BASE_URL="$BASE_URL" SMOKE_ANONYMOUS=1 scripts/smoke_test.sh
  warn "Girişli duman testi atlandı (ADMIN_USER/ADMIN_PASSWORD verilmedi)"
fi

# ------------------------------------------- 7. diğer container'lar değişmedi mi
say "7. Canlı sistemler etkilenmedi mi?"
AFTER="$(others_snapshot || true)"
if [ "$BEFORE" = "$AFTER" ]; then ok "TrendHub dışındaki tüm container'lar aynen çalışıyor (ID, durum, başlama zamanı değişmedi)"
else
  warn "TrendHub dışı container listesinde fark var (bu betik onlara dokunmaz; yine de kontrol edin):"
  diff <(printf '%s\n' "$BEFORE") <(printf '%s\n' "$AFTER") || true
fi
if [ -d "$PROTECTED_DIR" ]; then ok "$PROTECTED_DIR klasörüne erişilmedi/değiştirilmedi"; fi

HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
say "TAMAM"
echo "  Commit : $COMMIT"
echo "  Adres  : http://${HOST_IP:-SUNUCU_IP}:$NEW_PORT"
echo "  Yedek  : ${BACKUP:-ilk kurulum, yedek yok}"
echo "  Log    : $LOG"
echo "  Yönetici hesabı yoksa: ./deploy/create-admin.sh"
