"""Alan eşleştirmesini uygular: ham kayıt -> TrendHub tedarikçi ürünü.

Sayılar Türkçe ("1.234,56"), İngilizce ("1,234.56") ve yalın ("1234.5") biçimlerde
kabul edilir; para değerleri Decimal olarak tutulur (float yok).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from .fields import FIELD_NAMES, FIELD_TYPES, REQUIRED_FIELDS

_NUM_CLEAN = re.compile(r"[^\d,.\-]")
MAX_TEXT = {"name": 500, "description": 20000, "category": 500, "brand": 200, "supplier_sku": 200,
            "barcode": 100, "model_code": 200, "currency": 10,
            "color": 100, "variant": 200}


def parse_decimal(value) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, list):
        value = value[0] if value else None
        if value is None:
            return None
    s = _NUM_CLEAN.sub("", str(value).strip().replace(" ", ""))
    if not s or s in ("-", ".", ","):
        return None
    if "," in s and "." in s:
        # Son görülen ayraç ondalık ayracıdır
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        # Tek virgül ondalık ayracıdır (Türkçe); birden çok virgül binlik ayracıdır.
        s = s.replace(",", "") if s.count(",") > 1 else s.replace(",", ".")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def parse_int(value) -> int | None:
    d = parse_decimal(value)
    if d is None:
        return None
    return int(d.to_integral_value(rounding=ROUND_HALF_UP))


def _text(value, name: str) -> str | None:
    if isinstance(value, list):
        value = next((v for v in value if v not in (None, "")), None)
    if value is None:
        return None
    s = " ".join(str(value).split()) if name != "description" else str(value).strip()
    return s[: MAX_TEXT.get(name, 1000)] or None


def _list(value) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else re.split(r"[|;\n]+|,(?=\s*https?://)", str(value))
    out = []
    for it in items:
        s = str(it).strip()
        if s and s not in out:
            out.append(s[:1000])
    return out[:20]


@dataclass
class MappedItem:
    values: dict
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _resolve(rec: dict, path: str):
    if path in rec:
        return rec[path]
    # Kayıt yolu farklı yazılmışsa (baş/son '/' ya da büyük/küçük harf) esnek eşleşme
    p = path.strip("/")
    if p in rec:
        return rec[p]
    low = p.lower()
    for k, v in rec.items():
        if k.lower() == low:
            return v
    return None


def apply_mapping(rec: dict, mapping: dict[str, dict]) -> MappedItem:
    """mapping: {hedef_alan: {"source_path": ..., "default_value": ...}}"""
    out: dict = {}
    errors: list[str] = []
    for name in FIELD_NAMES:
        m = mapping.get(name) or {}
        raw = _resolve(rec, m["source_path"]) if m.get("source_path") else None
        if (raw is None or raw == "" or raw == []) and m.get("default_value") not in (None, ""):
            raw = m["default_value"]
        t = FIELD_TYPES[name]
        if t == "decimal":
            v = parse_decimal(raw)
            if raw not in (None, "", []) and v is None:
                errors.append(f"{name}: sayı okunamadı ({str(raw)[:40]})")
            if v is not None:
                v = v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                if v < 0:
                    errors.append(f"{name}: negatif olamaz")
        elif t == "int":
            v = parse_int(raw)
            if raw not in (None, "", []) and v is None:
                errors.append(f"{name}: tam sayı okunamadı ({str(raw)[:40]})")
        elif t == "list":
            v = _list(raw)
        else:
            v = _text(raw, name)
        out[name] = v
    if out.get("barcode"):
        out["barcode"] = re.sub(r"\s", "", out["barcode"])
    if out.get("currency"):
        out["currency"] = out["currency"].upper().replace("TL", "TRY")
    for name in REQUIRED_FIELDS:
        if not out.get(name):
            errors.append(f"{name}: zorunlu alan boş")
    return MappedItem(out, errors)
