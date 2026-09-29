"""Kontrollü yayın: yetki, önizleme, tek kullanımlık onay, kapılar, idempotency, hazır olmayan ürün."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.connectors.trendyol import TrendyolConnector
from app.security import hash_password
from app.services import sync_service
from tests.test_suppliers import MAP_A, XML_A, _catalog, make_supplier, upload

H = {"X-Requested-With": "TrendHub"}


@pytest.fixture
def client(engine):
    from app.main import app
    with TestClient(app) as c:
        assert c.post("/api/auth/login", json={"username": "admin", "password": "Admin-Password-123"},
                      headers=H).status_code == 200
        yield c


def _ready_drafts(client):
    sid = make_supplier(client, "Çanta Bayim", MAP_A)
    upload(client, sid, XML_A)
    pids = _catalog(client, sid)
    for cat, tid in (("Çanta > Sırt Çantası", "1001"), ("Çanta > Omuz Çantası", "1002")):
        client.put("/api/category-mappings", json={"marketplace": "trendyol", "source_category": cat,
                                                   "target_category_id": tid}, headers=H)
    ids = client.post("/api/transfer/drafts", json={"product_ids": pids, "marketplaces": ["trendyol"]}, headers=H).json()["draft_ids"]
    client.post("/api/listing-drafts/prepare", json={"ids": ids}, headers=H)
    return ids


def _open_all_gates(engine, monkeypatch):
    """Test için tüm kapıları açar: sahte connector (ağ yok) + izinler."""
    calls = []
    monkeypatch.setattr(TrendyolConnector, "listing_publish_supported", True)
    monkeypatch.setattr(TrendyolConnector, "is_configured", lambda self: True)
    monkeypatch.setattr(TrendyolConnector, "publish_listing", lambda self, payload: calls.append(payload) or "BATCH-1")
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "connector_write_enabled", True)
    with engine.begin() as c:
        c.execute(text("""INSERT INTO marketplace_connections(marketplace_id, status, write_enabled)
                          SELECT id, 'connected', TRUE FROM marketplaces WHERE code = 'trendyol'"""))
        c.execute(text("UPDATE marketplaces SET last_check_ok = TRUE WHERE code = 'trendyol'"))
    return calls


def test_preview_lists_sendable_blocked_and_gates_without_sending(client, engine):
    ids = _ready_drafts(client)
    pv = client.post("/api/publish/preview", json={"marketplace": "trendyol", "draft_ids": ids}, headers=H).json()
    assert len(pv["sendable"]) == 2 and pv["summary"] == "2 ürün Trendyol mağazasına gönderilecek"
    bad = next(b for b in pv["blocked"] if b["barcode"] is None)
    assert bad["reasons"][0] == "Bu ürün yayınlanmaya hazır değil." and len(bad["reasons"]) > 1
    gates = {g["code"]: g for g in pv["gates"]}
    assert not gates["api_verified"]["ok"] and not gates["server_write"]["ok"] and not gates["store_write"]["ok"]
    assert pv["can_confirm"] is False and "GÖNDERİLMEZ" in pv["notice"]
    assert "CONNECTOR_WRITE" not in str(pv)                     # ortam değişkeni adı arayüze sızmaz
    with engine.begin() as c:
        h = c.execute(text("SELECT token_hash FROM publish_requests")).scalar()
        audit = str(c.execute(text("SELECT details FROM audit_logs WHERE action = 'publish.previewed'")).scalar())
    assert h != pv["token"] and pv["token"] not in audit       # anahtar yalnızca hash olarak saklanır


def test_confirm_with_closed_gates_records_blocked_and_sends_nothing(client, engine, monkeypatch):
    sent = []
    monkeypatch.setattr(TrendyolConnector, "publish_listing", lambda self, p: sent.append(p))
    ids = _ready_drafts(client)
    pv = client.post("/api/publish/preview", json={"marketplace": "trendyol", "draft_ids": ids}, headers=H).json()
    r = client.post("/api/publish/confirm", json={"token": pv["token"], "confirm": True}, headers=H).json()
    assert r["blocked"] == 2 and r["queued"] == 0 and r["sent_to_marketplace"] == 0 and "GÖNDERİLMEDİ" in r["message"]
    with engine.begin() as c:
        st = c.execute(text("SELECT DISTINCT status FROM listing_publications")).scalars().all()
        jobs = c.execute(text("SELECT COUNT(*) FROM sync_jobs WHERE job_type = 'listing.publish'")).scalar()
    assert st == ["blocked"] and jobs == 0 and sent == []
    # Onay anahtarı tek kullanımlık
    again = client.post("/api/publish/confirm", json={"token": pv["token"], "confirm": True}, headers=H)
    assert again.status_code == 409 and "zaten kullanıldı" in again.json()["detail"]


def test_only_admin_can_publish_and_explicit_confirm_required(client, engine):
    ids = _ready_drafts(client)
    with engine.begin() as c:
        c.execute(text("INSERT INTO users(username, password_hash, role) VALUES ('op', :h, 'operator')"),
                  {"h": hash_password("Operator-Password-123")})
    pv = client.post("/api/publish/preview", json={"marketplace": "trendyol", "draft_ids": ids}, headers=H).json()
    assert client.post("/api/publish/confirm", json={"token": pv["token"], "confirm": False}, headers=H).status_code == 422
    from app.main import app
    with TestClient(app) as op:
        op.post("/api/auth/login", json={"username": "op", "password": "Operator-Password-123"}, headers=H)
        assert op.post("/api/publish/preview", json={"marketplace": "trendyol", "draft_ids": ids}, headers=H).status_code == 403
        assert op.post("/api/publish/confirm", json={"token": pv["token"], "confirm": True}, headers=H).status_code == 403
    bad = client.post("/api/publish/confirm", json={"token": "x" * 40, "confirm": True}, headers=H)
    assert bad.status_code == 409 and "geçersiz" in bad.json()["detail"]


def test_expired_token_is_rejected(client, engine):
    ids = _ready_drafts(client)
    pv = client.post("/api/publish/preview", json={"marketplace": "trendyol", "draft_ids": ids}, headers=H).json()
    with engine.begin() as c:
        c.execute(text("UPDATE publish_requests SET expires_at = NOW() - INTERVAL '1 minute'"))
    r = client.post("/api/publish/confirm", json={"token": pv["token"], "confirm": True}, headers=H)
    assert r.status_code == 409 and "süresi doldu" in r.json()["detail"]


def test_open_gates_queue_once_and_worker_sends_once(client, engine, monkeypatch):
    ids = _ready_drafts(client)
    calls = _open_all_gates(engine, monkeypatch)
    pv = client.post("/api/publish/preview", json={"marketplace": "trendyol", "draft_ids": ids}, headers=H).json()
    assert pv["can_confirm"] is True
    r = client.post("/api/publish/confirm", json={"token": pv["token"], "confirm": True}, headers=H).json()
    assert r["queued"] == 2
    with engine.begin() as c:
        job = dict(c.execute(text("SELECT id, job_type, marketplace, payload FROM sync_jobs WHERE job_type = 'listing.publish'")).mappings().one())
    res = sync_service.execute(engine, job)
    assert res == {"sent": 2, "failed": 0, "blocked": 0} and len(calls) == 2
    # Aynı ürünler aynı içerikle yeniden seçilirse: önizlemede engellenir, onayda tekrar gönderilmez
    pv2 = client.post("/api/publish/preview", json={"marketplace": "trendyol", "draft_ids": ids}, headers=H).json()
    assert pv2["sendable"] == [] and any("zaten gönderildi" in " ".join(b["reasons"]) for b in pv2["blocked"])
    assert sync_service.execute(engine, job)["sent"] == 0 and len(calls) == 2
    with engine.begin() as c:
        assert c.execute(text("SELECT COUNT(*) FROM listing_publications WHERE status = 'sent'")).scalar() == 2
    hist = client.get("/api/publish/history").json()
    assert hist["total"] == 2 and hist["items"][0]["status"] == "sent"


def test_gate_closed_at_send_time_blocks_queued(client, engine, monkeypatch):
    ids = _ready_drafts(client)
    calls = _open_all_gates(engine, monkeypatch)
    pv = client.post("/api/publish/preview", json={"marketplace": "trendyol", "draft_ids": ids}, headers=H).json()
    client.post("/api/publish/confirm", json={"token": pv["token"], "confirm": True}, headers=H)
    with engine.begin() as c:
        c.execute(text("UPDATE marketplace_connections SET write_enabled = FALSE"))
        job = dict(c.execute(text("SELECT id, job_type, marketplace, payload FROM sync_jobs WHERE job_type = 'listing.publish'")).mappings().one())
    assert sync_service.execute(engine, job)["blocked"] == 2 and calls == []


def test_store_write_permission_cannot_be_enabled_without_verified_api(client, engine):
    r = client.put("/api/integrations/trendyol/write-permission", json={"enabled": True}, headers=H)
    assert r.status_code == 409 and "doğrulanmadığı" in r.json()["detail"]
    assert client.put("/api/integrations/trendyol/write-permission", json={"enabled": False}, headers=H).status_code == 200
