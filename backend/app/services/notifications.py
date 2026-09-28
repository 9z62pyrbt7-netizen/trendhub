"""Bildirim kanalları.

Şimdilik yalnızca panel içi bildirim (🔔 zil) vardır: uyarının kendisi `alerts` tablosunda durur, zil
açık uyarı sayısını gösterir. E-posta / Telegram / mobil push gibi kanallar ileride `Channel`
alt sınıfı olarak eklenir ve `CHANNELS` listesine kaydedilir; üçüncü taraf bağımlılığı YOKTUR.
Kanallar uyarının açıklamasını gönderir, secret/credential içermez.
"""
from __future__ import annotations

import logging

from sqlalchemy.engine import Connection

from . import app_settings

log = logging.getLogger("trendhub.notifications")


class Channel:
    code = "base"
    setting_key: str | None = None

    def enabled(self, conn: Connection) -> bool:
        return bool(app_settings.get(conn, self.setting_key, False)) if self.setting_key else False

    def send(self, conn: Connection, alert_id: int, alert: dict) -> None:  # pragma: no cover - arayüz
        raise NotImplementedError


class InAppChannel(Channel):
    """Panel içi: uyarı zaten tabloda; zil /api/alerts/summary'den okur. Ek iş yok."""
    code = "in_app"
    setting_key = "notifications.in_app"

    def send(self, conn: Connection, alert_id: int, alert: dict) -> None:
        return None


CHANNELS: list[Channel] = [InAppChannel()]


def dispatch(conn: Connection, alert_id: int, alert: dict) -> None:
    """Yeni açılan uyarıyı etkin kanallara iletir. Kanal hatası uyarı kaydını ENGELLEMEZ."""
    for ch in CHANNELS:
        try:
            if ch.enabled(conn):
                ch.send(conn, alert_id, alert)
        except Exception:  # noqa: BLE001
            log.exception("Bildirim kanalı hatası: %s", ch.code)


def channels_status(conn: Connection) -> list[dict]:
    return [{"code": c.code, "enabled": c.enabled(conn)} for c in CHANNELS]
