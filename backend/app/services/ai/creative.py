"""Kreatif ajanı üreticisi (Ad Creative Strategist uyarlaması).

Şablon tabanlıdır (generator='template'): girdiler ürünün GERÇEK verisidir (ad, kategori, birim kâr, en çok sorulan konu,
iade sebebi). Ürün adı/soru metni dış içeriktir; temizlenir ve yalnızca metin olarak kullanılır. Kreatif performansı,
taslak bir kampanyaya bağlandığında o kampanyanın gerçek ROAS/net kârıyla değerlendirilir (`performance`).
"""
from __future__ import annotations

from sqlalchemy.engine import Connection

from ...db import row, rows
from . import sanitize
from .config import tl

INFO = {"Ölçü / ebat": ("ölçüleri ekranda yazıyla göster; içine A4 ve telefon koyarak kanıtla", "Ölçüsü tam istediğin gibi"),
        "Askı / kayış": ("askının takılıp çıkarıldığını ve uzunluk ayarını göster", "Askısı ayarlanabilir, omuzda rahat"),
        "Renk": ("tüm renkleri gün ışığında yan yana göster", "Gerçek renkleri gün ışığında"),
        "Malzeme": ("malzemeyi yakın çekimde göster", "Malzemesini yakından gör"),
        "İç bölme / cep / kapama": ("iç bölmeleri açarak tek tek göster", "İçinde her şeyin bir yeri var")}


def build_variants(conn: Connection, product_id: int, n: int = 2) -> tuple[list[dict], dict]:
    from .growth import _audience, top_question_category
    p = row(conn, "SELECT id, name, category, sale_price FROM products WHERE id = :i", i=product_id)
    if p is None:
        from .tools import ToolError
        raise ToolError("Ürün bulunamadı")
    name = sanitize.clean_text(p["name"], 80)
    who, use = _audience(name, p["category"])
    q = top_question_category(conn, product_id)
    show, proof = INFO.get(q or "", ("ürünü kullanımda (omuzda/elde) göster", "Her kombine uyar"))
    price = tl(p["sale_price"]) if p["sale_price"] else None
    base = [
        {"variant": "A", "concept": "Problem → çözüm (fayda odaklı)",
         "hook": f"{use.capitalize()} için tek çanta arıyorsan: {name}",
         "headline": f"{name} — {proof}", "primary_text": f"{who} için tasarlandı. {proof}. Trendyol'da Trendçantanız mağazasında.",
         "cta": "Hemen İncele", "video_concept": f"9:16, 12–15 sn. 0–2 sn hook; sonra {show}; son 3 sn fiyat + CTA.",
         "image_concept": "Beyaz zemin + model omzunda kullanım, sağ üstte tek fayda cümlesi"},
        {"variant": "B", "concept": "Sosyal kanıt / merak",
         "hook": f"Bu çanta neden bu kadar çok tercih ediliyor? {name}",
         "headline": f"{name}" + (f" — {price}" if price else ""),
         "primary_text": f"Müşterilerimizin en çok sorduğu: {q.lower() if q else 'kullanım'} — videoda cevabı var.",
         "cta": "Trendyol'da Gör", "video_concept": f"9:16, 15–20 sn. Kutudan çıkarma; {show}; kapanışta mağaza adı.",
         "image_concept": "Yan yana renk seçenekleri, gün ışığı"},
        {"variant": "C", "concept": "Kullanım senaryosu", "hook": f"Bir günüm bu çantayla: {name}",
         "headline": f"{use.capitalize()} için {name}", "primary_text": f"Sabahtan akşama {use}. {proof}.",
         "cta": "Satın Al", "video_concept": "9:16, 20 sn. Gün içi 3 sahne, her sahnede çanta görünür.",
         "image_concept": "Lifestyle: ofis/şehir arka planı"},
        {"variant": "D", "concept": "Fiyat/değer", "hook": f"Bu fiyata bu kalite: {name}",
         "headline": f"{name}" + (f" sadece {price}" if price else ""), "primary_text": f"{proof}. Hızlı kargo, kolay iade.",
         "cta": "Fırsatı Gör", "video_concept": "9:16, 10 sn. Fiyat etiketi + 3 hızlı detay çekimi.",
         "image_concept": "Ürün tek başına, fiyat rozeti"},
    ]
    basis = {"product_id": product_id, "audience": who, "use_case": use, "top_question": q, "price": str(p["sale_price"] or ""),
             "ab_test": "A ve B aynı bütçe ve hedef kitleyle en az 7 gün / 100 tıklama karşılaştırılır; ölçüt reklam sonrası net kâr."}
    return base[:n], basis


def performance(conn: Connection, product_id: int | None = None) -> list[dict]:
    """Kreatif → bağlı kampanya → gerçek sonuç. Bağlı kampanya yoksa performans 'ölçülmedi'."""
    from .config import Window, thresholds
    from .data import campaign_performance
    perf = {c["id"]: c for c in campaign_performance(conn, Window(thresholds(conn)["ads_window_days"]))}
    out = []
    for r in rows(conn, """SELECT id, product_id, campaign_id, variant, concept, headline, status, created_at FROM ai_creatives
                            WHERE (CAST(:p AS BIGINT) IS NULL OR product_id = :p) ORDER BY id DESC LIMIT 100""", p=product_id):
        c = perf.get(r["campaign_id"]) if r["campaign_id"] else None
        out.append({**r, "measured": bool(c and c["attributed_revenue"] is not None),
                    "roas": c["roas"] if c else None, "ad_net_profit": c["ad_net_profit"] if c else None})
    return out
