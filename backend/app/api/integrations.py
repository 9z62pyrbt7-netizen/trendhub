"""Entegrasyonlar. Credential DEĞERLERİ hiçbir zaman API'den dönmez; yalnızca
hangi ortam değişkeninin tanımlı olup olmadığı gösterilir."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import get_settings, is_set
from typing import Literal

from ..connectors.base import CAP_ORDERS_READ, CAP_PRODUCTS_READ, MarketplaceConnector
from ..connectors.registry import CONNECTOR_CLASSES, all_connectors, get_connector
from ..db import get_conn, get_engine, row, rows
from ..deps import CurrentUser, client_ip, operator, viewer
from ..services import jobs, sync_service
from ..services.audit import log_audit

router = APIRouter(prefix="/api/integrations", tags=["integrations"])

STATE_LABELS = {
    "not_connected": "Bağlı değil",
    "not_implemented": "Bilgiler tanımlı · senkronizasyon henüz uygulanmadı",
    "configured": "Yapılandırıldı · bağlantı test edilmedi",
    "connected": "Bağlı",
    "error": "Hata",
}


def integration_state(c: MarketplaceConnector, mp: dict | None) -> dict:
    mp = mp or {}
    values = c.credential_values()
    if not c.is_configured():
        state = "not_connected"
    elif not c.supports(CAP_ORDERS_READ):
        state = "not_implemented"
    elif mp.get("last_check_ok") is False:
        state = "error"
    elif mp.get("last_check_ok") is True:
        state = "connected"
    else:
        state = "configured"
    return {
        "code": c.code, "name": c.name, "state": state, "state_label": STATE_LABELS[state],
        "capabilities": sorted(c.capabilities),
        "credentials": [{"env": f.env, "label": f.label, "is_set": is_set(values.get(f.env))}
                        for f in c.credential_fields],
        "implementation_note": c.implementation_note,
        "last_check_at": mp.get("last_check_at"), "last_check_ok": mp.get("last_check_ok"),
        "last_check_message": mp.get("last_check_message"), "last_sync_at": mp.get("last_sync_at"),
        "write_enabled": bool(get_settings().connector_write_enabled),
        "optional_settings": c.optional_settings(),
        "source": None,
        "publish": c.publish_status(),
    }


def _connector_or_404(code: str) -> MarketplaceConnector:
    if code not in CONNECTOR_CLASSES:
        raise HTTPException(404, "Entegrasyon bulunamadı")
    return get_connector(code)


@router.get("")
def list_integrations(_: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    mps = {r["code"]: r for r in rows(conn, "SELECT * FROM marketplaces")}
    out = []
    for c in all_connectors():
        item = integration_state(c, mps.get(c.code))
        from ..services.marketplace_credentials import source as _source
        item["source"] = _source(conn, c.code)
        item["recent_jobs"] = rows(conn, """
            SELECT id, job_type, status, attempts, message, created_at, started_at, finished_at, result
              FROM sync_jobs WHERE marketplace = :m ORDER BY id DESC LIMIT 5
        """, m=c.code)
        # TrendHub'daki gerçek kayıt sayıları (pazaryerinden okunmuş veri; üretilmiş veri yok)
        item["counts"] = row(conn, """
            SELECT (SELECT COUNT(*) FROM orders o JOIN stores s ON s.id = o.store_id WHERE s.marketplace_id = m.id) AS orders,
                   (SELECT COUNT(*) FROM marketplace_listings l JOIN stores s ON s.id = l.store_id
                     WHERE s.marketplace_id = m.id) AS listings,
                   (SELECT COUNT(*) FROM listing_drafts d WHERE d.marketplace_id = m.id AND d.status = 'ready') AS ready_drafts
              FROM marketplaces m WHERE m.code = :m
        """, m=c.code) or {"orders": 0, "listings": 0, "ready_drafts": 0}
        out.append(item)
    return {"items": out, "sync_interval_minutes": get_settings().sync_interval_minutes,
            "job_labels": sync_service.JOB_LABELS_TR}


@router.post("/{code}/check")
def check(code: str, request: Request, user: CurrentUser = Depends(operator)):
    c = _connector_or_404(code)
    result = sync_service.run_integration_check(get_engine(), c.code)
    with get_engine().begin() as conn:
        log_audit(conn, actor=user.username, user_id=user.id, action="integration.checked",
                  entity_type="integration", entity_id=code, ip=client_ip(request), details=result)
    return result


class SyncIn(BaseModel):
    kind: Literal["orders", "listings"] = "orders"
    lookback_days: int | None = Field(None, ge=1, le=sync_service.MAX_LOOKBACK_DAYS)


@router.post("/{code}/sync", status_code=202)
def trigger_sync(code: str, request: Request, body: SyncIn | None = None, user: CurrentUser = Depends(operator),
                 conn: Connection = Depends(get_conn)):
    c = _connector_or_404(code)
    body = body or SyncIn()
    capability, job_type, what = {
        "orders": (CAP_ORDERS_READ, sync_service.ORDERS_SYNC, "sipariş senkronizasyonu"),
        "listings": (CAP_PRODUCTS_READ, sync_service.LISTINGS_SYNC, "ürün/ilan okuma"),
    }[body.kind]
    if not c.is_configured():
        raise HTTPException(409, f"{c.name} bağlı değil: eksik " + ", ".join(c.missing_credentials()))
    if not c.supports(capability):
        raise HTTPException(409, f"{c.name}: {what} henüz uygulanmadı")
    payload = {"requested_by": user.username}
    if body.lookback_days and body.kind == "orders":
        payload["lookback_days"] = body.lookback_days
    key = f"{job_type}:{c.code}"
    job_id = jobs.enqueue(conn, job_type, marketplace=c.code, payload=payload, idempotency_key=key,
                          max_attempts=get_settings().job_max_attempts)
    if job_id is None:
        existing = row(conn, "SELECT id FROM sync_jobs WHERE idempotency_key = :k AND status IN ('queued','running')", k=key)
        return {"queued": False, "job_id": existing and existing["id"], "message": "Zaten kuyrukta bekleyen bir iş var"}
    log_audit(conn, actor=user.username, user_id=user.id, action="integration.sync_requested",
              entity_type="integration", entity_id=code, ip=client_ip(request), details=body.model_dump())
    return {"queued": True, "job_id": job_id, "message": "İş kuyruğa alındı; worker birkaç saniye içinde işler"}


# ------------------------------------------------------------- web'den bağlantı yönetimi
from ..deps import admin as _admin  # noqa: E402
from ..services import marketplace_credentials as creds  # noqa: E402


class ConnectionIn(BaseModel):
    # alan adı -> değer. Secret alanlar boş/eksik gönderilirse kayıtlı değer korunur.
    values: dict[str, str | None] = Field(default_factory=dict)


def _friendly(name: str, ok: bool, message: str) -> str:
    if ok:
        return f"{name} bağlantısı doğrulandı (salt okunur test)."
    return f"{name} bağlantısı doğrulanamadı. {message}"


def _run_test(code: str, values: dict[str, str]) -> tuple[bool, str]:
    cls = CONNECTOR_CLASSES[code]
    connector = cls(creds.settings_with(values, creds.effective_settings()))
    try:
        check = connector.test_connection()   # yalnızca GET: pazaryerinde hiçbir şey değişmez
    except Exception as exc:  # noqa: BLE001 - beklenmeyen hata kullanıcıya teknik ayrıntısız döner
        return False, _friendly(connector.name, False, f"Beklenmeyen hata ({exc.__class__.__name__}).")
    return check.ok, _friendly(connector.name, check.ok, check.message)


@router.get("/{code}/connection")
def get_connection(code: str, _: CurrentUser = Depends(_admin), conn: Connection = Depends(get_conn)):
    _connector_or_404(code)
    return creds.public_view(conn, code)


@router.post("/{code}/connection/test")
def test_connection_values(code: str, body: ConnectionIn, request: Request, user: CurrentUser = Depends(_admin)):
    """Formdaki (henüz kaydedilmemiş) bilgilerle salt okunur bağlantı testi. Hiçbir şey kaydedilmez."""
    _connector_or_404(code)
    with get_engine().begin() as conn:
        try:
            values = creds.merge(conn, code, body.values)
        except creds.CredentialError as exc:
            raise HTTPException(422, str(exc)) from None
    ok, message = _run_test(code, values)
    with get_engine().begin() as conn:
        log_audit(conn, actor=user.username, user_id=user.id, action="integration.connection_tested",
                  entity_type="integration", entity_id=code, ip=client_ip(request), details={"ok": ok})
    return {"ok": ok, "message": message}


@router.put("/{code}/connection")
def save_connection(code: str, body: ConnectionIn, request: Request, user: CurrentUser = Depends(_admin)):
    """Bağlantı bilgilerini şifreli kaydeder ve hemen salt okunur test yapar."""
    c = _connector_or_404(code)
    with get_engine().begin() as conn:
        try:
            values = creds.merge(conn, code, body.values)
            creds.save(conn, code, values, user.id)
        except creds.CredentialError as exc:
            raise HTTPException(422, str(exc)) from None
        except Exception as exc:  # noqa: BLE001 - ör. APP_SECRET yok
            from ..suppliers.secrets import SecretStoreError
            if isinstance(exc, SecretStoreError):
                raise HTTPException(400, str(exc)) from None
            raise
        changed = sorted(k for k, v in (body.values or {}).items() if (v or "").strip())
        log_audit(conn, actor=user.username, user_id=user.id, action="integration.connection_saved",
                  entity_type="integration", entity_id=code, ip=client_ip(request),
                  details={"message": f"{c.name} bağlantı bilgileri güncellendi", "fields_changed": changed})
    ok, message = _run_test(code, values)
    with get_engine().begin() as conn:
        creds.record_test(conn, code, ok, message)
    return {"ok": ok, "message": message, "saved": True}


@router.delete("/{code}/connection")
def remove_connection(code: str, request: Request, user: CurrentUser = Depends(_admin),
                      conn: Connection = Depends(get_conn)):
    """Bağlantıyı kaldırır (secret'lar silinir). Sipariş, ilan ve finans verisi korunur."""
    c = _connector_or_404(code)
    creds.remove(conn, code, user.id)
    log_audit(conn, actor=user.username, user_id=user.id, action="integration.connection_removed",
              entity_type="integration", entity_id=code, ip=client_ip(request),
              details={"message": f"{c.name} bağlantısı kaldırıldı"})
    return {"ok": True}


class WritePermissionIn(BaseModel):
    enabled: bool


@router.put("/{code}/write-permission")
def set_write_permission(code: str, body: WritePermissionIn, request: Request, user: CurrentUser = Depends(_admin),
                         conn: Connection = Depends(get_conn)):
    """Mağaza yayın izni. Yayın API'si doğrulanmamış pazaryerinde AÇILAMAZ (409)."""
    from ..services import publishing
    c = _connector_or_404(code)
    try:
        publishing.set_store_write(conn, code, body.enabled)
    except publishing.PublishError as exc:
        raise HTTPException(409, str(exc)) from None
    log_audit(conn, actor=user.username, user_id=user.id, action="integration.write_permission",
              entity_type="integration", entity_id=code, ip=client_ip(request),
              details={"message": f"{c.name} yayın izni {'açıldı' if body.enabled else 'kapatıldı'}"})
    return {"ok": True, "enabled": body.enabled}
