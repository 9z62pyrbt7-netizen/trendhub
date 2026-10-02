"""Customer Experience Analyzer — YALNIZCA gerçek soru ve iade kayıtlarından.

* Tekrarlayan sorular (kategori bazında), iade sebepleri, şikâyetler, ürün sayfası bilgi eksikleri.
* Yüzde yalnızca örnek yeterliyse (ürün başına en az `cx_min_sample` soru / satış) üretilir; değilse
  `INSUFFICIENT_DATA` ve yalnızca adet gösterilir.
* Müşteriye cevap GÖNDERİLMEZ; öneri metni sahibin onayı/düzenlemesi içindir.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from sqlalchemy.engine import Connection

from ...db import rows
from ..ai.config import thresholds
from .questions import CATEGORY_TR, INFO_GAP_CATEGORIES

COMPLAINT_HINTS = ("kusur", "hasar", "defo", "bozuk", "yırt", "kırık", "eksik", "yanlış", "farklı", "defect", "damage",
                   "wrong", "missing", "beğenmed")


def _pct(n: int, total: int, min_sample: int):
    if total < min_sample or not total:
        return None
    return (Decimal(n) / Decimal(total)).quantize(Decimal("0.001"))


def analyze(conn: Connection, days: int = 30) -> dict:
    th = thresholds(conn)
    ms = int(th["cx_min_sample"])
    q = rows(conn, """SELECT COALESCE(product_id::text, 'n:' || COALESCE(product_name, '?')) AS k, product_id,
                             MAX(product_name) AS product_name, category, COUNT(*) AS n,
                             COUNT(*) FILTER (WHERE answer_text IS NULL) AS unanswered
                        FROM customer_questions WHERE asked_at > NOW() - make_interval(days => :d)
                       GROUP BY 1, 2, category""", d=days)
    r = rows(conn, """SELECT r.product_id, COALESCE(MAX(p.name), MAX(r.product_name)) AS product_name,
                             COALESCE(r.reason_name, r.reason_code, 'Sebep belirtilmemiş') AS reason, COUNT(*) AS n
                        FROM marketplace_returns r LEFT JOIN products p ON p.id = r.product_id
                       WHERE COALESCE(r.claim_date, r.fetched_at) > NOW() - make_interval(days => :d)
                       GROUP BY r.product_id, 3""", d=days)
    sold = {x["product_id"]: int(x["units"]) for x in rows(conn, """
        SELECT i.product_id, SUM(i.quantity) AS units FROM order_items i JOIN orders o ON o.id = i.order_id
         WHERE o.order_date > NOW() - make_interval(days => :d) AND o.internal_status <> 'cancelled' AND i.product_id IS NOT NULL
         GROUP BY 1""", d=days)}

    products: dict[str, dict] = {}
    for x in q:
        p = products.setdefault(x["k"], {"product_id": x["product_id"], "name": x["product_name"], "questions": 0,
                                         "unanswered": 0, "by_category": defaultdict(int), "returns": 0,
                                         "return_reasons": defaultdict(int)})
        p["questions"] += int(x["n"])
        p["unanswered"] += int(x["unanswered"])
        p["by_category"][x["category"] or "other"] += int(x["n"])
    for x in r:
        k = str(x["product_id"]) if x["product_id"] else "n:" + (x["product_name"] or "?")
        p = products.setdefault(k, {"product_id": x["product_id"], "name": x["product_name"], "questions": 0, "unanswered": 0,
                                    "by_category": defaultdict(int), "returns": 0, "return_reasons": defaultdict(int)})
        p["returns"] += int(x["n"])
        p["return_reasons"][x["reason"]] += int(x["n"])

    out_products, gaps = [], []
    for p in products.values():
        cats = []
        for c, n in sorted(p["by_category"].items(), key=lambda kv: -kv[1]):
            share = _pct(n, p["questions"], ms)
            cats.append({"category": c, "label": CATEGORY_TR.get(c, c), "count": n, "share": share,
                         "status": "OK" if share is not None else "INSUFFICIENT_DATA"})
            if c in INFO_GAP_CATEGORIES and n >= 3:
                gaps.append({"product_id": p["product_id"], "name": p["name"], "category": c, "label": CATEGORY_TR[c],
                             "count": n, "questions": p["questions"], "share": share,
                             "text": (f"{p['name']}: son {days} günde {n} soru {CATEGORY_TR[c].lower()} hakkında"
                                      + (f" (tüm sorularının %{share * 100:.0f} kadarı)" if share is not None
                                         else f" (toplam {p['questions']} soru — yüzde için örnek yetersiz)")
                                      + "; ürün sayfasında bu bilgi eksik veya görünmüyor olabilir.")})
        units = sold.get(p["product_id"], 0) if p["product_id"] else 0
        rr = _pct(p["returns"], units, ms) if units else None
        reasons = [{"reason": k, "count": v, "complaint": any(h in k.lower() for h in COMPLAINT_HINTS)}
                   for k, v in sorted(p["return_reasons"].items(), key=lambda kv: -kv[1])]
        out_products.append({"product_id": p["product_id"], "name": p["name"], "questions": p["questions"],
                             "unanswered": p["unanswered"], "categories": cats, "returns": p["returns"],
                             "units_sold": units, "return_rate": rr,
                             "return_rate_status": "OK" if rr is not None else "INSUFFICIENT_DATA",
                             "return_reasons": reasons})
    out_products.sort(key=lambda x: -(x["questions"] + x["returns"] * 2))
    gaps.sort(key=lambda g: -g["count"])
    totals = rows(conn, """SELECT category, COUNT(*) AS n FROM customer_questions
                            WHERE asked_at > NOW() - make_interval(days => :d) GROUP BY 1 ORDER BY 2 DESC""", d=days)
    tq = sum(int(t["n"]) for t in totals)
    unanswered = rows(conn, """SELECT id, product_id, product_name, question_text, category, suggested_answer, asked_at
                                 FROM customer_questions WHERE answer_text IS NULL
                                  AND COALESCE(status, '') NOT IN ('REJECTED', 'REPORTED')
                                ORDER BY asked_at DESC NULLS LAST LIMIT 20""")
    complaints = [{"product_id": p["product_id"], "name": p["name"], "reason": x["reason"], "count": x["count"]}
                  for p in out_products for x in p["return_reasons"] if x["complaint"]]
    return {"days": days, "min_sample": ms, "total_questions": tq,
            "categories": [{"category": t["category"], "label": CATEGORY_TR.get(t["category"], t["category"]), "count": int(t["n"]),
                            "share": _pct(int(t["n"]), tq, ms)} for t in totals],
            "total_returns": sum(p["returns"] for p in out_products), "products": out_products[:30], "info_gaps": gaps[:10],
            "complaints": sorted(complaints, key=lambda c: -c["count"])[:10], "unanswered": unanswered,
            "has_data": bool(tq or any(p["returns"] for p in out_products)),
            "note": "Cevaplar otomatik GÖNDERİLMEZ; öneriler Trendyol satıcı panelinden sahibin onayıyla gönderilmelidir."}
