"""Tedarikçi içe aktarma sihirbazı: analiz (salt okunur) ve kullanıcı onayı.

Akış: URL → bağlantı testi → ürün node adayları → node/varyant seçimi → alan eşleştirme
      → canlı 5-10 ürün önizleme → eksik alan uyarıları → KULLANICI ONAYI → Ürün Havuzu'na aktarım.

* analyze(): kaynağı okur, hiçbir şey yazmaz.
* approve(): eşleştirmeyi kaydeder, onay zamanını işaretler ve (istenirse) ilk aktarımı başlatır.
  Onay olmadan kaynaktan aktarım ve zamanlanmış senkron yapılmaz.
* Belirsiz fiyat alanı (ör. yalnızca 'Price'/'Fiyat') alış fiyatı olarak ancak kullanıcı doğrularsa kullanılır.
* Aktarım yalnızca Ürün Havuzu'na (supplier_products) yazar; pazaryerine hiçbir şey gönderilmez.
"""
from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..connectors.base import ConnectorError
from ..domain.suppliers import effective_cost
from ..suppliers.connectors import get_supplier_connector
from ..suppliers.fields import CONFIRM_SYNONYMS, FIELD_NAMES, REQUIRED_FIELDS, TARGET_FIELDS, normalize_key, \
    suggest_mapping_detailed
from ..suppliers.mapping import apply_mapping
from ..suppliers.parsing import ParseError, analyze as analyze_nodes, field_paths, parse
from . import app_settings

LABELS = {f[0]: f[1] for f in TARGET_FIELDS}
KEY_FIELDS = ("supplier_sku", "name", "barcode", "purchase_price", "stock", "images", "category", "brand", "vat_rate")


class ImportError_(Exception):
    """Kullanıcıya gösterilebilir Türkçe hata."""


def is_ambiguous_price(path: str | None) -> bool:
    """Yol(lar)ın son adı yalnızca genel 'price/fiyat' ise alış fiyatı olduğu doğrulanmalı."""
    if not path:
        return False
    leaves = {normalize_key(p.strip().rsplit("/", 1)[-1].lstrip("@")) for p in path.split(",") if p.strip()}
    return bool(leaves & set(CONFIRM_SYNONYMS["purchase_price"]))


def _mapping_from(items: list[dict] | None) -> dict[str, dict]:
    out = {}
    for m in items or []:
        f = m.get("target_field")
        if f in FIELD_NAMES and ((m.get("source_path") or "").strip() or (m.get("default_value") or "").strip()):
            out[f] = {"source_path": (m.get("source_path") or "").strip() or None,
                      "default_value": (m.get("default_value") or "").strip() or None}
    return out


def _load_content(cfg: dict, content: bytes | None, transport=None):
    con = cfg["connection"] or {"integration_type": "manual"}
    c = get_supplier_connector(con, transport)
    if content is not None:
        return c, content
    try:
        return c, c.fetch_raw()
    except ConnectorError as exc:
        raise ImportError_(str(exc)) from None


def analyze(conn: Connection, cfg: dict, *, record_path: str | None = None, variant_path: str | None = None,
            mappings: list[dict] | None = None, limit: int = 10, content: bytes | None = None,
            transport=None) -> dict:
    """Kaynağı okuyup ürün node adaylarını, alanları, öneriyi ve eşleştirilmiş önizlemeyi döner. Yazmaz."""
    from .supplier_sync import variant_sku
    con = dict(cfg["connection"] or {})
    options = dict(con.get("options") or {})
    connector, raw = _load_content(cfg, content, transport)
    itype = con.get("integration_type") or "manual"
    nodes = None
    try:
        if raw is not None:
            nodes = analyze_nodes(raw, itype)
            rp = record_path if record_path is not None else (con.get("record_path") or (nodes or {}).get("record_path"))
            if variant_path is not None:
                vp = variant_path or None
            elif options.get("variant_path") or con.get("record_path"):
                vp = options.get("variant_path")
            else:
                vp = (nodes or {}).get("variant_path")
            records, used = parse(raw, itype, rp or None, {**options, "variant_path": vp})
        else:   # sayfalı JSON API
            fetched = connector.fetch()
            records, used, vp = fetched.records, fetched.record_path, None
    except ParseError as exc:
        raise ImportError_(str(exc)) from None
    except ConnectorError as exc:
        raise ImportError_(str(exc)) from None
    fields = field_paths(records)
    suggestion = suggest_mapping_detailed(fields)
    notes = suggestion.pop("_notes", {})
    saved = {k: v for k, v in cfg["mapping"].items() if v.get("source_path") or v.get("default_value")}
    if mappings is not None:
        mapping, using = _mapping_from(mappings), "form"
    elif saved:
        mapping, using = {k: {"source_path": v.get("source_path"), "default_value": v.get("default_value")}
                          for k, v in saved.items()}, "saved"
    else:
        mapping, using = {k: {"source_path": v["path"], "default_value": None} for k, v in suggestion.items()}, "suggestion"
    confirmed = {k for k, v in cfg["mapping"].items() if v.get("confirmed")}

    fx = app_settings.fx_rates(conn)
    vat_mode = cfg["supplier"].get("price_vat_mode")
    ok = errors = zero_stock = 0
    skus: Counter = Counter()
    empty: Counter = Counter()
    currencies: Counter = Counter()
    samples = []
    for i, rec in enumerate(records):
        m = apply_mapping(rec, mapping)
        variant_sku(m.values, rec, mapping, vp)
        if m.ok:
            ok += 1
            skus[m.values["supplier_sku"]] += 1
        else:
            errors += 1
        for f in KEY_FIELDS:
            if f in mapping and m.values.get(f) in (None, "", []):
                empty[f] += 1
        if m.values.get("stock") is not None and m.values["stock"] <= 0:
            zero_stock += 1
        currencies[(m.values.get("currency") or "TRY").upper()] += 1
        if i < limit:
            cost, note = effective_cost(m.values.get("purchase_price"), m.values.get("currency"),
                                        m.values.get("vat_rate"), vat_mode, fx)
            samples.append({"values": {k: (str(v) if isinstance(v, Decimal) else v) for k, v in m.values.items()},
                            "errors": m.errors, "effective_cost": str(cost) if cost is not None else None,
                            "cost_note": note})
    total = len(records)
    missing_required = [LABELS[f] for f in REQUIRED_FIELDS if not (mapping.get(f) or {}).get("source_path")]
    price_path = (mapping.get("purchase_price") or {}).get("source_path")
    warnings = []
    if missing_required:
        warnings.append("Zorunlu alan eşleştirilmedi: " + ", ".join(missing_required))
    if "purchase_price" not in mapping:
        warnings.append("Alış fiyatı eşleştirilmedi: maliyet ve kâr hesaplanamaz.")
    for f, n in empty.items():
        if n:
            warnings.append(f"{LABELS[f]} {n}/{total} üründe boş.")
    dups = sum(n - 1 for n in skus.values() if n > 1)
    if dups:
        warnings.append(f"{dups} kayıtta tekrar eden SKU var (ilk kayıt kullanılır). SKU alanını kontrol edin.")
    if zero_stock:
        warnings.append(f"{zero_stock} ürünün stoğu 0 (havuza alınır, satışa uygun görünmez).")
    for cur in currencies:
        if cur not in fx:
            warnings.append(f"{currencies[cur]} ürünün para birimi {cur}: Ayarlar'da kur tanımlanmadan maliyet finansa girmez.")
    if not vat_mode:
        warnings.append("Tedarikçi fiyatlarının KDV dahil mi hariç mi olduğunu seçin.")
    if errors:
        warnings.append(f"{errors} kayıt eksik/hatalı olduğu için aktarılmayacak.")
    needs_confirmation = is_ambiguous_price(price_path) and "purchase_price" not in confirmed
    if needs_confirmation:
        warnings.append(f"'{price_path}' alanı tedarikçi ALIŞ fiyatı mı? Onaylamadan içe aktarma yapılamaz.")
    return {
        "record_path": used, "variant_path": vp, "total": total, "nodes": nodes,
        "fields": fields[:300], "suggestion": suggestion, "suggestion_notes": notes, "using": using,
        "mapping": [{"target_field": f, "label": LABELS[f], "required": f in REQUIRED_FIELDS,
                     "source_path": (mapping.get(f) or {}).get("source_path"),
                     "default_value": (mapping.get(f) or {}).get("default_value"),
                     "confidence": (suggestion.get(f) or {}).get("confidence")
                     if (mapping.get(f) or {}).get("source_path") == (suggestion.get(f) or {}).get("path") else "manual",
                     "confirmed": f in confirmed} for f in FIELD_NAMES],
        "samples": samples,
        "summary": {"total": total, "importable": ok, "invalid": errors, "duplicate_skus": dups,
                    "zero_stock": zero_stock, "empty": dict(empty), "currencies": dict(currencies)},
        "missing_required": missing_required, "price_needs_confirmation": needs_confirmation,
        "price_vat_mode": vat_mode, "approved_at": cfg["supplier"].get("mapping_approved_at"),
        "warnings": warnings, "can_approve": not missing_required and total > 0 and ok > 0,
    }


def approve(conn: Connection, cfg: dict, *, record_path: str | None, variant_path: str | None,
            mappings: list[dict], confirm_price: bool, price_vat_mode: str, user_id: int) -> dict:
    """Eşleştirmeyi kaydeder ve onaylar. Doğrulama başarısızsa hiçbir şey yazılmaz."""
    sid = cfg["supplier"]["id"]
    mapping = _mapping_from(mappings)
    missing = [LABELS[f] for f in REQUIRED_FIELDS if not (mapping.get(f) or {}).get("source_path")]
    if missing:
        raise ImportError_("Zorunlu alan eşleştirilmedi: " + ", ".join(missing))
    price_path = (mapping.get("purchase_price") or {}).get("source_path")
    if is_ambiguous_price(price_path) and not confirm_price:
        raise ImportError_(f"'{price_path}' alanının tedarikçi alış fiyatı olduğunu doğrulayın.")
    if price_vat_mode not in ("included", "excluded"):
        raise ImportError_("Tedarikçi fiyatlarının KDV dahil mi hariç mi olduğunu seçin.")
    for f in FIELD_NAMES:
        m = mapping.get(f) or {}
        conn.execute(text("""
            INSERT INTO supplier_field_mappings(supplier_id, target_field, source_path, default_value, confirmed, updated_at)
            VALUES (:s, :f, :p, :d, :c, NOW())
            ON CONFLICT (supplier_id, target_field) DO UPDATE SET source_path = EXCLUDED.source_path,
                   default_value = EXCLUDED.default_value, confirmed = EXCLUDED.confirmed, updated_at = NOW()
        """), {"s": sid, "f": f, "p": m.get("source_path"), "d": m.get("default_value"),
               "c": bool(f == "purchase_price" and confirm_price and price_path)})
    conn.execute(text("""
        UPDATE supplier_connections SET record_path = :rp,
               options = (COALESCE(options, '{}'::jsonb) - 'variant_path') || CAST(:vp AS JSONB), updated_at = NOW()
         WHERE supplier_id = :s"""), {"s": sid, "rp": (record_path or "").strip() or None,
                                     "vp": json.dumps({"variant_path": variant_path} if variant_path else {})})
    conn.execute(text("""UPDATE suppliers SET price_vat_mode = :m, mapping_approved_at = NOW(), mapping_approved_by = :u,
                                updated_at = NOW() WHERE id = :s"""), {"m": price_vat_mode, "u": user_id, "s": sid})
    return {"supplier_id": sid, "mapping": {k: v.get("source_path") for k, v in mapping.items()},
            "record_path": record_path, "variant_path": variant_path, "price_vat_mode": price_vat_mode,
            "price_confirmed": bool(confirm_price and price_path)}


def load(conn: Connection, supplier_id: int) -> dict:
    from .supplier_sync import load_config
    cfg = load_config(conn, supplier_id)
    confirmed = {r["target_field"] for r in conn.execute(text(
        "SELECT target_field FROM supplier_field_mappings WHERE supplier_id = :s AND confirmed"), {"s": supplier_id}).mappings()}
    for f in confirmed:
        cfg["mapping"].setdefault(f, {})["confirmed"] = True
    return cfg
