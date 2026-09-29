"""Tedarikçi yönetimi: tedarikçiler, bağlantılar, alan eşleştirme, senkron, ürün havuzu, teklif karşılaştırma.

Güvenlik:
  * Bağlantı URL'si ve secret'lar yalnızca YAZILIR; yanıtlarda maskeli URL ve
    "tanımlı mı" bilgisi döner, çözülmüş değer asla dönmez.
  * Bağlantı ve credential değişikliği yalnızca yönetici (admin) rolündedir.
  * Denetim kaydına secret/URL yazılmaz.
"""
import json
import re
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from ..config import get_settings, is_set
from ..connectors.base import ConnectorError
from ..db import get_conn, get_engine, row, rows
from ..deps import CurrentUser, admin, client_ip, operator, viewer
from ..domain.suppliers import STRATEGIES, STRATEGY_LABELS, effective_cost, select_offer
from ..services import app_settings, jobs
from ..services.audit import log_audit
from ..services.supplier_catalog import load_offers, refresh_catalog
from ..services.supplier_sync import SupplierSyncError, fetch_records, load_config, run_supplier_sync
from ..suppliers.connectors import CONNECTORS, get_supplier_connector
from ..suppliers.fetch import MAX_BYTES
from ..suppliers.fields import FIELD_NAMES, PRESETS, TARGET_FIELDS, suggest_mapping
from ..suppliers.mapping import apply_mapping
from ..suppliers.parsing import field_paths
from ..suppliers.secrets import ENV_NAME_RE, SecretStoreError, encrypt, mask_url
from .common import Page, not_found, paged

router = APIRouter(tags=["suppliers"])
SUPPLIER_SYNC = "supplier.sync"

INTEGRATION_TYPES = {"xml": "XML", "api": "API (JSON)", "csv": "CSV", "manual": "Manuel / dosya yükleme"}
AUTH_TYPES = {"none": "Yok", "basic": "Kullanıcı adı + şifre (Basic)", "bearer": "Bearer token",
              "header": "Özel başlık (API anahtarı)", "query": "URL parametresi (API anahtarı)"}
STATUS_TR = {"active": "Aktif", "missing": "Kaynağında bulunamadı", "inactive": "Pasif"}


# ------------------------------------------------------------------ modeller
class StockRules(BaseModel):
    buffer: int = Field(0, ge=0, le=100000)
    min_stock: int = Field(0, ge=0, le=100000)
    max_stock: int | None = Field(None, ge=0, le=1000000)


class ConnectionIn(BaseModel):
    integration_type: str = Field("manual", pattern=r"^(xml|api|csv|manual)$")
    # None = değiştirme, "" = temizle
    source_url: str | None = Field(None, max_length=2000)
    auth_type: str = Field("none", pattern=r"^(none|basic|bearer|header|query)$")
    auth_username: str | None = Field(None, max_length=200)
    auth_param_name: str | None = Field(None, max_length=100, pattern=r"^[A-Za-z0-9_\-.]*$")
    secret: str | None = Field(None, max_length=4000)
    secret_env: str | None = Field(None, max_length=80)
    record_path: str | None = Field(None, max_length=300)
    delimiter: str | None = Field(None, max_length=1)
    encoding: str | None = Field(None, pattern=r"^(|utf-8|utf-8-sig|cp1254|iso-8859-9)$")
    allow_mass_missing: bool = False
    # JSON API sayfalama (boşsa tek istek)
    page_param: str | None = Field(None, max_length=50, pattern=r"^[A-Za-z0-9_\-.]*$")
    page_size_param: str | None = Field(None, max_length=50, pattern=r"^[A-Za-z0-9_\-.]*$")
    page_size: int | None = Field(None, ge=1, le=5000)
    start_page: int | None = Field(None, ge=0, le=100000)
    max_pages: int | None = Field(None, ge=1, le=500)

    @field_validator("secret_env")
    @classmethod
    def _env(cls, v):
        if v and not ENV_NAME_RE.match(v):
            raise ValueError("Ortam değişkeni adı SUPPLIER_ ile başlamalı (ör. SUPPLIER_ACME_TOKEN)")
        return v or None


class SupplierIn(BaseModel):
    code: str | None = Field(None, max_length=50, pattern=r"^[a-z0-9_\-]*$")
    name: str = Field(min_length=1, max_length=200)
    contact_name: str | None = Field(None, max_length=200)
    phone: str | None = Field(None, max_length=50)
    email: str | None = Field(None, max_length=200)
    lead_time_days: int | None = Field(None, ge=0, le=365)
    notes: str | None = Field(None, max_length=2000)
    is_active: bool = True
    priority: int = Field(100, ge=1, le=10000)
    sync_interval_minutes: int = Field(0, ge=0, le=7 * 24 * 60)
    stock_rules: StockRules = StockRules()
    # Geriye uyumluluk: eski istemciler integration_type'ı burada gönderebilir.
    integration_type: str | None = Field(None, pattern=r"^(xml|api|csv|manual|external)$")
    connection: ConnectionIn | None = None
    preset: str | None = Field(None, max_length=50)
    # Tedarikçi fiyatları KDV dahil mi hariç mi? None = belirtilmedi (KDV dahil varsayılır, uyarı gösterilir)
    price_vat_mode: str | None = Field(None, pattern=r"^(included|excluded)$")


class MappingItem(BaseModel):
    target_field: str
    source_path: str | None = Field(None, max_length=300)
    default_value: str | None = Field(None, max_length=500)


class MappingsIn(BaseModel):
    mappings: list[MappingItem]


class SourcingIn(BaseModel):
    strategy: str = Field("manual")
    preferred_supplier_id: int | None = None


# ------------------------------------------------------------------ yardımcılar
def _slug(name: str) -> str:
    tr = str.maketrans("çğıöşüÇĞİÖŞÜ", "cgiosuCGIOSU")
    s = re.sub(r"[^a-z0-9]+", "_", name.translate(tr).lower()).strip("_")
    return (s or "tedarikci")[:40]


def _connection_public(con: dict | None) -> dict:
    con = con or {}
    return {"integration_type": con.get("integration_type") or "manual",
            "source_url_display": con.get("source_url_display"),
            "has_source_url": bool(con.get("source_url_enc")),
            "auth_type": con.get("auth_type") or "none", "auth_username": con.get("auth_username"),
            "auth_param_name": con.get("auth_param_name"), "has_secret": bool(con.get("secret_enc")),
            "secret_env": con.get("secret_env"), "record_path": con.get("record_path"),
            "options": con.get("options") or {}}


def _save_connection(conn: Connection, supplier_id: int, c: ConnectionIn) -> None:
    cur = row(conn, "SELECT * FROM supplier_connections WHERE supplier_id = :id", id=supplier_id) or {}
    url_enc, url_display = cur.get("source_url_enc"), cur.get("source_url_display")
    try:
        if c.source_url is not None:
            url = c.source_url.strip()
            if url:
                if not re.match(r"^https?://", url, re.I):
                    raise HTTPException(422, "Kaynak adresi http:// veya https:// ile başlamalı")
                url_enc, url_display = encrypt(url), mask_url(url)
            else:
                url_enc = url_display = None
        secret_enc = cur.get("secret_enc")
        if c.secret is not None:
            secret_enc = encrypt(c.secret) if c.secret else None
    except SecretStoreError as exc:
        raise HTTPException(400, str(exc)) from None
    if c.auth_type == "none":
        secret_enc = None
    options = {k: v for k, v in {"delimiter": c.delimiter or None, "encoding": c.encoding or None,
                                 "allow_mass_missing": c.allow_mass_missing,
                                 "page_param": c.page_param or None, "page_size_param": c.page_size_param or None,
                                 "page_size": c.page_size, "start_page": c.start_page,
                                 "max_pages": c.max_pages}.items() if v not in (None, "", False)}
    conn.execute(text("""
        INSERT INTO supplier_connections(supplier_id, integration_type, source_url_enc, source_url_display, auth_type,
               auth_username, auth_param_name, secret_enc, secret_env, record_path, options, updated_at)
        VALUES (:s, :t, :ue, :ud, :at, :au, :ap, :se, :env, :rp, CAST(:opt AS JSONB), NOW())
        ON CONFLICT (supplier_id) DO UPDATE SET integration_type = EXCLUDED.integration_type,
               source_url_enc = EXCLUDED.source_url_enc, source_url_display = EXCLUDED.source_url_display,
               auth_type = EXCLUDED.auth_type, auth_username = EXCLUDED.auth_username,
               auth_param_name = EXCLUDED.auth_param_name, secret_enc = EXCLUDED.secret_enc,
               secret_env = EXCLUDED.secret_env, record_path = EXCLUDED.record_path, options = EXCLUDED.options,
               updated_at = NOW()
    """), {"s": supplier_id, "t": c.integration_type, "ue": url_enc, "ud": url_display, "at": c.auth_type,
           "au": c.auth_username or None, "ap": c.auth_param_name or None, "se": secret_enc,
           "env": c.secret_env if c.auth_type != "none" else None, "rp": (c.record_path or "").strip() or None,
           "opt": json.dumps(options)})
    conn.execute(text("UPDATE suppliers SET integration_type = :t, updated_at = NOW() WHERE id = :id"),
                 {"t": c.integration_type, "id": supplier_id})


def _audit_connection(c: ConnectionIn) -> dict:
    """Denetim kaydı: URL/secret DEĞERİ yazılmaz, yalnızca değişip değişmediği."""
    return {"integration_type": c.integration_type, "auth_type": c.auth_type,
            "source_url_changed": c.source_url is not None, "secret_changed": c.secret is not None,
            "secret_env": c.secret_env, "record_path": c.record_path}


SUPPLIER_STATS_SQL = """
    SELECT sp.id, sp.code, sp.name, sp.contact_name, sp.phone, sp.email, sp.lead_time_days, sp.notes, sp.is_active,
           sp.priority, sp.sync_interval_minutes, sp.stock_rules, sp.last_sync_at, sp.last_sync_status,
           sp.last_sync_error, sp.created_at, sp.updated_at, sp.price_vat_mode, sp.mapping_approved_at,
           COALESCE(sc.integration_type, CASE WHEN sp.integration_type IN ('xml','api','csv') THEN sp.integration_type
                                               ELSE 'manual' END) AS integration_type,
           sc.source_url_display, (sc.source_url_enc IS NOT NULL) AS has_source_url,
           COALESCE(x.product_count, 0) AS product_count, COALESCE(x.active_count, 0) AS active_count,
           COALESCE(x.missing_count, 0) AS missing_count, COALESCE(x.in_stock_count, 0) AS in_stock_count,
           COALESCE(x.out_of_stock_count, 0) AS out_of_stock_count, COALESCE(x.linked_count, 0) AS linked_count,
           (SELECT COUNT(*) FROM supplier_orders so WHERE so.supplier_id = sp.id) AS order_count,
           (SELECT COUNT(*) FROM supplier_orders so WHERE so.supplier_id = sp.id AND so.last_error IS NOT NULL) AS error_count,
           (SELECT COUNT(*) FROM supplier_field_mappings m WHERE m.supplier_id = sp.id AND m.source_path IS NOT NULL) AS mapped_fields
      FROM suppliers sp
      LEFT JOIN supplier_connections sc ON sc.supplier_id = sp.id
      LEFT JOIN LATERAL (
          SELECT COUNT(*) AS product_count,
                 COUNT(*) FILTER (WHERE COALESCE(p.status,'active') = 'active') AS active_count,
                 COUNT(*) FILTER (WHERE p.status = 'missing') AS missing_count,
                 COUNT(*) FILTER (WHERE COALESCE(p.status,'active') = 'active' AND COALESCE(p.stock, 0) > 0) AS in_stock_count,
                 COUNT(*) FILTER (WHERE COALESCE(p.status,'active') = 'active' AND COALESCE(p.stock, 0) <= 0) AS out_of_stock_count,
                 COUNT(*) FILTER (WHERE p.product_id IS NOT NULL) AS linked_count
            FROM supplier_products p WHERE p.supplier_id = sp.id) x ON TRUE
"""


def _health(s: dict) -> str:
    return ("inactive" if not s["is_active"] else "never" if not s["last_sync_at"]
            else "error" if s["last_sync_status"] == "failed"
            else "warning" if s["last_sync_status"] == "partial" else "ok")


def supplier_overview(conn: Connection) -> list[dict]:
    items = rows(conn, SUPPLIER_STATS_SQL + " ORDER BY sp.priority, sp.name")
    for s in items:
        s["health"] = _health(s)
    return items


# ------------------------------------------------------------------ meta
@router.get("/api/supplier-meta")
def supplier_meta(_: CurrentUser = Depends(viewer)):
    return {"fields": [{"name": n, "label": label, "type": t, "required": req} for n, label, t, req in TARGET_FIELDS],
            "integration_types": INTEGRATION_TYPES, "auth_types": AUTH_TYPES,
            "connectors": [{"type": k, "label": c.label, "description": c.description, "remote": c.remote}
                           for k, c in CONNECTORS.items()],
            "strategies": STRATEGY_LABELS, "product_statuses": STATUS_TR,
            "presets": [{"key": k, **{f: v[f] for f in ("name", "integration_type", "sync_interval_minutes", "note")}}
                        for k, v in PRESETS.items()],
            "secret_storage_ready": is_set(get_settings().app_secret) and len(get_settings().app_secret) >= 16}


# ------------------------------------------------------------------ tedarikçiler
@router.get("/api/suppliers")
def list_suppliers(_: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    return supplier_overview(conn)


@router.post("/api/suppliers", status_code=201)
def create_supplier(body: SupplierIn, request: Request, user: CurrentUser = Depends(admin),
                    conn: Connection = Depends(get_conn)):
    preset = PRESETS.get(body.preset) if body.preset else None
    if body.preset and preset is None:
        raise HTTPException(422, "Bilinmeyen şablon")
    code = body.code or (preset["code"] if preset else _slug(body.name))
    connection = body.connection or ConnectionIn(
        integration_type=body.integration_type if body.integration_type in ("xml", "api", "csv") else
        (preset["integration_type"] if preset else "manual"))
    try:
        with conn.begin_nested():
            sid = conn.execute(text("""
                INSERT INTO suppliers(code, name, contact_name, phone, email, lead_time_days, integration_type, notes,
                                      is_active, priority, sync_interval_minutes, stock_rules, price_vat_mode)
                VALUES (:code, :name, :contact_name, :phone, :email, :lead_time_days, :it, :notes, :is_active,
                        :priority, :interval, CAST(:rules AS JSONB), :price_vat_mode)
                RETURNING id
            """), {**body.model_dump(exclude={"connection", "stock_rules", "code", "preset", "integration_type"}),
                   "code": code, "it": connection.integration_type, "interval": body.sync_interval_minutes,
                   "rules": body.stock_rules.model_dump_json()}).scalar()
    except IntegrityError:
        raise HTTPException(409, "Bu tedarikçi kodu zaten kayıtlı") from None
    _save_connection(conn, sid, connection)
    log_audit(conn, actor=user.username, user_id=user.id, action="supplier.created", entity_type="supplier",
              entity_id=sid, ip=client_ip(request),
              details={**body.model_dump(exclude={"connection"}, mode="json"), "code": code,
                       "connection": _audit_connection(connection)})
    return {"id": sid, "code": code}


@router.get("/api/suppliers/{supplier_id}")
def get_supplier(supplier_id: int, _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    s = row(conn, SUPPLIER_STATS_SQL + " WHERE sp.id = :id", id=supplier_id)
    if s is None:
        raise not_found("Tedarikçi")
    s["health"] = _health(s)
    con = row(conn, "SELECT * FROM supplier_connections WHERE supplier_id = :id", id=supplier_id)
    maps = {r["target_field"]: r for r in rows(conn, """
        SELECT target_field, source_path, default_value FROM supplier_field_mappings WHERE supplier_id = :id""",
        id=supplier_id)}
    return {"supplier": s, "connection": _connection_public(con),
            "mappings": [{"target_field": n, "label": label, "type": t, "required": req,
                          "source_path": maps.get(n, {}).get("source_path"),
                          "default_value": maps.get(n, {}).get("default_value")} for n, label, t, req in TARGET_FIELDS],
            "runs": rows(conn, """SELECT * FROM supplier_sync_runs WHERE supplier_id = :id
                                   ORDER BY started_at DESC, id DESC LIMIT 10""", id=supplier_id)}


@router.put("/api/suppliers/{supplier_id}")
def update_supplier(supplier_id: int, body: SupplierIn, request: Request, user: CurrentUser = Depends(admin),
                    conn: Connection = Depends(get_conn)):
    n = conn.execute(text("""
        UPDATE suppliers SET name = :name, contact_name = :contact_name, phone = :phone, email = :email,
               lead_time_days = :lead_time_days, notes = :notes, is_active = :is_active, priority = :priority,
               sync_interval_minutes = :interval, stock_rules = CAST(:rules AS JSONB),
               price_vat_mode = COALESCE(:price_vat_mode, price_vat_mode), updated_at = NOW()
         WHERE id = :id
    """), {**body.model_dump(exclude={"connection", "stock_rules", "code", "preset", "integration_type"}),
           "interval": body.sync_interval_minutes, "rules": body.stock_rules.model_dump_json(), "id": supplier_id}).rowcount
    if not n:
        raise not_found("Tedarikçi")
    if body.connection is not None:
        _save_connection(conn, supplier_id, body.connection)
    # Stok kuralı değişmiş olabilir: bu tedarikçiye bağlı katalog ürünlerini yeniden hesapla
    refresh_catalog(conn, [r["product_id"] for r in rows(conn, """
        SELECT DISTINCT product_id FROM supplier_products WHERE supplier_id = :s AND product_id IS NOT NULL""",
        s=supplier_id)])
    log_audit(conn, actor=user.username, user_id=user.id, action="supplier.updated", entity_type="supplier",
              entity_id=supplier_id, ip=client_ip(request),
              details={**body.model_dump(exclude={"connection"}, mode="json"),
                       **({"connection": _audit_connection(body.connection)} if body.connection else {})})
    return {"ok": True}


@router.put("/api/suppliers/{supplier_id}/connection")
def update_connection(supplier_id: int, body: ConnectionIn, request: Request, user: CurrentUser = Depends(admin),
                      conn: Connection = Depends(get_conn)):
    if not conn.execute(text("SELECT 1 FROM suppliers WHERE id = :id"), {"id": supplier_id}).first():
        raise not_found("Tedarikçi")
    _save_connection(conn, supplier_id, body)
    log_audit(conn, actor=user.username, user_id=user.id, action="supplier.connection_updated",
              entity_type="supplier", entity_id=supplier_id, ip=client_ip(request), details=_audit_connection(body))
    return {"ok": True}


@router.put("/api/suppliers/{supplier_id}/mappings")
def update_mappings(supplier_id: int, body: MappingsIn, request: Request, user: CurrentUser = Depends(operator),
                    conn: Connection = Depends(get_conn)):
    if not conn.execute(text("SELECT 1 FROM suppliers WHERE id = :id"), {"id": supplier_id}).first():
        raise not_found("Tedarikçi")
    for m in body.mappings:
        if m.target_field not in FIELD_NAMES:
            raise HTTPException(422, f"Bilinmeyen TrendHub alanı: {m.target_field}")
        conn.execute(text("""
            INSERT INTO supplier_field_mappings(supplier_id, target_field, source_path, default_value, updated_at)
            VALUES (:s, :f, :p, :d, NOW())
            ON CONFLICT (supplier_id, target_field) DO UPDATE SET source_path = EXCLUDED.source_path,
                   default_value = EXCLUDED.default_value, updated_at = NOW()
        """), {"s": supplier_id, "f": m.target_field, "p": (m.source_path or "").strip() or None,
               "d": (m.default_value or "").strip() or None})
    log_audit(conn, actor=user.username, user_id=user.id, action="supplier.mapping_updated", entity_type="supplier",
              entity_id=supplier_id, ip=client_ip(request),
              details={m.target_field: m.source_path for m in body.mappings})
    return {"ok": True}


async def _read_body(request: Request) -> bytes:
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > MAX_BYTES:
        raise HTTPException(413, f"Dosya çok büyük (en fazla {MAX_BYTES // (1024 * 1024)} MB)")
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_BYTES:
            raise HTTPException(413, f"Dosya çok büyük (en fazla {MAX_BYTES // (1024 * 1024)} MB)")
    return bytes(data)


def _preview(supplier_id: int, content: bytes | None) -> dict:
    with get_engine().begin() as conn:
        try:
            cfg = load_config(conn, supplier_id)
        except SupplierSyncError:
            raise not_found("Tedarikçi") from None
    try:
        fetched = fetch_records(cfg, content or None)
        records, used_path = fetched.records, fetched.record_path
    except ConnectorError as exc:
        raise HTTPException(422, str(exc)) from None
    fields = field_paths(records)
    suggestion = suggest_mapping([f["path"] for f in fields])
    current = {k: v for k, v in cfg["mapping"].items() if v.get("source_path")}
    effective = current or {k: {"source_path": v} for k, v in suggestion.items()}
    samples = []
    for rec in records[:5]:
        m = apply_mapping(rec, effective)
        samples.append({"values": {k: (str(v) if isinstance(v, Decimal) else v) for k, v in m.values.items()},
                        "errors": m.errors})
    return {"record_path": used_path, "total": len(records), "fields": fields[:300], "suggestion": suggestion,
            "using": "saved" if current else "suggestion", "samples": samples}


@router.post("/api/suppliers/{supplier_id}/preview")
async def preview(supplier_id: int, request: Request, _: CurrentUser = Depends(operator)):
    """Kaynağı okur (URL'den veya gövdede gelen dosyadan); alanları, öneriyi ve örnek kayıtları döner.
    Veritabanına hiçbir ürün yazılmaz."""
    content = await _read_body(request)
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(_preview, supplier_id, content or None)


@router.post("/api/suppliers/{supplier_id}/test")
def test_supplier_connection(supplier_id: int, request: Request, user: CurrentUser = Depends(operator)):
    """Kaynağa bağlanıp ayrıştırır; ürün yazmaz. Sonuç denetim kaydına yazılır (URL/secret hariç)."""
    with get_engine().begin() as conn:
        try:
            cfg = load_config(conn, supplier_id)
        except SupplierSyncError:
            raise not_found("Tedarikçi") from None
    check = get_supplier_connector(cfg["connection"] or {"integration_type": "manual"}).test_connection()
    with get_engine().begin() as conn:
        log_audit(conn, actor=user.username, user_id=user.id, action="supplier.connection_tested",
                  entity_type="supplier", entity_id=supplier_id, ip=client_ip(request),
                  details={"ok": check.ok, "records": check.records})
    return {"ok": check.ok, "message": check.message, "records": check.records, "record_path": check.record_path}


@router.post("/api/suppliers/{supplier_id}/sync", status_code=202)
def trigger_supplier_sync(supplier_id: int, request: Request, user: CurrentUser = Depends(operator),
                          conn: Connection = Depends(get_conn)):
    s = row(conn, """SELECT s.id, s.code, s.is_active, c.integration_type, c.source_url_enc FROM suppliers s
                      LEFT JOIN supplier_connections c ON c.supplier_id = s.id WHERE s.id = :id""", id=supplier_id)
    if s is None:
        raise not_found("Tedarikçi")
    if (s["integration_type"] or "manual") == "manual" or not s["source_url_enc"]:
        raise HTTPException(409, "Bu tedarikçinin kaynak adresi yok; dosya yükleyerek senkronize edin.")
    job_id = jobs.enqueue(conn, SUPPLIER_SYNC, payload={"supplier_id": supplier_id, "trigger": "manual"},
                          idempotency_key=f"{SUPPLIER_SYNC}:{supplier_id}", max_attempts=3)
    log_audit(conn, actor=user.username, user_id=user.id, action="supplier.sync_requested", entity_type="supplier",
              entity_id=supplier_id, ip=client_ip(request), details={"job_id": job_id})
    return {"queued": job_id is not None, "job_id": job_id,
            "message": "Senkronizasyon kuyruğa alındı" if job_id else "Bu tedarikçi için bekleyen bir iş zaten var"}


@router.post("/api/suppliers/{supplier_id}/upload")
async def upload_feed(supplier_id: int, request: Request, user: CurrentUser = Depends(operator)):
    """Dosya (XML/CSV/JSON) yükleyerek senkronize eder: manuel/CSV tedarikçiler için."""
    content = await _read_body(request)
    if not content:
        raise HTTPException(422, "Dosya boş")
    from starlette.concurrency import run_in_threadpool

    def work():
        try:
            return run_supplier_sync(get_engine(), supplier_id, trigger="upload", content=content)
        except SupplierSyncError as exc:
            raise HTTPException(422, str(exc)) from None
    result = await run_in_threadpool(work)
    with get_engine().begin() as conn:
        log_audit(conn, actor=user.username, user_id=user.id, action="supplier.file_uploaded", entity_type="supplier",
                  entity_id=supplier_id, ip=client_ip(request),
                  details={"bytes": len(content), "run_id": result.get("run_id"), "status": result.get("status")})
    return result


@router.get("/api/suppliers/{supplier_id}/runs")
def supplier_runs(supplier_id: int, page: Page = Depends(), _: CurrentUser = Depends(viewer),
                  conn: Connection = Depends(get_conn)):
    total = conn.execute(text("SELECT COUNT(*) FROM supplier_sync_runs WHERE supplier_id = :s"), {"s": supplier_id}).scalar()
    items = rows(conn, """SELECT * FROM supplier_sync_runs WHERE supplier_id = :s ORDER BY started_at DESC, id DESC
                          LIMIT :limit OFFSET :offset""", s=supplier_id, limit=page.page_size, offset=page.offset)
    return paged(items, total, page)


CHANGE_TR = {"new": "Yeni ürün", "price": "Fiyat değişti", "stock": "Stok değişti", "missing": "Kaynağında bulunamadı",
             "reactivated": "Yeniden göründü", "content": "İçerik değişti"}


@router.get("/api/suppliers/{supplier_id}/changes")
def supplier_changes(supplier_id: int, kind: str | None = Query(None, pattern=r"^(new|price|stock|missing|reactivated)$"),
                     page: Page = Depends(), _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    where = "c.supplier_id = :s" + (" AND c.kind = :k" if kind else "")
    total = conn.execute(text(f"SELECT COUNT(*) FROM supplier_product_changes c WHERE {where}"),
                         {"s": supplier_id, "k": kind}).scalar()
    items = rows(conn, f"""
        SELECT c.id, c.kind, c.old_value, c.new_value, c.created_at, c.run_id, p.supplier_sku, p.name, p.barcode
          FROM supplier_product_changes c JOIN supplier_products p ON p.id = c.supplier_product_id
         WHERE {where} ORDER BY c.created_at DESC, c.id DESC LIMIT :limit OFFSET :offset
    """, s=supplier_id, k=kind, limit=page.page_size, offset=page.offset)
    for it in items:
        it["kind_label"] = CHANGE_TR.get(it["kind"], it["kind"])
    return paged(items, total, page)


# ------------------------------------------------------------------ ürün havuzu
@router.get("/api/supplier-products")
def supplier_pool(page: Page = Depends(), supplier_id: int | None = None, q: str | None = Query(None, max_length=100),
                  status: str | None = Query(None, pattern=r"^(active|missing)$"),
                  in_catalog: str | None = Query(None, pattern=r"^(yes|no)$"), in_stock: bool = False,
                  multi_supplier: bool = False, category: str | None = Query(None, max_length=300),
                  stock: str | None = Query(None, pattern=r"^(in|out)$"),
                  price_min: Decimal | None = Query(None, ge=0), price_max: Decimal | None = Query(None, ge=0),
                  in_store: str | None = Query(None, pattern=r"^(yes|no)$"), problematic: bool = False,
                  _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    where, params = ["TRUE"], {}
    listed = """EXISTS (SELECT 1 FROM marketplace_listings l WHERE sp.product_id IS NOT NULL
                          AND (l.product_id = sp.product_id OR (sp.barcode IS NOT NULL AND sp.barcode <> '' AND l.barcode = sp.barcode)))"""
    if category:
        where.append("sp.category ILIKE :cat"); params["cat"] = f"%{category.strip()}%"
    if stock == "in":
        where.append("COALESCE(sp.stock, 0) > 0")
    elif stock == "out":
        where.append("COALESCE(sp.stock, 0) <= 0")
    if price_min is not None:
        where.append("sp.cost >= :pmin"); params["pmin"] = price_min
    if price_max is not None:
        where.append("sp.cost <= :pmax"); params["pmax"] = price_max
    if in_store == "yes":
        where.append(listed)
    elif in_store == "no":
        where.append("NOT " + listed)
    if problematic:
        where.append("""(COALESCE(sp.status, 'active') = 'missing' OR sp.barcode IS NULL OR sp.barcode = ''
                         OR sp.cost IS NULL OR jsonb_array_length(COALESCE(sp.images, '[]'::jsonb)) = 0
                         OR EXISTS (SELECT 1 FROM alerts a WHERE a.status = 'open' AND a.supplier_product_id = sp.id))""")
    if supplier_id:
        where.append("sp.supplier_id = :sid"); params["sid"] = supplier_id
    if q:
        where.append("(sp.supplier_sku ILIKE :q OR sp.barcode ILIKE :q OR sp.name ILIKE :q OR sp.model_code ILIKE :q)")
        params["q"] = f"%{q.strip()}%"
    if status:
        where.append("COALESCE(sp.status, 'active') = :st"); params["st"] = status
    if in_catalog == "yes":
        where.append("sp.product_id IS NOT NULL")
    elif in_catalog == "no":
        where.append("sp.product_id IS NULL")
    if in_stock:
        where.append("COALESCE(sp.stock, 0) > 0")
    if multi_supplier:
        where.append("sp.product_id IS NOT NULL AND (SELECT COUNT(*) FROM supplier_products o WHERE o.product_id = sp.product_id) > 1")
    w = " AND ".join(where)
    total = conn.execute(text(f"SELECT COUNT(*) FROM supplier_products sp WHERE {w}"), params).scalar()
    items = rows(conn, f"""
        SELECT sp.id, sp.supplier_id, s.name AS supplier_name, sp.supplier_sku, sp.barcode, sp.model_code, sp.name,
               sp.brand, sp.category, sp.cost, sp.sale_price, sp.stock, sp.vat_rate, sp.desi, sp.color, sp.variant,
               sp.size, sp.parent_code, sp.currency, s.price_vat_mode,
               COALESCE(sp.status, 'active') AS status, sp.images, sp.last_seen_at, sp.missing_since, sp.product_id,
               sp.updated_at,
               (SELECT COALESCE(json_agg(DISTINCT m.name), '[]'::json) FROM marketplace_listings l
                  JOIN stores st ON st.id = l.store_id JOIN marketplaces m ON m.id = st.marketplace_id
                 WHERE sp.product_id IS NOT NULL AND (l.product_id = sp.product_id
                       OR (sp.barcode IS NOT NULL AND sp.barcode <> '' AND l.barcode = sp.barcode))) AS stores,
               (SELECT COUNT(*) FROM alerts a WHERE a.status = 'open' AND a.supplier_product_id = sp.id) AS open_alerts,
               p.sku AS product_sku, p.preferred_supplier_id,
               (SELECT COUNT(*) FROM supplier_products o WHERE o.product_id = sp.product_id) AS offer_count,
               (SELECT COALESCE(json_agg(m.code ORDER BY m.id), '[]'::json) FROM listing_drafts d
                  JOIN marketplaces m ON m.id = d.marketplace_id
                 WHERE d.product_id = sp.product_id AND d.status <> 'cancelled') AS draft_marketplaces
          FROM supplier_products sp JOIN suppliers s ON s.id = sp.supplier_id
          LEFT JOIN products p ON p.id = sp.product_id
         WHERE {w} ORDER BY sp.name NULLS LAST, sp.id LIMIT :limit OFFSET :offset
    """, **params, limit=page.page_size, offset=page.offset)
    fx = app_settings.fx_rates(conn)
    for it in items:
        it["status_label"] = STATUS_TR.get(it["status"], it["status"])
        # Finansta kullanılacak maliyet (TL, KDV dahil) ve nedeni; kur yoksa maliyet YOK
        it["effective_cost"], it["cost_note"] = effective_cost(it["cost"], it["currency"], it["vat_rate"],
                                                               it.pop("price_vat_mode"), fx)
        imgs = it.pop("images") or []
        it["image"] = imgs[0] if imgs else None
        it["image_count"] = len(imgs)
        it["problems"] = [t for c, t in (
            (it["status"] == "missing", "Kaynakta bulunamadı"), (not it["barcode"], "Barkod yok"),
            (it["cost"] is None, "Alış fiyatı yok"), (not imgs, "Görsel yok"),
            ((it["stock"] or 0) <= 0, "Stok yok"), (it["open_alerts"], "Açık uyarı var"),
            (it["cost"] is not None and it["effective_cost"] is None, it["cost_note"] or "Maliyet kullanılamıyor")) if c]
    summary = row(conn, """
        SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE COALESCE(status,'active') = 'active') AS active,
               COUNT(*) FILTER (WHERE status = 'missing') AS missing,
               COUNT(*) FILTER (WHERE product_id IS NULL) AS not_in_catalog,
               COUNT(DISTINCT product_id) FILTER (WHERE product_id IS NOT NULL) AS catalog_products
          FROM supplier_products WHERE (CAST(:sid AS BIGINT) IS NULL OR supplier_id = :sid)
    """, sid=supplier_id)
    return {**paged(items, total, page), "summary": summary}


# ------------------------------------------------------------ teklif karşılaştırma
@router.get("/api/products/{product_id}/offers")
def product_offers(product_id: int, _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    p = row(conn, """SELECT id, sku, barcode, name, stock, cost, preferred_supplier_id,
                            COALESCE(supplier_strategy, 'manual') AS supplier_strategy FROM products WHERE id = :id""",
            id=product_id)
    if p is None:
        raise not_found("Ürün")
    offers = load_offers(conn, [product_id]).get(product_id, [])
    detail = {r["id"]: r for r in rows(conn, """
        SELECT sp.id, sp.supplier_sku, sp.stock AS raw_stock, sp.sale_price, COALESCE(sp.status,'active') AS status,
               sp.last_seen_at, sp.price_changed_at, s.is_active AS supplier_active
          FROM supplier_products sp JOIN suppliers s ON s.id = sp.supplier_id WHERE sp.product_id = :p""", p=product_id)}
    by_strategy = {k: (o.supplier_id if (o := select_offer(offers, k, p["preferred_supplier_id"])) else None)
                   for k in STRATEGIES}
    selected = select_offer(offers, p["supplier_strategy"], p["preferred_supplier_id"])
    return {"product": p, "strategy_labels": STRATEGY_LABELS, "by_strategy": by_strategy,
            "selected_supplier_id": selected.supplier_id if selected else None,
            "offers": sorted([{"supplier_product_id": o.supplier_product_id, "supplier_id": o.supplier_id,
                               "supplier_name": o.supplier_name, "cost": o.cost, "stock": o.stock,
                               "priority": o.priority, "available": o.available, "usable": o.usable,
                               **{k: v for k, v in detail.get(o.supplier_product_id, {}).items() if k != "id"},
                               "status_label": STATUS_TR.get(detail.get(o.supplier_product_id, {}).get("status"), "")}
                              for o in offers], key=lambda x: (x["cost"] is None, x["cost"] or 0))}


@router.get("/api/supplier-comparison")
def supplier_comparison(page: Page = Depends(), q: str | None = Query(None, max_length=100),
                        only_savings: bool = False, _: CurrentUser = Depends(viewer),
                        conn: Connection = Depends(get_conn)):
    """Birden fazla tedarikçi teklifi olan katalog ürünleri: fiyat/stok karşılaştırması ve olası tasarruf.

    Tasarruf = şu an seçili teklifin alış fiyatı − stokta olan en ucuz teklif (adet başı)."""
    where, params = ["TRUE"], {}
    if q:
        where.append("(p.sku ILIKE :q OR p.barcode ILIKE :q OR p.name ILIKE :q)")
        params["q"] = f"%{q.strip()}%"
    base = f"""FROM products p JOIN (SELECT product_id, COUNT(*) AS n FROM supplier_products
                                     WHERE product_id IS NOT NULL GROUP BY product_id HAVING COUNT(*) > 1) x
                 ON x.product_id = p.id WHERE {' AND '.join(where)}"""
    prods = rows(conn, f"""SELECT p.id, p.sku, p.barcode, p.name, p.stock, p.cost, p.preferred_supplier_id,
                                  COALESCE(p.supplier_strategy, 'manual') AS strategy, x.n AS offer_count
                             {base} ORDER BY p.name""", **params)
    offers = load_offers(conn, [p["id"] for p in prods])
    items = []
    for p in prods:
        os_ = offers.get(p["id"], [])
        usable = [o for o in os_ if o.usable]
        cheapest = select_offer(os_, "cheapest", None)
        most = select_offer(os_, "highest_stock", None)
        sel = select_offer(os_, p["strategy"], p["preferred_supplier_id"])
        costs = [o.cost for o in os_ if o.cost is not None]
        saving = (sel.cost - cheapest.cost) if sel and cheapest and sel.cost is not None else None
        if only_savings and not (saving and saving > 0):
            continue
        items.append({**p, "strategy_label": STRATEGY_LABELS.get(p["strategy"], p["strategy"]),
                      "usable_offers": len(usable), "min_cost": min(costs) if costs else None,
                      "max_cost": max(costs) if costs else None,
                      "total_stock": sum(o.stock for o in usable),
                      "cheapest_supplier": cheapest.supplier_name if cheapest else None,
                      "cheapest_cost": cheapest.cost if cheapest else None,
                      "highest_stock_supplier": most.supplier_name if most else None,
                      "highest_stock": most.stock if most else None,
                      "selected_supplier": sel.supplier_name if sel else None,
                      "selected_cost": sel.cost if sel else None, "saving_per_unit": saving,
                      "offers": [{"supplier_name": o.supplier_name, "cost": o.cost, "stock": o.stock,
                                  "usable": o.usable} for o in sorted(os_, key=lambda o: (o.cost is None, o.cost or 0))]})
    total = len(items)
    summary = {"products": total, "with_saving": sum(1 for i in items if i["saving_per_unit"] and i["saving_per_unit"] > 0),
               "no_usable_offer": sum(1 for i in items if not i["usable_offers"])}
    return {**paged(items[page.offset:page.offset + page.page_size], total, page), "summary": summary}


@router.put("/api/products/{product_id}/sourcing")
def set_sourcing(product_id: int, body: SourcingIn, request: Request, user: CurrentUser = Depends(operator),
                 conn: Connection = Depends(get_conn)):
    if body.strategy not in STRATEGIES:
        raise HTTPException(422, "Bilinmeyen strateji")
    if not conn.execute(text("SELECT 1 FROM products WHERE id = :id"), {"id": product_id}).first():
        raise not_found("Ürün")
    if body.preferred_supplier_id is not None and not conn.execute(text(
            "SELECT 1 FROM supplier_products WHERE product_id = :p AND supplier_id = :s"),
            {"p": product_id, "s": body.preferred_supplier_id}).first():
        raise HTTPException(422, "Bu tedarikçinin bu ürün için teklifi yok")
    conn.execute(text("""UPDATE products SET supplier_strategy = :st, preferred_supplier_id = :pref, updated_at = NOW()
                         WHERE id = :id"""), {"st": body.strategy, "pref": body.preferred_supplier_id, "id": product_id})
    refresh_catalog(conn, [product_id])
    log_audit(conn, actor=user.username, user_id=user.id, action="product.sourcing_updated", entity_type="product",
              entity_id=product_id, ip=client_ip(request), details=body.model_dump())
    return {"ok": True}
