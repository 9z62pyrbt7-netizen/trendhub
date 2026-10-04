"""CEO orkestratörü: USER → CEO → PLAN → UZMAN AJANLAR → ARAÇLAR → SONUÇ → GERÇEKLİK DENETÇİSİ → CEO → USER.

  * Plan kullanıcı metnindeki talimatlardan DEĞİL, deterministik niyet kurallarından çıkar (prompt injection plan değiştiremez).
  * Her görev: benzersiz task_uid, ai_agent_tasks kaydı, kendi ai_agent_runs kaydı, CEO→ajan görev mesajı, ajan→CEO sonuç mesajı.
  * Görev en fazla 3 kez denenir (geçici hata); kalıcı hata sessizce yutulmaz, cevapta yazılır.
  * Araç çağırmadan sonuç üreten görev `no_evidence` olur; sonucu CEO cevabında kullanılmaz.
  * CEO yalnızca Gerçeklik Denetçisi'nin VERIFIED / PARTIALLY_VERIFIED dediği rakamları söyler; diğerlerini "doğrulanamadı" der.
  * Kullanıcı bir aksiyon önerirse (reklam aç, fiyat düşür, bütçe harca) CEO bunu yetkili birimin yazma aracına verir; risk
    motoru / Bütçe Yöneticisi / Kâr Koruması engellerse açıkça KARŞI çıkar ve kanıtlı alternatif önerir.
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from decimal import Decimal, InvalidOperation

from sqlalchemy import text
from sqlalchemy.engine import Engine

from ...db import row, rows
from . import metrics, sanitize
from .config import tl
from .reality import Checker, aggregate
from .specialists import HANDLERS, TASK_TR, TaskRuntime

log = logging.getLogger("trendhub.agents")
MAX_ATTEMPTS = 3
RETRY_BACKOFF = 0.5
MARK = {"VERIFIED": "✓", "PARTIALLY_VERIFIED": "≈", "UNVERIFIED": "?", "FAILED": "✗"}
STATUS_PLAN = [("analytics", "store_health"), ("finance", "profitability"), ("product_trend", "portfolio"),
               ("operations", "ops_health"), ("advertising", "ads_review")]


def _fold(s: str) -> str:
    return s.translate(str.maketrans("ıİçÇğĞöÖşŞüÜâ", "iiccggoossuua")).lower()


# ------------------------------------------------------------------ niyet ve plan
def _find_product(engine: Engine, q: str) -> dict | None:
    with engine.connect() as c:
        for tok in re.findall(r"[A-Za-z0-9][A-Za-z0-9_\-]{2,}", q):
            p = row(c, "SELECT id, name, sku, barcode, sale_price FROM products WHERE UPPER(sku) = UPPER(:s) OR barcode = :s LIMIT 1", s=tok)
            if p:
                return p
        cands = rows(c, "SELECT id, name, sku, barcode, sale_price FROM products WHERE COALESCE(is_active, TRUE) ORDER BY id LIMIT 5000")
    fq = _fold(q)
    best = None
    for p in cands:
        n = _fold(p["name"] or "")
        if len(n) >= 4 and n in fq and (best is None or len(n) > len(best["name"])):
            best = p
    return best


def _amount(q: str, prod: dict | None = None) -> Decimal | None:
    """Para tutarı: ürün kodu/barkodu/adı ve yüzdeler metinden çıkarılır; para birimi (TL/₺/lira/bin) yazılmış sayı esas alınır.
    Para birimi olmayan çıplak sayı tutar sayılmaz (ör. 'PRE-1' içindeki 1 → 1 TL DEĞİL)."""
    from .chat import parse_amount
    s = q
    for t in ((prod or {}).get("sku"), (prod or {}).get("barcode"), (prod or {}).get("name")):
        if t:
            s = re.sub(re.escape(str(t)), " ", s, flags=re.IGNORECASE)
    s = re.sub(r"%\s*\d+(?:[.,]\d+)?|\d+(?:[.,]\d+)?\s*%", " ", s)
    m = re.search(r"(\d{1,3}(?:[.\s]\d{3})+|\d+(?:[.,]\d+)?)\s*(bin|k)?\s*(tl|₺|lira)\b|(\d+(?:[.,]\d+)?)\s*bin\b", _fold(s))
    return parse_amount(m.group(0)) if m else None


def _pct(q: str) -> Decimal | None:
    m = re.search(r"%\s*(\d+(?:[.,]\d+)?)|(\d+(?:[.,]\d+)?)\s*%", q)
    if not m:
        return None
    try:
        return Decimal((m.group(1) or m.group(2)).replace(",", ".")) / 100
    except InvalidOperation:
        return None


STATUS_WORDS = ("ne durumday", "durumunu analiz", "durumu analiz", "analiz et", "durum raporu", "genel durum", "durumumuz")


def plan(engine: Engine, message: str, mode: str = "api") -> dict:
    """mode='chat': yalnızca durum analizi ve açık aksiyon talimatları orkestre edilir; diğer sorular CEO sohbet motoruna gider."""
    p = _plan(engine, message)
    if mode == "chat" and not (p["intent"] == "status" or p["intent"].startswith("action")):
        return {"intent": "other", "tasks": []}
    return p


def _plan(engine: Engine, message: str) -> dict:
    q = _fold(message)
    has = lambda *w: any(x in q for x in w)  # noqa: E731
    question = q.rstrip().endswith("?") or has(" mi ", " mi?", "miyim", "misin", "miyiz", "nasil", "neden") or bool(
        re.search(r"\b\w+m[ae]li(y\w*)?\b", q))
    act_ad = not question and has("reklam ac", "kampanya ac", "reklam ver", "reklama koy", "reklama harca", "butceyi reklama",
                                  "reklam baslat", "kampanya baslat")
    act_price = not question and has("fiyat") and has("dusur", "indir", "artir", "yukselt", "zam yap")
    act_disc = not question and has("indirim yap", "indirim uygula", "indirime gir")
    if act_ad or act_price or act_disc:
        prod = _find_product(engine, message)
        amount, pct = _amount(message, prod), _pct(message)
        if act_ad and prod and amount:
            days = 7
            return {"intent": "action.ads", "product": prod, "amount": amount,
                    "tasks": [("product_trend", "product_check", {"product_id": prod["id"]}),
                              ("product_trend", "portfolio", {}),
                              ("finance", "budget_check", {}),
                              ("advertising", "create_campaign", {"product_id": prod["id"], "daily_budget": str((amount / days).quantize(Decimal("0.01"))),
                                                                  "days": days, "reason": f"Kullanıcı isteği: {sanitize.clean_text(message, 300)}"})]}
        if act_ad and amount:
            return {"intent": "action.ad_budget", "amount": amount,
                    "tasks": [("finance", "budget_check", {}), ("advertising", "ads_review", {}), ("product_trend", "portfolio", {})]}
        if (act_price or act_disc) and prod and (pct or amount):
            cur = Decimal(str(prod["sale_price"] or 0))
            up = has("artir", "yukselt", "zam")
            new = (cur * (1 + pct) if up else cur * (1 - pct)).quantize(Decimal("0.01")) if pct else amount
            return {"intent": "action.price", "product": prod, "new_price": new,
                    "tasks": [("product_trend", "product_check", {"product_id": prod["id"]}),
                              ("product_trend", "price_change", {"product_id": prod["id"], "new_price": str(new),
                                                                 "reason": f"Kullanıcı isteği: {sanitize.clean_text(message, 300)}"})]}
        return {"intent": "action.unclear", "tasks": [], "note": "Aksiyon için ürün (SKU/ad) ve tutar/yüzde gerekli."}
    if has(*STATUS_WORDS):
        return {"intent": "status", "tasks": [(a, t, {}) for a, t in STATUS_PLAN]}
    if has("reklam", "roas", "kampanya"):
        return {"intent": "ads", "tasks": [("advertising", "ads_review", {}), ("finance", "profitability", {})]}
    if has("stok", "operasyon", "siparis islen", "hata", "kuyruk"):
        return {"intent": "operations", "tasks": [("operations", "ops_health", {})]}
    if has("kar", "zarar", "finans", "marj", "nakit", "basabas"):
        return {"intent": "finance", "tasks": [("finance", "profitability", {}), ("product_trend", "portfolio", {})]}
    if has("deney", "buyume", "buyut"):
        return {"intent": "growth", "tasks": [("product_trend", "portfolio", {}), ("marketing", "growth_experiments", {})]}
    if has("kreatif", "reklam metni", "hook", "gorsel"):
        return {"intent": "creative", "tasks": [("creative", "creative_brief", {})]}
    if has("sosyal", "instagram", "tiktok", "icerik takvim", "paylas"):
        return {"intent": "social", "tasks": [("social_media", "content_calendar", {"publish": has("paylas", "yayinla")})]}
    if has("karar", "gecmis", "ogren", "hafiza", "yanlis yap"):
        return {"intent": "memory", "tasks": [("ceo", "memory", {})]}
    if has("urun", "en iyi", "en kotu", "trend"):
        return {"intent": "products", "tasks": [("product_trend", "portfolio", {})]}
    return {"intent": "other", "tasks": []}


# ------------------------------------------------------------------ kayıt yardımcıları
def _msg(engine: Engine, request_id: int, frm: str, to: str, kind: str, content: str, task_id: int | None = None, data=None) -> None:
    with engine.begin() as c:
        c.execute(text("""INSERT INTO ai_agent_messages(request_id, task_id, from_agent, to_agent, kind, content, data)
                          VALUES (:r, :t, :f, :to, :k, :c, CAST(:d AS JSONB))"""),
                  {"r": request_id, "t": task_id, "f": frm, "to": to, "k": kind, "c": content[:4000],
                   "d": sanitize.to_json(data or {})})


def _act(engine: Engine, msg: str, *, agent: str, request_id: int, task_id: int | None = None, level: str = "info", kind: str = "task",
         run_id: int | None = None) -> None:
    with engine.begin() as c:
        c.execute(text("""INSERT INTO ai_activity(agent_code, kind, level, message, request_id, task_id, run_id)
                          VALUES (:a, :k, :l, :m, :r, :t, :run)"""),
                  {"a": agent, "k": kind, "l": level, "m": msg[:1000], "r": request_id, "t": task_id, "run": run_id})


# ------------------------------------------------------------------ görev yürütme
def run_task(engine: Engine, request_id: int, agent: str, task_type: str, inp: dict, user_id: int | None = None) -> dict:
    from .agents import agent_run
    uid = uuid.uuid4().hex
    with engine.begin() as c:
        tid = c.execute(text("""INSERT INTO ai_agent_tasks(task_uid, request_id, agent_code, task_type, objective, input, status)
                                VALUES (:u, :r, :a, :t, :o, CAST(:i AS JSONB), 'queued') RETURNING id"""),
                        {"u": uid, "r": request_id, "a": agent, "t": task_type, "o": TASK_TR.get(task_type, task_type),
                         "i": sanitize.to_json(inp)}).scalar()
    _msg(engine, request_id, "ceo", agent, "delegation", f"Görev: {TASK_TR.get(task_type, task_type)}", tid, inp)
    _act(engine, f"CEO görev verdi → {agent}: {TASK_TR.get(task_type, task_type)}", agent="ceo", request_id=request_id, task_id=tid)
    handler = HANDLERS.get((agent, task_type))
    t0 = time.monotonic()
    status, result, error, rt, attempt = "failed", {}, None, None, 0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        with engine.begin() as c:
            c.execute(text("UPDATE ai_agent_tasks SET status = 'running', attempts = :n, started_at = COALESCE(started_at, NOW()) WHERE id = :i"),
                      {"n": attempt, "i": tid})
        try:
            with agent_run(engine, agent, "task") as actx:
                with engine.begin() as c:
                    c.execute(text("UPDATE ai_agent_runs SET request_id = :r, task_id = :t WHERE id = :i"),
                              {"r": request_id, "t": tid, "i": actx.run_id})
                    c.execute(text("UPDATE ai_agent_tasks SET run_id = :run WHERE id = :i"), {"run": actx.run_id, "i": tid})
                rt = TaskRuntime(engine, request_id=request_id, task_id=tid, agent_code=agent, run_id=actx.run_id, user_id=user_id)
                if handler is None:
                    raise LookupError(f"{agent} için '{task_type}' görevi tanımlı değil")
                result = handler(rt, inp) or {}
                actx.sources += sorted({x.tool for x in rt.calls})
                actx.output = {"summary": result.get("summary"), "tool_calls": len(rt.calls), "claims": len(rt.claim_ids)}
                transient = [x for x in rt.calls if x.status == "timeout"]
                if rt.calls and len(transient) == len(rt.calls):
                    raise TimeoutError("Tüm araç çağrıları zaman aşımına uğradı")
            error = None
            break
        except Exception as exc:  # noqa: BLE001 - görev hatası kaydedilir; geçiciyse yeniden denenir
            error = f"{exc.__class__.__name__}: {str(exc)[:400]}"
            retryable = isinstance(exc, (TimeoutError, ConnectionError))
            with engine.begin() as c:
                c.execute(text("""INSERT INTO ai_agent_errors(request_id, task_id, agent_code, error_type, message, retryable, attempt)
                                  VALUES (:r, :t, :a, :e, :m, :rt, :n)"""),
                          {"r": request_id, "t": tid, "a": agent, "e": exc.__class__.__name__, "m": sanitize.redact(error),
                           "rt": retryable, "n": attempt})
            _act(engine, f"{agent} görevi hata verdi (deneme {attempt}/{MAX_ATTEMPTS}): {error}", agent=agent, request_id=request_id,
                 task_id=tid, level="error")
            if not retryable or attempt == MAX_ATTEMPTS:
                break
            time.sleep(RETRY_BACKOFF * (2 ** (attempt - 1)))
    calls = rt.calls if rt else []
    ok_calls = [x for x in calls if x.status in ("succeeded", "pending_approval", "blocked", "not_connected")]
    writes = [x for x in calls if x.access == "WRITE"]
    if error:
        status = "failed"
    elif not ok_calls:
        status = "no_evidence"
        error = ("Ajan hiçbir aracı başarıyla çalıştırmadan sonuç üretti; sonuç kanıtsız (kullanılmayacak)." if calls or result
                 else "Ajan araç çağırmadı.")
    elif writes and all(w.status == "blocked" for w in writes):
        status = "blocked"
    elif any(w.status == "pending_approval" for w in writes):
        status = "waiting_approval"
    else:
        status = "completed"
    failed_calls = [x for x in calls if x.status in ("failed", "timeout", "denied")]
    agent_output = {"summary": result.get("summary"), "findings": rt.findings if rt else [],
                    "recommendations": rt.recommendations if rt else [], "data": result.get("data")}
    stored = {"agent_output": sanitize.bounded(json.loads(sanitize.to_json(agent_output))),
              "tool_calls": [{"uid": x.call_uid, "tool": x.tool, "status": x.status, "summary": x.summary} for x in calls],
              "claims": rt.claim_ids if rt else [], "failed_tool_calls": len(failed_calls)}
    ms = int((time.monotonic() - t0) * 1000)
    with engine.begin() as c:
        c.execute(text("""UPDATE ai_agent_tasks SET status = :s, result = CAST(:r AS JSONB), error = :e, finished_at = NOW(),
                                 duration_ms = :ms WHERE id = :i"""),
                  {"s": status, "r": sanitize.to_json(stored), "e": error, "ms": ms, "i": tid})
    _msg(engine, request_id, agent, "ceo", "result", f"[{status}] {result.get('summary') or error or ''}", tid,
         {"tool_calls": len(calls), "claims": len(rt.claim_ids) if rt else 0})
    level = {"completed": "success", "waiting_approval": "warning", "blocked": "warning"}.get(status, "error")
    _act(engine, f"{agent} görevi {status}: {result.get('summary') or error or ''}", agent=agent, request_id=request_id, task_id=tid,
         level=level)
    metrics.inc("agent_tasks_total", agent=agent, status=status)
    return {"task_id": tid, "task_uid": uid, "agent": agent, "task_type": task_type, "status": status, "error": error,
            "calls": calls, "claims": rt.claim_ids if rt else [], "findings": agent_output["findings"],
            "recommendations": agent_output["recommendations"], "summary": result.get("summary"), "data": result.get("data"),
            "write_status": result.get("write_status")}


# ------------------------------------------------------------------ istek
def handle(engine: Engine, message: str, *, user_id: int | None = None, source: str = "api",
           conversation_id: str | None = None) -> dict:
    from .agents import agent_run
    clean = sanitize.clean_text(message, 2000)
    flags = sanitize.injection_flags(clean)
    uid = uuid.uuid4().hex
    t0 = time.monotonic()
    with engine.begin() as c:
        rid = c.execute(text("""INSERT INTO ai_requests(request_uid, user_id, source, conversation_id, message, status)
                                VALUES (:u, :us, :s, :cv, :m, 'planning') RETURNING id"""),
                        {"u": uid, "us": user_id, "s": source, "cv": conversation_id, "m": clean}).scalar()
    _msg(engine, rid, "user", "ceo", "request", clean, data={"injection_suspected": bool(flags)})
    if flags:
        _act(engine, "Kullanıcı mesajında talimat-benzeri kalıp: plan deterministik kurallarla yapıldı, metin talimat olarak "
                     "yorumlanmadı", agent="ceo", request_id=rid, level="warning", kind="security")
    with agent_run(engine, "ceo", "request") as ceo:
        with engine.begin() as c:
            c.execute(text("UPDATE ai_agent_runs SET request_id = :r WHERE id = :i"), {"r": rid, "i": ceo.run_id})
            c.execute(text("UPDATE ai_requests SET ceo_run_id = :run WHERE id = :r"), {"run": ceo.run_id, "r": rid})
        try:
            p = plan(engine, clean, "chat" if source == "chat" else "api")
        except Exception as exc:  # noqa: BLE001
            p = {"intent": "error", "tasks": [], "note": f"Plan hatası: {exc.__class__.__name__}"}
        plan_rows = [{"agent": a, "task": t, "input": i} for a, t, i in p["tasks"]]
        with engine.begin() as c:
            c.execute(text("UPDATE ai_requests SET intent = :i, plan = CAST(:p AS JSONB), status = 'running' WHERE id = :r"),
                      {"i": p["intent"], "p": sanitize.to_json(plan_rows), "r": rid})
        _act(engine, f"CEO plan yaptı ({p['intent']}): " + (", ".join(f"{a}.{t}" for a, t, _ in p["tasks"]) or "uzman görevi yok"),
             agent="ceo", request_id=rid, run_id=ceo.run_id)
        tasks = [run_task(engine, rid, a, t, i, user_id) for a, t, i in p["tasks"]]
        verification = {"counts": {}, "claims": 0}
        if tasks:
            with engine.begin() as c:
                c.execute(text("UPDATE ai_requests SET status = 'verifying' WHERE id = :r"), {"r": rid})
            verification = verify_request(engine, rid, tasks)
        if p["intent"] == "other":
            answer, extra = _fallback_chat(engine, rid, clean, user_id)
        else:
            answer, extra = compose(engine, rid, p, tasks)
        st = _request_status(tasks, verification, p)
        ceo.sources += [f"{t['agent']}.{t['task_type']}" for t in tasks]
        ceo.output = {"request_id": rid, "intent": p["intent"], "status": st}
    ms = int((time.monotonic() - t0) * 1000)
    with engine.begin() as c:
        c.execute(text("""UPDATE ai_requests SET status = :s, answer = :a, verification = CAST(:v AS JSONB), finished_at = NOW(),
                                 duration_ms = :ms WHERE id = :r"""),
                  {"s": st, "a": answer, "v": sanitize.to_json({**verification, **extra.get("verification_extra", {})}), "ms": ms, "r": rid})
    _msg(engine, rid, "ceo", "user", "answer", answer, data={"status": st})
    _act(engine, f"CEO cevap verdi ({st}, {ms} ms)", agent="ceo", request_id=rid, kind="answer",
         level="success" if st == "completed" else "warning")
    metrics.inc("requests_total", intent=p["intent"], status=st)
    log.info(sanitize.to_json({"event": "agent_request", "request_uid": uid, "intent": p["intent"], "status": st, "ms": ms,
                               "tasks": [(t["agent"], t["status"]) for t in tasks]}))
    return {"request_id": rid, "request_uid": uid, "intent": p["intent"], "status": st, "answer": answer, "duration_ms": ms,
            "tasks": [{k: t[k] for k in ("task_id", "task_uid", "agent", "task_type", "status", "error", "summary")} |
                      {"tool_calls": [{"uid": x.call_uid, "tool": x.tool, "status": x.status} for x in t["calls"]]} for t in tasks],
            "verification": verification, **{k: v for k, v in extra.items() if k != "verification_extra"}}


def verify_request(engine: Engine, rid: int, tasks: list[dict]) -> dict:
    from .agents import agent_run
    uid = uuid.uuid4().hex
    with engine.begin() as c:
        tid = c.execute(text("""INSERT INTO ai_agent_tasks(task_uid, request_id, agent_code, delegated_by, task_type, objective, status,
                                                           attempts, started_at)
                                VALUES (:u, :r, 'reality_checker', 'ceo', 'verify', 'Ajan iddialarını bağımsız doğrula', 'running', 1, NOW())
                                RETURNING id"""), {"u": uid, "r": rid}).scalar()
    _msg(engine, rid, "ceo", "reality_checker", "delegation", "Tüm iddiaları kanıtla doğrula", tid)
    t0 = time.monotonic()
    with agent_run(engine, "reality_checker", "task") as actx:
        with engine.begin() as c:
            c.execute(text("UPDATE ai_agent_runs SET request_id = :r, task_id = :t WHERE id = :i"), {"r": rid, "t": tid, "i": actx.run_id})
            c.execute(text("UPDATE ai_agent_tasks SET run_id = :run WHERE id = :t"), {"run": actx.run_id, "t": tid})
        chk = Checker(engine, rid, tid, actx.run_id)
        res = chk.run(rid)
        actx.output = res
    with engine.begin() as c:
        for t in tasks:
            st = [r[0] for r in c.execute(text("SELECT verification FROM ai_evidence WHERE task_id = :t"), {"t": t["task_id"]})]
            v = aggregate(st) if t["status"] not in ("failed", "no_evidence") else ("FAILED" if t["status"] == "failed" else "UNVERIFIED")
            t["verification"] = v
            c.execute(text("UPDATE ai_agent_tasks SET verification_status = :v WHERE id = :i"), {"v": v, "i": t["task_id"]})
        overall = aggregate([t["verification"] for t in tasks])
        c.execute(text("""UPDATE ai_agent_tasks SET status = 'completed', verification_status = :v, finished_at = NOW(), duration_ms = :ms,
                                 result = CAST(:r AS JSONB) WHERE id = :i"""),
                  {"v": overall, "ms": int((time.monotonic() - t0) * 1000), "r": sanitize.to_json(res), "i": tid})
    c_ = res["counts"]
    _msg(engine, rid, "reality_checker", "ceo", "verification",
         f"{res['claims']} iddia: VERIFIED {c_['VERIFIED']}, PARTIAL {c_['PARTIALLY_VERIFIED']}, UNVERIFIED {c_['UNVERIFIED']}, FAILED {c_['FAILED']}",
         tid, res)
    _act(engine, f"Gerçeklik Denetçisi {res['claims']} iddiayı kontrol etti ({res['reruns']} bağımsız tekrar sorgu): "
                 f"✓{c_['VERIFIED']} ≈{c_['PARTIALLY_VERIFIED']} ?{c_['UNVERIFIED']} ✗{c_['FAILED']}",
         agent="reality_checker", request_id=rid, task_id=tid, level="warning" if c_["FAILED"] else "success")
    return {**res, "overall": overall, "task_id": tid}


def _request_status(tasks: list[dict], verification: dict, p: dict) -> str:
    if p["intent"] == "other":
        return "completed"
    if not tasks:
        return "failed" if p["intent"] in ("error",) else "completed"
    if all(t["status"] in ("failed", "no_evidence") for t in tasks):
        return "failed"
    if any(t["status"] == "blocked" for t in tasks) and p["intent"].startswith("action"):
        return "blocked"
    bad = any(t["status"] in ("failed", "no_evidence") for t in tasks) or verification.get("counts", {}).get("FAILED")
    return "partial" if bad or verification.get("overall") not in ("VERIFIED", "PARTIALLY_VERIFIED") else "completed"


# ------------------------------------------------------------------ CEO sentezi
def _evidence(engine: Engine, rid: int) -> dict[str, list[dict]]:
    with engine.connect() as c:
        evs = rows(c, "SELECT id, metric, claim, verification, verifier_note, task_id FROM ai_evidence WHERE request_id = :r ORDER BY id",
                   r=rid)
    out: dict[str, list[dict]] = {}
    for e in evs:
        out.setdefault(e["metric"] or "", []).append(e)
    return out


def _line(ev: dict) -> str:
    v = ev["verification"] or "UNVERIFIED"
    if v in ("VERIFIED", "PARTIALLY_VERIFIED"):
        suffix = " (kısmen tahmini/değişken veri)" if v == "PARTIALLY_VERIFIED" else ""
        return f"{ev['claim']}{suffix} [kanıt #{ev['id']} {MARK[v]}]"
    return f"Doğrulanamadı — {ev['verifier_note'] or 'kanıt yok'} [kanıt #{ev['id']} {MARK[v]}]"


SECTIONS = [("Bugünkü satış", ["sales.today.orders", "sales.today.net_sales", "sales.7d.change"]),
            ("Net kâr", ["finance.today.net_profit", "finance.30d.net_profit", "finance.30d.contribution_margin",
                         "finance.30d.break_even_roas", "finance.30d.break_even_cpa"]),
            ("Reklam harcaması", ["ads.today.spend", "ads.7d.spend", "ads.7d.roas", "ads.campaign.assessment", "ads.platforms_connected"]),
            ("En iyi ürün", ["products.best"]), ("En kötü ürün", ["products.worst", "products.danger"]),
            ("Kritik stok", ["stock.critical"]), ("Operasyon", ["ops.failed_jobs", "ops.incidents"])]


def compose(engine: Engine, rid: int, p: dict, tasks: list[dict]) -> tuple[str, dict]:
    ev = _evidence(engine, rid)
    lines: list[str] = []
    extra: dict = {}
    if p["intent"] == "action.unclear" or (not tasks and p.get("note")):
        return ("Bu isteği uygulamaya hazırlamadım: " + p.get("note", "ne istendiği anlaşılmadı.") +
                " Örnek: \"SKU-123 ürününe 3.000 TL reklam aç\" veya \"SKU-123 fiyatını %5 artır\". Tutar/ürün tahmin edilmez."), extra
    if p["intent"].startswith("action"):
        return _compose_action(engine, rid, p, tasks, ev)
    used = set()
    for title, metrics_ in SECTIONS:
        items = [e for m in metrics_ for e in ev.get(m, [])]
        if not items:
            continue
        used |= {e["id"] for e in items}
        lines.append(f"**{title}**")
        lines += [f"- {_line(e)}" for e in items]
    rest = [e for es in ev.values() for e in es if e["id"] not in used]
    if rest:
        lines.append("**Diğer bulgular**")
        lines += [f"- {_line(e)}" for e in rest]
    risks = [f for t in tasks for f in t["findings"]]
    if risks:
        lines.append("**Riskler**")
        lines += [f"- {sanitize.clean_text(r, 300)}" for r in dict.fromkeys(risks)][:8]
    recs = sorted((r for t in tasks for r in t["recommendations"]), key=lambda r: -r["priority"])
    lines.append("**Önerilen aksiyon**")
    if recs:
        lines.append(f"- {recs[0]['text']} (öneren: {recs[0]['agent']})")
        lines += [f"- Sonra: {r['text']}" for r in recs[1:3]]
    else:
        lines.append("- Kanıta dayalı acil aksiyon yok; mevcut durumu koru, veri biriktikçe tekrar bakalım.")
    failed = [t for t in tasks if t["status"] in ("failed", "no_evidence")]
    if failed:
        lines.append("**Tamamlanamayan görevler**")
        lines += [f"- {t['agent']}: {t['status']} — {t['error']}" for t in failed]
    head = "Mağaza durumu (yalnızca Gerçeklik Denetçisi'nin doğruladığı rakamlar):" if p["intent"] == "status" else "Sonuç:"
    return head + "\n" + "\n".join(lines), extra


ALTERNATIVES = {
    "profit_guard_danger": "Önce bu ürünün fiyat/maliyet/komisyon yapısını düzelt; reklam bütçesini kârı kanıtlanmış ürüne ver.",
    "negative_margin": "Önce bu ürünün fiyat/maliyet yapısını düzelt; zarar eden ürün büyütülmez.",
    "profit_guard_unknown": "Önce ürün maliyetini gir; sonra en fazla {unknown_cap} kanıt toplayan küçük bir test aç.",
    "needs_data": "Önce eksik veriyi (maliyet/fiyat) gir; tahminle karar vermiyorum.",
    "stock_risk": "Önce stoğu güvenli seviyeye getir; stok yokken reklam parayı yakar.",
    "governor_single_action": "Tutarı tek işlem sınırının altına indir ve sonucu ölçerek kademeli artır.",
    "governor_total": "Kalan bütçe yetmiyor; daha küçük bir test ile başla veya bütçeyi artır (senin kararın).",
    "governor_daily": "Günlük harcama limiti aşılıyor; tutarı düşür veya yarına böl.",
    "governor_weekly": "Haftalık limit aşılıyor; önceki harcamaların sonucunu ölçmeden daha fazla para harcamıyorum.",
    "governor_agent": "Reklam ajanının 7 günlük limiti aşılıyor; önce mevcut kampanyaların sonucunu ölç.",
    "governor_campaign": "Tek ürün/kampanyaya 7 günde ayrılabilecek tutar aşılıyor; daha küçük bir testle başla.",
    "governor_no_budget": "Önce Bütçe Yöneticisi'nde toplam bütçeyi veya kasa bilgisini tanımla.",
    "budget_limit": "Günlük toplam reklam limiti dolu; zarar eden kampanyayı kapatıp yer aç.",
    "price_step": "Fiyatı tek seferde en fazla %{max_pct} değiştir; etkisini ölçüp devam et.",
    "below_min_profit": "Bu fiyat kâr tabanının altında; en düşük güvenli fiyat {floor}.",
    "no_cash_data": "Önce kasa bilgisini gir; kasada olduğu doğrulanmayan para harcanamaz.",
    "capital_exceeded": "Kullanılabilir sermaye yetmiyor; daha küçük bir test öner.",
    "stale_orders": "Sipariş verisi bayat; senkron düzelmeden para harcamıyorum.",
    "finance_stale": "Finans verisi güncel değil; senkron düzelmeden harcama yok.",
}


def _compose_action(engine: Engine, rid: int, p: dict, tasks: list[dict], ev: dict) -> tuple[str, dict]:
    from .config import thresholds
    write = next((t for t in tasks if t["task_type"] in ("create_campaign", "price_change")), None)
    lines = []
    for m in ("guard.state", "product.unit_profit", "budget.available", "ads.proven", "ads.7d.roas", "products.best"):
        lines += [f"- {_line(e)}" for e in ev.get(m, [])]
    if p["intent"] == "action.ad_budget":
        return _compose_budget(engine, p, tasks, lines), {"stance": "evaluated"}
    if write is None or write["status"] in ("failed", "no_evidence"):
        return "İsteği uygulamaya hazırlayamadım: " + ((write or {}).get("error") or "yazma görevi çalışmadı") + "\n" + "\n".join(lines), \
            {"stance": "error"}
    data = write["data"] or {}
    blocks = data.get("blocks") or []
    with engine.connect() as c:
        th = thresholds(c)
        floor = None
        if p["intent"] == "action.price":
            from .economics import floor_price, unit_economics
            fl = floor_price(unit_economics(c, p["product"]["id"]), th)
            floor = tl(fl) if fl else "hesaplanamıyor (maliyet eksik)"
    if write["status"] == "blocked":
        alts = []
        for b in blocks:
            a = ALTERNATIVES.get(b["code"])
            if a:
                alts.append(a.format(unknown_cap=tl(th["profit_guard_unknown_max_spend"]),
                                     max_pct=f"{Decimal(str(th['max_price_change_pct'])) * 100:.0f}", floor=floor))
        best = next((e for e in ev.get("products.best", [])), None)
        txt = ["**KARŞIYIM.** Bu işlem net kârı korumuyor ve sistem kuralları tarafından engellendi:"]
        txt += [f"- {b['message']}" for b in blocks]
        txt.append("**Kanıt:**")
        txt += lines or ["- (ek kanıt yok)"]
        txt.append("**Daha iyi alternatif:**")
        txt += [f"- {a}" for a in dict.fromkeys(alts)] or ["- Önce engel nedenini gider."]
        if best and best["verification"] in ("VERIFIED", "PARTIALLY_VERIFIED") and p["intent"] == "action.ads":
            txt.append(f"- Reklam düşünüyorsan önce kanıtlanmış ürüne bak: {best['claim']}")
        txt.append(f"Öneri #{data.get('proposal_id')} BLOCKED olarak kaydedildi; sahip onayı bile bu bloğu aşamaz.")
        _msg(engine, rid, "ceo", "user", "objection", "\n".join(txt[:3]), data={"blocks": [b["code"] for b in blocks]})
        return "\n".join(txt), {"stance": "oppose", "proposal_id": data.get("proposal_id")}
    warns = data.get("warnings") or []
    stance = "conditional" if warns else "support"
    txt = [("**Şartlı destekliyorum.**" if warns else "**Mantıklı görünüyor**") +
           f" Öneri #{data.get('proposal_id')} oluşturuldu ve risk {str(data.get('risk_level', '')).upper()}: "
           "**henüz hiçbir şey uygulanmadı**, Onaylar ekranında senin onayını bekliyor."]
    txt += [f"- Uyarı: {w['message']}" for w in warns]
    txt.append("**Kanıt:**")
    txt += lines or ["- (ek kanıt yok)"]
    txt.append("Onaylansa bile platform bağlantısı yoksa sistem uygulamaz; manuel adımı verir (SKIPPED).")
    return "\n".join(txt), {"stance": stance, "proposal_id": data.get("proposal_id")}


def _compose_budget(engine: Engine, p: dict, tasks: list[dict], lines: list[str]) -> str:
    amount = p["amount"]
    with engine.connect() as c:
        from .chat import tool_ad_budget_plan
        bp = tool_ad_budget_plan(c, float(amount))
    txt = []
    if bp["justified"] < amount:
        txt.append(f"**Karşıyım: {tl(amount)} tutarın tamamını reklama koymayı önermiyorum.** Kanıtla desteklenen kısım "
                   f"{tl(bp['justified'])}; kalan {tl(bp['unused'])} için kârlılığı kanıtlanmış kampanya yok.")
    else:
        txt.append(f"{tl(amount)} kanıtlı kampanyalara dağıtılabilir.")
    txt += [f"- {x['campaign']}: {tl(x['monthly'])} — {x['reason']}" for x in bp["plan"]]
    if bp["excluded_losing"]:
        txt.append("- Zarar eden kampanyalara bütçe verilmez: " + ", ".join(bp["excluded_losing"]))
    txt.append("**Kanıt:**")
    txt += lines or ["- (ek kanıt yok)"]
    txt.append("Bu bir plan; her harcama ayrıca Bütçe Yöneticisi limitlerinden ve senin onayından geçer.")
    return "\n".join(txt)


# ------------------------------------------------------------------ genel sohbet (mevcut CEO sohbet motoru)
def _fallback_chat(engine: Engine, rid: int, message: str, user_id: int | None) -> tuple[str, dict]:
    """Uzman görevi gerektirmeyen soru: mevcut CEO sohbet motoru. Kullandığı her araç ai_tool_calls'a kaydedilir."""
    from . import chat
    from .agents import agent_run
    with agent_run(engine, "ceo", "chat") as actx:
        with engine.begin() as c:
            c.execute(text("UPDATE ai_agent_runs SET request_id = :r WHERE id = :i"), {"r": rid, "i": actx.run_id})
        token = chat.TOOL_LOG.set({"engine": engine, "request_id": rid, "run_id": actx.run_id})
        try:
            t0 = time.monotonic()
            with engine.begin() as c:
                res = chat.answer(c, message, [])
            if res.get("engine") == "claude":
                res.setdefault("usage", {})["latency_ms"] = int((time.monotonic() - t0) * 1000)
                metrics.observe("llm_latency_ms", res["usage"]["latency_ms"])
        finally:
            chat.TOOL_LOG.reset(token)
        actx.output = {"engine": res["engine"], "tools": res["tools_used"]}
    note = ("\n\n_(Bu cevap CEO sohbet motorundan; rakamlar kayıtlı araç çağrılarından geldi, uzman görevi ve bağımsız "
            "doğrulama yapılmadı. Kanıtlı analiz için: \"Mağazanın durumunu analiz et\".)_")
    return res["answer"] + note, {"engine": res["engine"], "tools_used": res["tools_used"], "usage": res.get("usage", {}),
                                  "verification_extra": {"overall": "UNVERIFIED", "mode": "chat_engine"}}
