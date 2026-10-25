"""Operator-only provisioning of independent query, login and chat logins."""

from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

KEYS = {
    "query": "DATABASE_URL",
    "login": "LOGIN_DATABASE_URL",
    "chat": "CHAT_DATABASE_URL",
}
SOURCE_TABLES = (
    "cards",
    "events",
    "gachas",
    "characters",
    "event_cards",
    "entity_labels",
    "event_rewards",
    "gacha_details",
    "gacha_pickups",
    "card_parameters",
    "gacha_behaviors",
    "gacha_rarity_rates",
    "card_training_costs",
    "event_reward_ranges",
    "ranking_observations",
    "card_master_resources",
)
POLICIES = {
    "login": {
        "accounts": {
            "SELECT": ("username", "role", "password_hash", "enabled"),
            "UPDATE": ("password_hash",),
        },
        "sessions": {
            "SELECT": None,
            "INSERT": ("token_hash", "username", "csrf", "expires_at"),
            "DELETE": None,
        },
        "login_attempts": {
            "SELECT": None,
            "INSERT": ("key", "attempts", "resets_at"),
            "UPDATE": ("attempts", "resets_at"),
        },
    },
    "chat": {
        "accounts": {
            "SELECT": (
                "username",
                "enabled",
                "limit_enabled",
                "token_limit",
                "window_seconds",
            ),
            "UPDATE": ("limit_enabled", "token_limit", "window_seconds"),
        },
        "budgets": {
            "SELECT": None,
            "UPDATE": ("used_tokens", "reserved_tokens", "started_at", "resets_at"),
        },
        "reservations": {
            "SELECT": None,
            "INSERT": ("id", "username", "amount"),
            "UPDATE": ("state", "charged_tokens"),
            "DELETE": None,
        },
        "jobs": {
            "SELECT": None,
            "INSERT": ("id", "username", "request"),
            "UPDATE": ("state", "request", "result"),
            "DELETE": None,
        },
    },
}


def target(params):
    return params.get("host"), params.get("port", "5432"), params.get("dbname")


def validate_credentials(cfg):
    parsed = {
        service: conninfo_to_dict(cfg.get(key) or "") for service, key in KEYS.items()
    }
    users = [p.get("user") for p in parsed.values()]
    passwords = [p.get("password") for p in parsed.values()]
    if (
        not all(users)
        or not all(passwords)
        or len(set(users)) != 3
        or len(set(passwords)) != 3
    ):
        raise ValueError(
            "Three independent database usernames and passwords are required"
        )
    if any(user in {"postgres", "rdsadmin"} for user in users):
        raise ValueError("Runtime connections cannot use database administrators")
    if len({target(p) for p in parsed.values()}) != 1:
        raise ValueError(
            "Runtime connections must target the same application database"
        )
    for params in parsed.values():
        if (
            params.get("host") not in {"localhost", "127.0.0.1"}
            and params.get("sslmode") != "require"
        ):
            raise ValueError("Remote database connections must require TLS")
    if cfg.get("ADMIN_DATABASE_URL"):
        admin = conninfo_to_dict(cfg["ADMIN_DATABASE_URL"])
        if (
            target(admin) != target(parsed["query"])
            or admin.get("user") in users
            or admin.get("password") in passwords
        ):
            raise ValueError(
                "Operator credentials must be separate and target the same database"
            )
    return parsed


def configure_role(conn, params):
    role = params["user"]
    existing = conn.execute(
        "SELECT oid,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls,"
        "shobj_description(oid,'pg_authid') FROM pg_roles WHERE rolname=%s",
        (role,),
    ).fetchone()
    if existing:
        if any(existing[1:6]) or existing[6] not in {"COA AWS runtime role", None}:
            raise ValueError(
                "Runtime role has unexpected attributes or ownership marker"
            )
        if existing[6] is None and role != "puroseka_reader":
            raise ValueError("Refusing an unmanaged existing role")
        if conn.execute(
            "SELECT 1 FROM pg_auth_members WHERE member=%s LIMIT 1", (existing[0],)
        ).fetchone():
            raise ValueError("Runtime role must have no role memberships")
        if conn.execute(
            "SELECT 1 FROM pg_shdepend WHERE refclassid='pg_authid'::regclass AND refobjid=%s AND deptype='o' LIMIT 1",
            (existing[0],),
        ).fetchone():
            raise ValueError("Runtime role must not own objects")
    # SCRAM verifier is computed locally; never put plaintext passwords in SQL.
    encrypted = conn.pgconn.encrypt_password(
        params["password"].encode(), role.encode(), b"scram-sha-256"
    ).decode()
    conn.execute(
        sql.SQL(
            "{} ROLE {} LOGIN NOCREATEDB NOCREATEROLE NOINHERIT CONNECTION LIMIT 8 PASSWORD {}"
        ).format(
            sql.SQL("ALTER" if existing else "CREATE"),
            sql.Identifier(role),
            sql.Literal(encrypted),
        )
    )
    conn.execute(
        sql.SQL("COMMENT ON ROLE {} IS 'COA AWS runtime role'").format(
            sql.Identifier(role)
        )
    )
    conn.execute(sql.SQL("ALTER ROLE {} RESET ALL").format(sql.Identifier(role)))
    for key, value in {
        "search_path": "pg_catalog",
        "statement_timeout": "15s",
        "lock_timeout": "1s",
        "idle_in_transaction_session_timeout": "30s",
    }.items():
        conn.execute(
            sql.SQL("ALTER ROLE {} SET {} TO {}").format(
                sql.Identifier(role), sql.Identifier(key), sql.Literal(value)
            )
        )
    return role


def revoke_schema(conn, schema, role):
    if not conn.execute(
        "SELECT 1 FROM pg_namespace WHERE nspname=%s", (schema,)
    ).fetchone():
        return
    for kind in ("TABLES", "SEQUENCES", "FUNCTIONS"):
        conn.execute(
            sql.SQL("REVOKE ALL ON ALL {} IN SCHEMA {} FROM {}").format(
                sql.SQL(kind), sql.Identifier(schema), sql.Identifier(role)
            )
        )
    columns = {}
    for table, column in conn.execute(
        "SELECT table_name,column_name FROM information_schema.columns WHERE table_schema=%s",
        (schema,),
    ).fetchall():
        columns.setdefault(table, []).append(column)
    for table, names in columns.items():
        conn.execute(
            sql.SQL("REVOKE ALL ({}) ON {}.{} FROM {}").format(
                sql.SQL(",").join(map(sql.Identifier, names)),
                sql.Identifier(schema),
                sql.Identifier(table),
                sql.Identifier(role),
            )
        )
    conn.execute(
        sql.SQL("REVOKE ALL ON SCHEMA {} FROM {}").format(
            sql.Identifier(schema), sql.Identifier(role)
        )
    )


def grant(conn, schema, table, role, permissions):
    for verb, columns in permissions.items():
        permission = sql.SQL(verb)
        if columns:
            permission += sql.SQL(" ({})").format(
                sql.SQL(",").join(map(sql.Identifier, columns))
            )
        conn.execute(
            sql.SQL("GRANT {} ON {}.{} TO {}").format(
                permission,
                sql.Identifier(schema),
                sql.Identifier(table),
                sql.Identifier(role),
            )
        )


def configure_reader(conn, url):
    role = configure_role(conn, conninfo_to_dict(url))
    for schema in ("data", "control"):
        revoke_schema(conn, schema, role)
    conn.execute(
        sql.SQL("GRANT USAGE ON SCHEMA data TO {}").format(sql.Identifier(role))
    )
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='data'"
        ).fetchall()
    }
    for table in SOURCE_TABLES:
        if table in tables:
            grant(conn, "data", table, role, {"SELECT": None})
    conn.execute(
        sql.SQL("ALTER ROLE {} SET default_transaction_read_only=on").format(
            sql.Identifier(role)
        )
    )


def configure_runtime_roles(conn, cfg):
    parsed = validate_credentials(cfg)
    # PUBLIC grants cannot be removed per user. Protect only our two schemas.
    for schema in ("data", "control"):
        if conn.execute(
            "SELECT 1 FROM pg_namespace WHERE nspname=%s", (schema,)
        ).fetchone():
            conn.execute(
                sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(
                    sql.Identifier(schema)
                )
            )
            conn.execute(
                sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM PUBLIC").format(
                    sql.Identifier(schema)
                )
            )
    configure_reader(conn, cfg["DATABASE_URL"])
    for service, policy in POLICIES.items():
        role = configure_role(conn, parsed[service])
        for schema in ("data", "control"):
            revoke_schema(conn, schema, role)
        conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA control TO {}").format(sql.Identifier(role))
        )
        for table, permissions in policy.items():
            grant(conn, "control", table, role, permissions)
    # Retire only the known previous application role's application-schema grants.
    legacy = "puroseka_control"
    if (
        legacy not in {p["user"] for p in parsed.values()}
        and conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname=%s", (legacy,)
        ).fetchone()
    ):
        for schema in ("data", "control"):
            revoke_schema(conn, schema, legacy)
