"""Ajan gözlemlenebilirliği.

İki kaynak:
  * Süreç içi sayaçlar (bu API/worker sürecinin ömrü; yeniden başlatmada sıfırlanır) — Prometheus metin biçimi.
  * Veritabanı türevi sayaçlar (kalıcı, tüm süreçler) — `db_metrics`. Panel bunu gösterir; Prometheus çıktısı ikisini de verir.
"""
from __future__ import annotations

import threading
from collections import defaultdict

from sqlalchemy.engine import Connection

from ...db import row, rows

_lock = threading.Lock()
_counters: dict[tuple, float] = defaultdict(float)
_latency: dict[str, list[float]] = defaultdict(list)


def inc(name: str, value: float = 1, **labels) -> None:
    with _lock:
        _counters[(name, tuple(sorted(labels.items())))] += value


def observe(name: str, ms: float) -> None:
    with _lock:
        lst = _latency[name]
        lst.append(ms)
        if len(lst) > 2000:
            del lst[:1000]


def observe_tool(tool: str, status: str, ms: float) -> None:
    inc("tool_calls_total", tool=tool, status=status)
    if status not in ("succeeded", "pending_approval", "not_connected", "blocked"):
        inc("tool_failures_total", tool=tool, status=status)
    observe("tool_latency_ms", ms)


def snapshot() -> dict:
    with _lock:
        return {"counters": [{"name": k[0], "labels": dict(k[1]), "value": v} for k, v in _counters.items()],
                "latency": {k: _pct(v) for k, v in _latency.items()}}


def _pct(vals: list[float]) -> dict:
    if not vals:
        return {"count": 0}
    s = sorted(vals)
    return {"count": len(s), "p50": s[len(s) // 2], "p95": s[min(len(s) - 1, int(len(s) * 0.95))], "max": s[-1]}


def db_metrics(conn: Connection, hours: int = 24) -> dict:
    p = {"h": hours}
    runs = rows(conn, """SELECT agent_code, status, COUNT(*) AS n FROM ai_agent_runs
                          WHERE started_at > NOW() - make_interval(hours => :h) GROUP BY 1, 2 ORDER BY 1, 2""", **p)
    tools = rows(conn, """SELECT tool, status, COUNT(*) AS n, AVG(duration_ms)::int AS avg_ms,
                                 PERCENTILE_DISC(0.95) WITHIN GROUP (ORDER BY duration_ms) AS p95_ms
                            FROM ai_tool_calls WHERE started_at > NOW() - make_interval(hours => :h) GROUP BY 1, 2 ORDER BY 1, 2""", **p)
    llm = row(conn, """SELECT COUNT(*) AS n, AVG((usage->>'latency_ms')::numeric)::int AS avg_ms,
                              MAX((usage->>'latency_ms')::numeric)::int AS max_ms
                         FROM ai_chat_messages WHERE engine = 'claude' AND created_at > NOW() - make_interval(hours => :h)
                          AND usage ? 'latency_ms'""", **p)
    r = row(conn, """SELECT (SELECT COUNT(*) FROM ai_agent_runs WHERE started_at > NOW() - make_interval(hours => :h)) AS agent_runs_total,
                            (SELECT COUNT(*) FROM ai_agent_runs WHERE status = 'error' AND started_at > NOW() - make_interval(hours => :h)) AS agent_failures_total,
                            (SELECT COUNT(*) FROM ai_tool_calls WHERE started_at > NOW() - make_interval(hours => :h)) AS tool_calls_total,
                            (SELECT COUNT(*) FROM ai_tool_calls WHERE status IN ('failed', 'timeout', 'denied')
                               AND started_at > NOW() - make_interval(hours => :h)) AS tool_failures_total,
                            (SELECT COUNT(*) FROM ai_proposals WHERE requires_approval AND created_at > NOW() - make_interval(hours => :h)) AS approval_requests,
                            (SELECT COUNT(*) FROM ai_proposals WHERE status = 'pending_approval' AND requires_approval) AS approvals_pending,
                            (SELECT COUNT(*) FROM ai_actions WHERE status = 'BLOCKED' AND created_at > NOW() - make_interval(hours => :h)) AS actions_blocked,
                            (SELECT COUNT(*) FROM ai_requests WHERE started_at > NOW() - make_interval(hours => :h)) AS requests_total,
                            (SELECT COUNT(*) FROM ai_agent_errors WHERE created_at > NOW() - make_interval(hours => :h)) AS agent_errors_total,
                            (SELECT AVG(duration_ms)::int FROM ai_tool_calls WHERE started_at > NOW() - make_interval(hours => :h)) AS tool_latency_avg_ms""", **p)
    return {"window_hours": hours, **{k: int(v or 0) for k, v in r.items()}, "runs": runs, "tools": tools,
            "llm": {"calls": int(llm["n"] or 0), "latency_avg_ms": llm["avg_ms"], "latency_max_ms": llm["max_ms"]}}


def prometheus(conn: Connection) -> str:
    m = db_metrics(conn)
    out = ["# HELP trendhub_agent_runs_total Ajan çalışmaları (son 24 saat, DB)", "# TYPE trendhub_agent_runs_total gauge"]
    for r in m["runs"]:
        out.append(f'trendhub_agent_runs_total{{agent="{r["agent_code"]}",status="{r["status"]}"}} {r["n"]}')
    out += ["# TYPE trendhub_agent_failures_total gauge", f"trendhub_agent_failures_total {m['agent_failures_total']}",
            "# TYPE trendhub_tool_calls_total gauge"]
    for r in m["tools"]:
        out.append(f'trendhub_tool_calls_total{{tool="{r["tool"]}",status="{r["status"]}"}} {r["n"]}')
        if r["avg_ms"] is not None:
            out.append(f'trendhub_tool_latency_ms_avg{{tool="{r["tool"]}",status="{r["status"]}"}} {r["avg_ms"]}')
    out += ["# TYPE trendhub_tool_failures_total gauge", f"trendhub_tool_failures_total {m['tool_failures_total']}",
            "# TYPE trendhub_approval_requests gauge", f"trendhub_approval_requests {m['approval_requests']}",
            f"trendhub_approvals_pending {m['approvals_pending']}",
            "# TYPE trendhub_actions_blocked gauge", f"trendhub_actions_blocked {m['actions_blocked']}",
            "# TYPE trendhub_llm_latency_ms_avg gauge", f"trendhub_llm_latency_ms_avg {m['llm']['latency_avg_ms'] or 0}",
            f"trendhub_llm_calls {m['llm']['calls']}"]
    snap = snapshot()
    for c in snap["counters"]:
        lab = ",".join(f'{k}="{v}"' for k, v in c["labels"].items())
        out.append(f"trendhub_process_{c['name']}{{{lab}}} {c['value']:g}")
    return "\n".join(out) + "\n"
