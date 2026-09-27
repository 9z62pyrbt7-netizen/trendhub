import socket

from sqlalchemy import text

from app import cli


def test_cli_create_user_and_reset_password(engine, monkeypatch):
    monkeypatch.setenv("TRENDHUB_NEW_PASSWORD", "Cli-Password-12345")
    assert cli.main(["create-user", "--username", "cliuser", "--role", "operator"]) == 0
    assert cli.main(["create-user", "--username", "cliuser"]) == 1
    with engine.begin() as c:
        c.execute(text("UPDATE users SET failed_login_count = 5, locked_until = NOW() + INTERVAL '1 hour' WHERE username='cliuser'"))
    assert cli.main(["reset-password", "--username", "cliuser"]) == 0
    assert cli.main(["reset-password", "--username", "yok"]) == 1
    with engine.connect() as c:
        u = c.execute(text("SELECT role, locked_until FROM users WHERE username='cliuser'")).one()
        assert u[0] == "operator" and u[1] is None
        assert c.execute(text("SELECT COUNT(*) FROM audit_logs WHERE actor='cli'")).scalar() == 2
    monkeypatch.setenv("TRENDHUB_NEW_PASSWORD", "kisa")
    assert cli.main(["create-user", "--username", "zayif"]) == 2


def test_cli_healthchecks(engine):
    assert cli.healthcheck("db") == 0
    assert cli.healthcheck("worker") == 1
    with engine.begin() as c:
        c.execute(text("INSERT INTO worker_heartbeats(worker_id, hostname) VALUES ('w', :h)"), {"h": socket.gethostname()})
    assert cli.healthcheck("worker") == 0
