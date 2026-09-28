"""Web'den mağaza bağlantısı: şifreli saklama, maskeleme, test, kaldırma, yetki, worker kullanımı."""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import Settings
from app.connectors.base import ConnectionCheck
from app.connectors.hepsiburada import HepsiburadaConnector
from app.connectors.registry import get_connector
from app.connectors.trendyol import TrendyolConnector
from app.security import hash_password
from app.services import sync_service

H = {"X-Requested-With": "TrendHub"}
KEY, SECRET = "ty-key-ABC123456", "ty-secret-XYZ987654"


@pytest.fixture
def client(engine, monkeypatch):
    seen = []

    def fake_test(self):  # ağa çıkmadan: formdaki bilgilerin connector'a ulaştığını doğrular
        seen.append((self.settings.trendyol_seller_id, self.settings.trendyol_api_key, self.settings.trendyol_api_secret))
        ok = self.settings.trendyol_api_secret == SECRET
        return ConnectionCheck(ok, "Bağlantı başarılı (salt okunur)" if ok else "Yetkisiz erişim (HTTP 401).")
    monkeypatch.setattr(TrendyolConnector, "test_connection", fake_test)
    # Testlerde gerçek pazaryeri API'sine asla istek gitmez
    monkeypatch.setattr(HepsiburadaConnector, "test_connection", lambda self: ConnectionCheck(True, "ok"))
    from app.connectors.amazon_tr import AmazonTrConnector
    monkeypatch.setattr(AmazonTrConnector, "test_connection", lambda self: ConnectionCheck(True, "ok"))
    from app.main import app
    with TestClient(app) as c:
        c.post("/api/auth/login", json={"username": "admin", "password": "Admin-Password-123"}, headers=H)
        c.seen = seen
        yield c


def test_form_fields_are_friendly_and_hide_env_names(client):
    d = client.get("/api/integrations/trendyol/connection").json()
    assert d["source"] == "none" and [f["label"] for f in d["fields"]] == ["Satıcı ID (Supplier ID)", "API Key", "API Secret"]
    hb = client.get("/api/integrations/hepsiburada/connection").json()
    assert [f["label"] for f in hb["fields"]] == ["Merchant ID", "Entegrasyon kullanıcı adı", "Entegrasyon şifresi", "User-Agent"]
    amz = client.get("/api/integrations/amazon_tr/connection").json()
    mp = next(f for f in amz["fields"] if f["key"] == "amazon_sp_marketplace_id")
    assert mp["default"] == "A33AVAJ2PDY3EV" and not mp["required"]
    assert "TRENDYOL_API" not in json.dumps(d)


def test_save_encrypts_masks_tests_and_audits(client, engine):
    r = client.put("/api/integrations/trendyol/connection", json={"values": {
        "trendyol_seller_id": "123456", "trendyol_api_key": KEY, "trendyol_api_secret": SECRET}}, headers=H).json()
    assert r["ok"] and r["saved"] and "doğrulandı" in r["message"]
    with engine.begin() as c:
        row = c.execute(text("SELECT settings, secrets_enc, status FROM marketplace_connections")).one()
        audit = " ".join(str(x) for x in c.execute(text("SELECT details FROM audit_logs")).scalars())
    assert row[0] == {"trendyol_seller_id": "123456"} and row[2] == "connected"
    assert KEY not in row[1] and SECRET not in row[1]
    assert KEY not in audit and SECRET not in audit and "bağlantı bilgileri güncellendi" in audit
    v = client.get("/api/integrations/trendyol/connection").json()
    assert v["source"] == "panel" and v["status"] == "connected"
    body = json.dumps(v) + client.get("/api/integrations").text + client.get("/api/dashboard").text
    assert KEY not in body and SECRET not in body
    assert {f["key"]: f.get("is_set") for f in v["fields"]}["trendyol_api_secret"] is True
    assert "value" not in next(f for f in v["fields"] if f["key"] == "trendyol_api_secret")
    # Kayıtlı secret boş gönderilirse korunur (yeni değer girmeden değişmez)
    r = client.put("/api/integrations/trendyol/connection", json={"values": {"trendyol_seller_id": "654321"}}, headers=H).json()
    assert r["ok"] and client.seen[-1] == ("654321", KEY, SECRET)
    # Worker/registry panel bilgilerini kullanır
    c = get_connector("trendyol", Settings(database_url="postgresql://x@y/z"))
    assert c.is_configured() and c.settings.trendyol_api_secret == SECRET
    ints = {i["code"]: i for i in client.get("/api/integrations").json()["items"]}
    assert ints["trendyol"]["source"] == "panel" and ints["trendyol"]["state"] == "connected"


def test_test_with_unsaved_values_does_not_save_and_bad_secret_fails(client, engine):
    r = client.post("/api/integrations/trendyol/connection/test", json={"values": {
        "trendyol_seller_id": "1", "trendyol_api_key": KEY, "trendyol_api_secret": "yanlis"}}, headers=H).json()
    assert r["ok"] is False and "doğrulanamadı" in r["message"] and "HTTP 401" in r["message"]
    with engine.begin() as c:
        assert c.execute(text("SELECT COUNT(*) FROM marketplace_connections")).scalar() == 0
    r = client.put("/api/integrations/trendyol/connection", json={"values": {"trendyol_seller_id": "1"}}, headers=H)
    assert r.status_code == 422 and "API Key" in r.json()["detail"]
    r = client.put("/api/integrations/trendyol/connection", json={"values": {"bilinmeyen": "x"}}, headers=H)
    assert r.status_code == 422


def test_remove_connection_overrides_server_env_and_keeps_data(client, engine, monkeypatch):
    from app.config import get_settings
    for k, v in {"trendyol_seller_id": "9", "trendyol_api_key": "env-k-1234", "trendyol_api_secret": "env-s-1234"}.items():
        monkeypatch.setattr(get_settings(), k, v)
    assert client.get("/api/integrations/trendyol/connection").json()["source"] == "server"
    assert get_connector("trendyol").is_configured()
    assert client.delete("/api/integrations/trendyol/connection", headers=H).json()["ok"]
    assert not get_connector("trendyol").is_configured()      # panelde kaldırıldı -> bağlı değil
    v = client.get("/api/integrations/trendyol/connection").json()
    assert v["source"] == "removed" and not any(f.get("is_set") for f in v["fields"] if f["secret"])
    with engine.begin() as c:
        assert c.execute(text("SELECT secrets_enc FROM marketplace_connections")).scalar() is None
        assert "bağlantısı kaldırıldı" in str(c.execute(text(
            "SELECT details FROM audit_logs WHERE action = 'integration.connection_removed'")).scalar())


def test_only_admin_manages_connections(client, engine):
    with engine.begin() as c:
        c.execute(text("""INSERT INTO users(username, password_hash, role) VALUES ('op', :h, 'operator'),
                          ('vw', :h, 'viewer'), ('mh', :h, 'accountant')"""), {"h": hash_password("Strong-Password-1")})
    for u in ("op", "vw", "mh"):
        client.post("/api/auth/logout", headers=H)
        assert client.post("/api/auth/login", json={"username": u, "password": "Strong-Password-1"}, headers=H).status_code == 200
        assert client.get("/api/integrations/trendyol/connection").status_code == 403
        assert client.put("/api/integrations/trendyol/connection", json={"values": {}}, headers=H).status_code == 403
        assert client.post("/api/integrations/trendyol/connection/test", json={"values": {}}, headers=H).status_code == 403
        assert client.delete("/api/integrations/trendyol/connection", headers=H).status_code == 403


def test_scheduler_uses_panel_credentials(client, conn):
    client.put("/api/integrations/hepsiburada/connection", json={"values": {
        "hepsiburada_merchant_id": "m-1", "hepsiburada_username": "u_dev", "hepsiburada_password": "pw-12345"}}, headers=H)
    created = sync_service.schedule_due_jobs(conn, 15, Settings(database_url="postgresql://x@y/z"))
    planned = {tuple(r) for r in conn.execute(text("SELECT marketplace, job_type FROM sync_jobs"))}
    assert ("hepsiburada", "orders.sync") in planned and created
    hb = get_connector("hepsiburada")
    assert isinstance(hb, HepsiburadaConnector) and hb.settings.hepsiburada_password == "pw-12345"
