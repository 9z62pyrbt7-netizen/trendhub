"""Tedarikçi senkronizasyonu (worker işi `supplier.sync` veya dosya yükleme).

Akış:
  1. Çalıştırma kaydı açılır (supplier_sync_runs, status=running).
  2. Kaynak indirilir (ağ çağrısı transaction DIŞINDA) veya yüklenen içerik kullanılır.
  3. Kayıtlar ayrıştırılır, alan eşleştirmesi uygulanır, satır hataları toplanır.
  4. Tek transaction'da fark uygulanır:
       yeni ürün -> eklenir ('new')
       fiyat değişti -> güncellenir ('price'), stok değişti -> ('stock')
       kaynağında artık yok -> SİLİNMEZ, status='missing' ('missing')
       yeniden göründü -> status='active' ('reactivated')
  5. Barkodla katalog eşleşmesi + tercih edilen tedarikçiye göre katalog stok/maliyet güncellemesi.

Güvenlik ağı: kaynak birden boş/çok küçük dönerse (ör. tedarikçi sunucusu hatalı sayfa
döndürdü) tüm ürünler "kayıp" işaretlenmez; çalıştırma 'partial' olur.
Pazaryerine HİÇBİR yazma yapılmaz.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ..connectors.base import ConnectorError
from ..db import row, rows
from ..suppliers.connectors import FetchResult, SupplierConnectorError, get_supplier_connector
from ..suppliers.mapping import apply_mapping
from . import alerts
from .events import record_event, resolve_fingerprint
from .supplier_catalog import link_by_barcode, refresh_catalog

MASS_MISSING_MIN_ACTIVE = 20
MASS_MISSING_RATIO = Decimal("0.5")
MAX_ERRORS_KEPT = 50


SupplierSyncError = SupplierConnectorError


@dataclass
class SyncStats:
    records_total: int = 0
    created_count: int = 0
    updated_count: int = 0
    price_changed: int = 0
    stock_changed: int = 0
    missing_count: int = 0
    reactivated_count: int = 0
    linked_count: int = 0
    error_count: int = 0
    errors: list[str] = field(default_factory=list)
    message: str | None = None
    status: str = "success"

    def error(self, msg: str) -> None:
        self.error_count += 1
        if len(self.errors) < MAX_ERRORS_KEPT:
            self.errors.append(msg[:300])

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def load_config(conn: Connection, supplier_id: int) -> dict:
    sup = row(conn, "SELECT * FROM suppliers WHERE id = :id", id=supplier_id)
    if sup is None:
        raise SupplierSyncError(f"Tedarikçi #{supplier_id} bulunamadı")
    con = row(conn, "SELECT * FROM supplier_connections WHERE supplier_id = :id", id=supplier_id) or {}
    mapping = {r["target_field"]: r for r in rows(conn, """
        SELECT target_field, source_path, default_value FROM supplier_field_mappings WHERE supplier_id = :id
    """, id=supplier_id)}
    return {"supplier": sup, "connection": con, "mapping": mapping}


def connector_for(cfg: dict, transport=None):
    return get_supplier_connector(cfg["connection"] or {"integration_type": "manual"}, transport)


def fetch_records(cfg: dict, content: bytes | None = None, transport=None) -> FetchResult:
    """Kaynağı (URL veya yüklenen içerik) tedarikçinin connector'ıyla okuyup kayıtlara çevirir."""
    c = connector_for(cfg, transport)
    return c.from_content(content) if content is not None else c.fetch()


def _hash(v: dict) -> str:
    keys = ("barcode", "model_code", "name", "category", "brand", "sale_price", "currency", "vat_rate", "desi",
            "description", "images")
    return hashlib.sha256(json.dumps({k: v.get(k) for k in keys}, default=str, sort_keys=True,
                                     ensure_ascii=False).encode()).hexdigest()


def _change(conn, supplier_id, sp_id, run_id, kind, old=None, new=None):
    conn.execute(text("""INSERT INTO supplier_product_changes(supplier_id, supplier_product_id, run_id, kind, old_value, new_value)
                         VALUES (:s, :sp, :r, :k, :o, :n)"""),
                 {"s": supplier_id, "sp": sp_id, "r": run_id, "k": kind,
                  "o": None if old is None else str(old), "n": None if new is None else str(new)})


def apply_items(conn: Connection, cfg: dict, records: list[dict], run_id: int, stats: SyncStats) -> None:
    sup = cfg["supplier"]
    sid = sup["id"]
    items: dict[str, dict] = {}
    for i, rec in enumerate(records, 1):
        m = apply_mapping(rec, cfg["mapping"])
        if not m.ok:
            stats.error(f"Kayıt {i}: " + "; ".join(m.errors))
            continue
        sku = m.values["supplier_sku"]
        if sku in items:
            stats.error(f"Kayıt {i}: tekrar eden tedarikçi SKU'su {sku} (ilki kullanıldı)")
            continue
        items[sku] = m.values

    existing = {r["supplier_sku"]: r for r in rows(conn, """
        SELECT id, supplier_sku, cost, stock, status, content_hash, product_id, barcode, name
          FROM supplier_products WHERE supplier_id = :s AND supplier_sku IS NOT NULL FOR UPDATE
    """, s=sid)}
    touched_products: set[int] = set()
    for sku, v in items.items():
        h = _hash(v)
        params = {"s": sid, "sku": sku, "barcode": v["barcode"], "model": v["model_code"], "name": v["name"],
                  "category": v["category"], "brand": v["brand"], "cost": v["purchase_price"],
                  "sale": v["sale_price"], "cur": v["currency"] or "TRY", "stock": v["stock"], "vat": v["vat_rate"],
                  "desi": v["desi"], "desc": v["description"], "images": json.dumps(v["images"], ensure_ascii=False),
                  "hash": h}
        old = existing.get(sku)
        if old is None:
            sp_id = conn.execute(text("""
                INSERT INTO supplier_products(supplier_id, supplier_sku, barcode, model_code, name, category, brand,
                    cost, sale_price, currency, stock, vat_rate, desi, description, images, content_hash, status,
                    is_primary, first_seen_at, last_seen_at, created_at, updated_at)
                VALUES (:s, :sku, :barcode, :model, :name, :category, :brand, :cost, :sale, :cur, :stock, :vat, :desi,
                        :desc, CAST(:images AS JSONB), :hash, 'active', FALSE, NOW(), NOW(), NOW(), NOW())
                RETURNING id
            """), params).scalar()
            _change(conn, sid, sp_id, run_id, "new", None, v["purchase_price"])
            stats.created_count += 1
            continue
        sp_id = old["id"]
        price_changed = (old["cost"] is None) != (v["purchase_price"] is None) or (
            old["cost"] is not None and v["purchase_price"] is not None and Decimal(old["cost"]) != v["purchase_price"])
        stock_changed = (old["stock"] if old["stock"] is not None else None) != v["stock"]
        reactivated = (old["status"] or "active") != "active"
        content_changed = old["content_hash"] != h
        if not (price_changed or stock_changed or reactivated or content_changed):
            conn.execute(text("UPDATE supplier_products SET last_seen_at = NOW() WHERE id = :id"), {"id": sp_id})
            continue
        conn.execute(text("""
            UPDATE supplier_products SET barcode = :barcode, model_code = :model, name = :name, category = :category,
                   brand = :brand, cost = :cost, sale_price = :sale, currency = :cur, stock = :stock, vat_rate = :vat,
                   desi = :desi, description = :desc, images = CAST(:images AS JSONB), content_hash = :hash,
                   status = 'active', missing_since = NULL, last_seen_at = NOW(), updated_at = NOW(),
                   price_changed_at = CASE WHEN :pc THEN NOW() ELSE price_changed_at END,
                   stock_changed_at = CASE WHEN :sc THEN NOW() ELSE stock_changed_at END
             WHERE id = :id
        """), {**params, "id": sp_id, "pc": price_changed, "sc": stock_changed})
        stats.updated_count += 1
        if price_changed:
            stats.price_changed += 1
            _change(conn, sid, sp_id, run_id, "price", old["cost"], v["purchase_price"])
            if old["cost"] is not None and v["purchase_price"] is not None:
                alerts.price_change_event(conn, {**old, "name": v["name"] or old["name"]}, Decimal(old["cost"]),
                                          v["purchase_price"], sup)
        if old["barcode"] and (old["barcode"] or None) != (v["barcode"] or None):
            alerts.barcode_change_event(conn, {**old, "name": v["name"] or old["name"]}, old["barcode"], v["barcode"], sup)
        if stock_changed:
            stats.stock_changed += 1
            _change(conn, sid, sp_id, run_id, "stock", old["stock"], v["stock"])
        if reactivated:
            stats.reactivated_count += 1
            _change(conn, sid, sp_id, run_id, "reactivated")
        if old["product_id"] and (price_changed or stock_changed or reactivated):
            touched_products.add(old["product_id"])

    # Kaynağında bulunmayanlar: silinmez, 'missing' işaretlenir.
    active_before = [r for r in existing.values() if (r["status"] or "active") == "active"]
    gone = [r for r in active_before if r["supplier_sku"] not in items]
    options = (cfg["connection"] or {}).get("options") or {}
    suspicious = not options.get("allow_mass_missing") and (
        (active_before and not items)
        or (len(active_before) >= MASS_MISSING_MIN_ACTIVE and len(items) < len(active_before) * MASS_MISSING_RATIO))
    if gone and suspicious:
        stats.status = "partial"
        stats.message = (f"Kaynak {len(items)} ürün döndürdü, önceki senkronda {len(active_before)} aktif ürün vardı. "
                         f"Olası hatalı kaynak: {len(gone)} ürün 'kaynağında bulunamadı' olarak İŞARETLENMEDİ.")
    else:
        for r in gone:
            conn.execute(text("""UPDATE supplier_products SET status = 'missing', missing_since = NOW(), updated_at = NOW()
                                 WHERE id = :id"""), {"id": r["id"]})
            _change(conn, sid, r["id"], run_id, "missing", r["stock"], None)
            stats.missing_count += 1
            if r["product_id"]:
                touched_products.add(r["product_id"])

    stats.linked_count = link_by_barcode(conn, sid)
    linked_now = [r["product_id"] for r in rows(conn, """
        SELECT product_id FROM supplier_products WHERE supplier_id = :s AND product_id IS NOT NULL
    """, s=sid)]
    refresh_catalog(conn, sorted(touched_products | set(linked_now if stats.linked_count else [])))
    if stats.error_count and stats.status == "success":
        stats.status = "partial"


def _finish(conn: Connection, run_id: int, supplier_id: int, stats: SyncStats) -> None:
    conn.execute(text("""
        UPDATE supplier_sync_runs SET status = :status, finished_at = NOW(), records_total = :records_total,
               created_count = :created_count, updated_count = :updated_count, price_changed = :price_changed,
               stock_changed = :stock_changed, missing_count = :missing_count, reactivated_count = :reactivated_count,
               linked_count = :linked_count, error_count = :error_count, errors = CAST(:errors AS JSONB), message = :message
         WHERE id = :id
    """), {**stats.as_dict(), "errors": json.dumps(stats.errors, ensure_ascii=False), "id": run_id})
    err = None if stats.status == "success" else (stats.message or f"{stats.error_count} kayıt işlenemedi")
    conn.execute(text("""UPDATE suppliers SET last_sync_at = NOW(), last_sync_status = :st, last_sync_error = :e,
                                updated_at = NOW() WHERE id = :id"""),
                 {"st": stats.status, "e": err, "id": supplier_id})


def start_run(engine: Engine, supplier_id: int, trigger: str, job_id: int | None) -> int:
    with engine.begin() as conn:
        # Çöken worker'dan kalan yarım çalıştırmalar
        conn.execute(text("""UPDATE supplier_sync_runs SET status = 'failed', finished_at = NOW(),
                                    message = 'Çalıştırma yarıda kaldı (worker yeniden başlatıldı)'
                              WHERE supplier_id = :s AND status = 'running' AND started_at < NOW() - INTERVAL '1 hour'"""),
                     {"s": supplier_id})
        return conn.execute(text("""INSERT INTO supplier_sync_runs(supplier_id, job_id, trigger) VALUES (:s, :j, :t)
                                    RETURNING id"""), {"s": supplier_id, "j": job_id, "t": trigger}).scalar()


def run_supplier_sync(engine: Engine, supplier_id: int, *, trigger: str = "manual", job_id: int | None = None,
                      content: bytes | None = None, transport=None) -> dict:
    with engine.begin() as conn:
        cfg = load_config(conn, supplier_id)
    if not cfg["supplier"]["is_active"] and trigger == "schedule":
        return {"skipped": "Tedarikçi pasif"}
    run_id = start_run(engine, supplier_id, trigger, job_id)
    stats = SyncStats()
    try:
        fetched = fetch_records(cfg, content, transport)
        records, used_path = fetched.records, fetched.record_path
        stats.records_total = len(records)
        if not cfg["mapping"].get("supplier_sku", {}).get("source_path"):
            raise SupplierSyncError("Alan eşleştirmesi eksik: 'Tedarikçi ürün kodu (SKU)' alanı eşleştirilmeli.")
        with engine.begin() as conn:
            apply_items(conn, cfg, records, run_id, stats)
            _finish(conn, run_id, supplier_id, stats)
            fp = f"supplier-sync:{supplier_id}"
            if stats.status == "success":
                resolve_fingerprint(conn, fp)
            elif stats.message:
                record_event(conn, level="warning", source=f"supplier:{cfg['supplier']['code']}",
                             message=f"{cfg['supplier']['name']}: {stats.message}", fingerprint=fp)
    except Exception as exc:
        stats.status = "failed"
        stats.message = str(exc)[:1000] if isinstance(exc, ConnectorError) else f"{exc.__class__.__name__}: {exc}"[:1000]
        with engine.begin() as conn:
            _finish(conn, run_id, supplier_id, stats)
            record_event(conn, level="error", source=f"supplier:{cfg['supplier']['code']}",
                         message=f"{cfg['supplier']['name']} senkronizasyonu başarısız: {stats.message}",
                         fingerprint=f"supplier-sync:{supplier_id}")
        raise
    return {"run_id": run_id, "record_path": used_path, **{k: v for k, v in stats.as_dict().items() if k != "errors"},
            "errors": stats.errors[:10]}
