"""Tedarikçi connector sınıfları: kayıt defteri, JSON API sayfalama, bağlantı testi."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.suppliers import fetch as fetcher
from app.suppliers.connectors import (CONNECTORS, JsonApiConnector, ManualUploadConnector, SupplierConnectorError,
                                      XmlFeedConnector, get_supplier_connector)
from app.suppliers.fields import PRESETS
from app.suppliers.secrets import encrypt

H = {"X-Requested-With": "TrendHub"}


def con(kind, url="https://api.tedarikci.example/v1/products?key=abc", **options):
    return {"integration_type": kind, "source_url_enc": encrypt(url), "auth_type": "none", "options": options}


def test_registry_is_generic_and_canta_bayim_is_only_a_preset():
    assert set(CONNECTORS) == {"xml", "api", "csv", "manual"}
    assert isinstance(get_supplier_connector(con("xml")), XmlFeedConnector)
    assert PRESETS["canta_bayim"]["integration_type"] == "xml"
    assert not any("canta" in name.lower() for name in CONNECTORS)
    with pytest.raises(SupplierConnectorError):
        get_supplier_connector({"integration_type": "ftp"})
    with pytest.raises(SupplierConnectorError):
        ManualUploadConnector({"integration_type": "manual"}).fetch()


def test_json_api_pagination_until_short_page():
    seen = []

    def handler(req):
        page = int(req.url.params["page"])
        seen.append((page, req.url.params["per_page"], req.url.params["key"]))
        n = 2 if page < 3 else 1
        return httpx.Response(200, json={"data": [{"sku": f"P{page}-{i}"} for i in range(n)]})
    c = JsonApiConnector(con("api", page_param="page", page_size_param="per_page", page_size=2, max_pages=10),
                         transport=httpx.MockTransport(handler))
    r = c.fetch()
    assert [x["sku"] for x in r.records] == ["P1-0", "P1-1", "P2-0", "P2-1", "P3-0"]
    assert r.pages == 3 and seen == [(1, "2", "abc"), (2, "2", "abc"), (3, "2", "abc")]


def test_json_api_pagination_stops_when_api_ignores_page_param():
    body = json.dumps({"items": [{"sku": "A"}, {"sku": "B"}]}).encode()
    c = JsonApiConnector(con("api", page_param="page", max_pages=50),
                         transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body)))
    r = c.fetch()
    assert len(r.records) == 2 and r.pages == 2


def test_connection_check_does_not_write_and_reports_problems():
    ok = XmlFeedConnector(con("xml", url="https://x.example/f.xml"),
                          transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"<r><p><a>1</a></p><p><a>2</a></p></r>")))
    chk = ok.test_connection()
    assert chk.ok and chk.records == 2 and chk.record_path == "p"
    empty = XmlFeedConnector(con("xml"), transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"<r></r>")))
    assert empty.test_connection().ok is False
    denied = XmlFeedConnector(con("xml"), transport=httpx.MockTransport(lambda r: httpx.Response(403)))
    assert "reddetti" in denied.test_connection().message
    assert ManualUploadConnector({"integration_type": "manual"}).test_connection().ok is False
    assert XmlFeedConnector({"integration_type": "xml"}).test_connection().ok is False   # URL yok


def test_test_endpoint_and_api_pagination_options_saved(engine, monkeypatch):
    from app.main import app
    with TestClient(app) as c:
        c.post("/api/auth/login", json={"username": "admin", "password": "Admin-Password-123"}, headers=H)
        r = c.post("/api/suppliers", json={"name": "API Tedarikçi", "connection": {
            "integration_type": "api", "source_url": "https://api.tedarikci.example/v1/products",
            "auth_type": "bearer", "secret": "tok-123456", "page_param": "page", "page_size_param": "limit",
            "page_size": 100, "max_pages": 20}}, headers=H)
        sid = r.json()["id"]
        d = c.get(f"/api/suppliers/{sid}").json()
        assert d["connection"]["options"] == {"page_param": "page", "page_size_param": "limit", "page_size": 100,
                                              "max_pages": 20}
        auth = []
        real = fetcher.fetch

        def fake(url, a=None, transport=None):
            auth.append(a.secret)
            return real(url, a, transport=httpx.MockTransport(
                lambda req: httpx.Response(200, json={"products": [{"code": "X1"}]})))
        monkeypatch.setattr(fetcher, "fetch", fake)
        t = c.post(f"/api/suppliers/{sid}/test", headers=H).json()
        assert t["ok"] and t["records"] == 1 and auth == ["tok-123456"]
        assert "tok-123456" not in json.dumps(t)
        assert c.post("/api/suppliers/99999/test", headers=H).status_code == 404
        meta = c.get("/api/supplier-meta").json()
        assert {x["type"] for x in meta["connectors"]} == {"xml", "api", "csv", "manual"}
