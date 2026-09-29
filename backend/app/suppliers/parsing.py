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


def _norm(name: str) -> str:
    from .fields import normalize_key
    return normalize_key(name.lstrip("@").split("[", 1)[0])


def _local_leaves(el, max_depth: int = 2) -> list[str]:
    """Elemanın kendi alanları (yol olarak): tekrar eden alt listelere (ör. Varyantlar/Varyant) GİRMEZ.
    Böylece bir sarmalayıcı (Urunler) ya da ürün içindeki varyant listesi ürün alanı sayılmaz."""
    out: list[str] = [f"@{k}" for k in el.attrib]

    def walk(e, prefix, depth):
        tags = Counter(_tag(ch) for ch in e)
        for ch in e:
            t = _tag(ch)
            if len(ch):
                # Tekrar eden alt kayıt listesi (ürün listesi, varyant listesi) elemanın kendi alanı değildir
                if tags[t] > 1 or any(n > 1 for n in Counter(_tag(x) for x in ch if len(x)).values()):
                    continue
                if depth + 1 < max_depth:
                    walk(ch, f"{prefix}{t}/", depth + 1)
            else:
                out.append(f"{prefix}{t}")
    walk(el, "", 0)
    return out


def _signals(leaves: list[str]) -> dict:
    from .fields import SIGNALS
    names = {_norm(x.rsplit("/", 1)[-1]) for x in leaves}
    return {k: bool(names & v) for k, v in SIGNALS.items()}


def _score(sig: dict) -> int:
    return 3 * sig["name"] + 2 * sig["sku"] + 2 * sig["price"] + sig["stock"] + sig["barcode"] + sig["image"]


def analyze_xml(root, sample: int = 30) -> dict:
    """Ürün node adayları ve varyant listesi adayları (kullanıcıya gösterilir; seçim kullanıcıdadır).

    Aday: alt elemanı olan, tekrar eden eleman. Öneri en çok tekrar eden DEĞİL en çok "ürüne benzeyen"
    elemandır (ad, SKU, fiyat, stok, barkod, görsel alanları). Ürün adayının içinde tekrar eden liste
    (ör. Varyantlar/Varyant) ürün değil varyant listesi olarak önerilir."""
    stats: dict[str, dict] = {}
    per_parent_max: Counter = Counter()

    def walk(el, path, depth):
        local: Counter = Counter()
        for ch in el:
            if not len(ch):
                continue
            p = f"{path}/{_tag(ch)}" if path else _tag(ch)
            st = stats.setdefault(p, {"count": 0, "samples": []})
            st["count"] += 1
            local[p] += 1
            if len(st["samples"]) < sample:
                st["samples"].append(ch)
            if depth < 5:
                walk(ch, p, depth + 1)
        for p, n in local.items():
            per_parent_max[p] = max(per_parent_max[p], n)
    walk(root, "", 0)
    cands = []
    for p, st in stats.items():
        leaves = sorted({x for el in st["samples"] for x in _local_leaves(el)})
        sig = _signals(leaves)
        cands.append({"path": p, "count": st["count"], "score": _score(sig), "signals": sig,
                      "repeats": per_parent_max[p] > 1, "fields": leaves[:25]})
    by_path = {c["path"]: c for c in cands}
    for c in cands:
        ancestors = [c["path"].rsplit("/", i)[0] for i in range(1, c["path"].count("/") + 1)]
        c["inside_product"] = next((a for a in ancestors if a in by_path and by_path[a]["signals"]["name"]
                                    and by_path[a]["score"] >= 5), None)
    product = [c for c in cands if c["repeats"] and not c["inside_product"] and c["score"] > 0]
    if not product:   # tek ürünlük besleme: tekrar yok ama ürün alanları olan eleman
        product = [c for c in cands if not c["inside_product"] and c["score"] >= 3]
    if not product:   # ürün alanı adı tanınmadı: en çok tekrar eden (kullanıcı sihirbazda değiştirebilir)
        product = [c for c in cands if c["repeats"]]
    product.sort(key=lambda c: (-c["score"], -c["count"], c["path"].count("/")))
    chosen = product[0]["path"] if product else None
    variants = []
    if chosen:
        for c in cands:
            if c["path"].startswith(chosen + "/") and c["repeats"]:
                sig = c["signals"]
                if sig["barcode"] or sig["stock"] or sig["sku"] or sig["variant"]:
                    variants.append({**c, "relative": c["path"][len(chosen) + 1:]})
        variants.sort(key=lambda c: (-c["count"], c["path"].count("/")))
    variant_paths = {v["path"] for v in variants}
    for c in cands:
        c["kind"] = ("product" if c["path"] == chosen else "variant" if c["path"] in variant_paths
                     else "nested" if c["inside_product"] else "other")
    rank = {"product": 0, "variant": 1, "other": 2, "nested": 3}
    ordered = sorted(cands, key=lambda c: (rank[c["kind"]], -c["score"], -c["count"]))
    return {"record_path": chosen, "variant_path": variants[0]["relative"] if variants else None,
            "candidates": ordered[:15],
            "variant_candidates": [{"path": v["relative"], "count": v["count"], "fields": v["fields"]}
                                   for v in variants[:5]]}


def detect_xml_record_path(root) -> str | None:
    return analyze_xml(root)["record_path"]


def _resolve_level(root, path: str) -> list:
    level = [root]
    for part in [p for p in path.split("/") if p]:
        level = [ch for el in level for ch in el if _tag(ch) == part]
    return level


def _flatten_product(el, variant_path: str | None) -> tuple[dict, list]:
    """Ana ürün alanları (varyant listesi hariç) ve varyant elemanları."""
    rec: dict = {}
    if not variant_path:
        _flatten_xml(el, "", rec)
        return rec, []
    first = variant_path.split("/", 1)[0]
    for k, v in el.attrib.items():
        _add(rec, f"@{k.split('}', 1)[-1]}", v.strip())
    for ch in el:
        if _tag(ch) == first:
            continue
        if len(ch):
            _flatten_xml(ch, f"{_tag(ch)}/", rec)
        else:
            _add(rec, _tag(ch), (ch.text or "").strip())
    return rec, _resolve_level(el, variant_path)


def _load_xml(content: bytes):
    try:
        return SafeET.fromstring(content, forbid_dtd=True)
    except DefusedXmlException:
        raise ParseError("XML güvenlik nedeniyle reddedildi (DTD / dış varlık içeriyor).") from None
    except SafeET.ParseError as exc:
        raise ParseError(f"XML okunamadı: {exc}") from None


def parse_xml(content: bytes, record_path: str | None = None,
              variant_path: str | None = None) -> tuple[list[dict], str | None]:
    """Ürün kayıtları. `variant_path` verilirse her varyant ayrı kayıt olur; ana ürün alanları varyanta
    miras kalır (varyant alanları `Varyant/yolu/Alan` adıyla). Varyantı olmayan ürün tek kayıttır."""
    root = _load_xml(content)
    path = (record_path or "").strip().strip("/")
    if path and path.split("/")[0] == _tag(root) and path.count("/") >= 1:
        path = path.split("/", 1)[1]
    path = path or detect_xml_record_path(root)
    if not path:
        raise ParseError("XML içinde tekrar eden ürün elemanı bulunamadı.")
    vpath = (variant_path or "").strip().strip("/") or None
    records: list[dict] = []
    for el in _resolve_level(root, path):
        base, variants = _flatten_product(el, vpath)
        if not variants:
            records.append(base)
        for i, v in enumerate(variants):
            rec = dict(base)
            _flatten_xml(v, f"{vpath}/", rec)
            rec["__variant_index"] = str(i + 1)
            records.append(rec)
        if len(records) >= MAX_RECORDS:
            break
    return records[:MAX_RECORDS], path


def analyze(content: bytes, integration_type: str) -> dict | None:
    """Yalnızca XML için ürün/varyant node analizi (JSON/CSV'de kayıt yolu tektir)."""
    if detect_format(content, integration_type) != "xml":
        return None
    return analyze_xml(_load_xml(content))


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
        return parse_xml(content, record_path, options.get("variant_path"))
    if fmt == "json":
        return parse_json(content, record_path)
    return parse_csv(content, options.get("delimiter") or None, options.get("encoding") or None), None


def field_paths(records: list[dict], limit: int = 500) -> list[dict]:
    """Önizleme için kaynak alan yolları, doluluk oranı, örnek değer ve farklı değer sayısı.
    `all_same`: örneklemdeki tüm kayıtlarda aynı değer (ör. kategori kodu) — SKU olarak önerilmez."""
    counts: Counter = Counter()
    sample: dict[str, str] = {}
    values: dict[str, set] = {}
    for rec in records[:limit]:
        for k, v in rec.items():
            if k.startswith("__"):
                continue
            counts[k] += 1
            first = v[0] if isinstance(v, list) else v
            if k not in sample:
                sample[k] = str(first)[:120]
            vs = values.setdefault(k, set())
            if len(vs) < 1000:
                vs.add(str(first))
    n = max(1, min(len(records), limit))
    return [{"path": k, "fill_rate": round(c / n, 3), "sample": sample.get(k, ""), "distinct": len(values.get(k, ())),
             "all_same": c > 1 and len(values.get(k, ())) == 1}
            for k, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
