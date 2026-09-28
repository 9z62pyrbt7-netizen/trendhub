from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import cookie_secure_for, get_settings
from ..db import get_conn, transaction
from ..deps import CurrentUser, client_ip, current_user
from ..security import (LOCK_MINUTES, MAX_FAILED_LOGINS, SESSION_COOKIE, create_session, hash_password,
                        login_limiter, revoke_session, revoke_user_sessions, validate_password_strength,
                        verify_password)
from ..services.audit import log_audit

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(max_length=200)


def _user_out(u) -> dict:
    return {"id": u["id"], "username": u["username"], "full_name": u["full_name"], "role": u["role"]}


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, conn: Connection = Depends(get_conn)):
    ip = client_ip(request)
    if not login_limiter.allow(ip or "unknown"):
        raise HTTPException(429, "Çok fazla deneme. Lütfen birkaç dakika sonra tekrar deneyin.")
    u = conn.execute(text("""
        SELECT id, username, full_name, role, password_hash, is_active, locked_until,
               (locked_until IS NOT NULL AND locked_until > NOW()) AS locked
          FROM users WHERE username = :u
    """), {"u": body.username.strip()}).mappings().first()

    ok = verify_password(u["password_hash"] if u else None, body.password)
    if u is None or not u["is_active"] or u["locked"] or not ok:
        # Hata kaydı ayrı transaction'da: istek 401 ile biterken geri alınmamalı.
        with transaction() as tx:
            if u is not None and not u["locked"] and not ok:
                tx.execute(text("""
                    UPDATE users SET failed_login_count = failed_login_count + 1,
                           locked_until = CASE WHEN failed_login_count + 1 >= :max
                                               THEN NOW() + make_interval(mins => :lock) ELSE locked_until END
                     WHERE id = :id
                """), {"id": u["id"], "max": MAX_FAILED_LOGINS, "lock": LOCK_MINUTES})
            log_audit(tx, actor=body.username[:100], action="auth.login_failed", ip=ip,
                      user_id=u["id"] if u else None)
        if u is not None and u["locked"]:
            raise HTTPException(423, f"Hesap geçici olarak kilitli ({LOCK_MINUTES} dk).")
        raise HTTPException(401, "Kullanıcı adı veya parola hatalı")

    settings = get_settings()
    conn.execute(text("UPDATE users SET failed_login_count = 0, locked_until = NULL, last_login_at = NOW() WHERE id = :id"),
                 {"id": u["id"]})
    token = create_session(conn, u["id"], settings.session_ttl_hours, ip, request.headers.get("User-Agent"))
    log_audit(conn, actor=u["username"], user_id=u["id"], action="auth.login", ip=ip)
    # API yalnızca nginx arkasında erişilebilir; X-Forwarded-Proto'yu nginx yazar.
    scheme = request.headers.get("X-Forwarded-Proto", request.url.scheme).split(",")[0].strip().lower()
    response.set_cookie(SESSION_COOKIE, token, httponly=True, secure=cookie_secure_for(settings, scheme),
                        samesite="strict", max_age=settings.session_ttl_hours * 3600, path="/")
    return {"user": _user_out(u)}


@router.post("/logout")
def logout(request: Request, response: Response, user: CurrentUser = Depends(current_user),
           conn: Connection = Depends(get_conn)):
    revoke_session(conn, user.token)
    log_audit(conn, actor=user.username, user_id=user.id, action="auth.logout", ip=client_ip(request))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(user: CurrentUser = Depends(current_user)):
    return {"id": user.id, "username": user.username, "full_name": user.full_name, "role": user.role}


@router.post("/change-password")
def change_password(body: ChangePasswordIn, request: Request, user: CurrentUser = Depends(current_user),
                    conn: Connection = Depends(get_conn)):
    h = conn.execute(text("SELECT password_hash FROM users WHERE id = :id"), {"id": user.id}).scalar()
    if not verify_password(h, body.current_password):
        raise HTTPException(400, "Mevcut parola hatalı")
    try:
        validate_password_strength(body.new_password)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    conn.execute(text("UPDATE users SET password_hash = :h, password_changed_at = NOW(), updated_at = NOW() WHERE id = :id"),
                 {"h": hash_password(body.new_password), "id": user.id})
    revoke_user_sessions(conn, user.id, except_token=user.token)
    log_audit(conn, actor=user.username, user_id=user.id, action="auth.password_changed", ip=client_ip(request))
    return {"ok": True}
