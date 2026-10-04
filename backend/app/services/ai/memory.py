"""Ajan hafızası: karar → gerekçe → beklenen sonuç → GERÇEK sonuç → fark → ders.

Ders, LLM'nin yazdığı metin DEĞİLDİR: `ai_decision_outcomes`'taki ölçülmüş net kâr değişiminden deterministik olarak
üretilir (`lesson_source = 'measured_outcome'`). Ölçüm yoksa ders yazılmaz.
Kullanıcının kendi kararları da aynı şekilde ölçülür; ama kullanıcı TERCİHİ (ai_owner_preferences) işletme gerçeği sayılmaz.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ...db import rows
from .config import d, tl

RESULT_TR = {"improved": "iyileşti", "worsened": "kötüleşti", "neutral": "değişmedi", "insufficient_data": "veri yetersiz"}


def lesson_for(dec: dict, outcome: dict) -> str | None:
    res = outcome["final_result"]
    if res == "insufficient_data":
        return None
    m = outcome.get("metrics") or {}
    pc = m.get("profit_change")
    who = "Sahibin" if dec["actor"] == "owner" else "Ajanın"
    exp = dec.get("expected_result") or {}
    exp_txt = f" Beklenen: {exp.get('metric')}." if exp.get("metric") else ""
    verb = {"improved": "net kârı artırdı", "worsened": "net kârı düşürdü", "neutral": "net kârı anlamlı değiştirmedi"}[res]
    tail = {"improved": "Benzer durumda tekrar düşünülebilir (tek ölçüm; korelasyon, kesin neden değil).",
            "worsened": "Benzer durumda bu karar varsayılan olarak önerilmemeli; önce nedeni incelenmeli.",
            "neutral": "Bu karar türü tek başına sonuç değiştirmiyor; maliyeti varsa gereksiz olabilir."}[res]
    return (f"{who} '{dec['decision_type']}' kararı ({dec['decision']}) {outcome['horizon_days']} günde {verb}"
            f"{f' ({tl(pc)})' if pc is not None else ''}.{exp_txt} {tail}")


def refresh_lessons(conn: Connection) -> int:
    """Ölçümü olan ama dersi yazılmamış (veya daha uzun ufukta yeniden ölçülmüş) kararların dersini günceller."""
    n = 0
    for r in rows(conn, """
        SELECT x.id, x.actor, x.decision, x.decision_type, x.expected_result, x.lesson,
               o.horizon_days, o.final_result, o.metrics
          FROM ai_decisions x
          JOIN LATERAL (SELECT * FROM ai_decision_outcomes o WHERE o.decision_id = x.id ORDER BY horizon_days DESC LIMIT 1) o ON TRUE"""):
        les = lesson_for(r, r)
        if les and les != r["lesson"]:
            conn.execute(text("UPDATE ai_decisions SET lesson = :l, lesson_source = 'measured_outcome' WHERE id = :i"),
                         {"l": les, "i": r["id"]})
            n += 1
    return n


def history(conn: Connection, limit: int = 20) -> list[dict]:
    out = []
    for r in rows(conn, """
        SELECT x.id, x.created_at, x.actor, x.decision, x.decision_type, x.entity_type, x.entity_id, x.reason,
               x.expected_result, x.override, x.lesson, x.lesson_source, x.proposal_id,
               o.horizon_days, o.final_result, o.metrics
          FROM ai_decisions x
          LEFT JOIN LATERAL (SELECT * FROM ai_decision_outcomes o WHERE o.decision_id = x.id ORDER BY horizon_days DESC LIMIT 1) o ON TRUE
         ORDER BY x.id DESC LIMIT :l""", l=limit):
        m = r["metrics"] or {}
        actual = None
        if r["final_result"]:
            actual = {"horizon_days": r["horizon_days"], "result": r["final_result"], "result_tr": RESULT_TR[r["final_result"]],
                      "profit_change": m.get("profit_change"), "revenue_change": m.get("revenue_change")}
        exp = r["expected_result"] or {}
        diff = None
        if actual and exp.get("expected_profit_change") is not None and actual["profit_change"] is not None:
            diff = str(d(actual["profit_change"]) - d(exp["expected_profit_change"]))
        out.append({"id": r["id"], "at": r["created_at"], "actor": r["actor"], "decision": r["decision"],
                    "decision_type": r["decision_type"], "entity": f"{r['entity_type']}:{r['entity_id']}" if r["entity_type"] else None,
                    "reason": r["reason"], "expected_result": exp, "actual_result": actual, "difference": diff,
                    "lesson": r["lesson"], "lesson_source": r["lesson_source"], "override": r["override"],
                    "proposal_id": r["proposal_id"]})
    return out
