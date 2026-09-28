#!/usr/bin/env bash
# İlk (veya ek) yönetici hesabını güvenli şekilde oluşturur.
#
#   ./deploy/create-admin.sh [kullanici_adi]
#
# Parola etkileşimli olarak (ekranda görünmeden, iki kez) sorulur:
#   * komut satırına / shell geçmişine yazılmaz,
#   * .env veya başka bir dosyaya kaydedilmez,
#   * loglanmaz (işlem audit log'a yalnızca "user.created" olarak düşer).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
USERNAME="${1:-admin}"
PROJECT="${COMPOSE_PROJECT_NAME:-$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' trendhub-api 2>/dev/null || basename "$PWD")}"
exec docker compose -p "$PROJECT" exec api python -m app.cli create-user --username "$USERNAME" --role admin
