import os
import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE"),
    reason="Set TEST_DATABASE=1 and DATABASE_URL for imported PostgreSQL",
)
from app.database import connection, physical_schema
from app.query import execute_query
from app.inference import infer
from fastapi.testclient import TestClient
from app.main import app


def test_counts_roundtrip_and_permissions():
    with connection() as c:
        assert c.execute("SELECT count(*) AS n FROM data.cards").fetchone()["n"] == 1229
        r = c.execute("SELECT name,raw_record FROM data.events WHERE id=1").fetchone()
        assert r["name"] == "雨上がりの一番星" and r["raw_record"]["name"] == r["name"]
        assert (
            c.execute(
                "SELECT has_table_privilege(current_user,'data.events','INSERT') AS allowed"
            ).fetchone()["allowed"]
            is False
        )


def test_ground_truth():
    r = execute_query(
        "SELECT DISTINCT c.character_id FROM cards c JOIN event_cards ec ON c.id=ec.card_id WHERE ec.event_id=1",
        set(physical_schema()),
    )
    assert sorted(x["character_id"] for x in r["rows"]) == [1, 2, 3, 4, 24]
    r = execute_query(
        "SELECT to_char(to_timestamp(aggregate_at / 1000.0) AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS') AS end FROM events WHERE id=1",
        set(physical_schema()),
    )
    assert r["rows"][0]["end"] == "2020-10-18 11:59:59"


def test_api_inspection():
    from app.auth import admin_user

    app.dependency_overrides[admin_user] = lambda: {
        "username": "test-admin",
        "role": "admin",
    }
    client = TestClient(app)
    assert client.get("/api/health").status_code == 200
    assert (
        client.get("/api/tables/events").json()["rows"][0]["name"] == "雨上がりの一番星"
    )
    assert client.get("/api/tables/not_a_table").status_code == 404
    assert client.get("/api/tables/events?offset=-1").status_code == 400
    assert client.get("/api/ontology.ttl").status_code == 200
    assert (
        client.post(
            "/api/inference", json={"question": "x", "mode": "administrator"}
        ).status_code
        == 422
    )
    app.dependency_overrides.pop(admin_user)


def test_context_isolation(monkeypatch):
    import asyncio

    async def mock_complete(messages, json_mode=False):
        if json_mode:
            return '{"sql":"SELECT count(*) AS n FROM cards","reason":"Count cards"}', {
                "total_tokens": 10
            }
        return "There are 1,229 cards (data.cards).", {"total_tokens": 5}

    monkeypatch.setattr("app.inference.complete", mock_complete)
    base = asyncio.run(infer("How many cards?", "base", []))
    enriched = asyncio.run(infer("How many cards?", "enriched", []))
    assert base["rows"] == enriched["rows"] == [{"n": 1229}]
    assert "semantics" not in base["context"] and "semantics" in enriched["context"]
    assert base["usage"]["total_tokens"] == 15
    assert base["status"] == enriched["status"] == "ok"


def test_model_failure_no_fabrication(monkeypatch):
    import asyncio
    from app.llm import ModelError

    async def fail(*args, **kwargs):
        raise ModelError("Quota exhausted")

    monkeypatch.setattr("app.inference.complete", fail)
    r = asyncio.run(infer("Count cards", "base", []))
    assert (
        r["status"] == "error" and r["rows"] == [] and r["error"] == "Quota exhausted"
    )


def test_full_rankings_and_few_shots():
    from app.config import artifact

    with connection() as c:
        if not c.execute(
            "SELECT to_regclass('data.ranking_observations') AS t"
        ).fetchone()["t"]:
            pytest.skip("Rankings not imported")
        assert (
            c.execute("SELECT count(*) AS n FROM data.ranking_observations").fetchone()[
                "n"
            ]
            == artifact("ranking_manifest.json")["row_count"]
        )
        assert (
            c.execute(
                "SELECT count(DISTINCT event_id) AS n FROM data.ranking_observations"
            ).fetchone()["n"]
            == artifact("ranking_manifest.json")["events"]
        )
    schema = set(physical_schema())
    for example in artifact("few_shots.json"):
        result = execute_query(example["sql"], schema)
        assert result["rows"] and not result["truncated"]
    # A gap-aware daily difference is NULL for the first observed daily endpoint.
    daily = execute_query(artifact("few_shots.json")[1]["sql"], schema)["rows"]
    assert daily[0]["score_change_since_previous_observed_day"] is None
    assert daily[0]["elapsed_calendar_days"] is None


def test_repeat_import_labels():
    from app.config import artifact

    with connection() as c:
        assert c.execute("SELECT count(*) AS n FROM data.entity_labels").fetchone()[
            "n"
        ] == len(artifact("translations.json")["labels"])
