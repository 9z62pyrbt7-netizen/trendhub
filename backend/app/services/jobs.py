"""PostgreSQL tabanlı iş kuyruğu (sync_jobs).

* Kuyruk: status='queued' ve run_after <= NOW() olan işler.
* Talep: `FOR UPDATE SKIP LOCKED` - birden fazla worker güvenle çalışır.
* Idempotency: aynı `idempotency_key` ile bekleyen/çalışan ikinci iş
  oluşturulamaz (partial unique index).
* Retry: hata tekrar denenebilir ise exponential backoff + jitter ile
  yeniden kuyruğa alınır; `max_attempts` aşılınca 'dead' olur ve sistem
  olayı yazılır.
* Kurtarma: worker çökerse 'running' kalan işler `locked_at` zaman aşımından
  sonra yeniden kuyruğa alınır.

Eski (legacy) sync_jobs kayıtları farklı statüler taşıyabilir; kuyruk
yalnızca yukarıdaki statülere baktığı için onlara dokunulmaz.
"""
from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ..connectors.http import backoff_delay
from .events import record_event

QUEUED, RUNNING, SUCCEEDED, FAILED, DEAD = "queued", "running", "succeeded", "failed", "dead"
STALE_AFTER = timedelta(minutes=15)


def enqueue(conn: Connection, job_type: str, *, marketplace: str | None = None,
            payload: dict | None = None, idempotency_key: str | None = None,
            delay_seconds: float = 0, max_attempts: int = 6, store_id: int | None = None) -> int | None:
    """İşi kuyruğa ekler. Aynı anahtarla bekleyen iş varsa None döner."""
    return conn.execute(text("""
        INSERT INTO sync_jobs(marketplace, job_type, status, payload, idempotency_key, run_after,
                              max_attempts, attempts, store_id, created_at, started_at)
        VALUES (:mp, :jt, 'queued', CAST(:payload AS JSONB), :key,
                NOW() + make_interval(secs => :delay), :max, 0, :store, NOW(), NULL)
        ON CONFLICT (idempotency_key) WHERE status IN ('queued','running') DO NOTHING
        RETURNING id
    """), {"mp": marketplace, "jt": job_type, "payload": json.dumps(payload or {}),
           "key": idempotency_key, "delay": delay_seconds, "max": max_attempts,
           "store": store_id}).scalar()


def claim(conn: Connection, worker_id: str) -> dict | None:
    job = conn.execute(text("""
        WITH next AS (
            SELECT id FROM sync_jobs
             WHERE status = 'queued' AND run_after <= NOW()
             ORDER BY run_after, id
             FOR UPDATE SKIP LOCKED
             LIMIT 1
        )
        UPDATE sync_jobs j
           SET status = 'running', locked_by = :w, locked_at = NOW(), started_at = NOW(),
               attempts = j.attempts + 1, finished_at = NULL
          FROM next WHERE j.id = next.id
        RETURNING j.id, j.job_type, j.marketplace, j.payload, j.attempts, j.max_attempts, j.store_id
    """), {"w": worker_id}).mappings().first()
    return dict(job) if job else None


def complete(conn: Connection, job_id: int, result: dict, message: str = "OK") -> None:
    conn.execute(text("""
        UPDATE sync_jobs SET status = 'succeeded', result = CAST(:r AS JSONB), message = :m,
               last_error = NULL, finished_at = NOW(), locked_by = NULL
         WHERE id = :id
    """), {"id": job_id, "r": json.dumps(result, default=str), "m": message[:1000]})


def fail(conn: Connection, job: dict, error: str, *, retryable: bool = True,
         retry_after: float | None = None) -> str:
    """Hatayı işler; yeni statüyü döner."""
    attempts, max_attempts = int(job["attempts"]), int(job["max_attempts"] or 6)
    if retryable and attempts < max_attempts:
        delay = retry_after if retry_after is not None else backoff_delay(attempts, base=30, cap=3600)
        conn.execute(text("""
            UPDATE sync_jobs SET status = 'queued', last_error = :e, message = :e,
                   run_after = NOW() + make_interval(secs => :d), locked_by = NULL, finished_at = NOW()
             WHERE id = :id
        """), {"id": job["id"], "e": error[:2000], "d": delay})
        return QUEUED
    status = DEAD if retryable else FAILED
    conn.execute(text("""
        UPDATE sync_jobs SET status = :s, last_error = :e, message = :e, finished_at = NOW(), locked_by = NULL
         WHERE id = :id
    """), {"id": job["id"], "e": error[:2000], "s": status})
    record_event(conn, level="error", source=f"job:{job['job_type']}",
                 message=f"İş #{job['id']} başarısız ({job.get('marketplace') or '-'}): {error[:500]}",
                 details={"job_id": job["id"], "attempts": attempts},
                 fingerprint=f"job:{job['job_type']}:{job.get('marketplace')}")
    return status


def requeue_stale(conn: Connection) -> int:
    return conn.execute(text("""
        UPDATE sync_jobs SET status = 'queued', locked_by = NULL,
               last_error = COALESCE(last_error, '') || ' [worker zaman aşımı, yeniden kuyrukta]'
         WHERE status = 'running' AND locked_at < NOW() - make_interval(secs => :s)
    """), {"s": STALE_AFTER.total_seconds()}).rowcount


def queue_stats(conn: Connection) -> dict:
    r = conn.execute(text("""
        SELECT COUNT(*) FILTER (WHERE status = 'queued') AS queued,
               COUNT(*) FILTER (WHERE status = 'running') AS running,
               COUNT(*) FILTER (WHERE status IN ('failed','dead') AND finished_at > NOW() - INTERVAL '24 hours') AS failed_24h,
               COUNT(*) FILTER (WHERE status = 'succeeded' AND finished_at > NOW() - INTERVAL '24 hours') AS succeeded_24h
          FROM sync_jobs
    """)).mappings().first()
    return dict(r)
