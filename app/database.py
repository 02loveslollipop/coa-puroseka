from contextlib import contextmanager
from psycopg import connect, sql
from psycopg.rows import dict_row
from app.config import settings, artifact


def schema_name():
    return "data"


@contextmanager
def connection():
    with connect(
        settings()["DATABASE_URL"],
        connect_timeout=10,
        row_factory=dict_row,
        options="-c default_transaction_read_only=on -c search_path=pg_catalog",
    ) as conn:
        yield conn


def physical_schema():
    # Introspection uses the actual DB, not semantic labels or translations.
    with connection() as conn:
        rows = conn.execute(
            "SELECT table_name,column_name,data_type FROM information_schema.columns WHERE table_schema='data' ORDER BY table_name,ordinal_position"
        ).fetchall()
    result = {}
    for r in rows:
        result.setdefault(r["table_name"], {})[r["column_name"]] = r["data_type"]
    return result


def preview(table, offset=0):
    if table not in artifact("catalog.json")["tables"]:
        raise ValueError("Unknown table")
    with connection() as conn:
        columns = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='data' AND table_name=%s AND column_name!='raw_record' ORDER BY ordinal_position",
            (table,),
        ).fetchall()
        cols = [r["column_name"] for r in columns]
        rows = conn.execute(
            sql.SQL(
                "SELECT {} FROM data.{} ORDER BY row_key LIMIT 25 OFFSET %s"
            ).format(
                sql.SQL(",").join(map(sql.Identifier, cols)), sql.Identifier(table)
            ),
            (offset,),
        ).fetchall()
        return {"columns": cols, "rows": rows, "offset": offset, "limit": 25}
