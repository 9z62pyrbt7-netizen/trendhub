"""deploy.sh regresyonu: web container'ı eski (silinmiş) bind mount klasörünü görüyorsa reload YAPILMAZ, önce yeniden oluşturulur.

Kök neden (Docker doğrulamasında kanıtlandı): ./web klasörü silinip yeniden oluşturulduğunda çalışan nginx container'ı
eski inode'u (boş klasör) görmeye devam eder; `nginx -t` boş conf.d'yi geçerli sayar ve `nginx -s reload` tüm
dinleyicileri kapatır (health 000). Test, deploy.sh içindeki gerçek `ensure_web_config` fonksiyonunu sahte compose
komutuyla çalıştırır.
"""
import os
import re
import subprocess

ROOT = os.path.join(os.path.dirname(__file__), "..", "..")


def _run(tmp_path, container_sees: str, after_recreate: str):
    src = open(os.path.join(ROOT, "deploy", "deploy.sh"), encoding="utf-8").read()
    fn = re.search(r"^ensure_web_config\(\) \{.*?^\}", src, re.S | re.M).group(0)
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "nginx.conf").write_text("server { listen 80; }\n")
    log = tmp_path / "calls.log"
    state = tmp_path / "state"
    state.write_text(container_sees)
    script = f"""
set -Eeuo pipefail
cd "{tmp_path}"
warn() {{ echo "WARN $*" >> "{log}"; }}
die() {{ echo "DIE $*" >> "{log}"; exit 1; }}
fake_dc() {{
  echo "DC $*" >> "{log}"
  if [ "$1" = exec ]; then
    case "$(cat "{state}")" in
      same) sha256sum web/nginx.conf ;;
      empty) true ;;
      other) echo "deadbeef  /etc/nginx/conf.d/nginx.conf" ;;
    esac
  elif [ "$1" = up ]; then
    echo "{after_recreate}" > "{state}"
  fi
}}
DC=(fake_dc)
{fn}
ensure_web_config
echo OK >> "{log}"
"""
    p = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    return p.returncode, log.read_text() if log.exists() else ""


def test_fresh_mount_no_recreate(tmp_path):
    code, log = _run(tmp_path, "same", "same")
    assert code == 0 and "--force-recreate" not in log and "OK" in log


def test_stale_empty_mount_forces_recreate_before_reload(tmp_path):
    code, log = _run(tmp_path, "empty", "same")
    assert code == 0 and "up -d --no-deps --force-recreate web" in log and "OK" in log


def test_still_broken_after_recreate_stops_without_reload(tmp_path):
    code, log = _run(tmp_path, "other", "other")
    assert code != 0 and "DIE" in log and "OK" not in log


def test_smoke_job_whitelist_matches_internal_jobs_only():
    """Regresyon: duman testi iç işleri (events.process dahil) kabul etmeli, dışarıya istek atan senkron işlerini ASLA."""
    from app.services import sync_service
    from app.services.platform import sync as platform_sync
    smoke = open(os.path.join(ROOT, "scripts", "smoke_test.sh"), encoding="utf-8").read()
    allowed = set(re.findall(r"""-e '"job_type":"([^"]+)"'""", smoke))
    internal = {sync_service.ALERTS_SCAN, sync_service.STOREFRONT_MAINTENANCE, sync_service.STOREFRONT_NOTIFY,
                sync_service.AI_CYCLE, platform_sync.EVENTS_PROCESS, sync_service.AI_DAILY_REVIEW,
                sync_service.AI_WEEKLY_REVIEW}
    external = {sync_service.ORDERS_SYNC, sync_service.ORDERS_DEEP_SYNC, sync_service.LISTINGS_SYNC,
                sync_service.INTEGRATION_CHECK, sync_service.SUPPLIER_SYNC, "listing.publish", *platform_sync.RESOURCE}
    assert allowed == internal
    assert not (allowed & external)
