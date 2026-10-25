"""Independent AWS database credentials and SQL-injection containment."""

from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo, conninfo_to_dict
import pytest

from app import control
from app.config import settings
from app.demo import DEMO_USERNAME
from scripts.database_roles import KEYS, configure_runtime_roles, validate_credentials
from scripts.provision_access import runtime_databases
from tests.test_aws_parity import scratch as _aws_scratch

scratch = _aws_scratch


@pytest.fixture
def runtime_roles(scratch, monkeypatch):
    cfg = {"ADMIN_DATABASE_URL": scratch}
    roles = {}
    for service, key in KEYS.items():
        roles[service] = "coa_scope_" + service + "_" + uuid4().hex[:10]
        cfg[key] = make_conninfo(scratch, user=roles[service], password=uuid4().hex)
    try:
        with psycopg.connect(scratch) as owner:
            configure_runtime_roles(owner, cfg)
        for key in KEYS.values():
            monkeypatch.setitem(settings(), key, cfg[key])
        yield cfg
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


def test_authentication_and_chat_work_with_independent_logins(runtime_roles):
    from tests.test_aws_parity import login

    before = control.usage(DEMO_USERNAME)
    client = login(DEMO_USERNAME, "demo1234")
    assert client.get("/api/auth/me").json()["role"] == "user"
    reservation = control.reserve(DEMO_USERNAME, 100)
    control.settle(DEMO_USERNAME, reservation, 25)
    assert control.usage(DEMO_USERNAME)["used_tokens"] == before["used_tokens"] + 25
    control.update_limit(DEMO_USERNAME, True, 120000, 21600)
    job = control.create_job(DEMO_USERNAME, {"question": "private"})
    control.update_job_activity(job, [])
    control.finish_job(job, {"answer": "local test"})
    assert control.get_job(DEMO_USERNAME, job)["state"] == "done"
    control.interrupt_jobs()
    control.clean_demo_jobs()
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").status_code == 401


@pytest.mark.parametrize(
    "service,statement",
    [
        ("query", "DELETE FROM data.events"),
        ("query", "UPDATE data.events SET name='Poisoned'"),
        ("query", "INSERT INTO data.events VALUES(2,'Poisoned')"),
        ("query", "TRUNCATE data.events"),
        ("query", "CREATE TABLE data.poisoned(id integer)"),
        ("query", "ALTER TABLE data.events ADD COLUMN poisoned text"),
        ("query", "DROP TABLE data.events"),
        ("query", "SELECT * FROM control.accounts"),
        ("query", "SELECT * FROM control.sessions"),
        ("query", "SELECT * FROM data.events FOR UPDATE"),
        ("login", "SELECT * FROM data.events"),
        ("login", "SELECT * FROM control.jobs"),
        ("login", "UPDATE control.accounts SET role='admin'"),
        ("login", "UPDATE control.accounts SET enabled=false"),
        ("login", "UPDATE control.accounts SET token_limit=999999"),
        ("chat", "SELECT * FROM data.events"),
        ("chat", "SELECT password_hash FROM control.accounts"),
        ("chat", "SELECT * FROM control.sessions"),
        ("chat", "UPDATE control.accounts SET role='admin'"),
        ("chat", "UPDATE control.accounts SET enabled=false"),
        ("chat", "UPDATE control.accounts SET password_hash='poisoned'"),
        ("chat", "UPDATE control.reservations SET amount=0"),
        ("chat", "UPDATE control.jobs SET username='other'"),
    ],
)
def test_acl_denies_hostile_sql_even_with_read_only_disabled(
    runtime_roles, service, statement
):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with psycopg.connect(runtime_roles[KEYS[service]]) as conn:
            conn.execute("SET TRANSACTION READ WRITE")
            conn.execute(statement)


def test_cannot_switch_between_service_roles_or_create_roles(runtime_roles):
    for service, key in KEYS.items():
        with psycopg.connect(runtime_roles[key]) as conn:
            flags = conn.execute(
                "SELECT rolsuper,rolcreatedb,rolcreaterole,rolbypassrls,rolreplication,rolinherit FROM pg_roles WHERE rolname=current_user"
            ).fetchone()
            assert not any(flags)
        for other, other_key in KEYS.items():
            if other == service:
                continue
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with psycopg.connect(runtime_roles[key]) as conn:
                    conn.execute(
                        sql.SQL("SET ROLE {}").format(
                            sql.Identifier(
                                conninfo_to_dict(runtime_roles[other_key])["user"]
                            )
                        )
                    )


def test_runtime_secrets_are_distinct_and_reused():
    app = {
        "DATABASE_URL": "postgresql://puroseka_reader:reader-private@db.example.test:5432/puroseka?sslmode=require"
    }
    runtime_databases(app, "db.example.test")
    validate_credentials(app)
    before = dict(app)
    runtime_databases(app, "db.example.test")
    assert app == before
    shared = dict(app, LOGIN_DATABASE_URL=app["CHAT_DATABASE_URL"])
    with pytest.raises(ValueError):
        validate_credentials(shared)
    with pytest.raises(ValueError):
        validate_credentials({"CONTROL_DATABASE_URL": app["CHAT_DATABASE_URL"]})


def test_operator_helper_is_included_in_image_and_image_digest():
    root = Path(__file__).resolve().parents[1]
    assert (
        "COPY scripts/database_roles.py scripts/database_roles.py"
        in (root / "Dockerfile").read_text()
    )
    assert '"scripts/database_roles.py"' in (root / "scripts/aws.py").read_text()


def test_runtime_never_falls_back_to_shared_control_or_admin_credentials(monkeypatch):
    for key in ("LOGIN_DATABASE_URL", "CHAT_DATABASE_URL"):
        monkeypatch.delitem(settings(), key, raising=False)
    monkeypatch.setitem(settings(), "CONTROL_DATABASE_URL", "unused-shared-credential")
    monkeypatch.setitem(settings(), "ADMIN_DATABASE_URL", "unused-operator-credential")
    from app.auth_database import login_connection

    for connect in (login_connection, control.control_connection):
        with pytest.raises(KeyError):
            with connect():
                pytest.fail(
                    "Missing service-specific credentials must stop before connecting"
                )


def test_reprovision_removes_stale_column_grants_and_future_tables_stay_private(
    runtime_roles,
):
    cfg = runtime_roles
    chat = conninfo_to_dict(cfg["CHAT_DATABASE_URL"])["user"]
    with psycopg.connect(cfg["ADMIN_DATABASE_URL"]) as owner:
        owner.execute(
            sql.SQL("GRANT UPDATE(enabled) ON control.accounts TO {}").format(
                sql.Identifier(chat)
            )
        )
        owner.execute("CREATE TABLE data.future_private(id int)")
        configure_runtime_roles(owner, cfg)
    for key, query in [
        ("CHAT_DATABASE_URL", "UPDATE control.accounts SET enabled=false"),
        ("DATABASE_URL", "SELECT * FROM data.future_private"),
    ]:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with psycopg.connect(cfg[key]) as conn:
                conn.execute("SET TRANSACTION READ WRITE")
                conn.execute(query)


def test_upgrade_retires_only_legacy_application_schema_grants(runtime_roles):
    cfg = runtime_roles
    role = "puroseka_control"
    with psycopg.connect(cfg["ADMIN_DATABASE_URL"]) as owner:
        created = not owner.execute(
            "SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)
        ).fetchone()
        if created:
            owner.execute(sql.SQL("CREATE ROLE {}").format(sql.Identifier(role)))
        owner.execute(
            sql.SQL("GRANT USAGE ON SCHEMA control TO {}").format(sql.Identifier(role))
        )
        owner.execute(
            sql.SQL("GRANT UPDATE(password_hash) ON control.accounts TO {}").format(
                sql.Identifier(role)
            )
        )
    try:
        with psycopg.connect(cfg["ADMIN_DATABASE_URL"]) as owner:
            configure_runtime_roles(owner, cfg)
            assert not owner.execute(
                "SELECT has_schema_privilege(%s,'control','USAGE')", (role,)
            ).fetchone()[0]
            assert not owner.execute(
                "SELECT has_column_privilege(%s,'control.accounts','password_hash','UPDATE')",
                (role,),
            ).fetchone()[0]
    finally:
        if created:
            with psycopg.connect(cfg["ADMIN_DATABASE_URL"]) as owner:
                owner.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
                owner.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_deploy_requires_access_migration_before_building_or_changing_resources(
    monkeypatch,
):
    from types import SimpleNamespace
    from scripts import aws

    monkeypatch.setattr(aws, "state", lambda: {"stack": "test"})
    monkeypatch.setattr(aws, "wait_stack", lambda *a, **k: None)
    monkeypatch.setattr(aws, "outputs", lambda *a: {"AppSecretArn": "test-secret"})
    monkeypatch.setattr(
        aws,
        "client",
        lambda name: SimpleNamespace(
            get_secret_value=lambda **k: {"SecretString": "{}"}
        )
        if name == "secretsmanager"
        else pytest.fail("No resource mutations expected"),
    )
    monkeypatch.setattr(
        aws.subprocess, "run", lambda *a, **k: pytest.fail("No image build expected")
    )
    with pytest.raises(RuntimeError, match="configure-access"):
        aws.deploy(app_only=True)
