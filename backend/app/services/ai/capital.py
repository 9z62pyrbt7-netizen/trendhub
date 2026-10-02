"""Sermaye & nakit motoru. KÂR ≠ NAKİT.

    kullanılabilir = min(sahibin belirlediği yatırım limiti, kasa − tedarikçi/reklam/gider borçları − ayrılmış − rezerv)
    rezerv          = son 90 günün aylık sabit gider ortalaması × reserve_months_opex
    bekleyen hakediş KULLANILABİLİR SAYILMAZ (henüz kasada değil)
    gerekçelendirilmiş = onay bekleyen / onaylanmış önerilerin gerektirdiği sermaye (kanıta dayalı fırsatlar)
    kullanılmayan      = kullanılabilir − gerekçelendirilmiş

Sermaye kategorileri sabit yüzdelerle değil, yalnızca önerilerden (kanıttan) oluşur. Fırsat yoksa sermaye kullanılmaz.

Nakit yapısı (TOPLAM PARA ≠ HARCANABİLİR SERMAYE):
    AVAILABLE CASH              kasa/banka — elle girilir (banka entegrasyonu yok)
    PENDING MARKETPLACE PAYOUT  vadesi yakın, ödenmemiş hakediş — Trendyol cari hesap ekstresi (bağlı değilse elle girilen)
    MARKETPLACE RECEIVABLE      vadesi gelmemiş pazaryeri alacağı — Trendyol cari hesap ekstresi
    KNOWN LIABILITIES           tedarikçi / reklam / işletme borçları — elle
    RESERVED CAPITAL            ayrılmış tutar + nakit rezervi
    DEPLOYABLE CAPITAL          = kullanılabilir (yalnızca kasadan; hakediş ve alacak ASLA dahil edilmez)
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy.engine import Connection

from ...db import rows
from .. import finance_view
from .config import d, thresholds, tl

KIND_TR = {"cash": "Kasa / banka", "pending_payout": "Bekleyen pazaryeri hakedişi", "supplier_liability": "Tedarikçi borcu",
           "ad_liability": "Reklam borcu", "opex_liability": "Ödenecek işletme gideri", "reserved": "Ayrılmış (dokunulmaz)",
           "investable_limit": "Sisteme ayırdığım sermaye (üst limit)"}
CATEGORY_TR = {"advertising": "Reklam", "inventory": "Stok", "testing": "Test", "operations": "Operasyon", "reserve": "Rezerv"}


def position(conn: Connection, exclude_proposal: int | None = None) -> dict:
    from .data import monthly_opex
    th = thresholds(conn)
    acc = rows(conn, "SELECT kind, name, amount, as_of FROM ai_capital_accounts")
    sums = {k: Decimal("0") for k in KIND_TR}
    for a in acc:
        sums[a["kind"]] += d(a["amount"])
    has_cash = any(a["kind"] == "cash" for a in acc)
    opex = monthly_opex(conn)
    reserve = finance_view.q2(opex * d(th["reserve_months_opex"]))
    liabilities = sums["supplier_liability"] + sums["ad_liability"] + sums["opex_liability"]
    free_cash = sums["cash"] - liabilities - sums["reserved"] - reserve if has_cash else None
    limit = sums["investable_limit"] if any(a["kind"] == "investable_limit" for a in acc) else None
    usable = None
    if free_cash is not None:
        usable = max(Decimal("0"), free_cash if limit is None else min(free_cash, limit))
    justified_rows = rows(conn, """
        SELECT COALESCE(capital_category, 'other') AS category, SUM(required_capital) AS amount, COUNT(*) AS n
          FROM ai_proposals WHERE status IN ('pending_approval', 'approved') AND required_capital > 0
           AND (CAST(:ex AS BIGINT) IS NULL OR id <> :ex) GROUP BY 1""", ex=exclude_proposal)
    justified = sum((d(r["amount"]) for r in justified_rows), Decimal("0"))
    unused = max(Decimal("0"), usable - justified) if usable is not None else None
    out = {"accounts": acc, "totals": {k: finance_view.q2(v) for k, v in sums.items()}, "monthly_opex": opex,
           "reserve_required": reserve, "liabilities": finance_view.q2(liabilities),
           "free_cash": finance_view.q2(free_cash) if free_cash is not None else None, "investable_limit": limit,
           "usable": finance_view.q2(usable) if usable is not None else None, "justified": finance_view.q2(justified),
           "justified_by_category": [{**r, "label": CATEGORY_TR.get(r["category"], r["category"])} for r in justified_rows],
           "unused": finance_view.q2(unused) if unused is not None else None,
           "pending_payout_not_counted": finance_view.q2(sums["pending_payout"])}
    out["cash_structure"] = cash_structure(conn, out, sums, acc)
    out["pending_payout_not_counted"] = out["cash_structure"]["pending_marketplace_payout"]["amount"]
    out["efficiency"] = efficiency(conn)
    out["recommendation"] = _recommendation(out)
    return out


def cash_structure(conn: Connection, p: dict, sums: dict, acc: list[dict]) -> dict:
    """Her kalemin tutarı ve KAYNAĞI. Hakediş/alacak bilgi amaçlıdır; harcanabilir sermayeye girmez."""
    from ..platform.finance import payout_summary
    from ..platform.sources import finance_status
    ps = payout_summary(conn)
    fs = finance_status(conn)
    cash_at = max((a["as_of"] for a in acc if a["kind"] == "cash"), default=None)
    if ps["connected"]:
        pending = {"amount": finance_view.q2(ps["pending_payout"]), "source": ps["source"], "kind": "ACTUAL",
                   "next_payment_date": ps["next_payment_date"], "freshness": fs["freshness"]}
        receivable = {"amount": finance_view.q2(ps["receivable"]), "source": ps["source"], "kind": "ACTUAL",
                      "freshness": fs["freshness"]}
    elif any(a["kind"] == "pending_payout" for a in acc):
        pending = {"amount": finance_view.q2(sums["pending_payout"]), "kind": "MANUAL",
                   "source": "Elle girilen bekleyen hakediş (Trendyol finans verisi henüz yok)"}
    else:
        # Veri yok ≠ 0: ne Trendyol finansı ne elle giriş var → tutar BİLİNMİYOR (0 gösterilmez)
        pending = {"amount": None, "kind": "UNKNOWN",
                   "source": "Bilinmiyor: Trendyol finans verisi yok ve elle bekleyen hakediş girilmedi"}
    if not ps["connected"]:
        receivable = {"amount": None, "kind": "UNKNOWN", "source": "Trendyol finans verisi yok"}
    reserved = finance_view.q2(sums["reserved"] + p["reserve_required"])
    total_money = None
    if p["free_cash"] is not None:
        # Bilinmeyen kalem toplamı 0 gibi tamamlamaz: toplam yalnızca bilinen kalemlerden, eksik olduğu belirtilerek
        total_money = finance_view.q2(sums["cash"] + d(pending["amount"]) + d(receivable["amount"]))
    return {
        "available_cash": {"amount": finance_view.q2(sums["cash"]) if any(a["kind"] == "cash" for a in acc) else None,
                           "source": "Elle girilen kasa/banka (banka entegrasyonu yok)", "kind": "MANUAL", "as_of": cash_at},
        "pending_marketplace_payout": pending,
        "marketplace_receivable": receivable,
        "known_liabilities": {"amount": p["liabilities"], "source": "Elle girilen borçlar (tedarikçi, reklam, işletme)",
                              "advertising_commitments": finance_view.q2(sums["ad_liability"])},
        "reserved_capital": {"amount": reserved, "reserved": finance_view.q2(sums["reserved"]),
                             "cash_reserve": p["reserve_required"]},
        "deployable_capital": {"amount": p["usable"], "rule": "Yalnızca kasadaki nakit − borçlar − ayrılmış − rezerv "
                               "(üst limit: sisteme ayrılan sermaye). Bekleyen hakediş ve alacak dahil DEĞİL."},
        "total_money": {"amount": total_money, "note": "Kasa + bekleyen hakediş + alacak. Harcanabilir sermaye DEĞİLDİR."
                        + ("" if pending["amount"] is not None and receivable["amount"] is not None
                           else " Eksik: bilinmeyen hakediş/alacak toplama dahil değil.")},
        "payout_reconciliation": ps["reconciliation"], "overdue_unverified": ps["overdue_unverified"],
    }


def _recommendation(p: dict) -> str:
    if p["usable"] is None:
        return ("Kasa bilgisi girilmedi; kullanılabilir sermaye hesaplanamaz. Kasadaki nakdi, borçları ve bekleyen "
                "hakedişi girin (bekleyen hakediş kullanılabilir sayılmaz).")
    cs = p.get("cash_structure") or {}
    pend = d((cs.get("pending_marketplace_payout") or {}).get("amount"))
    if p["usable"] <= 0 and pend > 0:
        return (f"Kasada harcanabilir sermaye yok. Trendyol'da {tl(pend)} bekleyen hakediş var ama henüz kasada olmadığı "
                "için harcanabilir sayılmaz. Yeni harcama önermiyorum.")
    if p["usable"] <= 0:
        return ("Borçlar, ayrılmış tutar ve nakit rezervi düşüldükten sonra kullanılabilir sermaye yok. "
                "Yeni harcama önermiyorum.")
    if p["justified"] <= 0:
        return (f"Kullanılabilir {tl(p['usable'])} var ama şu anda kanıtla desteklenen bir fırsat yok. "
                "Ek sermaye kullanmayı şu anda önermiyorum; para kasada kalmalı.")
    return (f"Kullanılabilir {tl(p['usable'])}'nin yalnızca {tl(p['justified'])}'si kanıta dayalı önerilerle "
            f"gerekçelendirilebiliyor. Kalan {tl(p['unused'])} için ek sermaye kullanmayı şu anda önermiyorum; "
            "para kasada kalmalı. Önce küçük ölçekte test → ölç → büyüt.")


def efficiency(conn: Connection) -> dict:
    """Harcanan sermaye başına üretilen net kâr: uygulanan, sermaye gerektiren kararların ölçülmüş kâr değişimi."""
    r = rows(conn, """
        SELECT p.required_capital,
               (SELECT (o.metrics->>'profit_change')::numeric FROM ai_decision_outcomes o WHERE o.decision_id = x.id
                 AND o.final_result <> 'insufficient_data' ORDER BY o.horizon_days DESC LIMIT 1) AS profit_change
          FROM ai_decisions x JOIN ai_proposals p ON p.id = x.proposal_id
         WHERE x.decision = 'approved' AND x.executed_at IS NOT NULL AND p.required_capital > 0""")
    measured = [i for i in r if i["profit_change"] is not None]
    cap = sum((d(i["required_capital"]) for i in measured), Decimal("0"))
    gain = sum((d(i["profit_change"]) for i in measured), Decimal("0"))
    return {"measured_decisions": len(measured), "capital": finance_view.q2(cap), "profit_change": finance_view.q2(gain),
            "profit_per_lira": finance_view.ratio(gain, cap) if cap else None}
