"""Büyüme ajanları (deterministik, gerçek veriye dayalı; platforma YAZMAZ):

  Fiyat          birim ekonomisi min. kâr/marj sınırının altındaysa kontrollü fiyat artışı önerir (adım ≤ max_price_change_pct);
                 maliyeti bilinmeyen ürüne fiyat önerisi YOK (BLOCKED / NEEDS_DATA). Fiyat düşürme önermez: talep esnekliği
                 verisi yok.
  Kampanya       indirim yalnızca indirim sonrası birim net kâr ve marj sınırı korunuyorsa önerilir; en fazla güvenli indirimi
                 hesaplar; sahibin planladığı indirimi `check_discount` ile değerlendirir.
  Ürün Takibi    son 7 gün / önceki 7 gün: adet, net kâr, marj, fiyat değişimi ve stok → CEO'ya sinyal.
  Reklam (ürün)  reklam verilecek ürünü seçer: STAR/PROFITABLE + stok güvenli + maliyet bilinen + satış geçmişi; aktif kampanyası
                 olmayan en iyi adaylar için "uygulanabilir öneri" (ads.create_campaign). Platform bağlı değilse uygulanmaz.
  Pazarlama      hangi ürün, neden, hangi hedef kitle, hangi teklif — ürün ve satış verisinden.
  Sosyal Medya   ürün bazlı hook / caption / CTA / kreatif brief. Instagram/TikTok bağlı değil: YAYINLANMAZ, yalnızca plan.
  Müşteri Den.   gerçek soru/iade verisinden ürün sayfası bilgi eksikleri (veri yoksa çalışır ama öneri üretmez).
"""
from __future__ import annotations

from decimal import ROUND_UP, Decimal

from sqlalchemy.engine import Connection

from ...db import rows
from . import actions
from .config import Window, d, thresholds, tl
from .economics import floor_price, max_safe_discount, simulate, unit_economics
from .proposals import activity, propose, retire_stale


def _pct(v) -> str:
    return "—" if v is None else f"%{d(v) * 100:.1f}"


def _round_price(v: Decimal) -> Decimal:
    """Fiyatı yukarı doğru ,90 ile biten tutara yuvarlar (ör. 412,13 → 412,90)."""
    whole = int(v)
    p = Decimal(whole) + Decimal("0.90")
    return p if p >= v else Decimal(whole + 1) + Decimal("0.90")


# ------------------------------------------------------------------ Fiyat
def run_pricing(conn: Connection, ctx) -> dict:
    from .agents import _quality_warnings, classified_products
    th = thresholds(conn)
    _quality_warnings(conn, ctx, {"orders_stale"})
    ctx.sources += ["birim ekonomisi (finance_view)", "ürün maliyeti", "komisyon/kargo (gerçek veya ayar)"]
    limit = int(th["max_price_changes_per_cycle"])
    candidates, needs_data = [], 0
    for p in classified_products(conn):
        if p["units"] < th["min_units_for_data"]:
            continue
        e = unit_economics(conn, p["product_id"])
        if e["missing"]:
            needs_data += 1
            actions.block(conn, agent="pricing", action_type="pricing.change_price", entity_type="product",
                          entity_id=p["product_id"], reason_code="needs_data", cycle_id=ctx.run_id,
                          reason=f"{p['name']}: {', '.join(e['missing'])} eksik; otomatik fiyat değişikliği yapılmaz (NEEDS_DATA).",
                          input_data={"units": p["units"], "missing": e["missing"]})
            continue
        fp = floor_price(e, th)
        if fp is None or e["price"] >= fp:
            continue
        step_max = (d(e["price"]) * (1 + d(th["max_price_change_pct"]))).quantize(Decimal("0.01"), rounding=ROUND_UP)
        new = min(_round_price(fp), step_max) if _round_price(fp) <= step_max else step_max
        gain = (simulate(e, new)["unit_profit"] - e["unit_profit"]) * p["units"]
        candidates.append((gain, p, e, fp, new))
    candidates.sort(key=lambda x: -x[0])
    n = 0
    for gain, p, e, fp, new in candidates:
        if n >= limit:
            actions.skip(conn, agent="pricing", action_type="pricing.change_price", entity_type="product", entity_id=p["product_id"],
                         reason=f"Döngü başına en fazla {limit} fiyat değişikliği; sonraki döngüye kaldı.", reason_code="cycle_limit",
                         cycle_id=ctx.run_id, input_data={"price": e["price"], "floor_price": fp})
            continue
        sim = simulate(e, new)
        reaches = new >= fp
        reason = (f"{p['name']}: mevcut {tl(e['price'])} fiyatta birim net kâr {tl(e['unit_profit'])} (marj {_pct(e['net_margin'])}); "
                  f"alt sınır {tl(th['min_unit_profit'])} / {_pct(th['min_net_margin'])}. {tl(new)} fiyatta birim net kâr "
                  f"{tl(sim['unit_profit'])} (marj {_pct(sim['net_margin'])}). "
                  + ("" if reaches else f"Hedef fiyat {tl(fp)}; tek adımda en fazla {_pct(th['max_price_change_pct'])} artış, kalan sonraki adımda. ")
                  + "Talep esnekliği bilinmiyor: satış adedi düşebilir; 7 gün sonra ölçülecek.")
        propose(conn, agent="pricing", action_type="pricing.change_price", entity_type="product", entity_id=p["product_id"],
                title=f"Fiyat: {p['name']} {tl(e['price'])} → {tl(new)}", reason=reason, run_id=ctx.run_id,
                evidence={"units": p["units"], "price": e["price"], "unit_cost": e["unit_cost"], "commission_rate": e["commission_rate"],
                          "shipping": e["shipping"], "ads": e["ads"], "unit_profit": e["unit_profit"], "basis": e["basis"]},
                params={"old_price": e["price"], "new_price": new, "unit_profit_old": e["unit_profit"],
                        "unit_profit_new": sim["unit_profit"], "floor_price": fp},
                expected_result={"measure_after_days": 7, "metric": "ürün net kârı",
                                 "expected_profit_change_same_volume": (gain if gain else 0)}, confidence=0.5)
        n += 1
    retired = retire_stale(conn, "pricing", ctx.run_id)
    ctx.output = {"candidates": len(candidates), "proposals": n, "needs_data": needs_data,
                  "skipped_cycle_limit": max(0, len(candidates) - limit), "retired": retired}
    activity(conn, f"Fiyat ajanı: {n} fiyat önerisi, {needs_data} üründe maliyet eksik (fiyat kararı yok)", agent="pricing",
             kind="run", run_id=ctx.run_id)
    return ctx.output


# ------------------------------------------------------------------ Kampanya
def check_discount(conn: Connection, product_id: int, rate) -> dict:
    """Sahibin düşündüğü indirimin net kâra etkisi. Sınırı bozan indirim BLOCKED."""
    th = thresholds(conn)
    e = unit_economics(conn, product_id)
    rate = d(rate)
    if e["missing"]:
        return {"status": "BLOCKED", "reason_code": "needs_data", "reason": "NEEDS_DATA: " + ", ".join(e["missing"]) + " eksik."}
    new_price = (d(e["price"]) * (1 - rate)).quantize(Decimal("0.01"))
    sim = simulate(e, new_price)
    safe = max_safe_discount(e, th)
    ok = sim["unit_profit"] >= d(th["min_unit_profit"]) and (sim["net_margin"] or 0) >= d(th["min_net_margin"])
    return {"status": "ALLOWED" if ok else "BLOCKED", "reason_code": None if ok else "campaign_loss", "price": e["price"],
            "discounted_price": new_price, "unit_profit_before": e["unit_profit"], "unit_profit_after": sim["unit_profit"],
            "net_margin_after": sim["net_margin"], "max_safe_discount": safe, "basis": e["basis"],
            "reason": (f"%{rate * 100:.0f} indirimle birim net kâr {tl(sim['unit_profit'])} (marj {_pct(sim['net_margin'])}). "
                       + ("Sınır korunuyor." if ok else f"Alt sınır {tl(th['min_unit_profit'])} / {_pct(th['min_net_margin'])} bozuluyor; ")
                       + ("" if ok else f"en fazla güvenli indirim {_pct(safe) if safe else 'yok'}."))}


def run_campaign(conn: Connection, ctx) -> dict:
    from .agents import classified_products
    from .data import inventory_status
    th = thresholds(conn)
    ctx.sources += ["birim ekonomisi", "ürün takibi (7g/7g)", "stok"]
    trend = {t["product_id"]: t for t in tracking_changes(conn)}
    prods = [p for p in classified_products(conn) if p["class"] in ("STAR", "PROFITABLE")]
    inv = {i["product_id"]: i for i in inventory_status(conn, [p["product_id"] for p in prods])} if prods else {}
    n = 0
    for p in prods:
        t = trend.get(p["product_id"])
        if not t or t["units_change"] is None or t["units_change"] > -d(th["tracking_change_pct"]):
            continue        # yalnızca satışı belirgin düşen kârlı ürünler için indirim düşünülür (ciro için değil)
        i = inv.get(p["product_id"])
        if not i or i["available"] < int(th["stock_safety_units"]) or i["stockout_risk"]:
            continue
        e = unit_economics(conn, p["product_id"])
        safe = max_safe_discount(e, th) if not e["missing"] else None
        if not safe or safe < Decimal("0.05"):
            actions.skip(conn, agent="campaign", action_type="campaign.discount", entity_type="product", entity_id=p["product_id"],
                         reason_code="no_margin_headroom", cycle_id=ctx.run_id,
                         reason=f"{p['name']}: satış düştü ama marj payı indirime izin vermiyor (en fazla güvenli indirim {_pct(safe)}).")
            continue
        rate = min(Decimal("0.10"), (safe * Decimal("0.8")).quantize(Decimal("0.01")))
        res = check_discount(conn, p["product_id"], rate)
        propose(conn, agent="campaign", action_type="campaign.discount", entity_type="product", entity_id=p["product_id"],
                title=f"İndirim: {p['name']} %{rate * 100:.0f}", run_id=ctx.run_id,
                reason=(f"Son 7 günde satış {_pct(t['units_change'])} değişti ({t['units_prev']} → {t['units_now']} adet). "
                        + res["reason"] + " Ciro için değil, kârlı ürünün satış hızını geri kazanmak için."),
                evidence={"units_now": t["units_now"], "units_prev": t["units_prev"], "unit_profit": e["unit_profit"],
                          "max_safe_discount": safe},
                params={"discount_rate": rate, "discount_pct": int(rate * 100), "min_price": res["discounted_price"]},
                expected_result={"measure_after_days": 7, "unit_profit_after": res["unit_profit_after"]}, confidence=0.4)
        n += 1
    retired = retire_stale(conn, "campaign", ctx.run_id)
    ctx.output = {"evaluated": len(prods), "proposals": n, "retired": retired}
    return ctx.output


# ------------------------------------------------------------------ Ürün takibi
def tracking_changes(conn: Connection) -> list[dict]:
    from .data import product_economics
    th = thresholds(conn)
    now_w = Window(7)
    prev_w = now_w.previous()
    now = {e["product_id"]: e for e in product_economics(conn, now_w)}
    prev = {e["product_id"]: e for e in product_economics(conn, prev_w)}
    out = []
    for pid in set(now) | set(prev):
        a, b = now.get(pid), prev.get(pid)
        ua, ub = (a or {}).get("units", 0), (b or {}).get("units", 0)
        if max(ua, ub) < int(th["tracking_min_units"]):
            continue
        ch = (Decimal(ua - ub) / Decimal(ub)) if ub else None
        pa = d(a["net_sales"]) / ua if a and ua else None
        pb = d(b["net_sales"]) / ub if b and ub else None
        out.append({"product_id": pid, "name": (a or b)["name"], "units_now": ua, "units_prev": ub,
                    "units_change": ch.quantize(Decimal("0.001")) if ch is not None else None,
                    "profit_now": (a or {}).get("net_profit", Decimal("0")), "profit_prev": (b or {}).get("net_profit", Decimal("0")),
                    "margin_now": (a or {}).get("net_margin"), "margin_prev": (b or {}).get("net_margin"),
                    "price_change": ((pa - pb) / pb).quantize(Decimal("0.001")) if pa and pb else None})
    return out


def run_product_tracking(conn: Connection, ctx) -> dict:
    from .data import inventory_status
    th = thresholds(conn)
    ctx.sources += ["product_economics 7g vs önceki 7g", "stok"]
    changes = tracking_changes(conn)
    stock = {i["product_id"]: i for i in inventory_status(conn, [c["product_id"] for c in changes])} if changes else {}
    signals = []
    for c in changes:
        why = []
        if c["units_change"] is not None and abs(c["units_change"]) >= d(th["tracking_change_pct"]):
            why.append(f"satış adedi {_pct(c['units_change'])} ({c['units_prev']} → {c['units_now']})")
        elif c["units_prev"] == 0 and c["units_now"] >= int(th["tracking_min_units"]):
            why.append(f"yeni hareket: önceki hafta 0, bu hafta {c['units_now']} adet")
        if c["margin_now"] is not None and c["margin_prev"] is not None and d(c["margin_prev"]) - d(c["margin_now"]) >= Decimal("0.05"):
            why.append(f"net marj {_pct(c['margin_prev'])} → {_pct(c['margin_now'])}")
        if c["price_change"] is not None and abs(c["price_change"]) >= Decimal("0.05"):
            why.append(f"ortalama satış fiyatı {_pct(c['price_change'])}")
        s = stock.get(c["product_id"])
        if s and s["stockout_risk"]:
            why.append(f"stok riski (kullanılabilir {s['available']})")
        if not why:
            continue
        signals.append({**c, "why": why})
        propose(conn, agent="product_tracking", action_type="tracking.alert", entity_type="product", entity_id=c["product_id"],
                title=f"Değişim: {c['name']}", reason="Son 7 gün / önceki 7 gün: " + "; ".join(why) + ".", run_id=ctx.run_id,
                requires_approval=False, evidence={k: c[k] for k in ("units_now", "units_prev", "units_change", "profit_now",
                                                                      "profit_prev", "margin_now", "margin_prev", "price_change")},
                confidence=0.8)
    retired = retire_stale(conn, "product_tracking", ctx.run_id)
    ctx.output = {"compared": len(changes), "signals": len(signals), "retired": retired,
                  "top": [{"name": s["name"], "why": s["why"]} for s in signals[:5]]}
    return ctx.output


# ------------------------------------------------------------------ Reklam: ürün seçimi
def ad_candidates(conn: Connection) -> list[dict]:
    """Reklam için en uygun ürünler: sınıf + birim kâr + stok güvenliği + satış hızı (+ varsa reklam dönüşümü)."""
    from .agents import classified_products
    from .data import inventory_status
    th = thresholds(conn)
    prods = [p for p in classified_products(conn) if p["class"] in ("STAR", "PROFITABLE") and not p["missing_cost"]]
    if not prods:
        return []
    inv = {i["product_id"]: i for i in inventory_status(conn, [p["product_id"] for p in prods])}
    active = {r["product_id"] for r in rows(conn, """SELECT x.product_id FROM ad_campaign_products x JOIN ad_campaigns c ON c.id = x.campaign_id
                                                       WHERE c.status = 'active'""")}
    out = []
    for p in prods:
        i = inv.get(p["product_id"])
        e = unit_economics(conn, p["product_id"])
        if e["missing"] or e["unit_profit"] is None or e["unit_profit"] < d(th["min_unit_profit"]):
            continue
        blocked = None
        if not i or i["available"] < int(th["stock_safety_units"]) or i["stockout_risk"]:
            blocked = "stok güvenli değil"
        score = d(e["unit_profit"]) * Decimal(p["units"]) / Decimal(th["analysis_days"])   # günlük kâr potansiyeli
        out.append({"product_id": p["product_id"], "name": p["name"], "class": p["class"], "units": p["units"],
                    "unit_profit": e["unit_profit"], "net_margin": e["net_margin"], "available": i["available"] if i else 0,
                    "has_active_campaign": p["product_id"] in active, "blocked": blocked, "score": score.quantize(Decimal("0.01")),
                    "max_cpa": e["unit_profit"]})   # bir siparişe ödenebilecek en fazla reklam = birim net kâr
    out.sort(key=lambda x: (x["blocked"] is not None, -x["score"]))
    return out


def propose_ad_products(conn: Connection, ctx, limit: int = 3) -> int:
    from .proposals import ad_budget_usage
    th = thresholds(conn)
    n = 0
    for c in ad_candidates(conn):
        if n >= limit:
            break
        if c["has_active_campaign"]:
            continue
        if c["blocked"]:
            actions.block(conn, agent="advertising", action_type="ads.create_campaign", entity_type="product",
                          entity_id=c["product_id"], reason_code="stock_risk", cycle_id=ctx.run_id,
                          reason=f"{c['name']} reklama uygun marjda ama {c['blocked']}; reklam açılmaz.")
            continue
        usage = ad_budget_usage(conn)
        budget = min(Decimal(str(th["ads_min_spend"])) / Decimal(th["ads_window_days"]) * 2, max(usage["headroom"], Decimal("0")))
        budget = budget.quantize(Decimal("1"))
        if budget <= 0:
            actions.skip(conn, agent="advertising", action_type="ads.create_campaign", entity_type="product", entity_id=c["product_id"],
                         reason="Günlük reklam bütçe limitinde yer yok.", reason_code="budget_limit", cycle_id=ctx.run_id)
            continue
        propose(conn, agent="advertising", action_type="ads.create_campaign", entity_type="product", entity_id=c["product_id"],
                channel="meta", title=f"Reklam aç: {c['name']} ({tl(budget)}/gün, test)", run_id=ctx.run_id,
                reason=(f"{c['name']}: {c['class']} sınıfı, son {th['analysis_days']} günde {c['units']} adet, birim net kâr "
                        f"{tl(c['unit_profit'])} (marj {_pct(c['net_margin'])}), stok {c['available']}. Sipariş başına en fazla "
                        f"{tl(c['max_cpa'])} reklam maliyeti kârı sıfırlar; {th['ads_window_days']} günlük küçük test önerilir."),
                evidence={k: c[k] for k in ("units", "unit_profit", "net_margin", "available", "score")},
                params={"daily_budget": budget, "days": th["ads_window_days"], "max_cpa": c["max_cpa"]},
                required_capital=budget * Decimal(th["ads_window_days"]), capital_category="advertising",
                expected_result={"measure_after_days": 7, "metric": "reklam sonrası net kâr", "max_cpa": c["max_cpa"]},
                confidence=0.4)
        n += 1
    return n


# ------------------------------------------------------------------ Pazarlama + Sosyal medya
AUDIENCE = [(("sırt", "backpack"), "Öğrenci ve şehirde yürüyen 18–30 yaş", "günlük kullanım, laptop/okul"),
            (("abiye", "clutch", "portföy"), "Düğün/davet hazırlığındaki 22–45 yaş kadın", "davet ve özel gün"),
            (("plaj", "hasır"), "Tatil planlayan 25–45 yaş kadın", "yaz / tatil"),
            (("omuz", "kol", "shopper", "çapraz", "baget"), "Çalışan 25–45 yaş kadın", "ofis ve günlük şıklık")]


def _audience(name: str, category: str | None) -> tuple[str, str]:
    t = f"{name} {category or ''}".lower().replace("I", "ı")
    for keys, who, use in AUDIENCE:
        if any(k in t for k in keys):
            return who, use
    return "Kadın 25–44 yaş, Trendyol çanta alıcısı", "günlük kullanım"


def top_question_category(conn: Connection, product_id: int) -> str | None:
    """Ürün için son 30 günde en çok sorulan konu (gerçek Trendyol soruları; yoksa None)."""
    from ..platform.questions import CATEGORY_TR
    r = rows(conn, """SELECT category, COUNT(*) AS n FROM customer_questions
                       WHERE product_id = :p AND asked_at > NOW() - INTERVAL '30 days' AND category <> 'other'
                       GROUP BY 1 ORDER BY 2 DESC LIMIT 1""", p=product_id)
    return CATEGORY_TR.get(r[0]["category"]) if r and r[0]["n"] >= 2 else None


def marketing_picks(conn: Connection, limit: int = 3) -> list[dict]:
    th = thresholds(conn)
    picks = []
    for c in ad_candidates(conn):
        if c["blocked"]:
            continue
        e = unit_economics(conn, c["product_id"])
        safe = max_safe_discount(e, th)
        cat = rows(conn, "SELECT category FROM products WHERE id = :i", i=c["product_id"])[0]["category"]
        who, use = _audience(c["name"], cat)
        q = top_question_category(conn, c["product_id"])
        offer = (f"%{int(min(Decimal('0.10'), safe) * 100)}'a kadar indirim (net kâr sınırı korunur)" if safe and safe >= Decimal("0.05")
                 else "İndirim yok (marj payı yetersiz); ücretsiz kargo / kalite ve ölçü bilgisi vurgusu")
        picks.append({**c, "audience": who, "use_case": use, "offer": offer, "max_safe_discount": safe, "top_question": q,
                      "category": cat})
        if len(picks) >= limit:
            break
    return picks


def run_marketing(conn: Connection, ctx) -> dict:
    ctx.sources += ["ürün sınıfı + birim kâr", "stok", "müşteri soruları (varsa)"]
    picks = marketing_picks(conn)
    for p in picks:
        reason = (f"Neden: {p['class']} sınıfı, birim net kâr {tl(p['unit_profit'])}, son dönemde {p['units']} adet, stok {p['available']}. "
                  f"Hedef kitle: {p['audience']} ({p['use_case']}). Teklif: {p['offer']}."
                  + (f" Müşteriler en çok '{p['top_question']}' soruyor: içerikte bu bilgi açıkça verilmeli." if p["top_question"] else ""))
        propose(conn, agent="marketing", action_type="marketing.plan", entity_type="product", entity_id=p["product_id"],
                title=f"Pazarla: {p['name']}", reason=reason, run_id=ctx.run_id, requires_approval=False,
                evidence={k: p[k] for k in ("class", "units", "unit_profit", "net_margin", "available", "max_safe_discount")},
                params={"audience": p["audience"], "offer": p["offer"], "use_case": p["use_case"]}, confidence=0.6)
    if not picks:
        ctx.warnings.append("Pazarlanacak kârlı + stoklu ürün yok (veya maliyet/satış verisi eksik)")
    retired = retire_stale(conn, "marketing", ctx.run_id)
    ctx.output = {"plans": len(picks), "products": [p["name"] for p in picks], "retired": retired}
    return ctx.output


def content_plan(p: dict) -> dict:
    name = p["name"]
    hook = {"ofis ve günlük şıklık": f"Ofiste her kombine uyan tek çanta: {name}",
            "davet ve özel gün": f"Davette tüm bakışlar bu çantada: {name}",
            "yaz / tatil": f"Tatil çantan hazır mı? {name}",
            "günlük kullanım, laptop/okul": f"Laptop + su şişesi + her şey sığıyor mu? {name} ile test ettik"}.get(
        p["use_case"], f"Bu çanta neden bu kadar çok satıyor? {name}")
    q = p.get("top_question")
    info = {"Ölçü / ebat": "ölçüleri (en x boy x derinlik) ekranda yazı olarak göster, içine A4/telefon koyarak kanıtla",
            "Askı / kayış": "askının çıkarılıp takıldığını ve uzunluk ayarını göster",
            "Renk": "tüm renkleri gün ışığında yan yana göster", "Malzeme": "malzemeyi yakın çekimde göster",
            "İç bölme / cep / kapama": "iç bölmeleri açarak tek tek göster"}.get(q or "", "ürünü kullanımda (omuzda/elde) göster")
    return {"platforms": ["Instagram Reels", "TikTok"], "hook": hook,
            "caption": f"{name} — {p['use_case']} için. {p['offer']}. Trendyol'da 'Trendçantanız' mağazasında.",
            "cta": "Profildeki linkten Trendyol mağazamıza git",
            "creative_brief": f"9:16 dikey video, 12–20 sn. İlk 2 sn hook. Sonra {info}. Son 3 sn fiyat/teklif + CTA.",
            "audience": p["audience"], "status_note": "YAYINLANMADI: Instagram/TikTok hesabı bağlı değil; plan sahibin onayı ve elle paylaşım içindir."}


def run_social(conn: Connection, ctx) -> dict:
    ctx.sources += ["pazarlama seçimleri", "müşteri soruları (varsa)"]
    picks = marketing_picks(conn)
    for p in picks:
        plan = content_plan(p)
        propose(conn, agent="social_media", action_type="social.content_plan", entity_type="product", entity_id=p["product_id"],
                title=f"İçerik planı: {p['name']}", reason=f"Hook: {plan['hook']} · CTA: {plan['cta']} · {plan['status_note']}",
                run_id=ctx.run_id, requires_approval=False, evidence={"class": p["class"], "unit_profit": p["unit_profit"]},
                params=plan, confidence=0.5)
    retired = retire_stale(conn, "social_media", ctx.run_id)
    ctx.output = {"content_plans": len(picks), "published": 0, "note": "Sosyal medya hesabı bağlı değil; hiçbir şey yayınlanmadı.",
                  "retired": retired}
    return ctx.output


def run_customer_experience(conn: Connection, ctx) -> dict:
    from ..platform.cx import analyze
    ctx.sources += ["customer_questions", "marketplace_returns"]
    r = analyze(conn, 30)
    if not r["has_data"]:
        ctx.warnings.append("Gerçek soru/iade verisi yok (Trendyol soru/iade senkronu kapalı veya veri gelmedi)")
    n = 0
    for g in r["info_gaps"]:
        if not g["product_id"]:
            continue
        propose(conn, agent="customer_experience", action_type="cx.info_gap", entity_type="product", entity_id=g["product_id"],
                title=f"Ürün sayfası eksiği: {g['name']} ({g['label']})", reason=g["text"], run_id=ctx.run_id,
                requires_approval=False, evidence={"count": g["count"], "questions": g["questions"], "share": g["share"]},
                confidence=0.7)
        n += 1
    retired = retire_stale(conn, "customer_experience", ctx.run_id)
    ctx.output = {"has_data": r["has_data"], "questions": r["total_questions"], "returns": r["total_returns"], "proposals": n,
                  "retired": retired}
    return ctx.output
