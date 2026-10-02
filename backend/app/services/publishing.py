"""Kontrollü pazaryeri yayını (WRITE) — önizleme + tek kullanımlık onay + idempotent kayıt.

Bir ürün pazaryerine ancak TÜM kapılar açıksa gönderilebilir:
  1. Yetkili kullanıcı (yalnızca yönetici; API katmanında zorunlu)
  2. Açık kullanıcı işlemi: önizleme -> tek kullanımlık, süreli onay anahtarı -> onay
  3. Mağaza bağlantısı doğrulanmış (son bağlantı testi başarılı)
  4. Ürün doğrulanmış (taslak 'yayına hazır', hata yok) — onay anında YENİDEN doğrulanır
  5. Pazaryerinin ürün oluşturma API'si resmi dokümantasyonla doğrulanmış (connector bayrağı)
  6. Sunucu genelinde pazaryeri yazma izni (varsayılan KAPALI)
  7. Bu mağaza için yayın izni (varsayılan KAPALI; yalnızca yayın desteği doğrulanmış connector'da açılabilir)

Bugün hiçbir connector'ın ürün oluşturma sözleşmesi doğrulanmadığından onaylar 'engellendi' olarak
kaydedilir ve pazaryerine İSTEK GÖNDERİLMEZ. Aynı ürün + aynı içerik için ikinci kez gönderim
`listing_publications.idempotency_key` tekilliğiyle engellenir.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..config import get_settings
from ..db import row, rows
from .listing_drafts import _manual_parts, _products, compute, marketplace_rule, revalidate

TOKEN_TTL = timedelta(minutes=15)
PUBLISH_JOB = "listing.publish"


class PublishError(Exception):
    """Kullanıcıya gösterilebilir Türkçe hata."""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def draft_payload(conn: Connection, draft: dict) -> dict:
    """Taslağın güncel hesaplanmış hâli (TrendHub nötr alanları; pazaryeri API şeması DEĞİLDİR)."""
    from .supplier_catalog import load_offers
    mp = {"id": draft["marketplace_id"], "code": draft["marketplace_code"]}
    rule, extra = marketplace_rule(conn, mp)
    product = _products(conn, [draft["product_id"]])[0]
    offers = load_offers(conn, [draft["product_id"]]).get(draft["product_id"], [])
    mcat, mattr = _manual_parts(draft)
    manual_price = Decimal(draft["price"]) if draft["price_is_manual"] and draft["price"] is not None else None
    c = compute(conn, product, mp, offers, rule, extra, manual_price=manual_price,
                manual_category=mcat, manual_attributes=mattr)
    payload = {"barcode": product["barcode"], "sku": product["sku"], "model_code": product["model_code"],
               "title": product["name"], "brand": product["brand"], "category_id": c["category_id"],
               "category_name": c["category_name"], "attributes": c["attributes"], "price": c["price"],
               "stock": c["stock"], "vat_rate": product["vat_rate"], "desi": product["desi"],
               "description": product["description"], "images": product["images"] or [],
               "supplier": c["supplier_name"]}
    return {"product": product, "rule": rule, "compute": c, "payload": payload}


def idempotency_key(marketplace_id: int, product_id: int, payload: dict) -> str:
    body = json.dumps(payload, default=str, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(f"{marketplace_id}:{product_id}:{body}".encode()).hexdigest()


def gates(conn: Connection, code: str, settings=None) -> list[dict]:
    """Mağaza düzeyindeki yayın kapıları (ürün doğrulaması hariç)."""
    from ..connectors.registry import CONNECTOR_CLASSES, get_connector
    settings = settings or get_settings()
    m = row(conn, """SELECT m.id, m.name, m.last_check_ok, c.status AS conn_status, COALESCE(c.write_enabled, FALSE) AS store_write
                       FROM marketplaces m LEFT JOIN marketplace_connections c ON c.marketplace_id = m.id
                      WHERE m.code = :c""", c=code)
    if m is None:
        raise PublishError("Pazaryeri bulunamadı")
    connector = get_connector(code, settings) if code in CONNECTOR_CLASSES else None
    connected = bool(connector and connector.is_configured() and m["last_check_ok"] and m["conn_status"] != "removed")
    supported = bool(connector and connector.listing_publish_supported)
    return [
        {"code": "connection", "ok": connected, "label": "Mağaza bağlantısı doğrulandı",
         "hint": None if connected else "Entegrasyonlar sayfasından bağlantıyı test edin."},
        {"code": "api_verified", "ok": supported, "label": "Ürün yayınlama API'si doğrulandı",
         "hint": None if supported else ("Bu pazaryerinin ürün oluşturma API'si henüz resmi dokümantasyonla "
                                         "doğrulanmadı. Taslakları CSV ile dışa aktarıp pazaryeri panelinden yükleyebilirsiniz.")},
        {"code": "server_write", "ok": bool(settings.connector_write_enabled), "label": "Sunucu genelinde yazma izni",
         "hint": None if settings.connector_write_enabled else "Güvenlik için pazaryerine yazma sunucu genelinde kapalı."},
        {"code": "store_write", "ok": bool(m["store_write"]), "label": "Bu mağaza için yayın izni",
         "hint": None if m["store_write"] else "Mağaza için yayın izni verilmedi."},
        _emergency_gate(conn),
    ]


def _emergency_gate(conn: Connection) -> dict:
    from .ai.config import emergency_stop
    stop = emergency_stop(conn)
    return {"code": "emergency_stop", "ok": not stop, "label": "Acil durdurma kapalı",
            "hint": "AI Control Center'da acil durdurma aktif: tüm yazma işlemleri durduruldu." if stop else None}


def _drafts(conn: Connection, ids: list[int], code: str) -> list[dict]:
    return rows(conn, """SELECT d.*, m.code AS marketplace_code, m.name AS marketplace_name
                           FROM listing_drafts d JOIN marketplaces m ON m.id = d.marketplace_id
                          WHERE d.id = ANY(:ids) AND m.code = :c ORDER BY d.id""", ids=ids, c=code)


def _evaluate(conn: Connection, draft_ids: list[int], code: str) -> tuple[list[dict], list[dict]]:
    """(gönderilebilir, engellenen) — her taslak yeniden doğrulanır."""
    revalidate(conn, draft_ids)
    sendable, blocked = [], []
    already = {r["idempotency_key"] for r in rows(conn, """SELECT idempotency_key FROM listing_publications
                                                         WHERE status IN ('queued','sent') AND draft_id = ANY(:ids)""",
                                                 ids=draft_ids)}
    found = _drafts(conn, draft_ids, code)
    for missing_id in sorted(set(draft_ids) - {d["id"] for d in found}):
        blocked.append({"draft_id": missing_id, "title": None, "reasons": ["Taslak bu mağazaya ait değil veya bulunamadı"]})
    for d in found:
        p = draft_payload(conn, d)
        item = {"draft_id": d["id"], "product_id": d["product_id"], "title": p["payload"]["title"],
                "barcode": p["payload"]["barcode"], "price": p["payload"]["price"], "stock": p["payload"]["stock"],
                "idempotency_key": idempotency_key(d["marketplace_id"], d["product_id"], p["payload"])}
        reasons = []
        if d["status"] == "cancelled":
            reasons.append("Taslak iptal edilmiş")
        if p["compute"]["errors"]:
            reasons.append("Bu ürün yayınlanmaya hazır değil.")
            reasons.extend(p["compute"]["errors"])
        elif d["status"] != "ready":
            reasons.append("Önce 'Yayına Hazırla' adımını tamamlayın")
        if item["idempotency_key"] in already:
            reasons.append("Bu ürün aynı içerikle zaten gönderildi / kuyrukta (tekrar gönderilmez)")
        if reasons:
            blocked.append({**item, "reasons": reasons})
        else:
            sendable.append(item)
    return sendable, blocked


def preview(conn: Connection, code: str, draft_ids: list[int], user_id: int) -> dict:
    sendable, blocked = _evaluate(conn, draft_ids, code)
    g = gates(conn, code)
    mp = row(conn, "SELECT id, name FROM marketplaces WHERE code = :c", c=code)
    can_confirm = bool(sendable) and all(x["ok"] for x in g)
    token = secrets.token_urlsafe(32)
    rid = conn.execute(text("""
        INSERT INTO publish_requests(marketplace_id, token_hash, draft_ids, preview, requested_by, expires_at)
        VALUES (:m, :h, CAST(:ids AS JSONB), CAST(:pv AS JSONB), :u, NOW() + make_interval(secs => :ttl))
        RETURNING id, expires_at
    """), {"m": mp["id"], "h": _hash(token), "ids": json.dumps([s["draft_id"] for s in sendable]),
           "pv": json.dumps({"sendable": len(sendable), "blocked": len(blocked), "gates": g}, default=str),
           "u": user_id, "ttl": int(TOKEN_TTL.total_seconds())}).one()
    return {
        "request_id": rid.id, "token": token, "expires_at": rid.expires_at, "marketplace": code,
        "marketplace_name": mp["name"], "sendable": sendable, "blocked": blocked, "gates": g,
        "can_confirm": can_confirm,
        "summary": (f"{len(sendable)} ürün {mp['name']} mağazasına gönderilecek" if sendable
                    else f"{mp['name']} mağazasına gönderilebilecek ürün yok"),
        "blocked_summary": f"{len(blocked)} ürün gönderilmeyecek" if blocked else None,
        "notice": None if can_confirm else "Yayın kapıları kapalı: onaylansa bile pazaryerine istek GÖNDERİLMEZ.",
    }


def confirm(conn: Connection, token: str, user_id: int) -> dict:
    """Tek kullanımlık onay. Kapılar kapalıysa her taslak 'blocked' kaydedilir; pazaryerine istek gitmez."""
    req = conn.execute(text("""
        UPDATE publish_requests SET status = 'confirmed', confirmed_by = :u, confirmed_at = NOW()
         WHERE token_hash = :h AND status = 'pending' AND expires_at > NOW() AND requested_by = :u
        RETURNING id, marketplace_id, draft_ids
    """), {"h": _hash(token), "u": user_id}).mappings().first()
    if req is None:
        old = row(conn, "SELECT status, expires_at < NOW() AS expired, requested_by FROM publish_requests WHERE token_hash = :h",
                  h=_hash(token))
        if old is None or old["requested_by"] != user_id:
            raise PublishError("Onay anahtarı geçersiz. Önizlemeyi yeniden oluşturun.")
        if old["status"] != "pending":
            raise PublishError("Bu onay zaten kullanıldı. Aynı ürünler ikinci kez gönderilmez.")
        conn.execute(text("UPDATE publish_requests SET status = 'expired' WHERE token_hash = :h"), {"h": _hash(token)})
        raise PublishError("Onay süresi doldu. Önizlemeyi yeniden oluşturun.")
    code = conn.execute(text("SELECT code FROM marketplaces WHERE id = :i"), {"i": req["marketplace_id"]}).scalar()
    ids = list(req["draft_ids"] or [])
    sendable, blocked = _evaluate(conn, ids, code) if ids else ([], [])
    g = gates(conn, code)
    open_all = all(x["ok"] for x in g)
    gate_msg = "; ".join(x["label"] + ": HAYIR" for x in g if not x["ok"])
    queued, blocked_ids, skipped = [], [], 0
    for s in sendable:
        status = "queued" if open_all else "blocked"
        pub = conn.execute(text("""
            INSERT INTO listing_publications(request_id, draft_id, product_id, marketplace_id, idempotency_key, status,
                                             message, created_by)
            VALUES (:r, :d, :p, :m, :k, :st, :msg, :u)
            ON CONFLICT (idempotency_key) DO UPDATE SET status = EXCLUDED.status, message = EXCLUDED.message,
                   request_id = EXCLUDED.request_id, updated_at = NOW()
             WHERE listing_publications.status IN ('blocked','failed')
            RETURNING id
        """), {"r": req["id"], "d": s["draft_id"], "p": s["product_id"], "m": req["marketplace_id"],
               "k": s["idempotency_key"], "st": status, "u": user_id,
               "msg": None if open_all else f"Gönderilmedi — kapalı kapılar: {gate_msg}"}).scalar()
        if pub is None:
            skipped += 1
        elif open_all:
            queued.append(pub)
        else:
            blocked_ids.append(pub)
    result = {"queued": len(queued), "blocked": len(blocked_ids), "skipped_duplicates": skipped,
              "not_ready": len(blocked), "sent_to_marketplace": 0, "gates": g}
    if queued:
        from . import jobs
        jobs.enqueue(conn, PUBLISH_JOB, marketplace=code, payload={"publication_ids": queued},
                     idempotency_key=f"{PUBLISH_JOB}:{req['id']}", max_attempts=1)
        result["message"] = f"{len(queued)} ürün gönderim kuyruğuna alındı"
    else:
        result["message"] = ("Pazaryerine istek GÖNDERİLMEDİ. " + (f"Kapalı kapılar: {gate_msg}." if not open_all else "")
                             ).strip()
    conn.execute(text("UPDATE publish_requests SET status = :st, result = CAST(:r AS JSONB) WHERE id = :i"),
                 {"st": "confirmed" if queued else "blocked", "r": json.dumps(result, default=str), "i": req["id"]})
    return result


def run_publish_job(engine, job: dict, settings=None) -> dict:
    """Worker: kuyruktaki yayınları gönderir. Kapılar gönderim anında YENİDEN kontrol edilir."""
    from ..connectors.base import ConnectorError
    from ..connectors.registry import get_connector
    ids = list((job.get("payload") or {}).get("publication_ids") or [])
    sent = failed = blocked = 0
    with engine.begin() as conn:
        code = job["marketplace"]
        g = gates(conn, code, settings)
        pubs = rows(conn, """SELECT p.*, d.*, p.id AS pub_id, m.code AS marketplace_code FROM listing_publications p
                               JOIN listing_drafts d ON d.id = p.draft_id JOIN marketplaces m ON m.id = p.marketplace_id
                              WHERE p.id = ANY(:ids) AND p.status = 'queued' ORDER BY p.id FOR UPDATE OF p""", ids=ids)
        if not all(x["ok"] for x in g):
            conn.execute(text("""UPDATE listing_publications SET status = 'blocked', message = 'Gönderim anında kapı kapalı',
                                        updated_at = NOW() WHERE id = ANY(:ids) AND status = 'queued'"""), {"ids": ids})
            return {"sent": 0, "failed": 0, "blocked": len(pubs)}
        connector = get_connector(code, settings)
        for p in pubs:
            payload = draft_payload(conn, p)["payload"]
            try:
                ref = connector.publish_listing(payload)
                conn.execute(text("""UPDATE listing_publications SET status = 'sent', external_ref = :r, attempts = attempts + 1,
                                            message = 'Gönderildi', updated_at = NOW() WHERE id = :i"""),
                             {"r": None if ref is None else str(ref), "i": p["pub_id"]})
                sent += 1
            except ConnectorError as exc:
                conn.execute(text("""UPDATE listing_publications SET status = 'failed', attempts = attempts + 1,
                                            message = :m, updated_at = NOW() WHERE id = :i"""),
                             {"m": str(exc)[:500], "i": p["pub_id"]})
                failed += 1
    return {"sent": sent, "failed": failed, "blocked": blocked}


def set_store_write(conn: Connection, code: str, enabled: bool) -> None:
    """Mağaza yayın izni. Yalnızca yayın API'si doğrulanmış connector için AÇILABİLİR."""
    from ..connectors.registry import CONNECTOR_CLASSES
    cls = CONNECTOR_CLASSES.get(code)
    if enabled and not (cls and cls.listing_publish_supported):
        raise PublishError("Bu pazaryerinin ürün yayınlama API'si doğrulanmadığı için yayın izni açılamaz.")
    n = conn.execute(text("""UPDATE marketplace_connections c SET write_enabled = :e, updated_at = NOW()
                               FROM marketplaces m WHERE m.id = c.marketplace_id AND m.code = :c AND c.status <> 'removed'"""),
                     {"e": enabled, "c": code}).rowcount
    if not n and enabled:
        raise PublishError("Önce mağaza bağlantısını panelden kaydedin.")
