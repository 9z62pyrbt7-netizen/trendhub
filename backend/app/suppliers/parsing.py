"""Tedarikçi kaynaklarını (XML / JSON / CSV) düz kayıtlara çevirir.

Her kayıt `{"yol": değer}` sözlüğüdür:
  * XML:  `UrunKodu`, `Fiyatlar/Alis`, `@id` (öznitelik), `Resimler/Resim` (tekrarlıysa liste)
  * JSON: `sku`, `price/amount`, `images` (liste)
  * CSV:  başlık satırındaki kolon adları
Yollar tedarikçiden tedarikçiye değişir; hangi yolun hangi TrendHub alanına
karşılık geldiği `supplier_field_mappings` ile belirlenir.

Güvenlik: XML `defusedxml` ile okunur (XXE, dış varlık, "billion laughs" engelli).
"""
from __future__ import annotations

import csv
import io
import json
from collections import Counter

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

MAX_RECORDS = 200_000


class ParseError(Exception):
    """Kaynak okunamadı (kullanıcıya gösterilebilir Türkçe mesaj)."""


def _tag(el) -> str:
    t = el.tag
    return t.split("}", 1)[1] if isinstance(t, str) and "}" in t else str(t)


def _add(rec: dict, key: str, value) -> None:
    if value is None or value == "":
        return
    if key in rec:
        cur = rec[key]
        if isinstance(cur, list):
            cur.append(value)
        else:
            rec[key] = [cur, value]
    else:
        rec[key] = value


def _flatten_xml(el, prefix: str, rec: dict) -> None:
    for k, v in el.attrib.items():
        k = k.split("}", 1)[1] if "}" in k else k
        _add(rec, f"{prefix}@{k}", v.strip())
    children = list(el)
    if not children:
        text = (el.text or "").strip()
        if prefix:
            _add(rec, prefix.rstrip("/"), text)
        return
    for ch in children:
        _flatten_xml(ch, f"{prefix}{_tag(ch)}/", rec)


def _xml_paths(root) -> Counter:
    """Alt elemanı olan elemanların (kök hariç) yol sayıları: kayıt yolu tespiti için."""
    counts: Counter = Counter()

    def walk(el, path):
        for ch in el:
            p = f"{path}/{_tag(ch)}" if path else _tag(ch)
            if len(ch):
                counts[p] += 1
                if p.count("/") < 3:
                    walk(ch, p)
    walk(root, "")
    return counts


def detect_xml_record_path(root) -> str | None:
    counts = _xml_paths(root)
    if not counts:
        return None
    # En çok tekrar eden; eşitlikte daha sığ olan
    return max(counts.items(), key=lambda kv: (kv[1], -kv[0].count("/")))[0]


def parse_xml(content: bytes, record_path: str | None = None) -> tuple[list[dict], str | None]:
    try:
        root = SafeET.fromstring(content, forbid_dtd=True)
    except DefusedXmlException:
        raise ParseError("XML güvenlik nedeniyle reddedildi (DTD / dış varlık içeriyor).") from None
    except SafeET.ParseError as exc:
        raise ParseError(f"XML okunamadı: {exc}") from None
    path = (record_path or "").strip().strip("/")
    if path and path.split("/")[0] == _tag(root) and path.count("/") >= 1:
        path = path.split("/", 1)[1]
    path = path or detect_xml_record_path(root)
    if not path:
        raise ParseError("XML içinde tekrar eden ürün elemanı bulunamadı.")
    parts = path.split("/")
    level = [root]
    for part in parts:
        level = [ch for el in level for ch in el if _tag(ch) == part]
    records = []
    for el in level[:MAX_RECORDS]:
        rec: dict = {}
        _flatten_xml(el, "", rec)
        records.append(rec)
    return records, path


def _flatten_json(obj, prefix: str, rec: dict) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            _flatten_json(v, f"{prefix}{k}/", rec)
    elif isinstance(obj, list):
        for item in obj:
            _flatten_json(item, prefix, rec)
    elif obj is not None and prefix:
        _add(rec, prefix.rstrip("/"), str(obj).strip() if not isinstance(obj, bool) else str(obj).lower())


def _get_path(obj, path: str):
    for part in [p for p in path.replace(".", "/").split("/") if p]:
        if not isinstance(obj, dict) or part not in obj:
            return None
        obj = obj[part]
    return obj


def parse_json(content: bytes, record_path: str | None = None) -> tuple[list[dict], str | None]:
    try:
        data = json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ParseError(f"JSON okunamadı: {exc}") from None
    path = (record_path or "").strip().strip("/")
    items = _get_path(data, path) if path else None
    if items is None and not path:
        if isinstance(data, list):
            items, path = data, ""
        elif isinstance(data, dict):
            lists = [(k, v) for k, v in data.items() if isinstance(v, list) and v and isinstance(v[0], dict)]
            if lists:
                path, items = max(lists, key=lambda kv: len(kv[1]))
    if not isinstance(items, list):
        raise ParseError("JSON içinde ürün listesi bulunamadı; kayıt yolunu (ör. data/products) belirtin.")
    records = []
    for it in items[:MAX_RECORDS]:
        if isinstance(it, dict):
            rec: dict = {}
            _flatten_json(it, "", rec)
            records.append(rec)
    return records, path or None


def _decode(content: bytes, encoding: str | None) -> str:
    for enc in ([encoding] if encoding else []) + ["utf-8-sig", "cp1254"]:
        try:
            return content.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    raise ParseError("CSV karakter kodlaması çözülemedi (UTF-8 veya Windows-1254 bekleniyor).")


def parse_csv(content: bytes, delimiter: str | None = None, encoding: str | None = None) -> list[dict]:
    text = _decode(content, encoding)
    if not delimiter:
        sample = text[:20000]
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ";" if sample.count(";") > sample.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if not reader.fieldnames:
        raise ParseError("CSV başlık satırı bulunamadı.")
    records = []
    for i, r in enumerate(reader):
        if i >= MAX_RECORDS:
            break
        rec: dict = {}
        for k, v in r.items():
            if k is None:
                continue
            _add(rec, k.strip(), (v or "").strip())
        records.append(rec)
    return records


def detect_format(content: bytes, declared: str) -> str:
    head = content[:200].lstrip(b"\xef\xbb\xbf \t\r\n")
    if head.startswith(b"<"):
        return "xml"
    if head[:1] in (b"{", b"["):
        return "json"
    return "csv" if declared in ("csv", "manual") else declared


def parse(content: bytes, integration_type: str, record_path: str | None = None,
          options: dict | None = None) -> tuple[list[dict], str | None]:
    """Kaynağı biçimine göre ayrıştırır. (kayıtlar, kullanılan kayıt yolu) döner."""
    options = options or {}
    if not content or not content.strip():
        raise ParseError("Kaynak boş döndü.")
    fmt = detect_format(content, integration_type)
    if fmt == "xml":
        return parse_xml(content, record_path)
    if fmt == "json":
        return parse_json(content, record_path)
    return parse_csv(content, options.get("delimiter") or None, options.get("encoding") or None), None


def field_paths(records: list[dict], limit: int = 500) -> list[dict]:
    """Önizleme için kaynak alan yolları, doluluk oranı ve örnek değer."""
    counts: Counter = Counter()
    sample: dict[str, str] = {}
    for rec in records[:limit]:
        for k, v in rec.items():
            counts[k] += 1
            if k not in sample:
                first = v[0] if isinstance(v, list) else v
                sample[k] = str(first)[:120]
    n = max(1, min(len(records), limit))
    return [{"path": k, "fill_rate": round(c / n, 3), "sample": sample.get(k, "")}
            for k, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
