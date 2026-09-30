"""Müşteri hesabı: kayıt, giriş/çıkış, oturum, adresler, sipariş geçmişi, şifre sıfırlama.

Güvenlik:
  * Parolalar argon2 ile özetlenir (app.security.hash_password); düz metin hiçbir yerde tutulmaz.
  * Oturum çerezi `tc_hesap`: HttpOnly, SameSite=Lax, HTTPS'te Secure. Veritabanında yalnızca SHA-256 özeti.
  * 5 hatalı girişte hesap 15 dk kilitlenir; e-posta kayıtlı mı bilgisi hata mesajlarından sızdırılmaz.
  * Şifre sıfırlama anahtarı tek kullanımlık, 1 saat geçerli, veritabanında özet olarak saklanır; e-posta
    sağlayıcısı (SMTP) yapılandırılmamışsa sıfırlama e-postası gönderilemez ve kullanıcıya iletişim bilgisi gösterilir.
  * Sipariş geçmişi yalnızca hesaba bağlı (customer_id) siparişleri gösterir; aynı e-postayla verilmiş misafir
    siparişleri, e-posta sahipliği doğrulanmadığı için otomatik bağlanmaz.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..security import hash_password, token_hash, verify_password
from . import checkout

COOKIE = "tc_hesap"
SESSION_DAYS = 30
RESET_MINUTES = 60
MAX_FAILED = 5
LOCK_MINUTES = 15
MIN_PASSWORD = 8
MAX_ADDRESSES = 10


class AccountError(ValueError):
    def __init__(self, message: str, fields: dict | None = None, status: int = 422):
        super().__init__(message)
        self.fields = fields or {}
        self.status = status


def _password_ok(pw: str) -> str:
    if len(pw) < MIN_PASSWORD or len(pw) > 200:
        raise ValueError(f"Şifre en az {MIN_PASSWORD} karakter olmalı")
    if not (re.search(r"[A-Za-zÇĞİÖŞÜçğıöşü]", pw) and re.search(r"\d", pw)):
        raise ValueError("Şifre en az bir harf ve bir rakam içermeli")
    return pw


class RegisterIn(BaseModel):
    full_name: str = Field(min_length=3, max_length=120)
    email: str = Field(max_length=254)
    phone: str | None = Field(None, max_length=20)
    password: str = Field(max_length=200)
    accept_kvkk: bool = False
    marketing: bool = False

    _name = field_validator("full_name")(lambda cls, v: checkout.valid_name(v))
    _email = field_validator("email")(lambda cls, v: checkout.valid_email(v))
    _pw = field_validator("password")(lambda cls, v: _password_ok(v))
    _phone = field_validator("phone")(lambda cls, v: checkout.valid_phone(v) if v else None)


class LoginIn(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)


class ForgotIn(BaseModel):
    email: str = Field(max_length=254)


class ResetIn(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    password: str = Field(max_length=200)
    _pw = field_validator("password")(lambda cls, v: _password_ok(v))


class ChangePasswordIn(BaseModel):
    current_password: str = Field(max_length=200)
    password: str = Field(max_length=200)
    _pw = field_validator("password")(lambda cls, v: _password_ok(v))


class ProfileIn(BaseModel):
    full_name: str = Field(min_length=3, max_length=120)
    phone: str | None = Field(None, max_length=20)
    marketing: bool = False
    _name = field_validator("full_name")(lambda cls, v: checkout.valid_name(v))
    _phone = field_validator("phone")(lambda cls, v: checkout.valid_phone(v) if v else None)


class AddressIn(BaseModel):
    title: str = Field("Adresim", min_length=1, max_length=40)
    full_name: str = Field(min_length=3, max_length=120)
    phone: str = Field(max_length=20)
    city: str
    district: str = Field(min_length=2, max_length=80)
    address: str = Field(min_length=10, max_length=500)
    postal_code: str | None = Field(None, max_length=5)
    is_default: bool = False

    _strip = field_validator("title", "district", "address")(lambda cls, v: checkout.norm_space(v))
    _name = field_validator("full_name")(lambda cls, v: checkout.valid_name(v))
    _phone = field_validator("phone")(lambda cls, v: checkout.valid_phone(v))
    _city = field_validator("city")(lambda cls, v: checkout.valid_city(v))
    _zip = field_validator("postal_code")(lambda cls, v: checkout.valid_zip(v))


# ------------------------------------------------------------------ hesap
def register(conn: Connection, data: RegisterIn) -> int:
    if not data.accept_kvkk:
        raise AccountError("Devam etmek için onay gerekli.", {"accept_kvkk": "KVKK aydınlatma metnini onaylayın"})
    if conn.execute(text("SELECT 1 FROM storefront_customers WHERE lower(email) = lower(:e)"), {"e": data.email}).first():
        # Kayıtlı e-posta: hesabın varlığını doğrulamamak için genel mesaj.
        raise AccountError("Bu e-posta adresiyle hesap açılamıyor. Hesabınız varsa giriş yapın veya şifrenizi yenileyin.",
                           {"email": "Bu e-posta ile giriş yapmayı deneyin"})
    return conn.execute(text("""
        INSERT INTO storefront_customers(email, password_hash, full_name, phone, kvkk_consent_at, marketing_consent_at)
        VALUES (:e, :h, :n, :p, NOW(), CASE WHEN :m THEN NOW() END) RETURNING id"""),
        {"e": data.email, "h": hash_password(data.password), "n": data.full_name, "p": data.phone, "m": data.marketing}).scalar()


def authenticate(conn: Connection, email: str, password: str) -> tuple[int | None, str | None]:
    """(müşteri id, hata mesajı). Hatalı deneme sayacı aynı işlemde yazılır; çağıran işlemi commit etmelidir."""
    email = (email or "").strip().lower()
    c = conn.execute(text("""SELECT id, password_hash, is_active, failed_login_count, locked_until FROM storefront_customers
                             WHERE lower(email) = :e FOR UPDATE"""), {"e": email}).mappings().first()
    if c and c["locked_until"] and c["locked_until"] > datetime.now(timezone.utc):
        verify_password(None, password)
        return None, "Çok fazla hatalı deneme. Lütfen birkaç dakika sonra tekrar deneyin."
    ok = verify_password(c["password_hash"] if c else None, password)
    if not c or not ok or not c["is_active"]:
        if c:
            fails = c["failed_login_count"] + 1
            locked = fails >= MAX_FAILED
            conn.execute(text("""UPDATE storefront_customers SET failed_login_count = :f,
                                 locked_until = CASE WHEN :locked THEN NOW() + make_interval(mins => :lock) END
                                 WHERE id = :id"""), {"f": 0 if locked else fails, "locked": locked, "lock": LOCK_MINUTES,
                                                     "id": c["id"]})
        return None, "E-posta veya şifre hatalı."
    conn.execute(text("""UPDATE storefront_customers SET failed_login_count = 0, locked_until = NULL, last_login_at = NOW()
                         WHERE id = :id"""), {"id": c["id"]})
    return c["id"], None


def create_session(conn: Connection, customer_id: int) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(text("""INSERT INTO storefront_customer_sessions(customer_id, token_hash, expires_at)
                         VALUES (:c, :h, NOW() + make_interval(days => :d))"""),
                 {"c": customer_id, "h": token_hash(token), "d": SESSION_DAYS})
    return token


def resolve(conn: Connection, token: str | None) -> dict | None:
    if not token or len(token) > 200:
        return None
    c = conn.execute(text("""
        SELECT c.id, c.email, c.full_name, c.phone, c.marketing_consent_at, c.created_at
          FROM storefront_customer_sessions s JOIN storefront_customers c ON c.id = s.customer_id
         WHERE s.token_hash = :h AND s.revoked_at IS NULL AND s.expires_at > NOW() AND c.is_active"""),
        {"h": token_hash(token)}).mappings().first()
    return dict(c) if c else None


def logout(conn: Connection, token: str | None) -> None:
    if token:
        conn.execute(text("UPDATE storefront_customer_sessions SET revoked_at = NOW() WHERE token_hash = :h AND revoked_at IS NULL"),
                     {"h": token_hash(token)})


def revoke_all(conn: Connection, customer_id: int) -> None:
    conn.execute(text("UPDATE storefront_customer_sessions SET revoked_at = NOW() WHERE customer_id = :c AND revoked_at IS NULL"),
                 {"c": customer_id})


def update_profile(conn: Connection, customer_id: int, data: ProfileIn) -> None:
    conn.execute(text("""UPDATE storefront_customers SET full_name = :n, phone = :p, updated_at = NOW(),
                         marketing_consent_at = CASE WHEN :m THEN COALESCE(marketing_consent_at, NOW()) END WHERE id = :id"""),
                 {"n": data.full_name, "p": data.phone, "m": data.marketing, "id": customer_id})


def change_password(conn: Connection, customer_id: int, data: ChangePasswordIn, keep_token: str | None) -> None:
    h = conn.execute(text("SELECT password_hash FROM storefront_customers WHERE id = :id"), {"id": customer_id}).scalar()
    if not verify_password(h, data.current_password):
        raise AccountError("Mevcut şifre hatalı.", {"current_password": "Mevcut şifre hatalı"})
    conn.execute(text("UPDATE storefront_customers SET password_hash = :h, updated_at = NOW() WHERE id = :id"),
                 {"h": hash_password(data.password), "id": customer_id})
    conn.execute(text("""UPDATE storefront_customer_sessions SET revoked_at = NOW()
                         WHERE customer_id = :c AND revoked_at IS NULL AND token_hash <> :keep"""),
                 {"c": customer_id, "keep": token_hash(keep_token or "")})


# ------------------------------------------------------------------ şifre sıfırlama
def create_reset(conn: Connection, email: str) -> tuple[dict, str] | None:
    """Hesap varsa sıfırlama anahtarı üretir. Yoksa None (çağıran her durumda aynı yanıtı verir)."""
    c = conn.execute(text("SELECT id, email, full_name FROM storefront_customers WHERE lower(email) = lower(:e) AND is_active"),
                     {"e": (email or "").strip()}).mappings().first()
    if c is None:
        return None
    recent = conn.execute(text("""SELECT COUNT(*) FROM storefront_password_resets
                                  WHERE customer_id = :c AND created_at > NOW() - INTERVAL '1 hour'"""), {"c": c["id"]}).scalar()
    if recent >= 3:
        return None
    token = secrets.token_urlsafe(32)
    conn.execute(text("""INSERT INTO storefront_password_resets(customer_id, token_hash, expires_at)
                         VALUES (:c, :h, NOW() + make_interval(mins => :m))"""),
                 {"c": c["id"], "h": token_hash(token), "m": RESET_MINUTES})
    return dict(c), token


def reset_password(conn: Connection, data: ResetIn) -> int:
    r = conn.execute(text("""SELECT id, customer_id FROM storefront_password_resets
                             WHERE token_hash = :h AND used_at IS NULL AND expires_at > NOW() FOR UPDATE"""),
                     {"h": token_hash(data.token)}).mappings().first()
    if r is None:
        raise AccountError("Bağlantının süresi dolmuş veya daha önce kullanılmış. Yeni bir bağlantı isteyin.")
    conn.execute(text("UPDATE storefront_password_resets SET used_at = NOW() WHERE customer_id = :c AND used_at IS NULL"),
                 {"c": r["customer_id"]})
    conn.execute(text("""UPDATE storefront_customers SET password_hash = :h, failed_login_count = 0, locked_until = NULL,
                         updated_at = NOW() WHERE id = :id"""), {"h": hash_password(data.password), "id": r["customer_id"]})
    revoke_all(conn, r["customer_id"])
    return r["customer_id"]


# ------------------------------------------------------------------ adresler
def addresses(conn: Connection, customer_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(text("""
        SELECT id, title, full_name, phone, city, district, address, postal_code, is_default
          FROM storefront_customer_addresses WHERE customer_id = :c ORDER BY is_default DESC, id"""), {"c": customer_id}).mappings()]


def save_address(conn: Connection, customer_id: int, data: AddressIn, address_id: int | None = None) -> int:
    count = conn.execute(text("SELECT COUNT(*) FROM storefront_customer_addresses WHERE customer_id = :c"),
                         {"c": customer_id}).scalar()
    make_default = data.is_default or count == 0 or (address_id is None and count == 0)
    if make_default:
        conn.execute(text("UPDATE storefront_customer_addresses SET is_default = FALSE WHERE customer_id = :c"), {"c": customer_id})
    params = {**data.model_dump(), "is_default": make_default, "c": customer_id}
    if address_id is None:
        if count >= MAX_ADDRESSES:
            raise AccountError(f"En fazla {MAX_ADDRESSES} adres kaydedebilirsiniz.")
        return conn.execute(text("""
            INSERT INTO storefront_customer_addresses(customer_id, title, full_name, phone, city, district, address,
                                                      postal_code, is_default)
            VALUES (:c, :title, :full_name, :phone, :city, :district, :address, :postal_code, :is_default) RETURNING id"""),
            params).scalar()
    updated = conn.execute(text("""
        UPDATE storefront_customer_addresses SET title = :title, full_name = :full_name, phone = :phone, city = :city,
               district = :district, address = :address, postal_code = :postal_code,
               is_default = is_default OR :is_default, updated_at = NOW()
         WHERE id = :id AND customer_id = :c"""), {**params, "id": address_id}).rowcount
    if not updated:
        raise AccountError("Adres bulunamadı.", status=404)
    return address_id


def delete_address(conn: Connection, customer_id: int, address_id: int) -> None:
    was_default = conn.execute(text("""DELETE FROM storefront_customer_addresses WHERE id = :id AND customer_id = :c
                                       RETURNING is_default"""), {"id": address_id, "c": customer_id}).scalar()
    if was_default is None:
        raise AccountError("Adres bulunamadı.", status=404)
    if was_default:
        conn.execute(text("""UPDATE storefront_customer_addresses SET is_default = TRUE
                             WHERE id = (SELECT MIN(id) FROM storefront_customer_addresses WHERE customer_id = :c)"""),
                     {"c": customer_id})


# ------------------------------------------------------------------ siparişler
def orders(conn: Connection, customer_id: int, limit: int = 50) -> list[dict]:
    out = []
    for r in conn.execute(text("""
        SELECT so.public_code, so.status, so.payment_method, so.total, so.created_at, so.lines, o.internal_status
          FROM storefront_orders so LEFT JOIN orders o ON o.id = so.order_id
         WHERE so.customer_id = :c ORDER BY so.created_at DESC LIMIT :l"""), {"c": customer_id, "l": limit}).mappings():
        d = dict(r)
        d["item_count"] = sum(int(line.get("quantity", 0)) for line in d["lines"] or [])
        d["image"] = next((line.get("image") for line in d["lines"] or [] if line.get("image")), None)
        out.append(d)
    return out


def order(conn: Connection, customer_id: int, code: str) -> dict | None:
    r = conn.execute(text("""
        SELECT so.*, o.internal_status FROM storefront_orders so LEFT JOIN orders o ON o.id = so.order_id
         WHERE so.public_code = :code AND so.customer_id = :c"""), {"code": code, "c": customer_id}).mappings().first()
    return dict(r) if r else None


def session_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
