"""Kimlik doğrulama: argon2 parola hash'i + veritabanında tutulan oturumlar.

Oturum belirteci rastgele 256 bit; veritabanında yalnızca SHA-256 özeti
saklanır (DB sızsa bile oturum çalınamaz). Çıkışta / parola değişiminde
oturumlar iptal edilir.
"""
from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import defaultdict, deque

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import text
from sqlalchemy.engine import Connection

SESSION_COOKIE = "th_session"
MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 15
MIN_PASSWORD_LENGTH = 12

_hasher = PasswordHasher()
# Kullanıcı bulunamadığında da aynı süre harcansın (kullanıcı adı keşfini zorlaştırır).
_DUMMY_HASH = _hasher.hash(secrets.token_hex(16))


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def validate_password_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Parola en az {MIN_PASSWORD_LENGTH} karakter olmalı")
    if password.strip().upper() in {"CHANGE_ME", "CHANGEME", "PASSWORD", "ADMIN"}:
        raise ValueError("Bu parola kullanılamaz")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(conn: Connection, user_id: int, ttl_hours: int, ip: str | None, user_agent: str | None) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(text("""
        INSERT INTO user_sessions(user_id, token_hash, expires_at, ip, user_agent)
        VALUES (:u, :h, NOW() + make_interval(hours => :ttl), :ip, :ua)
    """), {"u": user_id, "h": token_hash(token), "ttl": ttl_hours, "ip": ip, "ua": (user_agent or "")[:300]})
    return token


def resolve_session(conn: Connection, token: str) -> dict | None:
    user = conn.execute(text("""
        SELECT u.id, u.username, u.full_name, u.role, s.id AS session_id, s.last_seen_at
          FROM user_sessions s JOIN users u ON u.id = s.user_id
         WHERE s.token_hash = :h AND s.revoked_at IS NULL AND s.expires_at > NOW() AND u.is_active
    """), {"h": token_hash(token)}).mappings().first()
    if user is None:
        return None
    conn.execute(text("""UPDATE user_sessions SET last_seen_at = NOW()
                          WHERE id = :id AND last_seen_at < NOW() - INTERVAL '1 minute'"""),
                 {"id": user["session_id"]})
    return dict(user)


def revoke_session(conn: Connection, token: str) -> None:
    conn.execute(text("UPDATE user_sessions SET revoked_at = NOW() WHERE token_hash = :h AND revoked_at IS NULL"),
                 {"h": token_hash(token)})


def revoke_user_sessions(conn: Connection, user_id: int, except_token: str | None = None) -> None:
    conn.execute(text("""UPDATE user_sessions SET revoked_at = NOW()
                          WHERE user_id = :u AND revoked_at IS NULL AND token_hash <> :keep"""),
                 {"u": user_id, "keep": token_hash(except_token) if except_token else ""})


class IpRateLimiter:
    """Giriş denemeleri için basit kayan pencere sınırlayıcı (süreç içi).

    Hesap bazlı kilitleme veritabanında (users.failed_login_count) ayrıca
    yapılır; bu sınırlayıcı çok sayıda farklı kullanıcı adının denenmesine
    karşıdır.
    """

    def __init__(self, limit: int = 20, window_seconds: int = 300):
        self.limit, self.window = limit, window_seconds
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False
            q.append(now)
            return True


login_limiter = IpRateLimiter()


def bootstrap_admin(conn: Connection, username: str, password: str) -> str:
    """Kullanıcı tablosu boşsa ortam değişkenlerinden ilk admin'i oluşturur."""
    from .config import is_set
    if conn.execute(text("SELECT EXISTS (SELECT 1 FROM users)")).scalar():
        return "exists"
    if not (is_set(username) and is_set(password)):
        return "missing"
    try:
        validate_password_strength(password)
    except ValueError:
        return "weak"
    conn.execute(text("""
        INSERT INTO users(username, full_name, password_hash, role)
        VALUES (:u, 'Yönetici', :h, 'admin') ON CONFLICT (username) DO NOTHING
    """), {"u": username.strip(), "h": hash_password(password)})
    return "created"
