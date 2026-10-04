"""Büyüme deneyleri: başlangıçta temel değer kaydedilir, süre sonunda GERÇEK veriden ölçülür.
Ölçüm olmadan deney başarılı sayılmaz (SUCCESS / FAILED / INCONCLUSIVE)."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ...db import rows


def metric(conn: Connection, e: dict, start: date, end: date) -> Decimal | None:
    from .config import Window
    from .data import product_economics
    if not e["product_id"]:
        return None
    x = next(iter(product_economics(conn, Window(1, start_date=start, end_date=end), [e["product_id"]])), None)
    if x is None:
        return Decimal("0")
    return {"net_profit": x["net_profit"], "units": Decimal(x["units"]), "net_sales": x["net_sales"],
            "net_margin": x["net_margin"]}.get(e["success_metric"])


def measure_experiments(conn: Connection) -> int:
    """Süresi dolan deneyleri ölçer. Ölçüm olmadan deney başarılı sayılmaz."""
    from datetime import timedelta

    from .config import today
    n = 0
    for e in rows(conn, "SELECT * FROM ai_experiments WHERE status = 'running' AND measure_after <= :t", t=today()):
        start = e["started_at"].date()
        val = metric(conn, e, start, start + timedelta(days=e["duration_days"] - 1))
        base = (e["baseline"] or {}).get("value")
        if val is None or base is None:
            outcome = "INCONCLUSIVE"
        else:
            diff = val - Decimal(base)
            thr = Decimal(str(e["success_threshold"] or 0))
            outcome = "SUCCESS" if diff >= thr and diff > 0 else ("FAILED" if diff < 0 else "INCONCLUSIVE")
        conn.execute(text("""UPDATE ai_experiments SET status = 'measured', outcome = :o, result = CAST(:r AS JSONB), updated_at = NOW()
                              WHERE id = :i"""),
                     {"o": outcome, "r": json.dumps({"value": str(val) if val is not None else None, "baseline": base}), "i": e["id"]})
        n += 1
    return n
