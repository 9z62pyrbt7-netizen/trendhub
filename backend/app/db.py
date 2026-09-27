"""Veritabanı bağlantısı.

SQLAlchemy Core + açık SQL kullanılır: mevcut (legacy) tablolar ORM'e
zorlanmadan, PostgreSQL özelliklerinden (ON CONFLICT, SKIP LOCKED, advisory
lock) doğrudan yararlanılır. Tek bir bağlantı havuzu tüm istekleri paylaşır.
"""
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

from .config import get_settings


@lru_cache
def get_engine() -> Engine:
    return create_engine(
        get_settings().sqlalchemy_url,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
        pool_recycle=1800,
        future=True,
    )


@contextmanager
def transaction() -> Iterator[Connection]:
    """Tek bir transaction açar; hata olursa geri alır."""
    with get_engine().begin() as conn:
        yield conn


def get_conn() -> Iterator[Connection]:
    """FastAPI dependency: istek başına bir transaction."""
    with get_engine().begin() as conn:
        yield conn


def rows(conn: Connection, sql: str, **params: Any) -> list[dict]:
    return [dict(r._mapping) for r in conn.execute(text(sql), params)]


def row(conn: Connection, sql: str, **params: Any) -> dict | None:
    r = conn.execute(text(sql), params).first()
    return dict(r._mapping) if r else None


def scalar(conn: Connection, sql: str, **params: Any) -> Any:
    return conn.execute(text(sql), params).scalar()
