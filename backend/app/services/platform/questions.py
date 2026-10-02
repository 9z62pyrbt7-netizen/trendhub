"""Trendyol müşteri soruları (SALT OKUNUR) + kural tabanlı sınıflandırma ve cevap ÖNERİSİ.

* Cevaplama servisi hiçbir yerde çağrılmaz; AI yalnızca öneri metni üretir (`suggested_answer`).
* Müşteri adı / müşteri id saklanmaz; soru metni telefon/e-posta/IBAN maskelenerek saklanır.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ...db import row
from .pii import scrub

# (kod, etiket, anahtar kelimeler) — sıra önemlidir: ilk eşleşen kategori seçilir
CATEGORIES: list[tuple[str, str, tuple[str, ...]]] = [
    ("shipping", "Kargo / teslim süresi", ("kargo", "ne zaman gönder", "ne zaman gelir", "teslim", "gönderim", "kaç günde",
                                           "kac gunde", "ne zaman kargo", "hızlı gelir", "bugün gönder")),
    ("color", "Renk", ("renk", "rengi", "siyah", "beyaz", "bej", "kahverengi", "taba", "lacivert", "kırmızı", "pembe",
                       "gri ", "vizon", "haki")),
    ("size", "Ölçü / ebat", ("boyut", "ölçü", "olcu", "ebat", "kaç cm", " cm", "genişli", "yüksekli", "derinli", "büyüklü",
                             "küçük mü", "büyük mü", "a4", "laptop", "sığar", "sigar", "inç", "inch", "en boy")),
    ("strap", "Askı / kayış", ("askı", "aski", "zincir", "kayış", "kayis", "omuz", "çıkarıl", "cikaril", "ayarlan", "sap ")),
    ("material", "Malzeme", ("malzeme", "deri", "suni", "kumaş", "kumas", "materyal", "su geçir", "su gecir", "hakiki", "kösele")),
    ("compartments", "İç bölme / cep / kapama", ("bölme", "bolme", "cep", "fermuar", "kilit", "astar", "içi ", "kapak", "mıknatıs")),
    ("stock", "Stok / bulunabilirlik", ("stok", "stoğ", "tekrar gel", "mevcut mu", "kalmadı", "tükendi", "ne zaman gelecek")),
    ("quality", "Kalite / orijinallik", ("orijinal", "kalite", "sağlam", "dayanıklı", "garanti")),
]
CATEGORY_TR = {c: label for c, label, _ in CATEGORIES} | {"other": "Diğer"}
# Ürün sayfasında eksik bilgi işareti sayılan kategoriler
INFO_GAP_CATEGORIES = {"size", "strap", "color", "material", "compartments"}

SUGGESTIONS = {
    "shipping": "Merhaba, siparişleriniz genellikle 1–2 iş günü içinde kargoya verilir; kargo takip bilgisi sipariş "
                "detayınızda görünür. [Gerçek kargoya veriliş süresini kontrol edin]",
    "color": "Merhaba, ürünün mevcut renk seçenekleri ürün sayfasındaki renk seçiminde listelenir; görünmeyen renk şu an "
             "stokta değildir. [Stok durumunu kontrol edin]",
    "size": "Merhaba, ürünün ölçüleri: [en x boy x derinlik cm — ürün kartına ekleyin]. Ölçüler ürün açıklamasında da yer alır.",
    "strap": "Merhaba, ürünün askısı [çıkarılabilir / sabit; ayarlanabilir uzunluk: ... cm — ürün kartına ekleyin].",
    "material": "Merhaba, ürünün malzemesi [malzeme bilgisi — ürün kartına ekleyin].",
    "compartments": "Merhaba, ürünün içinde [bölme / cep / kapama bilgisi — ürün kartına ekleyin] bulunur.",
    "stock": "Merhaba, ürün stokta göründüğü sürece sipariş verebilirsiniz; tükenen seçenekler için stok yenilendiğinde "
             "ürün sayfasında görünür olacaktır.",
    "quality": "Merhaba, ürünlerimiz orijinaldir; ürün özellikleri ürün sayfasında belirtilmiştir.",
}


def _lower_tr(s: str) -> str:
    return s.replace("I", "ı").replace("İ", "i").lower()


def classify(question: str | None) -> str:
    q = " " + _lower_tr(question or "") + " "
    for code, _label, words in CATEGORIES:
        if any(w in q for w in words):
            return code
    return "other"


def suggest(category: str) -> str | None:
    return SUGGESTIONS.get(category)


def _dt(v) -> datetime | None:
    if v in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(v) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def normalize_question(q: dict) -> dict | None:
    if not q.get("id") or not q.get("text"):
        return None
    ans = q.get("answer") or {}
    answer_text = ans.get("text") if isinstance(ans, dict) else None
    status = q.get("status")
    category = classify(q.get("text"))
    return {
        "external_id": str(q["id"]), "product_main_id": q.get("productMainId"), "barcode": q.get("barcode"),
        "product_name": q.get("productName"), "web_url": q.get("webUrl"), "question_text": scrub(q.get("text")),
        "status": status, "is_public": q.get("public"), "asked_at": _dt(q.get("creationDate")),
        "answer_text": scrub(answer_text), "answered_at": _dt(ans.get("creationDate")) if isinstance(ans, dict) else None,
        "category": category, "suggested_answer": None if answer_text else suggest(category),
    }


def _product_for(conn: Connection, q: dict) -> int | None:
    if q["barcode"]:
        pid = conn.execute(text("SELECT id FROM products WHERE barcode = :b ORDER BY id LIMIT 1"), {"b": q["barcode"]}).scalar()
        if pid:
            return pid
    if q["product_main_id"]:
        pid = conn.execute(text("SELECT id FROM products WHERE model_code = :m OR sku = :m ORDER BY (model_code = :m) DESC, id LIMIT 1"),
                           {"m": q["product_main_id"]}).scalar()
        if pid:
            return pid
    if q["product_name"]:
        return conn.execute(text("SELECT id FROM products WHERE name = :n ORDER BY id LIMIT 1"), {"n": q["product_name"]}).scalar()
    return None


def upsert_question(conn: Connection, store_id: int, q: dict) -> str | None:
    """Dönüş: QUESTION_CREATED | QUESTION_UPDATED | None"""
    prev = row(conn, "SELECT id, status FROM customer_questions WHERE store_id = :s AND external_id = :e", s=store_id, e=q["external_id"])
    pid = _product_for(conn, q)
    conn.execute(text("""
        INSERT INTO customer_questions(store_id, external_id, product_main_id, barcode, product_id, product_name, web_url,
            question_text, status, is_public, asked_at, answer_text, answered_at, category, suggested_answer)
        VALUES (:store, :external_id, :product_main_id, :barcode, :pid, :product_name, :web_url, :question_text, :status,
            :is_public, :asked_at, :answer_text, :answered_at, :category, :suggested_answer)
        ON CONFLICT (store_id, external_id) DO UPDATE SET status = EXCLUDED.status, answer_text = EXCLUDED.answer_text,
            answered_at = EXCLUDED.answered_at, is_public = EXCLUDED.is_public,
            product_id = COALESCE(EXCLUDED.product_id, customer_questions.product_id),
            suggested_answer = CASE WHEN EXCLUDED.answer_text IS NULL THEN EXCLUDED.suggested_answer ELSE NULL END,
            updated_at = NOW()"""), {"store": store_id, "pid": pid, **q})
    if prev is None:
        return "QUESTION_CREATED"
    return "QUESTION_UPDATED" if prev["status"] != q["status"] else None
