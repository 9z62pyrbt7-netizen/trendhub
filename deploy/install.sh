#!/usr/bin/env bash
# TrendHub tek komutla kurulum / güncelleme (sunucuda, sudo yetkili kullanıcıyla).
#
#   curl ... değil; repo içinden:   bash deploy/install.sh
#   veya ilk kurulumda:             git clone https://github.com/9z62pyrbt7-netizen/trendhub.git /opt/trendhub \
#                                   && bash /opt/trendhub/deploy/install.sh
#
# Yaptıkları (hepsi yalnızca TrendHub klasöründe):
#   1. /opt/trendcantamiz-xml içinde çalışmayı reddeder; o sisteme hiçbir şekilde dokunmaz.
#   2. main dalını fast-forward ile günceller (yerel değişiklik varsa durur).
#   3. .env YOKSA .env.example'dan oluşturur; veritabanı parolası ve APP_SECRET'ı rastgele üretir
#      (değerler ekrana/loga yazılmaz, dosya izni 600). .env VARSA mevcut değerlere dokunmaz,
#      yalnızca eksik güvenlik anahtarlarını (CONNECTOR_WRITE_ENABLED=false vb.) ekler.
#   4. deploy/deploy.sh: yedek (pg_dump) → migration → sağlık kontrolü → duman testi.
#   5. Aktif yönetici yoksa parolayı gizli sorarak yönetici oluşturur.
set -Eeuo pipefail
PROTECTED_DIR="/opt/trendcantamiz-xml"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31m✗ DURDU: %s\033[0m\n' "$*" >&2; exit 1; }
case "$REPO_DIR/" in "$PROTECTED_DIR"/*) die "Bu betik $PROTECTED_DIR içinde çalıştırılamaz." ;; esac
cd "$REPO_DIR"

say "1. Kod güncelleniyor ($REPO_DIR)"
[ -z "$(git status --porcelain --untracked-files=no)" ] || die "Yerel değişiklik var (git status). Önce inceleyin."
git fetch --quiet origin main
git checkout --quiet main
git merge --ff-only --quiet origin/main
echo "  sürüm: $(git rev-parse --short HEAD)"

say "2. .env"
rand() { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
if [ ! -f .env ]; then
  umask 077
  cp .env.example .env
  DBPW="$(rand)"
  sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$DBPW/" .env
  sed -i "s#^DATABASE_URL=.*#DATABASE_URL=postgresql://trendhub:$DBPW@db:5432/trendhub#" .env
  sed -i "s/^APP_SECRET=.*/APP_SECRET=$(rand)/" .env
  unset DBPW
  echo "  yeni .env oluşturuldu (rastgele DB parolası ve APP_SECRET; değerler gösterilmez)"
  echo "  Pazaryeri API bilgilerini daha sonra .env'e ekleyip bu betiği tekrar çalıştırabilirsiniz."
else
  echo "  mevcut .env korunuyor (değerler değiştirilmez)"
fi
chmod 600 .env
for kv in CONNECTOR_WRITE_ENABLED=false TRENDYOL_LISTINGS_ENABLED=false HEPSIBURADA_LISTINGS_ENABLED=false \
          COOKIE_SECURE=auto TRENDHUB_HTTP_PORT=8081 TRENDCANTANIZ_HTTP_PORT=8090; do
  grep -qE "^${kv%%=*}=" .env || { printf '\n%s\n' "$kv" >> .env; echo "  eklendi: ${kv%%=*}"; }
done
grep -qE '^CONNECTOR_WRITE_ENABLED=false$' .env || die "CONNECTOR_WRITE_ENABLED=false olmalı"

say "3. Deploy (yedek → migration → sağlık → duman testi)"
TRENDHUB_GIT_PULL=0 ./deploy/deploy.sh

say "4. Yönetici hesabı"
PROJECT="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' trendhub-api)"
if docker compose -p "$PROJECT" exec -T api python -m app.cli has-admin >/dev/null 2>&1; then
  echo "  aktif yönetici mevcut"
elif [ -t 0 ]; then
  read -r -p "  Yönetici kullanıcı adı [admin]: " U
  ./deploy/create-admin.sh "${U:-admin}"
else
  echo "  Yönetici yok. Etkileşimli terminalde çalıştırın: ./deploy/create-admin.sh"
fi
say "Tamam. Adres deploy çıktısının sonunda yazıyor."
