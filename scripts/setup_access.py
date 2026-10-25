"""Idempotent control-schema migration; never alters the source data schema."""

import json

import psycopg

from app.auth import password_hash
from app.config import settings
from app.demo import DEMO_USERNAME
from scripts.database_roles import configure_runtime_roles, validate_credentials


def run():
    cfg = dict(settings())
    admin = cfg.get("ADMIN_DATABASE_URL")
    if cfg.get("ADMIN_SECRET_ARN"):
        import boto3

        cfg.update(
            json.loads(
                boto3.client(
                    "secretsmanager", region_name="us-east-1"
                ).get_secret_value(SecretId=cfg["ADMIN_SECRET_ARN"])["SecretString"]
            )
        )
        admin = cfg["ADMIN_DATABASE_URL"]
    if not admin:
        raise RuntimeError("Operator database connection is required")
    validate_credentials(cfg)
    username = (
        (cfg.get("ADMIN_USERNAME") or cfg.get("CHAT_USERNAME", "")).strip().lower()
    )
    password = cfg.get("ADMIN_PASSWORD") or cfg.get("CHAT_PASSWORD")
    if not username or not password:
        raise RuntimeError("Set the shared account username and password in .env")
    if username == DEMO_USERNAME:
        raise RuntimeError("The public demo cannot be the private administrator")
    if len(password) < 12:
        raise RuntimeError(
            "The shared account password must contain at least 12 characters"
        )
    with psycopg.connect(admin) as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS control")
        conn.execute("REVOKE ALL ON SCHEMA control FROM PUBLIC")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS control.accounts (
                username text PRIMARY KEY, role text NOT NULL CHECK(role IN ('admin','user')),
                password_hash text, enabled boolean NOT NULL DEFAULT true,
                limit_enabled boolean NOT NULL DEFAULT false,
                token_limit bigint NOT NULL CHECK(token_limit > 0),
                window_seconds integer NOT NULL CHECK(window_seconds BETWEEN 60 AND 2592000)
            );
            CREATE TABLE IF NOT EXISTS control.budgets (
                username text PRIMARY KEY REFERENCES control.accounts(username),
                used_tokens bigint NOT NULL DEFAULT 0 CHECK(used_tokens >= 0),
                reserved_tokens bigint NOT NULL DEFAULT 0 CHECK(reserved_tokens >= 0),
                started_at timestamptz NOT NULL DEFAULT now(), resets_at timestamptz NOT NULL
            );
            CREATE TABLE IF NOT EXISTS control.reservations (
                id uuid PRIMARY KEY, username text NOT NULL REFERENCES control.accounts(username),
                amount bigint NOT NULL CHECK(amount >= 0), state text NOT NULL DEFAULT 'active',
                charged_tokens bigint, created_at timestamptz NOT NULL DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS reservations_active ON control.reservations(username,created_at) WHERE state='active';
            CREATE TABLE IF NOT EXISTS control.sessions (
                token_hash text PRIMARY KEY, username text NOT NULL REFERENCES control.accounts(username),
                csrf text NOT NULL, expires_at timestamptz NOT NULL
            );
            CREATE TABLE IF NOT EXISTS control.login_attempts (
                key text PRIMARY KEY, attempts integer NOT NULL, resets_at timestamptz NOT NULL
            );
            CREATE TABLE IF NOT EXISTS control.jobs (
                id uuid PRIMARY KEY, username text NOT NULL REFERENCES control.accounts(username),
                state text NOT NULL DEFAULT 'running', request jsonb NOT NULL, result jsonb,
                created_at timestamptz NOT NULL DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS jobs_owner ON control.jobs(username,created_at);
        """)
        encoded = (
            password_hash(password)
            if cfg.get("AUTH_PROVIDER", "local") == "local"
            else None
        )
        demo = conn.execute(
            "SELECT role FROM control.accounts WHERE username=%s FOR UPDATE",
            (DEMO_USERNAME,),
        ).fetchone()
        if demo and demo[0] != "user":
            raise RuntimeError("Refusing to alter a privileged demo account")
        conn.execute(
            "INSERT INTO control.accounts(username,role,password_hash,limit_enabled,token_limit,window_seconds) "
            "VALUES (%s,'admin',%s,false,%s,%s) ON CONFLICT(username) DO UPDATE SET role='admin',password_hash=EXCLUDED.password_hash",
            (
                username,
                encoded,
                int(cfg.get("TOKEN_LIMIT", "100000")),
                int(cfg.get("TOKEN_WINDOW_SECONDS", "3600")),
            ),
        )
        conn.execute(
            "INSERT INTO control.budgets(username,resets_at) VALUES (%s,now()+interval '1 hour') ON CONFLICT DO NOTHING",
            (username,),
        )
        conn.execute(
            "INSERT INTO control.accounts(username,role,password_hash,limit_enabled,token_limit,window_seconds) "
            "VALUES (%s,'user',%s,true,%s,%s) ON CONFLICT(username) DO NOTHING",
            (
                DEMO_USERNAME,
                password_hash("demo1234"),
                int(cfg.get("DEMO_TOKEN_LIMIT", "100000")),
                int(cfg.get("DEMO_WINDOW_SECONDS", "21600")),
            ),
        )
        conn.execute(
            "INSERT INTO control.budgets(username,resets_at) SELECT username,now()+window_seconds*interval '1 second' FROM control.accounts WHERE username=%s ON CONFLICT DO NOTHING",
            (DEMO_USERNAME,),
        )
        configure_runtime_roles(conn, cfg)
    print(
        "Control schema ready; shared administrator account seeded; existing usage and policy preserved.",
        flush=True,
    )


if __name__ == "__main__":
    run()
