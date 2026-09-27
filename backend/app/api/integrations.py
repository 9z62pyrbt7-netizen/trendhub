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
        item["recent_jobs"] = rows(conn, """
            SELECT id, job_type, status, attempts, message, created_at, started_at, finished_at, result
              FROM sync_jobs WHERE marketplace = :m ORDER BY id DESC LIMIT 5
        """, m=c.code)
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
    job_id = jobs.enqueue(conn, job_type, marketplace=c.code, payload=payload, idempotency_key=key)
    if job_id is None:
        existing = row(conn, "SELECT id FROM sync_jobs WHERE idempotency_key = :k AND status IN ('queued','running')", k=key)
        return {"queued": False, "job_id": existing and existing["id"], "message": "Zaten kuyrukta bekleyen bir iş var"}
    log_audit(conn, actor=user.username, user_id=user.id, action="integration.sync_requested",
              entity_type="integration", entity_id=code, ip=client_ip(request), details=body.model_dump())
    return {"queued": True, "job_id": job_id, "message": "İş kuyruğa alındı; worker birkaç saniye içinde işler"}
