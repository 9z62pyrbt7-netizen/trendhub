"""Sistem / Hatalar, Ayarlar ve Kullanıcılar."""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from ..config import get_settings
from ..db import get_conn, get_engine, row, rows
from ..deps import CurrentUser, admin, client_ip, operator, viewer
from ..security import hash_password, revoke_user_sessions, validate_password_strength
from ..services import app_settings, jobs
from ..services.audit import log_audit
from .common import Page, not_found, paged

router = APIRouter(tags=["system"])


@router.get("/api/health")
def health():
    """Liveness/readiness (kimlik doğrulama gerektirmez, hassas bilgi içermez)."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        return {"status": "unhealthy", "database": "unreachable"}
    return {"status": "healthy", "database": "connected"}


@router.get("/api/system/health")
def system_health(_: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    version = None
    try:
        with conn.begin_nested():
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:  # noqa: BLE001
        version = None
    workers = rows(conn, """
        SELECT worker_id, hostname, started_at, last_seen_at, current_job_id,
               last_seen_at > NOW() - INTERVAL '90 seconds' AS alive
          FROM worker_heartbeats ORDER BY last_seen_at DESC LIMIT 10
    """)
    events = row(conn, """
        SELECT COUNT(*) FILTER (WHERE level IN ('error','critical')) AS errors,
               COUNT(*) FILTER (WHERE level = 'warning') AS warnings
          FROM system_events WHERE resolved_at IS NULL
    """)
    db_time = conn.execute(text("SELECT NOW()")).scalar()
    alive = any(w["alive"] for w in workers)
    queue = jobs.queue_stats(conn)
    overall = "healthy"
    if not alive or events["errors"]:
        overall = "degraded"
    from ..connectors.registry import all_connectors
    from .integrations import integration_state
    mp_rows = {r["code"]: r for r in rows(conn, "SELECT * FROM marketplaces")}
    last_jobs = {r["marketplace"]: r for r in rows(conn, """
        SELECT marketplace,
               MAX(finished_at) FILTER (WHERE status = 'succeeded') AS last_success_at,
               MAX(finished_at) FILTER (WHERE status IN ('failed', 'dead')) AS last_failure_at,
               COUNT(*) FILTER (WHERE status IN ('failed', 'dead') AND finished_at > NOW() - INTERVAL '24 hours') AS failed_24h
          FROM sync_jobs WHERE marketplace IS NOT NULL GROUP BY marketplace
    """)}
    connectors = []
    for c in all_connectors():
        st = integration_state(c, mp_rows.get(c.code))
        lj = last_jobs.get(c.code, {})
        # Yalnızca durum bilgisi: credential alanları bu listeye hiç eklenmez.
        connectors.append({"code": c.code, "name": c.name, "state": st["state"], "state_label": st["state_label"],
                           "last_sync_at": st["last_sync_at"], "last_check_at": st["last_check_at"],
                           "last_check_ok": st["last_check_ok"], "last_success_at": lj.get("last_success_at"),
                           "last_failure_at": lj.get("last_failure_at"), "failed_24h": lj.get("failed_24h", 0)})
    return {"status": overall, "database": {"ok": True, "time": db_time, "migration": version},
            "workers": workers, "worker_alive": alive, "queue": queue, "open_events": events,
            "last_heartbeat_at": workers[0]["last_seen_at"] if workers else None,
            "connectors": connectors,
            "config": {"connector_write_enabled": get_settings().connector_write_enabled,
                       "sync_interval_minutes": get_settings().sync_interval_minutes,
                       "app_env": get_settings().app_env}}


@router.get("/api/system/jobs")
def list_jobs(page: Page = Depends(), status: str | None = None, marketplace: str | None = None,
              _: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    where, params = ["TRUE"], {}
    if status:
        where.append("status = :s"); params["s"] = status
    if marketplace:
        where.append("marketplace = :m"); params["m"] = marketplace
    w = " AND ".join(where)
    total = conn.execute(text(f"SELECT COUNT(*) FROM sync_jobs WHERE {w}"), params).scalar()
    items = rows(conn, f"""
        SELECT id, marketplace, job_type, status, attempts, max_attempts, message, last_error, result,
               created_at, started_at, finished_at, run_after, locked_by
          FROM sync_jobs WHERE {w} ORDER BY id DESC LIMIT :limit OFFSET :offset
    """, **params, limit=page.page_size, offset=page.offset)
    return paged(items, total, page)


@router.post("/api/system/jobs/{job_id}/retry")
def retry_job(job_id: int, request: Request, user: CurrentUser = Depends(operator),
              conn: Connection = Depends(get_conn)):
    j = row(conn, "SELECT id, status FROM sync_jobs WHERE id = :id FOR UPDATE", id=job_id)
    if j is None:
        raise not_found("İş")
    if j["status"] not in ("failed", "dead"):
        raise HTTPException(409, "Yalnızca başarısız işler yeniden denenebilir")
    try:
        with conn.begin_nested():
            conn.execute(text("""UPDATE sync_jobs SET status = 'queued', attempts = 0, run_after = NOW(),
                                  last_error = NULL WHERE id = :id"""), {"id": job_id})
    except IntegrityError:
        raise HTTPException(409, "Aynı iş zaten kuyrukta bekliyor") from None
    log_audit(conn, actor=user.username, user_id=user.id, action="job.retried", entity_type="sync_job",
              entity_id=job_id, ip=client_ip(request))
    return {"ok": True}


@router.get("/api/system/events")
def list_events(page: Page = Depends(), include_resolved: bool = False, _: CurrentUser = Depends(viewer),
                conn: Connection = Depends(get_conn)):
    w = "TRUE" if include_resolved else "resolved_at IS NULL"
    total = conn.execute(text(f"SELECT COUNT(*) FROM system_events WHERE {w}")).scalar()
    items = rows(conn, f"""SELECT id, occurred_at, last_occurred_at, level, source, message, details, occurrences, resolved_at
                            FROM system_events WHERE {w} ORDER BY last_occurred_at DESC LIMIT :limit OFFSET :offset""",
                 limit=page.page_size, offset=page.offset)
    return paged(items, total, page)


@router.post("/api/system/events/{event_id}/resolve")
def resolve_event(event_id: int, request: Request, user: CurrentUser = Depends(operator),
                  conn: Connection = Depends(get_conn)):
    n = conn.execute(text("UPDATE system_events SET resolved_at = NOW(), resolved_by = :u WHERE id = :id AND resolved_at IS NULL"),
                     {"u": user.id, "id": event_id}).rowcount
    if not n:
        raise not_found("Açık olay")
    log_audit(conn, actor=user.username, user_id=user.id, action="event.resolved", entity_type="system_event",
              entity_id=event_id, ip=client_ip(request))
    return {"ok": True}


@router.get("/api/system/audit")
def list_audit(page: Page = Depends(), action: str | None = Query(None, max_length=100),
               _: CurrentUser = Depends(admin), conn: Connection = Depends(get_conn)):
    w, params = ("action ILIKE :a", {"a": f"{action}%"}) if action else ("TRUE", {})
    total = conn.execute(text(f"SELECT COUNT(*) FROM audit_logs WHERE {w}"), params).scalar()
    items = rows(conn, f"""SELECT id, occurred_at, actor, action, entity_type, entity_id, ip, details
                            FROM audit_logs WHERE {w} ORDER BY id DESC LIMIT :limit OFFSET :offset""",
                 **params, limit=page.page_size, offset=page.offset)
    return paged(items, total, page)


@router.get("/api/marketplaces")
def marketplaces(_: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    return rows(conn, "SELECT code, name, enabled FROM marketplaces ORDER BY id")


# ------------------------------------------------------------------ ayarlar
@router.get("/api/settings")
def get_app_settings(_: CurrentUser = Depends(viewer), conn: Connection = Depends(get_conn)):
    values = app_settings.get_all(conn)
    return {"items": [{"key": k, "type": t, "label": label, "value": values.get(k), "group": app_settings.group_of(k)}
                      for k, (t, label) in app_settings.EDITABLE.items()]}


class SettingsIn(BaseModel):
    values: dict[str, object]


@router.put("/api/settings")
def put_app_settings(body: SettingsIn, request: Request, user: CurrentUser = Depends(admin),
                     conn: Connection = Depends(get_conn)):
    before = app_settings.get_all(conn)
    changed = {}
    labels = {k: label for k, (_, label) in app_settings.EDITABLE.items()}
    validated = {}
    for key, value in body.values.items():
        try:
            validated[key] = app_settings.validate(key, value)
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, f"{labels.get(key, key)}: {exc}") from None
    same = validated.get("shipping.same_day_before", before.get("shipping.same_day_before", "11:00"))
    nxt = validated.get("shipping.next_day_from", before.get("shipping.next_day_from", "12:00"))
    if same > nxt:
        raise HTTPException(422, "Kargo kesim saati, 'yarın kargoya verilir' saatinden sonra olamaz")
    for key, v in validated.items():
        if before.get(key) != v:
            app_settings.set_value(conn, key, v, user.id)
            changed[key] = {"from": before.get(key), "to": v}
    if changed:
        log_audit(conn, actor=user.username, user_id=user.id, action="settings.updated", ip=client_ip(request),
                  details=changed)
    return {"ok": True, "changed": list(changed)}


# -------------------------------------------------------------- kullanıcılar
@router.get("/api/users")
def list_users(_: CurrentUser = Depends(admin), conn: Connection = Depends(get_conn)):
    return rows(conn, """SELECT id, username, full_name, role, is_active, last_login_at, created_at,
                                (locked_until IS NOT NULL AND locked_until > NOW()) AS locked
                           FROM users ORDER BY id""")


class UserIn(BaseModel):
    username: str = Field(min_length=3, max_length=50, pattern=r"^[A-Za-z0-9_.\-]+$")
    full_name: str | None = Field(None, max_length=100)
    role: str = Field(pattern=r"^(admin|operator|viewer|accountant)$")
    password: str = Field(max_length=200)


@router.post("/api/users", status_code=201)
def create_user(body: UserIn, request: Request, user: CurrentUser = Depends(admin),
                conn: Connection = Depends(get_conn)):
    try:
        validate_password_strength(body.password)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    try:
        with conn.begin_nested():
            uid = conn.execute(text("""INSERT INTO users(username, full_name, role, password_hash)
                                       VALUES (:u, :f, :r, :h) RETURNING id"""),
                               {"u": body.username, "f": body.full_name, "r": body.role,
                                "h": hash_password(body.password)}).scalar()
    except IntegrityError:
        raise HTTPException(409, "Bu kullanıcı adı zaten var") from None
    log_audit(conn, actor=user.username, user_id=user.id, action="user.created", entity_type="user",
              entity_id=uid, ip=client_ip(request), details={"username": body.username, "role": body.role})
    return {"id": uid}


class UserPatch(BaseModel):
    role: str | None = Field(None, pattern=r"^(admin|operator|viewer|accountant)$")
    is_active: bool | None = None
    full_name: str | None = Field(None, max_length=100)
    new_password: str | None = Field(None, max_length=200)


@router.patch("/api/users/{user_id}")
def update_user(user_id: int, body: UserPatch, request: Request, user: CurrentUser = Depends(admin),
                conn: Connection = Depends(get_conn)):
    target = row(conn, "SELECT id, role, is_active FROM users WHERE id = :id FOR UPDATE", id=user_id)
    if target is None:
        raise not_found("Kullanıcı")
    if user_id == user.id and (body.is_active is False or (body.role and body.role != "admin")):
        raise HTTPException(409, "Kendi yetkinizi kaldıramazsınız")
    changes = body.model_dump(exclude_unset=True)
    pw = changes.pop("new_password", None)
    if pw is not None:
        try:
            validate_password_strength(pw)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        conn.execute(text("UPDATE users SET password_hash = :h, password_changed_at = NOW(), failed_login_count = 0, locked_until = NULL WHERE id = :id"),
                     {"h": hash_password(pw), "id": user_id})
        revoke_user_sessions(conn, user_id)
    if changes:
        sets = ", ".join(f"{k} = :{k}" for k in changes)
        conn.execute(text(f"UPDATE users SET {sets}, updated_at = NOW() WHERE id = :id"), {**changes, "id": user_id})
        if changes.get("is_active") is False:
            revoke_user_sessions(conn, user_id)
    log_audit(conn, actor=user.username, user_id=user.id, action="user.updated", entity_type="user",
              entity_id=user_id, ip=client_ip(request),
              details={**changes, "password_reset": pw is not None})
    return {"ok": True}
