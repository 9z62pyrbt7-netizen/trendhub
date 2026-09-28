"""Tedarikçi connector'ları: her entegrasyon türü bir sınıftır, tedarikçiye özel kod yoktur.

    xml     XmlFeedConnector      URL'den XML besleme (tek dosya)
    api     JsonApiConnector      JSON API; isteğe bağlı sayfalama (page/limit parametreleri)
    csv     CsvFeedConnector      URL'den CSV
    manual  ManualUploadConnector yalnızca panelden dosya yükleme

Yeni bir tür eklemek: `SupplierConnector`'dan türet, `CONNECTORS` sözlüğüne kaydet.
Tedarikçiye özgü tek şey `supplier_connections` satırı (URL, kimlik doğrulama, kayıt yolu,
seçenekler) ve `supplier_field_mappings` eşleştirmesidir. Çanta Bayim yalnızca XmlFeedConnector
kullanan bir şablondur (`fields.PRESETS`).

Tüm connector'lar SALT OKUNURDUR: yalnızca GET yapar, tedarikçi sistemine yazmaz.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..connectors.base import ConnectorError
from . import fetch as fetch_module
from .parsing import ParseError, parse
from .secrets import SecretStoreError, decrypt, read_env_secret

MAX_API_PAGES = 500


class SupplierConnectorError(ConnectorError):
    retryable = False


@dataclass
class FetchResult:
    records: list[dict]
    record_path: str | None
    pages: int = 1
    bytes: int = 0


@dataclass
class SupplierCheck:
    ok: bool
    message: str
    records: int = 0
    record_path: str | None = None


class SupplierConnector:
    type = "base"
    label = ""
    remote = True
    description = ""

    def __init__(self, connection: dict | None, transport=None):
        self.con = connection or {}
        self.options = self.con.get("options") or {}
        self.transport = transport

    # -- kaynak ----------------------------------------------------------------
    def _url(self) -> str:
        try:
            url = decrypt(self.con.get("source_url_enc"))
        except SecretStoreError as exc:
            raise SupplierConnectorError(str(exc)) from None
        if not url:
            raise SupplierConnectorError("Tedarikçi kaynak adresi (XML/API/CSV URL) tanımlı değil.")
        return url

    def _auth(self) -> fetch_module.SourceAuth:
        try:
            secret = (decrypt(self.con.get("secret_enc")) if self.con.get("secret_enc")
                      else read_env_secret(self.con.get("secret_env")))
        except SecretStoreError as exc:
            raise SupplierConnectorError(str(exc)) from None
        return fetch_module.SourceAuth(auth_type=self.con.get("auth_type") or "none",
                                       username=self.con.get("auth_username"),
                                       param_name=self.con.get("auth_param_name"), secret=secret)

    def _download(self, url: str) -> bytes:
        # Modül üzerinden çağrılır: testler fetch'i tek noktadan değiştirebilir.
        return fetch_module.fetch(url, self._auth(), transport=self.transport)

    def parse(self, content: bytes) -> tuple[list[dict], str | None]:
        try:
            return parse(content, self.type, self.con.get("record_path"), self.options)
        except ParseError as exc:
            raise SupplierConnectorError(str(exc)) from None

    def fetch(self) -> FetchResult:
        if not self.remote:
            raise SupplierConnectorError("Bu tedarikçi manuel; senkronizasyon için dosya yükleyin.")
        data = self._download(self._url())
        records, path = self.parse(data)
        return FetchResult(records, path, 1, len(data))

    def from_content(self, content: bytes) -> FetchResult:
        records, path = self.parse(content)
        return FetchResult(records, path, 1, len(content))

    def test_connection(self) -> SupplierCheck:
        """Kaynağı okuyup ayrıştırır; veritabanına hiçbir şey yazmaz."""
        if not self.remote:
            return SupplierCheck(False, "Manuel tedarikçi: uzak kaynak yok, dosya yükleyerek çalışır.")
        try:
            r = self.fetch()
        except ConnectorError as exc:
            return SupplierCheck(False, str(exc))
        if not r.records:
            return SupplierCheck(False, "Bağlantı kuruldu ancak kaynakta ürün kaydı bulunamadı.", 0, r.record_path)
        return SupplierCheck(True, f"Bağlantı başarılı: {len(r.records)} kayıt okundu"
                             + (f" ({r.pages} sayfa)" if r.pages > 1 else ""), len(r.records), r.record_path)


class XmlFeedConnector(SupplierConnector):
    type = "xml"
    label = "XML besleme"
    description = "Tek bir URL'den XML ürün listesi (ör. Çanta Bayim)."


class CsvFeedConnector(SupplierConnector):
    type = "csv"
    label = "CSV besleme"
    description = "URL'den CSV; ayırıcı ve karakter kodlaması otomatik veya seçilebilir."


class JsonApiConnector(SupplierConnector):
    type = "api"
    label = "JSON API"
    description = "JSON dönen API; sayfa/limit parametreleriyle sayfalama desteklenir."

    @staticmethod
    def _with_params(url: str, params: dict) -> str:
        parts = urlsplit(url)
        q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in params]
        q.extend((k, str(v)) for k, v in params.items())
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q), ""))

    def fetch(self) -> FetchResult:
        page_param = (self.options.get("page_param") or "").strip()
        if not page_param:
            return super().fetch()
        size_param = (self.options.get("page_size_param") or "").strip()
        size = int(self.options.get("page_size") or 100)
        page = int(self.options.get("start_page") if self.options.get("start_page") is not None else 1)
        max_pages = min(int(self.options.get("max_pages") or 100), MAX_API_PAGES)
        base = self._url()
        records: list[dict] = []
        path = None
        total_bytes = pages = 0
        seen_first: str | None = None
        for _ in range(max_pages):
            params = {page_param: page, **({size_param: size} if size_param else {})}
            data = self._download(self._with_params(base, params))
            total_bytes += len(data)
            batch, path = self.parse(data)
            pages += 1
            if not batch:
                break
            # Sayfalamayı yok sayan API aynı sayfayı döndürürse sonsuz döngü olmasın
            marker = repr(batch[0])
            if marker == seen_first:
                break
            seen_first = seen_first or marker
            records.extend(batch)
            if size_param and len(batch) < size:
                break
            page += 1
        return FetchResult(records, path, pages, total_bytes)


class ManualUploadConnector(SupplierConnector):
    type = "manual"
    label = "Manuel / dosya yükleme"
    remote = False
    description = "Uzak kaynak yok; XML/CSV/JSON dosyası panelden yüklenir."

    def parse(self, content: bytes) -> tuple[list[dict], str | None]:
        try:
            return parse(content, "manual", self.con.get("record_path"), self.options)
        except ParseError as exc:
            raise SupplierConnectorError(str(exc)) from None


CONNECTORS: dict[str, type[SupplierConnector]] = {
    c.type: c for c in (XmlFeedConnector, JsonApiConnector, CsvFeedConnector, ManualUploadConnector)
}


def get_supplier_connector(connection: dict | None, transport=None) -> SupplierConnector:
    kind = (connection or {}).get("integration_type") or "manual"
    cls = CONNECTORS.get(kind)
    if cls is None:
        raise SupplierConnectorError(f"Bilinmeyen tedarikçi entegrasyon türü: {kind}")
    return cls(connection, transport)
