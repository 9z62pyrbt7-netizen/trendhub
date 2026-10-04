"""Arka plan worker + zamanlayıcı.

Çalıştırma:  python -m app.worker

* Birden fazla worker çalışabilir; zamanlayıcıyı aynı anda yalnızca biri
  çalıştırır (pg_try_advisory_lock ile lider seçimi).
* Her döngüde heartbeat yazılır; Sistem ekranı worker'ın canlı olup
  olmadığını buradan gösterir.
* SIGTERM/SIGINT ile mevcut iş bitince temiz kapanır.
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
import uuid
from contextlib import contextmanager

from sqlalchemy import text

from .config import get_settings
from .connectors.base import ConnectorError, RetryableError
from .db import get_engine
from .services import jobs, sync_service

log = logging.getLogger("trendhub.worker")
SCHEDULER_LOCK = 7342002


class Worker:
    def __init__(self, engine=None, settings=None):
        self.engine = engine or get_engine()
        self.settings = settings or get_settings()
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.stopping = False
        self._leader_conn = None
        self._leader = False

    def heartbeat(self, current_job_id: int | None = None) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO worker_heartbeats(worker_id, hostname, started_at, last_seen_at, current_job_id)
                VALUES (:w, :h, NOW(), NOW(), :j)
                ON CONFLICT (worker_id) DO UPDATE SET last_seen_at = NOW(), current_job_id = EXCLUDED.current_job_id
            """), {"w": self.worker_id, "h": socket.gethostname(), "j": current_job_id})

    def _is_leader(self) -> bool:
        # Oturum seviyesinde advisory lock ayrı, kalıcı bir bağlantıda tutulur;
        # bağlantı koparsa kilit PostgreSQL tarafından bırakılır.
        if self._leader:
            return True
        if self._leader_conn is None:
            self._leader_conn = self.engine.connect()
        got = self._leader_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": SCHEDULER_LOCK}).scalar()
        self._leader_conn.commit()
        self._leader = bool(got)
        return self._leader

    def schedule(self) -> None:
        if not self._is_leader():
            return
        with self.engine.begin() as conn:
            jobs.requeue_stale(conn)
            sync_service.schedule_due_jobs(conn, self.settings.sync_interval_minutes, self.settings)
            sync_service.schedule_supplier_jobs(conn)
            sync_service.schedule_alerts_scan(conn)
            sync_service.schedule_storefront_maintenance(conn)
            sync_service.schedule_ai_cycle(conn)
            sync_service.schedule_ai_reviews(conn)
            sync_service.schedule_event_processing(conn)

    @contextmanager
    def _keepalive(self, job_id: int):
        """İş sürerken arka planda heartbeat + iş kilidi tazeleme."""
        stop = threading.Event()

        def beat():
            while not stop.wait(jobs.HEARTBEAT_SECONDS):
                try:
                    with self.engine.begin() as conn:
                        jobs.touch(conn, job_id, self.worker_id)
                    self.heartbeat(job_id)
                except Exception:  # noqa: BLE001
                    log.exception("Heartbeat yazılamadı")

        t = threading.Thread(target=beat, name=f"keepalive-{job_id}", daemon=True)
        t.start()
        try:
            yield
        finally:
            stop.set()
            t.join(timeout=5)

    def run_once(self) -> bool:
        """Bir iş çalıştırır. İş bulunduysa True döner."""
        with self.engine.begin() as conn:
            job = jobs.claim(conn, self.worker_id)
        if job is None:
            return False
        self.heartbeat(job["id"])
        log.info("İş #%s başladı: %s %s", job["id"], job["job_type"], job["marketplace"])
        try:
            with self._keepalive(job["id"]):
                result = sync_service.execute(self.engine, job, self.settings)
        except ConnectorError as exc:
            retry_after = exc.retry_after if isinstance(exc, RetryableError) else None
            with self.engine.begin() as conn:
                status = jobs.fail(conn, job, str(exc), retryable=exc.retryable, retry_after=retry_after)
            log.warning("İş #%s hata (%s): %s", job["id"], status, exc)
        except Exception as exc:  # noqa: BLE001 - beklenmeyen hatalar da kuyruğu durdurmamalı
            log.exception("İş #%s beklenmeyen hata", job["id"])
            with self.engine.begin() as conn:
                jobs.fail(conn, job, f"{exc.__class__.__name__}: {exc}", retryable=True)
        else:
            with self.engine.begin() as conn:
                if not jobs.complete(conn, job["id"], result, worker_id=self.worker_id):
                    log.warning("İş #%s artık bu worker'da değil; sonuç yazılmadı", job["id"])
            log.info("İş #%s tamamlandı: %s", job["id"], result)
        finally:
            self.heartbeat(None)
        return True

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)
        log.info("Worker başladı: %s", self.worker_id)
        while not self.stopping:
            try:
                self.heartbeat()
                self.schedule()
                while not self.stopping and self.run_once():
                    pass
            except Exception:  # noqa: BLE001 - DB kesintisinde bekleyip tekrar dene
                log.exception("Worker döngü hatası")
                if self._leader_conn is not None:
                    try:
                        self._leader_conn.close()
                    finally:
                        self._leader_conn = None
                        self._leader = False
            time.sleep(self.settings.worker_poll_seconds)
        log.info("Worker durdu")

    def _stop(self, *_):
        self.stopping = True


def main() -> None:
    from .logging_setup import configure_logging
    configure_logging()
    Worker().run_forever()


if __name__ == "__main__":
    main()
