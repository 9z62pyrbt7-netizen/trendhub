"""Pazaryeri bağlantı bilgileri: web panelinden yönetilir, secret'lar ŞİFRELİ saklanır.

* Gizli OLMAYAN alanlar (satıcı ID, kullanıcı adı, pazaryeri kimliği) `settings` JSONB'de,
  gizli alanlar (API secret, şifre, refresh token) tek bir Fernet paketinde (`secrets_enc`) tutulur.
  Anahtar APP_SECRET'tan türetilir (tedarikçi secret'larıyla aynı altyapı: suppliers/secrets.py).
* API yanıtları secret DEĞERİ döndürmez; yalnızca "tanımlı mı" bilgisi döner.
* Kaynak önceliği: panelde bir bağlantı kaydı VARSA o esastır (kaldırıldıysa "Bağlı değil");
  yoksa eski kurulumlar için sunucu ortam değişkenleri (.env) kullanılır.
* Çözülen secret'lar log maskeleyicisine eklenir (suppliers.secrets.decrypt).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import Settings, get_settings, is_set
from ..suppliers.secrets import decrypt, encrypt

log = logging.getLogger("trendhub.credentials")


@dataclass(frozen=True)
class FieldSpec:
    key: str            # Settings alan adı
    label: str
    secret: bool = False
    required: bool = True
    help: str | None = None
    default: str | None = None
    advanced: bool = False


# Yalnızca connector'ların gerçekten kullandığı alanlar (uydurma alan yok).
FIELD_SPECS: dict[str, list[FieldSpec]] = {
    "trendyol": [
        FieldSpec("trendyol_seller_id", "Satıcı ID (Supplier ID)", help="Trendyol satıcı panelinde Hesap Bilgileri'nde yazar."),
        FieldSpec("trendyol_api_key", "API Key", secret=True),
        FieldSpec("trendyol_api_secret", "API Secret", secret=True),
    ],
    "hepsiburada": [
        FieldSpec("hepsiburada_merchant_id", "Merchant ID"),
        FieldSpec("hepsiburada_username", "Entegrasyon kullanıcı adı"),
        FieldSpec("hepsiburada_password", "Entegrasyon şifresi", secret=True),
        FieldSpec("hepsiburada_user_agent", "User-Agent", required=False, advanced=True,
                  help="Boş bırakılırsa entegrasyon kullanıcı adı gönderilir."),
    ],
    "amazon_tr": [
        FieldSpec("amazon_sp_seller_id", "Seller ID"),
        FieldSpec("amazon_sp_client_id", "LWA Client ID"),
        FieldSpec("amazon_sp_client_secret", "LWA Client Secret", secret=True),
        FieldSpec("amazon_sp_refresh_token", "Refresh Token", secret=True),
        FieldSpec("amazon_sp_marketplace_id", "Marketplace ID", required=False, default="A33AVAJ2PDY3EV",
                  help="Amazon.com.tr için A33AVAJ2PDY3EV."),
        FieldSpec("amazon_sp_endpoint", "SP-API bölge adresi", required=False, advanced=True,
                  default="https://sellingpartnerapi-eu.amazon.com", help="Türkiye için Avrupa (EU) bölgesi."),
    ],
}


class CredentialError(Exception):
    """Kullanıcıya gösterilebilir Türkçe hata."""


def specs(code: str) -> list[FieldSpec]:
    if code not in FIELD_SPECS:
        raise CredentialError("Bu pazaryeri için bağlantı formu yok")
    return FIELD_SPECS[code]


def _row(conn: Connection, code: str) -> dict | None:
    r = conn.execute(text("""SELECT c.*, m.code FROM marketplace_connections c JOIN marketplaces m ON m.id = c.marketplace_id
                             WHERE m.code = :c"""), {"c": code}).mappings().first()
    return dict(r) if r else None


def _values_from_row(r: dict) -> dict[str, str]:
    """DB kaydındaki tüm değerler (secret'lar çözülmüş). YALNIZCA backend içinde kullanılır."""
    vals = dict(r.get("settings") or {})
    if r.get("secrets_enc"):
        vals.update(json.loads(decrypt(r["secrets_enc"]) or "{}"))
    return vals


def stored_values(conn: Connection, code: str) -> dict[str, str] | None:
    r = _row(conn, code)
    if r is None:
        return None
    if r["status"] == "removed" or not r["enabled"]:
        return {s.key: "" for s in specs(code)}
    return _values_from_row(r)


def overlay(settings: Settings, conn: Connection) -> Settings:
    """Panel bağlantılarını ortam değişkenlerinin üzerine uygular (panel kaydı varsa o esastır)."""
    updates: dict[str, str] = {}
    rows = conn.execute(text("""SELECT c.*, m.code FROM marketplace_connections c
                                  JOIN marketplaces m ON m.id = c.marketplace_id""")).mappings().all()
    for r in rows:
        r = dict(r)
        if r["code"] not in FIELD_SPECS:
            continue
        removed = r["status"] == "removed" or not r["enabled"]
        vals = {} if removed else _values_from_row(r)
        for s in FIELD_SPECS[r["code"]]:
            v = vals.get(s.key)
            if removed:
                updates[s.key] = s.default or ""
            elif is_set(v):
                updates[s.key] = v
            elif s.default is not None:
                updates[s.key] = s.default
            else:
                updates[s.key] = ""
    return settings.model_copy(update=updates) if updates else settings


def effective_settings(base: Settings | None = None) -> Settings:
    """Worker ve API'nin kullandığı ayarlar: .env + panelden girilen bağlantılar."""
    base = base or get_settings()
    try:
        from ..db import get_engine
        with get_engine().connect() as conn:
            return overlay(base, conn)
    except Exception:  # noqa: BLE001 - DB yoksa (ör. birim test) ortam ayarlarıyla devam
        log.warning("Panel bağlantı bilgileri okunamadı; yalnızca sunucu ayarları kullanılıyor")
        return base


def source(conn: Connection, code: str) -> str:
    r = _row(conn, code)
    if r is not None:
        return "removed" if r["status"] == "removed" else "panel"
    s = get_settings()
    return "server" if any(is_set(getattr(s, f.key, "")) for f in FIELD_SPECS.get(code, []) if f.required) else "none"


def public_view(conn: Connection, code: str) -> dict:
    """Arayüz için: gizli olmayan değerler + secret'ların yalnızca "tanımlı mı" bilgisi."""
    r = _row(conn, code)
    src = source(conn, code)
    if r is not None and src == "panel":
        stored = dict(r.get("settings") or {})
        has_secret = set(json.loads(decrypt(r["secrets_enc"]) or "{}")) if r.get("secrets_enc") else set()
    elif src == "server":
        s = get_settings()
        stored = {f.key: getattr(s, f.key, "") for f in specs(code) if not f.secret}
        has_secret = {f.key for f in specs(code) if f.secret and is_set(getattr(s, f.key, ""))}
    else:
        stored, has_secret = {}, set()
    fields = []
    for f in specs(code):
        item = {"key": f.key, "label": f.label, "secret": f.secret, "required": f.required, "help": f.help,
                "advanced": f.advanced, "default": f.default}
        if f.secret:
            item["is_set"] = f.key in has_secret
        else:
            item["value"] = stored.get(f.key) or ""
            item["is_set"] = is_set(stored.get(f.key))
        fields.append(item)
    return {"code": code, "source": src, "fields": fields,
            "status": (r or {}).get("status") or ("server" if src == "server" else "not_connected"),
            "last_test_at": (r or {}).get("last_test_at"), "last_test_ok": (r or {}).get("last_test_ok"),
            "last_test_message": (r or {}).get("last_test_message"),
            "write_enabled": bool((r or {}).get("write_enabled"))}


def merge(conn: Connection, code: str, incoming: dict[str, str | None]) -> dict[str, str]:
    """Kayıtlı değerler + formdan gelenler. Secret alan boş/None gelirse KAYITLI değer korunur."""
    base = stored_values(conn, code)
    if base is None:  # panelde kayıt yok: sunucu ayarındaki değerlerden başla (taşıma kolaylığı)
        s = get_settings()
        base = {f.key: getattr(s, f.key, "") or "" for f in specs(code)}
    out = dict(base)
    keys = {f.key: f for f in specs(code)}
    for k, v in (incoming or {}).items():
        if k not in keys:
            raise CredentialError(f"Bilinmeyen alan: {k}")
        v = (v or "").strip()
        if keys[k].secret and not v:
            continue
        out[k] = v
    for f in specs(code):
        if not is_set(out.get(f.key)) and f.default:
            out[f.key] = f.default
    missing = [f.label for f in specs(code) if f.required and not is_set(out.get(f.key))]
    if missing:
        raise CredentialError("Eksik bilgi: " + ", ".join(missing))
    return out


def settings_with(values: dict[str, str], base: Settings | None = None) -> Settings:
    return (base or get_settings()).model_copy(update=values)


def save(conn: Connection, code: str, values: dict[str, str], user_id: int | None) -> None:
    public = {f.key: values.get(f.key, "") for f in specs(code) if not f.secret}
    secrets = {f.key: values[f.key] for f in specs(code) if f.secret and is_set(values.get(f.key))}
    enc = encrypt(json.dumps(secrets)) if secrets else None
    conn.execute(text("""
        INSERT INTO marketplace_connections(marketplace_id, settings, secrets_enc, enabled, status, created_by,
               updated_by, updated_at)
        SELECT m.id, CAST(:s AS JSONB), :e, TRUE, 'untested', :u, :u, NOW() FROM marketplaces m WHERE m.code = :c
        ON CONFLICT (marketplace_id) DO UPDATE SET settings = EXCLUDED.settings, secrets_enc = EXCLUDED.secrets_enc,
               enabled = TRUE, status = 'untested', last_test_ok = NULL, last_test_message = NULL,
               updated_by = EXCLUDED.updated_by, updated_at = NOW()
    """), {"s": json.dumps(public), "e": enc, "u": user_id, "c": code})


def record_test(conn: Connection, code: str, ok: bool, message: str) -> None:
    conn.execute(text("""
        UPDATE marketplace_connections c SET last_test_at = NOW(), last_test_ok = :ok, last_test_message = :m,
               status = CASE WHEN :ok THEN 'connected' ELSE 'failed' END, updated_at = NOW()
          FROM marketplaces m WHERE m.id = c.marketplace_id AND m.code = :c AND c.status <> 'removed'
    """), {"ok": ok, "m": message[:500], "c": code})
    conn.execute(text("""UPDATE marketplaces SET last_check_at = NOW(), last_check_ok = :ok, last_check_message = :m,
                         updated_at = NOW() WHERE code = :c"""), {"ok": ok, "m": message[:500], "c": code})


def remove(conn: Connection, code: str, user_id: int | None) -> bool:
    """Bağlantıyı kaldırır: secret'lar silinir, kayıt 'removed' işaretlenir. Sipariş/ilan verisi KORUNUR."""
    n = conn.execute(text("""
        INSERT INTO marketplace_connections(marketplace_id, settings, secrets_enc, enabled, status, created_by, updated_by)
        SELECT m.id, '{}'::jsonb, NULL, FALSE, 'removed', :u, :u FROM marketplaces m WHERE m.code = :c
        ON CONFLICT (marketplace_id) DO UPDATE SET settings = '{}'::jsonb, secrets_enc = NULL, enabled = FALSE,
               status = 'removed', write_enabled = FALSE, updated_by = EXCLUDED.updated_by, updated_at = NOW()
    """), {"u": user_id, "c": code}).rowcount
    conn.execute(text("""UPDATE marketplaces SET last_check_ok = NULL, last_check_message = 'Bağlantı kaldırıldı',
                         updated_at = NOW() WHERE code = :c"""), {"c": code})
    return bool(n)
