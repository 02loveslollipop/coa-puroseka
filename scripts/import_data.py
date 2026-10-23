"""Run once as an ECS task. Replaces the data schema atomically on repeat import."""

import gzip
import json
import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb
from app.config import ARTIFACTS, settings


def run():
    cfg = settings()
    admin = cfg.get("ADMIN_DATABASE_URL")
    if cfg.get("ADMIN_SECRET_ARN"):
        import boto3

        admin = json.loads(
            boto3.client(
                "secretsmanager", region_name=cfg.get("AWS_REGION", "us-east-1")
            ).get_secret_value(SecretId=cfg["ADMIN_SECRET_ARN"])["SecretString"]
        )["ADMIN_DATABASE_URL"]
    if not admin:
        raise RuntimeError("ADMIN_DATABASE_URL or ADMIN_SECRET_ARN required")
    import urllib.parse

    parsed = urllib.parse.urlparse(cfg["DATABASE_URL"])
    reader = parsed.username
    password = urllib.parse.unquote(parsed.password)
    with gzip.open(ARTIFACTS / "dataset.json.gz", "rt") as f:
        payload = json.load(f)
    labels = (
        json.loads((ARTIFACTS / "translations.json").read_text()).get("labels", [])
        if (ARTIFACTS / "translations.json").exists()
        else []
    )
    payload["schema"]["entity_labels"] = {
        "row_key": "text",
        "entity_table": "text",
        "entity_id": "bigint",
        "original": "text",
        "english": "text",
        "provenance": "text",
        "raw_record": "jsonb",
    }
    payload["tables"]["entity_labels"] = [
        {
            "row_key": f"{x['table']}/{x['id']}",
            "entity_table": x["table"],
            "entity_id": x["id"],
            "original": x["original"],
            "english": x.get("english"),
            "provenance": x.get("provenance", "generated_mistral"),
            "raw_record": x,
        }
        for x in labels
    ]
    with psycopg.connect(admin) as conn:
        conn.execute("DROP SCHEMA IF EXISTS data CASCADE")
        conn.execute("CREATE SCHEMA data")
        for table, columns in payload["schema"].items():
            parts = [
                sql.SQL("{} {}").format(sql.Identifier(k), sql.SQL(typ))
                for k, typ in columns.items()
            ]
            parts.append(sql.SQL("PRIMARY KEY (row_key)"))
            if table in ("characters", "cards", "events", "event_cards", "gachas"):
                parts.append(sql.SQL("UNIQUE (id)"))
            conn.execute(
                sql.SQL("CREATE TABLE data.{} ({})").format(
                    sql.Identifier(table), sql.SQL(",").join(parts)
                )
            )
            with conn.cursor().copy(
                sql.SQL("COPY data.{} ({}) FROM STDIN").format(
                    sql.Identifier(table),
                    sql.SQL(",").join(map(sql.Identifier, columns)),
                )
            ) as copy:
                for row in payload["tables"][table]:
                    copy.write_row(
                        [
                            Jsonb(row.get(k))
                            if columns[k] == "jsonb" and row.get(k) is not None
                            else row.get(k)
                            for k in columns
                        ]
                    )
            for key in (
                "id",
                "parent_id",
                "card_id",
                "event_id",
                "character_id",
                "entity_id",
            ):
                if key in columns:
                    conn.execute(
                        sql.SQL("CREATE INDEX ON data.{} ({})").format(
                            sql.Identifier(table), sql.Identifier(key)
                        )
                    )
            print(table, len(payload["tables"][table]), flush=True)
        rankings = ARTIFACTS / "rankings.jsonl.gz"
        if rankings.exists():
            columns = {
                "row_key": "text",
                "event_id": "bigint",
                "rank": "bigint",
                "score": "bigint",
                "observed_at": "timestamptz",
                "user_id": "text",
                "leader_card_id": "bigint",
                "source_file": "text",
                "raw_record": "jsonb",
            }
            conn.execute(
                sql.SQL(
                    "CREATE TABLE data.ranking_observations ({}, PRIMARY KEY (row_key))"
                ).format(
                    sql.SQL(",").join(
                        sql.SQL("{} {}").format(sql.Identifier(k), sql.SQL(t))
                        for k, t in columns.items()
                    )
                )
            )
            count = 0
            with conn.cursor().copy(
                sql.SQL("COPY data.ranking_observations ({}) FROM STDIN").format(
                    sql.SQL(",").join(map(sql.Identifier, columns))
                )
            ) as copy:
                with gzip.open(rankings, "rt", encoding="utf-8") as source:
                    for line in source:
                        row = json.loads(line)
                        copy.write_row(
                            [
                                Jsonb(row[k]) if k == "raw_record" else row.get(k)
                                for k in columns
                            ]
                        )
                        count += 1
                        if count % 100000 == 0:
                            print("Ranking observations imported:", count, flush=True)
            expected = json.loads((ARTIFACTS / "ranking_manifest.json").read_text())[
                "row_count"
            ]
            if count != expected:
                raise RuntimeError("Ranking row count mismatch")
            conn.execute(
                "CREATE INDEX ON data.ranking_observations (event_id, rank, observed_at DESC) INCLUDE (score)"
            )
            conn.execute(
                "CREATE INDEX ON data.ranking_observations (rank, event_id, observed_at DESC) INCLUDE (score)"
            )
            print("ranking_observations", count, flush=True)
        catalog = json.loads((ARTIFACTS / "catalog.json").read_text())
        for r in catalog["relationships"]:
            if r["status"] == "validated":
                conn.execute(
                    sql.SQL(
                        "ALTER TABLE data.{} ADD FOREIGN KEY ({}) REFERENCES data.{} ({})"
                    ).format(
                        *map(
                            sql.Identifier,
                            [r["source"], r["column"], r["target"], r["target_column"]],
                        )
                    )
                )
        if not conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname=%s", (reader,)
        ).fetchone():
            conn.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(reader), sql.Literal(password)
                )
            )
        else:
            conn.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    sql.Identifier(reader), sql.Literal(password)
                )
            )
        conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA data TO {}").format(sql.Identifier(reader))
        )
        conn.execute(
            sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA data TO {}").format(
                sql.Identifier(reader)
            )
        )
        conn.execute(
            sql.SQL("ALTER ROLE {} SET statement_timeout = '15s'").format(
                sql.Identifier(reader)
            )
        )
        conn.execute("ANALYZE")
    if (ARTIFACTS / "rankings.jsonl.gz").exists():
        with psycopg.connect(admin, autocommit=True) as maintenance:
            maintenance.execute("SET max_parallel_maintenance_workers = 0")
            maintenance.execute("VACUUM ANALYZE data.ranking_observations")
    print("Import committed; reader has SELECT-only access to data.", flush=True)


if __name__ == "__main__":
    run()
