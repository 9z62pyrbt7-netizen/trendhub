"""Gerçeklik Denetçisi (Reality Checker uyarlaması): diğer ajanların iddialarına güvenmez.

Her iddia (ai_evidence) için:
  1. Araç çağrısı yok                              → UNVERIFIED ("kanıt yok")
  2. Araç çağrısı başarısız / bu göreve ait değil  → FAILED / UNVERIFIED
  3. İddia edilen değer ≠ aracın kaydedilmiş sonucu → FAILED (ajan yanlış raporladı)
  4. Araç Denetçi tarafından BAĞIMSIZ yeniden çalıştırılır (kendi araç çağrısı, kendi kaydı):
        aynı değer + veri gerçek (ACTUAL)            → VERIFIED
        aynı değer + veri tahmini/kısmi              → PARTIALLY_VERIFIED
        değer değişti (veri arada güncellendi)        → PARTIALLY_VERIFIED
  5. Değişim iddiası ("satış %20 arttı") temel dönem + tarih aralığı olmadan → UNVERIFIED
  6. Değerlendirme ("reklam başarılı") ancak dayandığı ROAS/net kâr iddiası doğrulanırsa ve yön tutarlıysa VERIFIED
  7. Aksiyon iddiası ("uygulandı") yalnızca araç kaydı durumu ile birebir aynıysa; dış etki için platform yanıt kimliği şart
Varsayılan sonuç UNVERIFIED'dır.
"""
from __future__ import annotations

import json
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Engine

from ...db import row, rows
from . import sanitize
from .specialists import get_path, numeric
from .tools import ToolContext, invoke

ORDER = ("VERIFIED", "PARTIALLY_VERIFIED", "UNVERIFIED", "FAILED")


def _equal(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    na, nb = numeric(a), numeric(b)
    if na is not None and nb is not None:
        return abs(na - nb) <= max(Decimal("0.01"), abs(nb) * Decimal("0.005"))
    if isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
        return sanitize.to_json(a) == sanitize.to_json(b)
    return str(a) == str(b)


DERIVED = {
    "count_connected": lambda data: sum(1 for p in data.get("platforms", []) if p.get("connected")),
    "count_proven": lambda data: sum(1 for c in data.get("campaigns", []) if c.get("verdict") in ("INCREASE_BUDGET", "CONTINUE")
                                     and c.get("ad_net_profit") is not None and Decimal(str(c["ad_net_profit"])) > 0),
}


def _extract(kind: str, data: dict, path: str | None, baseline):
    if kind in DERIVED:
        return DERIVED[kind](data)
    if kind == "count_class":
        return min(3, sum(1 for p in data.get("products", []) if p.get("trend_class") in (baseline or [])))
    v = get_path(data, path) if path else None
    if kind == "count":
        return len(v) if isinstance(v, list) else v
    return v


class Checker:
    def __init__(self, engine: Engine, request_id: int, task_id: int, run_id: int | None):
        self.engine = engine
        self.ctx = ToolContext(engine, "reality_checker", request_id, task_id, run_id)
        self._cache: dict[str, object] = {}
        self.rerun_calls = 0

    def _rerun(self, tool: str, args: dict):
        key = tool + sanitize.to_json(args)
        if key not in self._cache:
            self._cache[key] = invoke(self.ctx, tool, args)
            self.rerun_calls += 1
        return self._cache[key]

    def verify(self, ev: dict, by_id: dict[int, dict]) -> tuple[str, object, str]:
        payload = ev["value"] or {}
        kind, path, claimed, basis = payload.get("kind", "metric"), payload.get("path"), payload.get("value"), payload.get("basis")
        if ev["tool_call_id"] is None and kind != "assessment":
            return "UNVERIFIED", None, "Kanıt yok: iddia hiçbir araç çağrısına bağlı değil."
        if kind == "assessment":
            sup = [by_id.get(s) for s in payload.get("supports", [])]
            if not sup or sup[0] is None:
                return "UNVERIFIED", None, "Değerlendirme dayandığı metrik iddiası olmadan kabul edilmez."
            if sup[0]["verification"] not in ("VERIFIED", "PARTIALLY_VERIFIED"):
                return "UNVERIFIED", None, f"Dayandığı metrik doğrulanmadı ({sup[0]['verification']})."
            key = numeric(sup[0]["verified_value"])
            if key is None:
                return "UNVERIFIED", None, "Dayandığı metrik değeri yok."
            ok = (key > 0) == bool(claimed)
            return ("VERIFIED" if ok else "FAILED"), key > 0, (
                "Yön doğrulandı (reklam sonrası net kâr)." if ok else "Değerlendirme dayandığı metrikle çelişiyor.")
        call = row(self.engine_conn, "SELECT * FROM ai_tool_calls WHERE id = :i", i=ev["tool_call_id"])
        if call is None or call["request_id"] != ev["request_id"]:
            return "UNVERIFIED", None, "Araç çağrısı bu isteğe ait değil."
        if kind == "action":
            if call["status"] != claimed:
                return "FAILED", call["status"], f"İddia '{claimed}', araç kaydı '{call['status']}'."
            if call["status"] == "succeeded" and call["access"] == "WRITE" and call["risk_level"] != "LOW" and not call["external_ref"]:
                return "FAILED", call["status"], "Dış platformda 'uygulandı' iddiası platform yanıt kimliği olmadan kabul edilmez."
            if call["proposal_id"]:
                p = row(self.engine_conn, "SELECT status FROM ai_proposals WHERE id = :i", i=call["proposal_id"])
                expect = {"pending_approval": ("pending_approval",), "blocked": ("blocked",), "succeeded": ("executed",)}.get(claimed, ())
                if p is None or p["status"] not in expect:
                    return "FAILED", p and p["status"], f"Öneri kaydı durumu ({p and p['status']}) iddiayla uyuşmuyor."
            return "VERIFIED", call["status"], "Araç kaydı ve onay kaydı iddiayla aynı."
        if call["status"] != "succeeded":
            return "FAILED", None, f"Araç çağrısı başarısız ({call['status']}): {call['error']}"
        stored = _extract(kind, call["result"] or {}, path, ev["baseline"])
        if (call["result"] or {}).get("__truncated__"):
            stored = claimed
        elif not _equal(claimed, stored):
            return "FAILED", stored, f"Ajan {claimed!r} dedi; araç sonucu {stored!r}."
        if kind == "action_internal":
            return self._verify_internal(ev, claimed)
        if kind == "change" and (ev["period_start"] is None or ev["baseline"] is None):
            return "UNVERIFIED", stored, "Değişim iddiası temel dönem ve tarih aralığı olmadan kabul edilmez."
        if call["access"] != "READ":
            return "UNVERIFIED", stored, "Yazma sonucu yeniden çalıştırılarak doğrulanamaz."
        r = self._rerun(call["tool"], call["arguments"] or {})
        if not r.ok:
            return "UNVERIFIED", stored, f"Bağımsız tekrar çalıştırma başarısız: {r.error}"
        fresh = _extract(kind, r.data, path, ev["baseline"])
        if not _equal(fresh, stored):
            return "PARTIALLY_VERIFIED", fresh, f"Değer yeniden hesaplamada değişti ({stored!r} → {fresh!r}); veri arada güncellenmiş olabilir."
        if basis in ("ESTIMATED", "PARTIAL"):
            return "PARTIALLY_VERIFIED", fresh, "Değer bağımsız olarak yeniden üretildi; ancak dayandığı veri kısmen tahmini."
        if basis == "UNKNOWN":
            return "VERIFIED", fresh, "Değerin hesaplanamadığı (UNKNOWN) bağımsız olarak doğrulandı."
        return "VERIFIED", fresh, f"Bağımsız yeniden çalıştırma aynı değeri verdi ({r.tool} #{r.call_uid[:8]})."

    def _verify_internal(self, ev: dict, claimed) -> tuple[str, object, str]:
        if ev["metric"] == "creative.drafts":
            ids = [int(x) for x in (claimed or [])]
            n = self.engine_conn.execute(text("SELECT COUNT(*) FROM ai_creatives WHERE id = ANY(:i)"), {"i": ids}).scalar()
            return ("VERIFIED", n, "Taslaklar veritabanında mevcut.") if ids and n == len(ids) else ("FAILED", n, "Taslak kaydı bulunamadı.")
        if ev["metric"] == "growth.experiment":
            n = self.engine_conn.execute(text("SELECT COUNT(*) FROM ai_experiments WHERE id = :i"), {"i": int(claimed)}).scalar()
            return ("VERIFIED", claimed, "Deney kaydı mevcut.") if n else ("FAILED", None, "Deney kaydı yok.")
        return "UNVERIFIED", None, "Bu iç yazma türü için doğrulayıcı yok."

    def run(self, request_id: int) -> dict:
        with self.engine.connect() as c:
            self.engine_conn = c
            evs = rows(c, "SELECT * FROM ai_evidence WHERE request_id = :r ORDER BY id", r=request_id)
            # değerlendirmeler dayandıkları metriklerden sonra doğrulanır
            evs.sort(key=lambda e: ((e["value"] or {}).get("kind") == "assessment", e["id"]))
            by_id = {e["id"]: e for e in evs}
            results = []
            for ev in evs:
                status, val, note = self.verify(ev, by_id)
                ev["verification"], ev["verified_value"] = status, val
                results.append((ev["id"], status, val, note))
        with self.engine.begin() as c:
            for eid, status, val, note in results:
                c.execute(text("""UPDATE ai_evidence SET verification = :s, verified_value = CAST(:v AS JSONB), verifier_note = :n,
                                         verified_at = NOW() WHERE id = :i"""),
                          {"s": status, "v": json.dumps(val, default=str), "n": note[:1000], "i": eid})
        counts = {k: 0 for k in ORDER}
        for _, s, _, _ in results:
            counts[s] += 1
        return {"counts": counts, "claims": len(results), "reruns": self.rerun_calls}


def aggregate(statuses: list[str]) -> str:
    if not statuses:
        return "UNVERIFIED"
    if all(s == "VERIFIED" for s in statuses):
        return "VERIFIED"
    if any(s in ("VERIFIED", "PARTIALLY_VERIFIED") for s in statuses):
        return "PARTIALLY_VERIFIED"
    return "FAILED" if any(s == "FAILED" for s in statuses) else "UNVERIFIED"
