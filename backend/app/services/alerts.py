"""Merkezi uyarı / sorun sistemi.

İki tür tespit:
  * DURUM tabanlı (tarayıcı, `scan`): veritabanındaki GERÇEK kayıtlardan türetilir (tedarikçi stoğu,
    kaynakta bulunamayan ürün, senkron hatası, bağlantı hatası, ilan/katalog uyuşmazlığı, sipariş/kargo).
    Koşul ortadan kalkınca uyarı otomatik "çözüldü" (resolution='system') olur.
  * OLAY tabanlı (`raise_event`): senkronda görülen tek seferlik değişiklikler (ciddi fiyat değişimi,
    barkod değişimi). Kullanıcı "Çözüldü" diyene kadar açık kalır.

Tekilleştirme: aynı `fingerprint` için yalnızca BİR açık uyarı olur (DB'de partial unique index);
tekrar tespit edilirse yeni satır açılmaz, `occurrences` ve `last_detected_at` güncellenir.
Kullanıcı çözdükten sonra sorun hâlâ sürüyorsa bir sonraki taramada yeni bir uyarı açılır.

Veri yoksa sorun ÜRETİLMEZ (ör. kargo takibi verisi olmayan pazaryeri için kargo uyarısı yok).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..db import rows
from . import app_settings

SEVERITIES = ("critical", "warning", "info")
SEVERITY_TR = {"critical": "Kritik", "warning": "Uyarı", "info": "Bilgi"}
CATEGORY_TR = {"supplier": "Tedarikçi", "marketplace": "Pazaryeri", "order": "Sipariş", "shipping": "Kargo",
               "product": "Ürün", "system": "Sistem"}

# Tarayıcının sahip olduğu (otomatik çözülebilen) kodlar
STATE_CODES = (
    "supplier.stock_zero", "supplier.stock_critical", "supplier.missing", "supplier.feed_failed",
    "supplier.data_error", "supplier.stale", "marketplace.connection_failed", "marketplace.sync_failed",
    "marketplace.listing_rejected", "marketplace.stock_mismatch", "marketplace.price_mismatch",
    "marketplace.drafts_not_ready", "order.needs_review", "shipping.overdue", "shipping.no_tracking",
)


def upsert(conn: Connection, a: dict) -> tuple[int, bool]:
    """Uyarıyı açar veya (aynı fingerprint açıksa) günceller. (id, yeni_mi) döner."""
    params = {"category": a["category"], "severity": a["severity"], "code": a["code"], "title": a["title"][:300],
              "description": (a.get("description") or "")[:2000], "source": a.get("source"),
              "fingerprint": a["fingerprint"], "product_id": a.get("product_id"), "supplier_id": a.get("supplier_id"),
              "supplier_product_id": a.get("supplier_product_id"), "marketplace_id": a.get("marketplace_id"),
              "order_id": a.get("order_id"), "link": a.get("link"),
              "details": json.dumps(a.get("details") or {}, default=str, ensure_ascii=False)}
    r = conn.execute(text("""
        INSERT INTO alerts(category, severity, code, title, description, source, fingerprint, product_id, supplier_id,
                           supplier_product_id, marketplace_id, order_id, link, details)
        VALUES (:category, :severity, :code, :title, :description, :source, :fingerprint, :product_id, :supplier_id,
                :supplier_product_id, :marketplace_id, :order_id, :link, CAST(:details AS JSONB))
        ON CONFLICT (fingerprint) WHERE status = 'open' DO UPDATE SET
               severity = EXCLUDED.severity, title = EXCLUDED.title, description = EXCLUDED.description,
               details = EXCLUDED.details, link = EXCLUDED.link, last_checked_at = NOW(),
               last_detected_at = CASE WHEN alerts.description IS DISTINCT FROM EXCLUDED.description
                                       THEN NOW() ELSE alerts.last_detected_at END,
               occurrences = alerts.occurrences + 1
        RETURNING id, (xmax = 0) AS inserted
    """), params).one()
    if r.inserted:
        from .notifications import dispatch
        dispatch(conn, r.id, a)
    return r.id, bool(r.inserted)


def raise_event(conn: Connection, a: dict) -> int:
    return upsert(conn, a)[0]


def resolve(conn: Connection, alert_id: int, user_id: int | None, note: str | None = None) -> bool:
    return bool(conn.execute(text("""
        UPDATE alerts SET status = 'resolved', resolved_at = NOW(), resolved_by = :u, resolution = :r
         WHERE id = :id AND status = 'open'
    """), {"id": alert_id, "u": user_id, "r": ("user: " + note)[:500] if note else "user"}).rowcount)


def _auto_resolve(conn: Connection, codes: tuple[str, ...], seen: set[str]) -> int:
    return conn.execute(text("""
        UPDATE alerts SET status = 'resolved', resolved_at = NOW(), resolution = 'system'
         WHERE status = 'open' AND code = ANY(:codes) AND NOT (fingerprint = ANY(:seen))
    """), {"codes": list(codes), "seen": list(seen) or [""]}).rowcount


def _listed_product_ids(conn: Connection) -> dict[int, list[str]]:
    """Katalog ürünü -> ilanda olduğu pazaryeri adları (ilan veya yayına hazır taslak)."""
    out: dict[int, list[str]] = {}
    for r in rows(conn, """
        SELECT DISTINCT p.id, m.name FROM products p
          JOIN marketplace_listings l ON l.product_id = p.id OR (p.barcode IS NOT NULL AND l.barcode = p.barcode)
          JOIN stores s ON s.id = l.store_id JOIN marketplaces m ON m.id = s.marketplace_id
        UNION
        SELECT DISTINCT d.product_id, m.name FROM listing_drafts d JOIN marketplaces m ON m.id = d.marketplace_id
         WHERE d.status = 'ready'
    """):
        out.setdefault(r["id"], []).append(r["name"])
    return out


def _last_stock_change(conn: Connection, sp_ids: list[int]) -> dict[int, str]:
    if not sp_ids:
        return {}
    return {r["supplier_product_id"]: r["old_value"] for r in rows(conn, """
        SELECT DISTINCT ON (supplier_product_id) supplier_product_id, old_value FROM supplier_product_changes
         WHERE kind = 'stock' AND supplier_product_id = ANY(:ids) ORDER BY supplier_product_id, id DESC
    """, ids=sp_ids)}


def _scan_suppliers(conn: Connection, found: list[dict]) -> None:
    threshold = int(app_settings.get(conn, "alerts.critical_stock_threshold", 2))
    listed = _listed_product_ids(conn)
    items = rows(conn, """
        SELECT sp.id, sp.supplier_id, s.name AS supplier_name, sp.product_id, sp.supplier_sku, sp.stock,
               COALESCE(sp.status, 'active') AS status, COALESCE(p.name, sp.name) AS name
          FROM supplier_products sp JOIN suppliers s ON s.id = sp.supplier_id
          JOIN products p ON p.id = sp.product_id
         WHERE s.is_active AND (COALESCE(sp.status, 'active') = 'missing' OR COALESCE(sp.stock, 0) <= :th)
    """, th=threshold)
    prev = _last_stock_change(conn, [i["id"] for i in items if (i["stock"] or 0) <= 0])
    for it in items:
        stores = listed.get(it["product_id"], [])
        base = {"category": "supplier", "source": "Tedarikçi", "product_id": it["product_id"],
                "supplier_id": it["supplier_id"], "supplier_product_id": it["id"],
                "link": f"#/products?id={it['product_id']}",
                "details": {"supplier": it["supplier_name"], "supplier_sku": it["supplier_sku"], "stores": stores}}
        store_txt = f" Etkilenen mağaza: {', '.join(stores)}." if stores else ""
        if it["status"] == "missing":
            found.append({**base, "code": "supplier.missing", "severity": "critical" if stores else "warning",
                          "fingerprint": f"supplier.missing:{it['id']}", "title": it["name"],
                          "description": f"Ürün {it['supplier_name']} kaynağında bulunamadı (XML/API'den kayboldu).{store_txt}"})
        elif (it["stock"] or 0) <= 0:
            old = prev.get(it["id"])
            found.append({**base, "code": "supplier.stock_zero", "severity": "critical" if stores else "warning",
                          "fingerprint": f"supplier.stock_zero:{it['id']}", "title": it["name"],
                          "description": (f"Stok {old} → 0" if old not in (None, "", "0") else "Stok 0")
                          + f" ({it['supplier_name']}).{store_txt}"})
        else:
            found.append({**base, "code": "supplier.stock_critical", "severity": "warning",
                          "fingerprint": f"supplier.stock_critical:{it['id']}", "title": it["name"],
                          "description": f"Stok kritik seviyede: {it['stock']} (eşik {threshold}, {it['supplier_name']}).{store_txt}"})
    for s in rows(conn, """
        SELECT s.id, s.name, s.last_sync_status, s.last_sync_error, s.last_sync_at, s.sync_interval_minutes,
               (SELECT r.error_count FROM supplier_sync_runs r WHERE r.supplier_id = s.id
                 ORDER BY r.started_at DESC, r.id DESC LIMIT 1) AS last_errors
          FROM suppliers s WHERE s.is_active
    """):
        base = {"category": "supplier", "source": "Tedarikçi", "supplier_id": s["id"], "link": f"#/suppliers?id={s['id']}"}
        if s["last_sync_status"] == "failed":
            found.append({**base, "code": "supplier.feed_failed", "severity": "critical", "title": s["name"],
                          "fingerprint": f"supplier.feed_failed:{s['id']}",
                          "description": f"Tedarikçi kaynağına erişilemedi veya okunamadı: {s['last_sync_error'] or 'bilinmeyen hata'}"})
        elif s["last_errors"]:
            found.append({**base, "code": "supplier.data_error", "severity": "warning", "title": s["name"],
                          "fingerprint": f"supplier.data_error:{s['id']}",
                          "description": f"Son senkronda {s['last_errors']} kayıt işlenemedi (eksik/bozuk ürün verisi)."})
        interval = int(s["sync_interval_minutes"] or 0)
        if interval and s["last_sync_at"] and \
                s["last_sync_at"] < datetime.now(timezone.utc) - timedelta(minutes=max(interval * 3, 60)):
            found.append({**base, "code": "supplier.stale", "severity": "warning", "title": s["name"],
                          "fingerprint": f"supplier.stale:{s['id']}",
                          "description": f"Tedarikçi {interval} dakikada bir senkronize edilmeliydi; son senkron gecikti."})


def _scan_marketplaces(conn: Connection, found: list[dict]) -> None:
    for m in rows(conn, """
        SELECT m.id, m.code, m.name, m.last_check_ok, m.last_check_message, c.status AS conn_status
          FROM marketplaces m LEFT JOIN marketplace_connections c ON c.marketplace_id = m.id
    """):
        base = {"category": "marketplace", "source": "Pazaryeri", "marketplace_id": m["id"], "link": "#/integrations"}
        if m["conn_status"] != "removed" and (m["conn_status"] == "failed" or m["last_check_ok"] is False):
            found.append({**base, "code": "marketplace.connection_failed", "severity": "critical", "title": m["name"],
                          "fingerprint": f"marketplace.connection_failed:{m['id']}",
                          "description": f"API bağlantısı başarısız: {m['last_check_message'] or 'bilgileri kontrol edin'}"})
        last = rows(conn, """SELECT job_type, status, last_error, finished_at FROM sync_jobs
                              WHERE marketplace = :c AND job_type IN ('orders.sync','listings.sync')
                              ORDER BY id DESC LIMIT 1""", c=m["code"])
        if last and last[0]["status"] in ("failed", "dead"):
            what = "Siparişler çekilemedi" if last[0]["job_type"] == "orders.sync" else "İlanlar okunamadı"
            found.append({**base, "code": "marketplace.sync_failed", "severity": "critical", "title": m["name"],
                          "fingerprint": f"marketplace.sync_failed:{m['id']}:{last[0]['job_type']}",
                          "description": f"{what}: {(last[0]['last_error'] or '')[:300]}"})
        n = conn.execute(text("SELECT COUNT(*) FROM listing_drafts WHERE marketplace_id = :m AND status = 'invalid'"),
                         {"m": m["id"]}).scalar()
        if n:
            found.append({**base, "code": "marketplace.drafts_not_ready", "severity": "info", "title": m["name"],
                          "fingerprint": f"marketplace.drafts_not_ready:{m['id']}", "link": "#/transfer?tab=drafts",
                          "description": f"{n} taslak ürün yayına hazır değil (kategori/özellik/fiyat eksik)."})
    for l in rows(conn, """
        SELECT l.id, l.title, l.status, l.listed_stock, l.listed_price, l.product_id, p.stock AS local_stock,
               m.id AS marketplace_id, m.name AS marketplace_name,
               (SELECT d.price FROM listing_drafts d WHERE d.product_id = l.product_id AND d.marketplace_id = m.id
                  AND d.status = 'ready') AS ready_price
          FROM marketplace_listings l JOIN stores s ON s.id = l.store_id JOIN marketplaces m ON m.id = s.marketplace_id
          LEFT JOIN products p ON p.id = l.product_id
    """):
        base = {"category": "marketplace", "source": "Pazaryeri", "marketplace_id": l["marketplace_id"],
                "product_id": l["product_id"], "link": f"#/products?id={l['product_id']}" if l["product_id"] else "#/products?tab=listings",
                "details": {"listing_id": l["id"]}}
        if l["status"] == "rejected":
            found.append({**base, "code": "marketplace.listing_rejected", "severity": "warning", "title": l["title"] or "İlan",
                          "fingerprint": f"marketplace.listing_rejected:{l['id']}",
                          "description": f"{l['marketplace_name']} ilanı reddedildi."})
        if l["product_id"] is not None and l["listed_stock"] is not None and l["local_stock"] is not None \
                and int(l["listed_stock"]) != int(l["local_stock"]):
            found.append({**base, "code": "marketplace.stock_mismatch", "severity": "warning", "title": l["title"] or "İlan",
                          "fingerprint": f"marketplace.stock_mismatch:{l['id']}",
                          "description": f"{l['marketplace_name']} stoğu {l['listed_stock']}, TrendHub stoğu {l['local_stock']}."})
        if l["ready_price"] is not None and l["listed_price"] is not None \
                and Decimal(l["ready_price"]) != Decimal(l["listed_price"]):
            found.append({**base, "code": "marketplace.price_mismatch", "severity": "info", "title": l["title"] or "İlan",
                          "fingerprint": f"marketplace.price_mismatch:{l['id']}",
                          "description": f"{l['marketplace_name']} fiyatı {l['listed_price']} TL, hazırlanan fiyat {l['ready_price']} TL."})


def _scan_orders(conn: Connection, found: list[dict]) -> None:
    for o in rows(conn, """SELECT id, external_order_id, review_reason FROM orders WHERE internal_status = 'needs_review'
                           AND order_date > NOW() - INTERVAL '60 days'"""):
        found.append({"category": "order", "source": "Sipariş", "code": "order.needs_review", "severity": "warning",
                      "order_id": o["id"], "fingerprint": f"order.needs_review:{o['id']}", "link": f"#/orders?open={o['id']}",
                      "title": f"Sipariş {o['external_order_id']}", "description": o["review_reason"] or "İnceleme gerekiyor."})
    # Kargo: yalnızca GERÇEK veriden. Planlanan kargo günü (TrendHub tahmini) geçtiği hâlde kargoya verilmemiş
    from .shipping_plan import attach
    overdue_h = int(app_settings.get(conn, "alerts.shipping_overdue_hours", 24))
    pending = rows(conn, """SELECT id, external_order_id, order_date, internal_status FROM orders
                            WHERE internal_status IN ('new','preparing','sent_to_supplier','awaiting_shipment')
                              AND order_date > NOW() - INTERVAL '30 days'""")
    attach(conn, pending)
    now = datetime.now(timezone.utc)
    for o in pending:
        sp = o["shipping_plan"]
        if sp.get("date"):
            from zoneinfo import ZoneInfo
            plan_end = datetime.fromisoformat(sp["date"]).replace(tzinfo=ZoneInfo("Europe/Istanbul")) + timedelta(days=1)
            if now > plan_end + timedelta(hours=overdue_h):
                found.append({"category": "shipping", "source": "Kargo", "code": "shipping.overdue", "severity": "warning",
                              "order_id": o["id"], "fingerprint": f"shipping.overdue:{o['id']}", "link": f"#/orders?open={o['id']}",
                              "title": f"Sipariş {o['external_order_id']}",
                              "description": f"Planlanan kargo günü {sp['date_label']} geçti, sipariş hâlâ kargoya verilmedi (tahmini plan)."})
    for o in rows(conn, """
        SELECT DISTINCT o.id, o.external_order_id FROM orders o JOIN shipments sh ON sh.order_id = o.id
         WHERE o.internal_status = 'shipped' AND (sh.tracking_number IS NULL OR sh.tracking_number = '')
           AND o.order_date > NOW() - INTERVAL '30 days'
    """):
        found.append({"category": "shipping", "source": "Kargo", "code": "shipping.no_tracking", "severity": "warning",
                      "order_id": o["id"], "fingerprint": f"shipping.no_tracking:{o['id']}", "link": f"#/orders?open={o['id']}",
                      "title": f"Sipariş {o['external_order_id']}", "description": "Sipariş kargoda görünüyor ancak takip numarası yok."})


def scan(conn: Connection) -> dict:
    """Tüm durum tabanlı kontrolleri çalıştırır; yeni/güncellenen/otomatik çözülen sayıları döner."""
    found: list[dict] = []
    _scan_suppliers(conn, found)
    _scan_marketplaces(conn, found)
    _scan_orders(conn, found)
    new = 0
    seen: set[str] = set()
    for a in found:
        if a["fingerprint"] in seen:
            continue
        seen.add(a["fingerprint"])
        new += upsert(conn, a)[1]
    resolved = _auto_resolve(conn, STATE_CODES, seen)
    return {"detected": len(seen), "new": new, "auto_resolved": resolved}


def summary(conn: Connection) -> dict:
    r = conn.execute(text("""
        SELECT COUNT(*) FILTER (WHERE severity = 'critical') AS critical,
               COUNT(*) FILTER (WHERE severity = 'warning') AS warning,
               COUNT(*) FILTER (WHERE severity = 'info') AS info, COUNT(*) AS total
          FROM alerts WHERE status = 'open'
    """)).mappings().one()
    return dict(r)


def price_change_event(conn: Connection, sp: dict, old: Decimal, new: Decimal, supplier: dict) -> None:
    """Senkronda alış fiyatı eşikten fazla değiştiyse olay tabanlı uyarı."""
    pct = Decimal(str(app_settings.get(conn, "alerts.price_change_pct", 20)))
    if not old or old <= 0 or new is None:
        return
    change = (new - old) / old * 100
    if abs(change) < pct:
        return
    raise_event(conn, {"category": "supplier", "source": "Tedarikçi", "code": "supplier.price_change",
                       "severity": "warning", "supplier_id": supplier["id"], "supplier_product_id": sp["id"],
                       "product_id": sp.get("product_id"),
                       "link": f"#/products?id={sp['product_id']}" if sp.get("product_id") else f"#/suppliers?id={supplier['id']}&tab=changes",
                       "fingerprint": f"supplier.price_change:{sp['id']}:{new}",
                       "title": sp.get("name") or sp.get("supplier_sku") or "Ürün",
                       "description": f"Alış fiyatı {old} → {new} TL (%{change.quantize(Decimal('0.1'))}, {supplier['name']})."})


def barcode_change_event(conn: Connection, sp: dict, old: str, new: str | None, supplier: dict) -> None:
    raise_event(conn, {"category": "supplier", "source": "Tedarikçi", "code": "supplier.barcode_change",
                       "severity": "warning", "supplier_id": supplier["id"], "supplier_product_id": sp["id"],
                       "product_id": sp.get("product_id"),
                       "link": f"#/suppliers?id={supplier['id']}&tab=changes",
                       "fingerprint": f"supplier.barcode_change:{sp['id']}:{new}",
                       "title": sp.get("name") or sp.get("supplier_sku") or "Ürün",
                       "description": f"Barkod değişti: {old} → {new or 'boş'} ({supplier['name']}). Katalog eşleşmesini kontrol edin."})
