"""CEO: döngüyü yönetir, günlük "Bugün bilmen gerekenler" özetini önceliklendirir.

Hedef fonksiyon: sürdürülebilir NET KÂR. Ciro tek başına başarı sayılmaz; özet ve öneriler net kâr, nakit, risk ve
veri kalitesine göre sıralanır. Veri yoksa "veri yok" der, rakam uydurmaz.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from ...db import row
from .. import finance_view
from . import agents, config
from .config import Window, d, today
from .proposals import activity, expire_old

log = logging.getLogger("trendhub.ai")
from . import growth, operations  # noqa: E402

# Ortak döngü sırası: VERİ NORMALİZASYONU (run_cycle başında) → KÂR → ÜRÜN/STOK → FİYAT/KAMPANYA → REKLAM/PAZARLAMA/SOSYAL
# → CEO HAKEMLİĞİ → GUARDRAIL (risk motoru her öneride) → ÖNERİ/AKSİYON → DENETİM KAYDI → SONUÇ → GERİ BİLDİRİM.
# Stok ve kâr ajanları önce çalışır; sonraki ajanlar onların sonucunu (stok riski, sınıf) okur.
AGENT_ORDER = (("inventory", agents.run_inventory), ("product_profit", agents.run_product_profit),
               ("product_tracking", growth.run_product_tracking), ("pricing", growth.run_pricing),
               ("campaign", growth.run_campaign), ("advertising", agents.run_advertising),
               ("marketing", growth.run_marketing), ("social_media", growth.run_social),
               ("customer_experience", growth.run_customer_experience), ("operations", operations.run_operations),
               ("capital", agents.run_capital))
CYCLE_LOCK = 7342301


def run_cycle(engine: Engine, trigger: str = "schedule") -> dict:
    """Tüm ajanları sırayla çalıştırır. Bir ajanın hatası diğerlerini durdurmaz (ama sessizce de kaybolmaz)."""
    out: dict = {}
    with engine.begin() as conn:
        if not config.enabled(conn):
            return {"skipped": "ai.enabled=false"}
    # Aynı anda iki döngü (zamanlayıcı + elle "şimdi çalıştır") aynı öneriyi iki kez üretmesin: oturum kilidi
    lock = engine.connect()
    try:
        got = lock.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": CYCLE_LOCK}).scalar()
        lock.commit()   # oturum kilidi transaction'dan bağımsızdır; bağlantı "idle in transaction" kalmasın
        if not got:
            return {"skipped": "Başka bir AI döngüsü çalışıyor (tekrar eden döngü engellendi)"}
        return _run_cycle_locked(engine, trigger, out)
    finally:
        try:
            lock.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": CYCLE_LOCK})
            lock.commit()
        finally:
            lock.close()


def _run_cycle_locked(engine: Engine, trigger: str, out: dict) -> dict:
    with engine.begin() as conn:
        # Worker yeniden başladıysa yarıda kalan çalışmalar sonsuza dek 'running' görünmesin
        stale = conn.execute(text("""UPDATE ai_agent_runs SET status = 'error', finished_at = NOW(),
                                     error = 'Çalışma yarıda kesildi (worker yeniden başladı veya bağlantı koptu)'
                                     WHERE status = 'running' AND started_at < NOW() - INTERVAL '30 minutes'
                                     RETURNING agent_code""")).all()
        for (code,) in stale:
            conn.execute(text("UPDATE ai_agents SET last_status = 'error', last_error = 'Çalışma yarıda kesildi' WHERE code = :c"),
                         {"c": code})
            activity(conn, "Önceki çalışma yarıda kesilmiş (worker yeniden başladı); hata olarak işaretlendi", agent=code,
                     kind="run", level="error")
    with agents.agent_run(engine, "ceo", trigger) as ceo:
        ceo.sources += ["veri normalizasyonu"] + [c for c, _ in AGENT_ORDER]
        from .reconcile import coverage, normalize
        with engine.begin() as conn:
            out["normalization"] = normalize(conn)
        for code, fn in AGENT_ORDER:
            with engine.begin() as conn:
                if not agents.agent_enabled(conn, code):
                    out[code] = {"skipped": "kapalı"}
                    continue
            try:
                with agents.agent_run(engine, code, trigger) as ctx:
                    with engine.begin() as conn:
                        out[code] = fn(conn, ctx)
            except Exception as exc:  # noqa: BLE001 - ajan hatası kayıtlı (agent_run), döngü sürer
                log.exception("AI ajanı başarısız: %s", code)
                out[code] = {"error": f"{exc.__class__.__name__}: {exc}"}
                ceo.warnings.append(f"{code} ajanı hata verdi")
        with engine.begin() as conn:
            out["risk_anomalies"] = len(agents.scan_anomalies(conn))
            conn.execute(text("UPDATE ai_agents SET last_run_at = NOW(), last_status = 'ok', last_error = NULL WHERE code = 'risk'"))
            out["expired"] = expire_old(conn)
            from .governor import release_stale
            out["budget_released"] = release_stale(conn)
        with engine.begin() as conn:
            from .ceo_review import resolve_conflicts
            out["ceo_blocked_conflicts"] = resolve_conflicts(conn, ceo.run_id)
        with engine.begin() as conn:
            from .decisions import evaluate_outcomes
            out["outcomes_written"] = evaluate_outcomes(conn)
            from .memory import refresh_lessons
            out["lessons_updated"] = refresh_lessons(conn)
            from .experiments import measure_experiments
            out["experiments_measured"] = measure_experiments(conn)
        with engine.begin() as conn:
            brief = build_brief(conn)
            store_brief(conn, brief)
            from .ceo_review import executive_summary
            cov = coverage(conn)
            out["coverage"] = {k: cov[k] for k in ("sales_lines", "unmatched_lines", "missing_cost_lines", "classes", "decidable_products")}
            summary = executive_summary(conn, cov)
            conn.execute(text("UPDATE ai_briefs SET summary = CAST(:s AS JSONB) WHERE brief_date = :d"),
                         {"s": json.dumps(summary, ensure_ascii=False, default=str), "d": brief["date"]})
            out["summary"] = summary
            out["brief_items"] = len(brief["items"])
            activity(conn, f"Günlük özet güncellendi ({len(brief['items'])} madde)", agent="ceo", kind="brief", run_id=ceo.run_id)
        ceo.output = out
    return out


def _money(v) -> str:
    from ...storefront.store_config import fmt_try
    return fmt_try(v)


def build_brief(conn: Connection) -> dict:
    """En önemli 3–7 konu, önem sırasıyla. Her madde kaynağını (`source`) taşır."""
    from .capital import position
    from .data import data_quality, period_summary
    items: list[dict] = []
    t = today()
    y = period_summary(conn, Window(1, end_date=t - timedelta(days=1)))
    prev = period_summary(conn, Window(7, end_date=t - timedelta(days=2)))
    avg_profit = d(prev["net_profit"]) / 7
    if y["orders"] or prev["orders"]:
        diff = d(y["net_profit"]) - avg_profit
        trend = ("ortalamanın üstünde" if diff > 0 else "ortalamanın altında") if prev["orders"] else ""
        from ..platform.finance import profit_provenance
        yw = Window(1, end_date=t - timedelta(days=1))
        pv = profit_provenance(conn, yw.start, yw.end)
        label = {"ACTUAL": "gerçekleşen net kâr", "PARTIAL": "net kâr (kısmen gerçek, kısmen tahmini)",
                 "ESTIMATED": "tahmini net kâr", "NO_DATA": "tahmini net kâr"}[pv["status"]]
        basis = (f" Komisyonun {pv['actual_commission_items']}/{pv['items']} kalemi gerçek Trendyol kesintisi, kalanı tahmin."
                 if pv["status"] == "PARTIAL" else " Komisyon/kargo gerçek Trendyol finans kaydı değil, tahmin." if pv["status"] in ("ESTIMATED", "NO_DATA") else "")
        items.append({"priority": 80 if diff < 0 else 60, "kind": "yesterday", "level": "warning" if d(y["net_profit"]) < 0 else "info",
                      "text": f"Dün {y['orders']} sipariş geldi; {label} {_money(y['net_profit'])} "
                              f"(son 7 gün günlük ortalaması {_money(finance_view.q2(avg_profit))}{', ' + trend if trend else ''}).{basis}",
                      "source": "Satış: Trendyol Orders · komisyon: " + ("Trendyol Finance" if pv["status"] in ("ACTUAL", "PARTIAL") else "tahmin (oran)")
                                + " · maliyet: yerel maliyet kaydı · reklam: elle girilen reklam verisi", "link": "#/finance",
                      "provenance": pv})
    else:
        items.append({"priority": 50, "kind": "yesterday", "level": "info", "text": "Dün ve önceki 7 günde sipariş kaydı yok.",
                      "source": "Siparişler", "link": "#/orders"})
    camps = agents.advertising_analysis(conn)
    losing = [c for c in camps if c["ad_net_profit"] is not None and c["ad_net_profit"] < 0]
    if losing:
        loss = sum((-c["ad_net_profit"] for c in losing), Decimal("0"))
        items.append({"priority": 90, "kind": "ads_loss", "level": "warning",
                      "text": f"{len(losing)} reklam kampanyası net zarar ediyor (son 7 gün toplam {_money(loss)}).",
                      "source": "Reklam ajanı", "link": "#/ai?tab=ads"})
    nodata = [c for c in camps if c["verdict"] == "INSUFFICIENT_DATA" and c["spend"] > 0]
    if nodata:
        items.append({"priority": 55, "kind": "ads_nodata", "level": "info",
                      "text": f"{len(nodata)} kampanyaya para harcanıyor ama kâr etkisi ölçülemiyor (performans/maliyet verisi eksik).",
                      "source": "Reklam ajanı", "link": "#/ai?tab=ads"})
    from .data import inventory_status
    risky = [i for i in inventory_status(conn) if i["stockout_risk"]]
    if risky:
        top = risky[0]
        txt = (f"{top['name']} yaklaşık {top['days_of_inventory']} günlük stoğa düştü" if top["days_of_inventory"] and top["available"] > 0
               else f"{top['name']} satılabilir stokta yok ama satılıyordu")
        items.append({"priority": 75, "kind": "stock", "level": "warning",
                      "text": txt + (f"; toplam {len(risky)} üründe tükenme riski." if len(risky) > 1 else "."),
                      "source": "Stok ajanı", "link": "#/ai?tab=inventory"})
    r = row(conn, """SELECT COUNT(*) FILTER (WHERE status = 'pending_approval' AND requires_approval) AS pending,
                            COUNT(*) FILTER (WHERE status = 'pending_approval' AND NOT requires_approval) AS tasks,
                            COUNT(*) FILTER (WHERE status = 'approved') AS to_apply,
                            COUNT(*) FILTER (WHERE status = 'blocked' AND created_at > NOW() - INTERVAL '1 day') AS blocked
                       FROM ai_proposals""")
    if r["pending"]:
        items.append({"priority": 85, "kind": "approvals", "level": "action",
                      "text": f"Bugün onaylamanı önerdiğim {r['pending']} işlem var.", "source": "Onaylar", "link": "#/ai?tab=approvals"})
    if r["to_apply"]:
        items.append({"priority": 70, "kind": "to_apply", "level": "action",
                      "text": f"Onayladığın {r['to_apply']} işlem platformda uygulanmayı bekliyor.", "source": "Onaylar",
                      "link": "#/ai?tab=approvals"})
    if r["tasks"]:
        items.append({"priority": 45, "kind": "tasks", "level": "info",
                      "text": f"{r['tasks']} inceleme görevi var (zarar eden ürün, eksik maliyet, stok).", "source": "Ajanlar",
                      "link": "#/ai?tab=approvals"})
    loss_products = [p for p in agents.classified_products(conn) if p["class"] == "LOSS"]
    if loss_products:
        tot = sum((p["net_profit"] for p in loss_products), Decimal("0"))
        items.append({"priority": 72, "kind": "loss_products", "level": "warning",
                      "text": f"Son 30 günde {len(loss_products)} ürün zarar ettirdi (toplam {_money(tot)}).",
                      "source": "Ürün & Kâr ajanı", "link": "#/ai?tab=profit"})
    pos = position(conn)
    cs = pos["cash_structure"]
    pend = cs["pending_marketplace_payout"]
    if pend["kind"] == "ACTUAL" and (d(pend["amount"]) or d(cs["marketplace_receivable"]["amount"])):
        cash_txt = (f"Kasada {_money(cs['available_cash']['amount'])}" if cs["available_cash"]["amount"] is not None
                    else "Kasa bilgisi girilmedi")
        nxt = f" (sonraki ödeme {pend['next_payment_date']:%d.%m.%Y})" if pend.get("next_payment_date") else ""
        items.append({"priority": 78, "kind": "cash", "level": "info",
                      "text": f"{cash_txt}; Trendyol'da bekleyen hakediş {_money(pend['amount'])}{nxt}, vadesi gelmemiş alacak "
                              f"{_money(cs['marketplace_receivable']['amount'])}. Bekleyen hakediş harcanabilir sermaye sayılmaz; "
                              f"harcanabilir: {_money(pos['usable']) if pos['usable'] is not None else 'hesaplanamıyor'}.",
                      "source": "Kasa: elle · hakediş: Trendyol Finance (cari hesap)", "link": "#/ai?tab=finance"})
    from ..platform import cx as cx_mod
    sig = cx_mod.analyze(conn, 30)
    if sig["info_gaps"]:
        g = sig["info_gaps"][0]
        items.append({"priority": 50, "kind": "cx_gap", "level": "info", "text": g["text"],
                      "source": "Trendyol Soru-Cevap (gerçek sorular)", "link": "#/ai?tab=cx"})
    if sig["complaints"]:
        c0 = sig["complaints"][0]
        items.append({"priority": 52, "kind": "cx_returns", "level": "warning",
                      "text": f"{c0['name']}: son 30 günde {c0['count']} iade '{c0['reason']}' sebebiyle.",
                      "source": "Trendyol İadeler (gerçek iade sebepleri)", "link": "#/ai?tab=cx"})
    if pos["usable"] is not None and pos["justified"] == 0 and pos["usable"] > 0:
        items.append({"priority": 30, "kind": "capital", "level": "info", "text": pos["recommendation"],
                      "source": "Sermaye motoru", "link": "#/ai?tab=capital"})
    failed = conn.execute(text("SELECT name, last_error FROM ai_agents WHERE last_status = 'error' AND enabled")).all()
    for name, err in failed:
        items.append({"priority": 95, "kind": "agent_error", "level": "critical",
                      "text": f"{name} ajanı son çalışmada hata verdi; bu alandaki öneriler güncel değil. ({(err or '')[:120]})",
                      "source": "Ajan durumu", "link": "#/ai?tab=agents"})
    quality = data_quality(conn)
    for q in quality:
        if q["severity"] == "warning":
            items.append({"priority": 65, "kind": "data_quality", "level": "warning", "text": q["message"],
                          "source": "Veri kalitesi", "link": "#/ai?tab=risk"})
    if config.emergency_stop(conn):
        items.append({"priority": 100, "kind": "emergency", "level": "critical",
                      "text": "ACİL DURDURMA AKTİF: hiçbir yazma işlemi yapılmıyor; analiz devam ediyor.", "source": "Risk motoru",
                      "link": "#/ai"})
    items.sort(key=lambda x: (BRIEF_RANK.get(x["kind"], 9), -x["priority"]))
    return {"date": t, "items": items[:7], "data_quality": quality}


# Brief sırası: (0) sistem durumu: acil durdurma / ajan hatası — her şeyin üstünde, çünkü diğer maddelerin
# güvenilirliğini etkiler; sonra sahibin istediği öncelik: 1 net kâr, 2 nakit/hakediş, 3 kritik risk (veri tazeliği),
# 4 zarar eden ürünler, 5 reklam, 6 iade/müşteri, 7 stok, 8 fırsatlar/onaylar.
BRIEF_RANK = {"emergency": 0, "agent_error": 0, "yesterday": 1, "cash": 2, "data_quality": 3, "loss_products": 4,
              "ads_loss": 5, "ads_nodata": 5, "cx_returns": 6, "cx_gap": 6, "stock": 7, "approvals": 8, "to_apply": 8,
              "tasks": 8, "capital": 8}


def store_brief(conn: Connection, brief: dict) -> None:
    conn.execute(text("""INSERT INTO ai_briefs(brief_date, items, data_quality) VALUES (:d, CAST(:i AS JSONB), CAST(:q AS JSONB))
                         ON CONFLICT (brief_date) DO UPDATE SET items = EXCLUDED.items, data_quality = EXCLUDED.data_quality,
                         updated_at = NOW()"""),
                 {"d": brief["date"], "i": json.dumps(brief["items"], ensure_ascii=False, default=str),
                  "q": json.dumps(brief["data_quality"], ensure_ascii=False, default=str)})
