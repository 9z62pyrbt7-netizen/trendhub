"""Yönetim komutları.

    python -m app.cli healthcheck worker    # bu konteynerdeki worker son 90 sn'de sinyal verdi mi
    python -m app.cli healthcheck db        # DB erişimi + şema en güncel sürümde mi
    python -m app.cli create-user --username ali --role admin
    python -m app.cli reset-password --username admin

Parolalar komut satırından ALINMAZ (shell geçmişine düşmesin): etkileşimli
olarak sorulur veya TRENDHUB_NEW_PASSWORD ortam değişkeninden okunur.
"""
from __future__ import annotations

import argparse
import getpass
import os
import socket
import sys

from sqlalchemy import text

from .db import get_engine, transaction
from .security import hash_password, revoke_user_sessions, validate_password_strength


def _password() -> str:
    pw = os.environ.get("TRENDHUB_NEW_PASSWORD")
    if not pw:
        pw = getpass.getpass("Yeni parola: ")
        if pw != getpass.getpass("Tekrar: "):
            raise SystemExit("Parolalar eşleşmiyor")
    validate_password_strength(pw)
    return pw


def _head_revision() -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = Config(os.path.join(here, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(here, "migrations"))
    return ScriptDirectory.from_config(cfg).get_current_head()


def healthcheck(target: str) -> int:
    try:
        with get_engine().connect() as conn:
            if target == "worker":
                ok = conn.execute(text("""
                    SELECT EXISTS (SELECT 1 FROM worker_heartbeats
                                    WHERE hostname = :h AND last_seen_at > NOW() - INTERVAL '90 seconds')
                """), {"h": socket.gethostname()}).scalar()
                print("worker: ok" if ok else "worker: son 90 sn içinde heartbeat yok")
                return 0 if ok else 1
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            head = _head_revision()
            print(f"db: şema {version}, beklenen {head}")
            return 0 if version == head else 1
    except Exception as exc:  # noqa: BLE001
        print(f"{target}: hata {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("healthcheck")
    h.add_argument("target", choices=["worker", "db"])
    c = sub.add_parser("create-user")
    c.add_argument("--username", required=True)
    c.add_argument("--role", choices=["admin", "operator", "viewer"], default="viewer")
    c.add_argument("--full-name")
    r = sub.add_parser("reset-password")
    r.add_argument("--username", required=True)
    sub.add_parser("has-admin", help="Aktif yönetici varsa 0, yoksa 3 ile çıkar (deploy betiği için)")
    a = p.parse_args(argv)

    if a.cmd == "healthcheck":
        return healthcheck(a.target)
    if a.cmd == "has-admin":
        with transaction() as conn:
            n = conn.execute(text("SELECT COUNT(*) FROM users WHERE role = 'admin' AND is_active")).scalar()
        print("yönetici var" if n else "yönetici yok")
        return 0 if n else 3
    try:
        pw = _password()
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    with transaction() as conn:
        if a.cmd == "create-user":
            exists = conn.execute(text("SELECT 1 FROM users WHERE username = :u"), {"u": a.username}).first()
            if exists:
                print("Bu kullanıcı adı zaten var", file=sys.stderr)
                return 1
            conn.execute(text("INSERT INTO users(username, full_name, role, password_hash) VALUES (:u, :f, :r, :h)"),
                         {"u": a.username, "f": a.full_name, "r": a.role, "h": hash_password(pw)})
            action = "user.created"
        else:
            uid = conn.execute(text("""UPDATE users SET password_hash = :h, failed_login_count = 0, locked_until = NULL,
                                              password_changed_at = NOW(), updated_at = NOW()
                                        WHERE username = :u RETURNING id"""),
                               {"h": hash_password(pw), "u": a.username}).scalar()
            if uid is None:
                print("Kullanıcı bulunamadı", file=sys.stderr)
                return 1
            revoke_user_sessions(conn, uid)
            action = "user.password_reset"
        from .services.audit import log_audit
        log_audit(conn, actor="cli", action=action, entity_type="user", entity_id=a.username)
    print("Tamam")
    return 0


if __name__ == "__main__":
    sys.exit(main())
