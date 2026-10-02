"""Trendyol → TrendHub webhook uç noktası.

Akış: Webhook → DOĞRULA (kimlik + şema) → KAYDET (platform_events, kişisel veri ayıklanmış, dedupe) → KUYRUK →
WORKER (`events.process`) → ilgili hesaplama. HTTP isteği içinde ağır iş / AI / LLM ÇALIŞTIRILMAZ.

Resmî dokümantasyona göre Trendyol webhook'u yalnızca SİPARİŞ PAKETİ statü değişimlerini gönderir (CREATED, PICKING,
INVOICED, SHIPPED, CANCELLED, DELIVERED, UNDELIVERED, RETURNED, UNSUPPLIED, AWAITING, UNPACKED, AT_COLLECTION_POINT,
VERIFIED). Kimlik doğrulama: BASIC_AUTHENTICATION veya API_KEY ("x-api-key" başlığı). Trendyol imza (HMAC) veya
zaman damgalı nonce göndermez → klasik replay koruması YOK; bunun yerine:
  * dedupe_key = paket id + statü + lastModifiedDate (aynı teslimat ikinci kez işlenmez),
  * işleyici daha yeni bir olay işlenmişse eski (yeniden gönderilmiş) paketi yok sayar.
Hata durumunda Trendyol bir süre yeniden dener ve sonra webhook'u PASİF yapar (e-posta ile bildirir): bu yüzden
geçerli istek hızla 200 döner; işleme hatası kuyrukta tekrar denenir, Trendyol'a hata döndürülmez.
"""
from __future__ import annotations

import base64
import hmac
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..config import get_settings, is_set
from ..db import transaction
from ..deps import client_ip
from ..services.audit import log_audit
from ..services.events import record_event
from ..services.platform import events
from ..services.platform.pii import strip_pii

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])
MAX_BODY = 1_000_000
CREATED_STATUSES = {"CREATED", "AWAITING"}


def _authorized(request: Request) -> tuple[bool, str]:
    s = get_settings()
    key, user, pwd = s.trendyol_webhook_api_key, s.trendyol_webhook_username, s.trendyol_webhook_password
    if not is_set(key) and not (is_set(user) and is_set(pwd)):
        return False, "disabled"
    if is_set(key):
        got = request.headers.get("x-api-key") or ""
        if got and hmac.compare_digest(got.encode(), key.encode()):
            return True, "api_key"
    if is_set(user) and is_set(pwd):
        auth = request.headers.get("authorization") or ""
        if auth.lower().startswith("basic "):
            try:
                u, _, p = base64.b64decode(auth[6:].strip()).decode().partition(":")
            except Exception:  # noqa: BLE001
                return False, "bad_header"
            if hmac.compare_digest(u.encode(), user.encode()) and hmac.compare_digest(p.encode(), pwd.encode()):
                return True, "basic"
    return False, "invalid"


def _packages(body) -> list[dict]:
    if isinstance(body, list):
        return [x for x in body if isinstance(x, dict)]
    if isinstance(body, dict):
        for k in ("content", "shipmentPackages", "packages"):
            if isinstance(body.get(k), list):
                return [x for x in body[k] if isinstance(x, dict)]
        return [body]
    return []


@router.post("/trendyol")
async def trendyol_webhook(request: Request):
    ip = client_ip(request)
    ok, how = _authorized(request)
    if not ok:
        with transaction() as conn:
            # Her başarısız deneme ayrı audit satırı yerine tek sistem olayında sayılır (log taşması olmaz)
            record_event(conn, level="warning", source="webhook:trendyol",
                         message=f"Reddedilen webhook isteği ({how})", details={"ip": ip, "reason": how},
                         fingerprint="webhook:trendyol:auth")
        status = 503 if how == "disabled" else 401
        return JSONResponse({"detail": "Webhook kapalı" if how == "disabled" else "Yetkisiz"}, status_code=status)
    raw = await request.body()
    if len(raw) > MAX_BODY:
        return JSONResponse({"detail": "İstek çok büyük"}, status_code=413)
    try:
        body = json.loads(raw or b"null")
    except ValueError:
        return JSONResponse({"detail": "Geçersiz JSON"}, status_code=400)
    pkgs = _packages(body)
    accepted, duplicates, invalid = 0, 0, 0
    with transaction() as conn:
        for pkg in pkgs:
            pid = pkg.get("shipmentPackageId") or pkg.get("id")
            number = pkg.get("orderNumber")
            status = str(pkg.get("shipmentPackageStatus") or pkg.get("status") or "").upper()
            if not pid or not number or not status:
                invalid += 1
                continue
            lm = pkg.get("lastModifiedDate") or ""
            ev = events.emit(conn, source="webhook", marketplace="trendyol",
                             event_type="ORDER_CREATED" if status in CREATED_STATUSES else "ORDER_UPDATED",
                             dedupe_key=f"tywh:{pid}:{status}:{lm}", entity_type="shipment_package", entity_ref=str(pid),
                             payload={"package": strip_pii(pkg), "status": status})
            if ev:
                accepted += 1
            else:
                duplicates += 1
        log_audit(conn, actor="trendyol-webhook", action="webhook.received", entity_type="webhook", entity_id="trendyol", ip=ip,
                  details={"auth": how, "packages": len(pkgs), "accepted": accepted, "duplicates": duplicates, "invalid": invalid})
        if accepted:
            from ..services import jobs
            from ..services.platform.sync import EVENTS_PROCESS
            jobs.enqueue(conn, EVENTS_PROCESS, payload={}, idempotency_key=EVENTS_PROCESS, max_attempts=3)
    if not pkgs or invalid == len(pkgs):
        return JSONResponse({"detail": "Paket bulunamadı (shipmentPackageId / orderNumber / statü zorunlu)"}, status_code=400)
    return {"accepted": accepted, "duplicates": duplicates, "invalid": invalid}
