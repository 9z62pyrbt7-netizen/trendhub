"""FastAPI bağımlılıkları: oturum ve rol kontrolü."""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from sqlalchemy.engine import Connection

from .db import get_conn
from .security import SESSION_COOKIE, resolve_session

ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2}


@dataclass
class CurrentUser:
    id: int
    username: str
    full_name: str | None
    role: str
    token: str

    def has_role(self, minimum: str) -> bool:
        return ROLE_RANK.get(self.role, -1) >= ROLE_RANK[minimum]


def _token_from_request(request: Request) -> str | None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        return token
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


def client_ip(request: Request) -> str | None:
    # nginx X-Real-IP başlığını ayarlar; doğrudan erişimde soket adresi.
    return request.headers.get("X-Real-IP") or (request.client.host if request.client else None)


def current_user(request: Request, conn: Connection = Depends(get_conn)) -> CurrentUser:
    token = _token_from_request(request)
    if not token:
        raise HTTPException(401, "Oturum açmanız gerekiyor")
    user = resolve_session(conn, token)
    if user is None:
        raise HTTPException(401, "Oturum süresi doldu veya geçersiz")
    return CurrentUser(id=user["id"], username=user["username"], full_name=user["full_name"],
                       role=user["role"], token=token)


def require(minimum: str):
    def dep(user: CurrentUser = Depends(current_user)) -> CurrentUser:
        if not user.has_role(minimum):
            raise HTTPException(403, "Bu işlem için yetkiniz yok")
        return user
    return dep


viewer = require("viewer")
operator = require("operator")
admin = require("admin")
