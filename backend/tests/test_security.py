"""Güvenlik: çerez bayrakları, log maskeleme, credential sızıntısı, güvenlik başlıkları."""
import logging

import pytest

from app.config import Settings, cookie_secure_for, get_settings
from app.logging_setup import RedactSecretsFilter, secret_values

H = {"X-Requested-With": "TrendHub"}
SECRETS = {"trendyol_api_key": "TY-KEY-cok-gizli-111", "trendyol_api_secret": "TY-SECRET-cok-gizli-222",
           "amazon_sp_client_secret": "AMZ-SECRET-333", "amazon_sp_refresh_token": "Atzr|REFRESH-444",
           "hepsiburada_password": "HB-PASS-555"}


def _login(client, extra_headers=None):
    return client.post("/api/auth/login", json={"username": "admin", "password": "Admin-Password-123"},
                       headers={**H, **(extra_headers or {})})


@pytest.mark.parametrize("mode,scheme,expected", [
    ("auto", "http", False), ("auto", "https", True), ("true", "http", True), ("false", "https", False)])
def test_cookie_secure_modes(mode, scheme, expected):
    assert cookie_secure_for(Settings(database_url="postgresql://x@y/z", cookie_secure=mode), scheme) is expected


def test_login_cookie_flags_follow_forwarded_proto(client_factory, monkeypatch):
    client, _ = client_factory
    monkeypatch.setattr(get_settings(), "cookie_secure", "auto")
    plain = _login(client).headers["set-cookie"].lower()
    assert "httponly" in plain and "samesite=strict" in plain and "secure" not in plain.replace("samesite", "")
    tls = _login(client, {"X-Forwarded-Proto": "https"}).headers["set-cookie"].lower()
    assert "; secure" in tls


def test_log_filter_masks_secret_values_in_messages_args_and_tracebacks():
    s = Settings(database_url="postgresql://u:DB-PASS-999@db/x", **SECRETS)
    values = secret_values(s)
    assert "DB-PASS-999" in values and "TY-SECRET-cok-gizli-222" in values
    f = RedactSecretsFilter(values)
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "key=%s secret=%s", ("TY-KEY-cok-gizli-111",
                                                                                "TY-SECRET-cok-gizli-222"), None)
    f.filter(rec)
    assert rec.getMessage() == "key=*** secret=***"
    try:
        raise RuntimeError("bağlantı hatası Atzr|REFRESH-444")
    except RuntimeError:
        import sys
        rec2 = logging.LogRecord("t", logging.ERROR, __file__, 1, "hata", None, sys.exc_info())
    f.filter(rec2)
    assert "REFRESH-444" not in rec2.exc_text and "***" in rec2.exc_text


def test_no_api_response_contains_credential_values(client_factory, engine, monkeypatch):
    """Tüm GET uç noktaları gezilir; hiçbir yanıtta credential değeri geçmemeli."""
    client, login = client_factory
    s = get_settings()
    for k, v in {**SECRETS, "trendyol_seller_id": "12345", "amazon_sp_seller_id": "A1S"}.items():
        monkeypatch.setattr(s, k, v)
    login("admin", "Admin-Password-123")
    from app.main import app
    paths = sorted({r.path for r in app.routes if "GET" in getattr(r, "methods", set())
                    and r.path.startswith("/api/") and "{" not in r.path})
    assert len(paths) >= 20
    for path in paths:
        resp = client.get(path)
        assert resp.status_code in (200, 403), (path, resp.status_code, resp.text[:200])
        body = resp.text
        for secret in SECRETS.values():
            assert secret not in body, f"{path} yanıtında credential sızıntısı"


def test_security_headers_on_api_and_sql_injection_is_inert(client_factory):
    client, login = client_factory
    login("admin", "Admin-Password-123")
    r = client.get("/api/orders", params={"q": "' OR 1=1; DROP TABLE orders; --", "sort": "id; DROP TABLE x"})
    assert r.status_code == 200 and r.json()["total"] == 0
    assert r.headers["cache-control"] == "no-store"
    # tablo hâlâ yerinde
    assert client.get("/api/orders").status_code == 200
    # bilinmeyen statü parametresi reddedilir (whitelist)
    assert client.get("/api/orders", params={"status": "x' OR '1'='1"}).status_code == 422
