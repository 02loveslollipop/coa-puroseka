"""AWS feature parity using a disposable local database, never the lab database."""

import ast
import hashlib
from datetime import timedelta
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest
from fastapi.testclient import TestClient

from app import auth, control
from app.config import settings
from app.database import connection
from app.demo import DEMO_USERNAME
from app.main import app
from app.query import execute_query


@pytest.fixture
def scratch(monkeypatch):
    url = os.getenv("LOCAL_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Requires disposable local PostgreSQL")
    parts = urlsplit(url)
    assert parts.hostname in {"localhost", "127.0.0.1"}
    database = "coa_aws_test_" + uuid4().hex[:10]
    scratch_url = urlunsplit(parts._replace(path="/" + database))
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    try:
        with psycopg.connect(scratch_url) as conn:
            conn.execute("CREATE SCHEMA control; CREATE SCHEMA data")
            setup = ast.parse(
                (
                    Path(__file__).resolve().parents[1] / "scripts/setup_access.py"
                ).read_text()
            )
            ddl = next(
                n.value
                for n in ast.walk(setup)
                if isinstance(n, ast.Constant)
                and isinstance(n.value, str)
                and "CREATE TABLE IF NOT EXISTS control.accounts (" in n.value
            )
            conn.execute(ddl)
            conn.execute(
                "CREATE TABLE data.events(id int,name text); INSERT INTO data.events VALUES(1,'Original')"
            )
            for username, role, limit, window in [
                ("aws-admin@example.test", "admin", 100000, 3600),
                (DEMO_USERNAME, "user", 100000, 21600),
            ]:
                conn.execute(
                    "INSERT INTO control.accounts(username,role,password_hash,token_limit,window_seconds,limit_enabled) VALUES(%s,%s,%s,%s,%s,%s)",
                    (
                        username,
                        role,
                        auth.password_hash(
                            "demo1234" if role == "user" else "Local-test-password-7"
                        ),
                        limit,
                        window,
                        role == "user",
                    ),
                )
                conn.execute(
                    "INSERT INTO control.budgets(username,used_tokens,resets_at) VALUES(%s,1234,now()+interval '6 hours')",
                    (username,),
                )
        monkeypatch.setitem(settings(), "DATABASE_URL", scratch_url)
        monkeypatch.setitem(settings(), "LOGIN_DATABASE_URL", scratch_url)
        monkeypatch.setitem(settings(), "CHAT_DATABASE_URL", scratch_url)
        monkeypatch.setitem(settings(), "AUTH_PROVIDER", "local")
        monkeypatch.setitem(settings(), "PUBLIC_ORIGIN", "http://testserver")
        yield scratch_url
    finally:
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                    sql.Identifier(database)
                )
            )


def login(username, password):
    client = TestClient(app)
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 200
    client.headers.update(
        {"Origin": "http://testserver", "X-CSRF-Token": response.json()["csrf"]}
    )
    return client


def test_admin_edits_demo_policy_without_resetting_usage(scratch):
    admin = login("aws-admin@example.test", "Local-test-password-7")
    before = admin.get("/api/admin/token-policy?account=demo").json()
    own = admin.get("/api/admin/token-policy").json()
    response = admin.put(
        "/api/admin/token-policy?account=demo",
        json={"enabled": True, "token_limit": 120000, "window_seconds": 43200},
    )
    assert response.status_code == 200
    after = response.json()
    assert after["token_limit"] == 120000 and after["window_seconds"] == 43200
    assert all(
        after[k] == before[k] for k in ("used_tokens", "reserved_tokens", "resets_at")
    )
    assert admin.get("/api/admin/token-policy").json() == own
    demo = login(DEMO_USERNAME, "demo1234")
    assert demo.get("/api/admin/token-policy?account=demo").status_code == 403
    assert (
        demo.put(
            "/api/admin/token-policy?account=demo",
            json={"enabled": False, "token_limit": 999999, "window_seconds": 60},
        ).status_code
        == 403
    )


def test_local_demo_works_with_cognito_admin_and_retains_no_prompt(
    scratch, monkeypatch
):
    monkeypatch.setitem(settings(), "AUTH_PROVIDER", "cognito")

    def forbidden(*args, **kwargs):
        pytest.fail("The demo must never contact Cognito")

    monkeypatch.setattr(auth.boto3, "client", forbidden)
    client = login(DEMO_USERNAME, "demo1234")
    assert client.get("/api/auth/me").json()["role"] == "user"
    before = control.usage(DEMO_USERNAME)
    job = control.create_job(
        DEMO_USERNAME, {"question": "private", "history": [{"content": "private"}]}
    )
    control.finish_job(
        job,
        {"answer": "Example", "_review": {"input": "private"}, "question": "private"},
    )
    with control.control_connection() as conn:
        row = conn.execute(
            "SELECT request,result FROM control.jobs WHERE id=%s", (job,)
        ).fetchone()
        assert row["request"] == {} and row["result"] == {"answer": "Example"}
        conn.execute(
            "UPDATE control.jobs SET created_at=%s WHERE id=%s",
            (control.utcnow() - timedelta(days=2), job),
        )
    assert control.get_job(DEMO_USERNAME, job) is None
    control.clean_demo_jobs()
    assert control.usage(DEMO_USERNAME) == before


def test_source_reads_only_and_control_still_writable(scratch):
    with connection() as conn:
        assert (
            conn.execute("SHOW default_transaction_read_only").fetchone()[
                "default_transaction_read_only"
            ]
            == "on"
        )
        assert (
            conn.execute("SHOW search_path").fetchone()["search_path"] == "pg_catalog"
        )
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        with connection() as conn:
            conn.execute("UPDATE data.events SET name='Poisoned'")
    assert execute_query("SELECT * FROM events", {"events"})["rows"] == [
        {"id": 1, "name": "Original"}
    ]
    reservation = control.reserve(DEMO_USERNAME, 100)
    control.settle(DEMO_USERNAME, reservation, 25)
    assert control.usage(DEMO_USERNAME)["used_tokens"] == 1259


def test_cognito_admin_authentication_does_not_rehash_local_password(
    scratch, monkeypatch
):
    monkeypatch.setitem(settings(), "AUTH_PROVIDER", "cognito")
    monkeypatch.setitem(settings(), "COGNITO_CLIENT_ID", "test-client")
    monkeypatch.setitem(settings(), "COGNITO_CLIENT_SECRET", "test-client-secret")
    with psycopg.connect(scratch) as conn:
        conn.execute(
            "UPDATE control.accounts SET password_hash=NULL WHERE username='aws-admin@example.test'"
        )

    class Cognito:
        def initiate_auth(self, **kwargs):
            return {"AuthenticationResult": {"AccessToken": "test-access-token"}}

        def get_user(self, **kwargs):
            return {"Username": "aws-admin@example.test"}

    monkeypatch.setattr(auth.boto3, "client", lambda *a, **k: Cognito())
    token, identity = auth.login(
        "aws-admin@example.test", "Cognito-only-password", "127.0.0.1"
    )
    assert token and identity["role"] == "admin"
    with psycopg.connect(scratch) as conn:
        assert (
            conn.execute(
                "SELECT password_hash FROM control.accounts WHERE username='aws-admin@example.test'"
            ).fetchone()[0]
            is None
        )


def test_operator_setup_preserves_policy_and_restricts_account_writes(
    scratch, monkeypatch
):
    from scripts import setup_access
    from psycopg.conninfo import make_conninfo

    roles = {
        service: "coa_aws_" + service + "_" + uuid4().hex[:10]
        for service in ("query", "login", "chat")
    }
    urls = {
        service: make_conninfo(
            scratch, user=role, password="Local-" + service + "-password-7"
        )
        for service, role in roles.items()
    }
    monkeypatch.setitem(settings(), "ADMIN_DATABASE_URL", scratch)
    monkeypatch.setitem(settings(), "DATABASE_URL", urls["query"])
    monkeypatch.setitem(settings(), "LOGIN_DATABASE_URL", urls["login"])
    monkeypatch.setitem(settings(), "CHAT_DATABASE_URL", urls["chat"])
    monkeypatch.setitem(settings(), "ADMIN_USERNAME", "aws-admin@example.test")
    monkeypatch.setitem(settings(), "ADMIN_PASSWORD", "Local-test-password-7")
    monkeypatch.delitem(settings(), "ADMIN_SECRET_ARN", raising=False)
    try:
        setup_access.run()
        control.update_limit(DEMO_USERNAME, True, 120000, 43200)
        before = control.usage(DEMO_USERNAME)
        setup_access.run()
        assert control.usage(DEMO_USERNAME) == before
        reservation = control.reserve(DEMO_USERNAME, 100)
        control.settle(DEMO_USERNAME, reservation, 25)
        for statement in (
            "UPDATE control.accounts SET role='admin' WHERE username='demo@02labs.me'",
            "UPDATE control.accounts SET enabled=false WHERE username='demo@02labs.me'",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with psycopg.connect(urls["chat"]) as conn:
                    conn.execute(statement)
        # A legacy login upgrades only the permitted hash column.
        salt = "01" * 16
        legacy = (
            salt
            + ":"
            + hashlib.scrypt(b"demo1234", salt=salt.encode(), n=16384, r=8, p=1).hex()
        )
        with psycopg.connect(scratch) as owner:
            owner.execute(
                "UPDATE control.accounts SET password_hash=%s WHERE username=%s",
                (legacy, DEMO_USERNAME),
            )
        before = control.usage(DEMO_USERNAME)
        login(DEMO_USERNAME, "demo1234")
        assert control.usage(DEMO_USERNAME) == before
        with psycopg.connect(urls["login"]) as conn:
            current = conn.execute(
                "SELECT password_hash FROM control.accounts WHERE username=%s",
                (DEMO_USERNAME,),
            ).fetchone()[0]
        assert current.startswith("$argon2id$") and auth.password_matches(
            "demo1234", current
        )
    finally:
        with psycopg.connect(scratch) as owner:
            for role in roles.values():
                if owner.execute(
                    "SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)
                ).fetchone():
                    owner.execute(
                        sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role))
                    )
                    owner.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
