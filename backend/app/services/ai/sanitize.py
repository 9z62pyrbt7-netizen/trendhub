"""Ajan girdi/çıktı temizliği.

  * Dış içerik (ürün adı/açıklaması, müşteri sorusu, iade sebebi, web içeriği) VERİDİR, talimat değildir. Sistem talimatı
    gibi görünen kalıplar işaretlenir; ajan planı kullanıcı metninden değil deterministik niyet kurallarından çıkar.
  * Secret değerleri ve secret benzeri anahtarlar hiçbir araç sonucuna, mesaja veya kayda yazılmaz.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata

_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁦-⁩]")
_INJECTION = re.compile(
    r"(ignore (all |the )?(previous|above|prior) (instructions|rules)|disregard (the )?(system|previous)|you are now|"
    r"system prompt|</?(system|assistant|instructions?)>|önceki talimatlar[ıi]? (yok say|unut|görmezden gel)|"
    r"sistem talimat|yeni talimat|onayla(?:yın|)\s+ve\s+uygula|developer mode|jailbreak)", re.IGNORECASE)
SECRET_KEYS = re.compile(r"(api[_-]?key|secret|token|password|passwd|parola|authorization|cookie|credential)", re.IGNORECASE)
MASK = "***"


def clean_text(value, max_len: int = 2000) -> str:
    s = unicodedata.normalize("NFC", str(value or ""))
    s = _CTRL.sub("", s)
    s = re.sub(r"[ \t]+", " ", s).strip()
    return s[:max_len]


def injection_flags(value) -> list[str]:
    return sorted({m.group(0).lower() for m in _INJECTION.finditer(str(value or ""))})


def untrusted(value, max_len: int = 500) -> dict:
    """Dış kaynaklı metin: temizlenmiş içerik + talimat benzeri kalıp bayrağı. Asla talimat olarak yorumlanmaz."""
    s = clean_text(value, max_len)
    return {"text": s, "untrusted": True, "injection_suspected": bool(injection_flags(s))}


def _secrets() -> list[str]:
    try:
        from ...logging_setup import _RUNTIME_SECRETS, secret_values
        return sorted(set(secret_values()) | set(_RUNTIME_SECRETS), key=len, reverse=True)
    except Exception:  # noqa: BLE001 - ayar yüklenemezse maskeleme yine anahtar adına göre yapılır
        return []


def redact(obj, _secrets_cache: list[str] | None = None):
    """Sonuç/argüman içinde secret değerlerini ve secret benzeri anahtarları maskeler (özyinelemeli)."""
    secrets = _secrets() if _secrets_cache is None else _secrets_cache
    if isinstance(obj, dict):
        return {k: (MASK if isinstance(k, str) and SECRET_KEYS.search(k) and v not in (None, "", False) else redact(v, secrets))
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(v, secrets) for v in obj]
    if isinstance(obj, str):
        for s in secrets:
            if s in obj:
                obj = obj.replace(s, MASK)
        return obj
    return obj


def to_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str, sort_keys=True)


def bounded(obj, max_bytes: int = 60_000):
    """Kayıt için sınırlı boyutlu sonuç. Büyük listeler kırpılır; kırpıldığı açıkça işaretlenir."""
    raw = to_json(obj)
    if len(raw.encode()) <= max_bytes:
        return json.loads(raw)
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(v, list) and len(v) > 20:
                out[k] = v[:20]
                out[f"{k}__truncated"] = len(v)
            else:
                out[k] = v
        raw = to_json(out)
        if len(raw.encode()) <= max_bytes:
            return json.loads(raw)
    return {"__truncated__": True, "bytes": len(raw.encode()), "preview": raw[:2000]}


def digest(obj) -> str:
    return hashlib.sha256(to_json(obj).encode()).hexdigest()
