"""Authentication boundaries and durable token accounting against PostgreSQL."""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app import auth, control
from app.config import settings
from app.main import app
from app.metering import principal

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE"), reason="Needs local PostgreSQL control schema"
)
PASSWORD = "Local-test-password-7"
USERNAME = "access-test@example.test"


@pytest.fixture(autouse=True)
def state(monkeypatch):
    monkeypatch.setitem(settings(), "AUTH_PROVIDER", "local")
    monkeypatch.delenv("PUBLIC_ORIGIN", raising=False)
    monkeypatch.delitem(settings(), "PUBLIC_ORIGIN", raising=False)
    with control.control_connection() as conn:
        conn.execute("DELETE FROM control.login_attempts")
        conn.execute(
            "INSERT INTO control.accounts(username,role,password_hash,token_limit,window_seconds) VALUES (%s,'admin',%s,100000,3600) ON CONFLICT(username) DO UPDATE SET role='admin',password_hash=EXCLUDED.password_hash,enabled=true,limit_enabled=false,token_limit=100000,window_seconds=3600",
            (USERNAME, auth.password_hash(PASSWORD)),
        )
        conn.execute("DELETE FROM control.reservations WHERE username=%s", (USERNAME,))
        conn.execute("DELETE FROM control.sessions WHERE username=%s", (USERNAME,))
        conn.execute(
            "INSERT INTO control.budgets(username,resets_at) VALUES (%s,now()+interval '1 hour') ON CONFLICT(username) DO UPDATE SET used_tokens=0,reserved_tokens=0,resets_at=now()+interval '1 hour'",
            (USERNAME,),
        )
    yield
    with control.control_connection() as conn:
        for table in ["jobs", "sessions", "reservations", "budgets", "accounts"]:
            # Table names are fixed test constants, never request values.
            conn.execute(f"DELETE FROM control.{table} WHERE username=%s", (USERNAME,))


def signed_in():
    client = TestClient(app)
    r = client.post(
        "/api/auth/login",
        json={"username": USERNAME, "password": PASSWORD},
        headers={"Origin": "http://testserver"},
    )
    assert r.status_code == 200
    client.headers.update(
        {"Origin": "http://testserver", "X-CSRF-Token": r.json()["csrf"]}
    )
    return client


def test_login_logout_csrf_and_protected_routes():
    client = TestClient(app)
    assert client.get("/api/catalog").status_code == 401
    assert (
        client.post(
            "/api/auth/login",
            json={"username": USERNAME, "password": PASSWORD},
            headers={"Origin": "https://attacker.test"},
        ).status_code
        == 403
    )
    client = signed_in()
    assert (
        "HttpOnly"
        in client.cookies.jar._cookies["testserver.local"]["/"][auth.COOKIE]._rest
    )
    assert client.get("/api/auth/me").json()["role"] == "admin"
    assert client.get("/api/catalog").status_code == 200
    assert (
        client.put(
            "/api/admin/token-policy",
            json={"enabled": True, "token_limit": 1, "window_seconds": 3600},
            headers={"X-CSRF-Token": "wrong"},
        ).status_code
        == 403
    )
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").status_code == 401


def test_expired_sessions_and_user_cannot_call_admin_routes():
    client = signed_in()
    with control.control_connection() as conn:
        conn.execute(
            "UPDATE control.accounts SET role='user' WHERE username=%s", (USERNAME,)
        )
    assert client.get("/api/catalog").status_code == 403
    assert client.post("/api/inference", json={"question": "x"}).status_code == 403
    with control.control_connection() as conn:
        conn.execute(
            "UPDATE control.sessions SET expires_at=now()-interval '1 second' WHERE username=%s",
            (USERNAME,),
        )
    assert client.get("/api/usage").status_code == 401


def test_login_throttling_invalid_password():
    client = TestClient(app)
    for _ in range(12):
        assert (
            client.post(
                "/api/auth/login",
                json={"username": USERNAME, "password": "wrong"},
                headers={"Origin": "http://testserver"},
            ).status_code
            == 401
        )
    assert (
        client.post(
            "/api/auth/login",
            json={"username": USERNAME, "password": PASSWORD},
            headers={"Origin": "http://testserver"},
        ).status_code
        == 429
    )


def test_limits_disabled_still_meter_and_enabling_preserves_usage():
    reservation = control.reserve(USERNAME, 120000)
    control.settle(USERNAME, reservation, 110000)
    assert control.usage(USERNAME)["used_tokens"] == 110000
    policy = control.update_limit(USERNAME, True, 100000, 3600)
    assert policy["used_tokens"] == 110000
    with pytest.raises(control.BudgetExceeded):
        control.reserve(USERNAME, 1)


def test_concurrent_reservations_do_not_exceed_limit_and_settle_once():
    control.update_limit(USERNAME, True, 100, 3600)

    def attempt(_):
        try:
            return control.reserve(USERNAME, 60)
        except control.BudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(4)))
    accepted = [r for r in results if r]
    assert len(accepted) == 1
    control.settle(USERNAME, accepted[0], 20)
    control.settle(USERNAME, accepted[0], 20)
    assert control.usage(USERNAME)["used_tokens"] == 20
    assert control.usage(USERNAME)["reserved_tokens"] == 0


def test_rollover_and_unknown_usage_charge():
    reservation = control.reserve(USERNAME, 500)
    control.settle(USERNAME, reservation, None)
    assert control.usage(USERNAME)["used_tokens"] == 500
    with control.control_connection() as conn:
        conn.execute(
            "UPDATE control.budgets SET resets_at=%s WHERE username=%s",
            (control.utcnow() - timedelta(seconds=1), USERNAME),
        )
    assert control.usage(USERNAME)["used_tokens"] == 0


def test_model_calls_both_stages_are_metered(monkeypatch):
    import httpx
    from app.inference import infer

    calls = []

    async def response(self, url, **kwargs):
        calls.append(kwargs["json"])
        content = (
            '{"sql":"SELECT count(*) AS n FROM data.events","reason":"Count"}'
            if len(calls) == 1
            else "There are 180 events."
        )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {"total_tokens": 123},
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", response)
    monkeypatch.setitem(settings(), "MISTRAL_API_KEY", "test-only")

    async def run():
        token = principal.set(USERNAME)
        try:
            return await infer("How many events?", "enriched", [])
        finally:
            principal.reset(token)

    result = asyncio.run(run())
    assert result["status"] == "ok"
    assert result["usage"]["total_tokens"] == 246
    assert control.usage(USERNAME)["used_tokens"] == 246
    assert control.usage(USERNAME)["reserved_tokens"] == 0


def test_budget_blocks_before_provider_call(monkeypatch):
    import httpx
    from app.llm import complete

    control.update_limit(USERNAME, True, 1, 3600)

    async def unexpected(*args, **kwargs):
        raise AssertionError("Provider must not be called")

    monkeypatch.setattr(httpx.AsyncClient, "post", unexpected)
    monkeypatch.setitem(settings(), "MISTRAL_API_KEY", "test-only")

    async def run():
        token = principal.set(USERNAME)
        try:
            with pytest.raises(control.BudgetExceeded):
                await complete([{"role": "user", "content": "hello"}])
        finally:
            principal.reset(token)

    asyncio.run(run())


def test_chat_redaction_job_ownership_and_admin_policy(monkeypatch):
    async def fake(*args, **kwargs):
        return {
            "question": "x",
            "answer": "180 events",
            "status": "ok",
            "usage": {},
            "sql": "private",
            "context": "private",
            "query_messages": "private",
        }

    monkeypatch.setattr("app.main.infer", fake)
    client = signed_in()
    job = client.post("/api/chat/jobs", json={"question": "x", "mode": "base"})
    assert job.status_code == 202
    result = client.get("/api/jobs/" + job.json()["id"]).json()["result"]
    assert (
        result["answer"] == "180 events"
        and "sql" not in result
        and "context" not in result
    )
    assert (
        client.get("/api/jobs/00000000-0000-0000-0000-000000000000").status_code == 404
    )
    policy = client.put(
        "/api/admin/token-policy",
        json={"enabled": False, "token_limit": 25000, "window_seconds": 3600},
    )
    assert policy.status_code == 200 and policy.json()["enabled"] is False


def test_query_role_cannot_read_control_and_control_cannot_read_data():
    from app.database import connection
    import psycopg

    with connection() as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM control.accounts")
    with control.control_connection() as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM data.events")


def test_cognito_failure_does_not_fall_back_to_local_password(monkeypatch):
    from botocore.exceptions import ClientError

    class Denied:
        def initiate_auth(self, **kwargs):
            raise ClientError(
                {"Error": {"Code": "NotAuthorizedException", "Message": "Denied"}},
                "InitiateAuth",
            )

    monkeypatch.setitem(settings(), "AUTH_PROVIDER", "cognito")
    monkeypatch.setitem(settings(), "COGNITO_CLIENT_ID", "test-client")
    monkeypatch.setitem(settings(), "COGNITO_CLIENT_SECRET", "test-secret")
    monkeypatch.setattr(auth.boto3, "client", lambda *args, **kwargs: Denied())
    client = TestClient(app)
    # Even the correct locally hashed password cannot bypass the chosen provider.
    assert (
        client.post(
            "/api/auth/login",
            json={"username": USERNAME, "password": PASSWORD},
            headers={"Origin": "http://testserver"},
        ).status_code
        == 401
    )


@pytest.mark.parametrize("failure", ["rejected", "timeout"])
def test_provider_failures_settle_reservations(monkeypatch, failure):
    import httpx
    from app.llm import complete, ModelError

    async def fail(*args, **kwargs):
        if failure == "timeout":
            raise httpx.ReadTimeout("test timeout")
        return httpx.Response(401)

    monkeypatch.setattr(httpx.AsyncClient, "post", fail)
    monkeypatch.setitem(settings(), "MISTRAL_API_KEY", "test-only")

    async def run():
        token = principal.set(USERNAME)
        try:
            with pytest.raises(ModelError):
                await complete([{"role": "user", "content": "hello"}])
        finally:
            principal.reset(token)

    asyncio.run(run())
    measured = control.usage(USERNAME)
    assert measured["reserved_tokens"] == 0
    assert (
        measured["used_tokens"] == 0
        if failure == "rejected"
        else measured["used_tokens"] > 0
    )


@pytest.mark.parametrize("mode", ["base", "enriched"])
def test_user_selects_variant_without_receiving_internal_context(monkeypatch, mode):
    calls = []

    async def fake(question, selected, history, presentation):
        calls.append((selected, history, presentation))
        return {
            "question": question,
            "answer": "180 events",
            "status": "ok",
            "mode": selected,
            "latency_ms": 1234,
            "usage": {"total_tokens": 50},
            "sql": "private",
            "context": {"private": True},
            "query_messages": "private",
        }

    monkeypatch.setattr("app.main.infer", fake)
    client = signed_in()
    with control.control_connection() as conn:
        conn.execute(
            "UPDATE control.accounts SET role='user' WHERE username=%s", (USERNAME,)
        )
    response = client.post(
        "/api/chat/jobs", json={"question": "How many?", "mode": mode, "history": []}
    )
    assert response.status_code == 202
    result = client.get("/api/jobs/" + response.json()["id"]).json()["result"]
    assert calls == [(mode, [], "chat")]
    assert result["mode"] == mode and result["latency_ms"] == 1234
    assert not {"sql", "context", "query_messages"}.intersection(result)
    assert client.get("/api/catalog").status_code == 403


def test_chat_rejects_unknown_variant():
    client = signed_in()
    assert (
        client.post(
            "/api/chat/jobs", json={"question": "How many?", "mode": "administrator"}
        ).status_code
        == 422
    )


@pytest.mark.parametrize("mode", ["base", "enriched"])
def test_review_records_real_calls_without_credentials_or_replay(monkeypatch, mode):
    import httpx
    import json

    provider_calls = []
    query = "SELECT count(*) AS n FROM data.events"

    async def provider(self, url, **kwargs):
        payload = kwargs["json"]
        provider_calls.append(payload)
        content = (
            json.dumps({"sql": query, "reason": "Count events"})
            if payload.get("response_format")
            else "There are 180 events."
        )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {"total_tokens": 25},
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", provider)
    monkeypatch.setitem(
        settings(), "MISTRAL_API_KEY", "private-key-must-never-appear-in-review"
    )
    client = signed_in()
    job = client.post(
        "/api/chat/jobs", json={"question": "Count events", "mode": mode}
    ).json()["id"]
    public = client.get(f"/api/jobs/{job}").json()["result"]
    assert public["job_id"] == job
    assert "_review" not in public and "sql" not in public
    review = client.get(f"/api/jobs/{job}/review")
    assert review.status_code == 200
    data = review.json()
    assert [c["kind"] for c in data["calls"]] == ["tool", "model", "tool", "model"]
    assert all(c["status"] == "ok" and c["latency_ms"] >= 0 for c in data["calls"])
    assert data["input"]["mode"] == mode
    assert data["calls"][2]["input"]["sql"] == query
    assert data["calls"][2]["output"]["rows"] == [{"n": 180}]
    assert json.loads(data["calls"][1]["output"]["content"])["sql"] == query
    context = json.loads(
        data["calls"][1]["input"]["messages"][0]["content"].split("CONTEXT:\n")[1]
    )
    assert ("semantics" in context) is (mode == "enriched")
    assert data["output"]["rows"] == [{"n": 180}]
    assert "private-key-must-never-appear-in-review" not in review.text
    assert "Authorization" not in review.text and "_started" not in review.text
    usage_before = control.usage(USERNAME)["used_tokens"]
    assert client.get(f"/api/jobs/{job}/review").status_code == 200
    assert (
        len(provider_calls) == 2
        and control.usage(USERNAME)["used_tokens"] == usage_before
    )


def test_review_is_owned_admin_only_and_handles_earlier_jobs():
    client = signed_in()
    job = control.create_job(USERNAME, {"question": "x"})
    assert client.get(f"/api/jobs/{job}/review").status_code == 409
    control.finish_job(job, {"answer": "earlier", "status": "ok"})
    assert client.get(f"/api/jobs/{job}/review").status_code == 404
    control.finish_job(
        job,
        {
            "answer": "ok",
            "status": "ok",
            "_review": {"input": {}, "output": {}, "calls": []},
        },
    )
    assert client.get(f"/api/jobs/{job}/review").status_code == 200
    assert client.get("/api/jobs/not-a-uuid/review").status_code == 404
    assert (
        client.get("/api/jobs/00000000-0000-0000-0000-000000000000/review").status_code
        == 404
    )
    with control.control_connection() as conn:
        conn.execute(
            "UPDATE control.accounts SET role='user' WHERE username=%s", (USERNAME,)
        )
    assert client.get(f"/api/jobs/{job}/review").status_code == 403
    assert "_review" not in client.get(f"/api/jobs/{job}").json()["result"]
    assert TestClient(app).get(f"/api/jobs/{job}/review").status_code == 401


def test_review_records_failed_sql_tool_without_synthesizing(monkeypatch):
    import httpx
    import json

    async def provider(*args, **kwargs):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "sql": "DELETE FROM data.events",
                                    "reason": "Invalid operation",
                                }
                            )
                        }
                    }
                ],
                "usage": {"total_tokens": 10},
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", provider)
    monkeypatch.setitem(settings(), "MISTRAL_API_KEY", "test-only")
    client = signed_in()
    job = client.post("/api/chat/jobs", json={"question": "x"}).json()["id"]
    data = client.get(f"/api/jobs/{job}/review").json()
    assert data["output"]["status"] == "error"
    assert [c["kind"] for c in data["calls"]] == ["tool", "model", "tool"]
    assert data["calls"][-1]["status"] == "error"
    assert "error" in data["calls"][-1]["output"]


def test_review_trace_isolation_between_concurrent_tasks():
    from app import tracing

    async def run(value):
        trace = []
        token = tracing.calls.set(trace)
        try:
            entry = tracing.start("tool", "test", {"value": value})
            await asyncio.sleep(0)
            tracing.finish(entry, {"value": value})
            return trace
        finally:
            tracing.calls.reset(token)

    async def pair():
        return await asyncio.gather(run("base"), run("enriched"))

    left, right = asyncio.run(pair())
    assert len(left) == len(right) == 1
    assert left[0]["output"] == {"value": "base"}
    assert right[0]["output"] == {"value": "enriched"}
    assert tracing.calls.get() is None


def test_live_activity_is_public_metadata_and_never_a_partial_answer(monkeypatch):
    from app import tracing
    from app.main import run_job

    client = signed_in()
    job = control.create_job(USERNAME, {"question": "Count events"})
    observed = []

    async def fake(*args, **kwargs):
        entry = tracing.start("tool", "execute_query", {"sql": "private SQL"})
        await tracing.publish()
        response = await asyncio.to_thread(client.get, f"/api/jobs/{job}")
        observed.append(response.json())
        tracing.finish(entry, {"rows": [{"private": "result"}]})
        await tracing.publish()
        response = await asyncio.to_thread(client.get, f"/api/jobs/{job}")
        observed.append(response.json())
        return {"answer": "180 events", "status": "ok"}

    monkeypatch.setattr("app.main.infer", fake)

    async def run():
        await run_job(job, {"username": USERNAME}, {"question": "Count events"}, "chat")
        assert tracing.progress.get() is None
        assert tracing.calls.get() is None

    asyncio.run(run())
    for data in observed:
        assert data["state"] == "running" and data["result"] is None
        assert len(data["activity"]) == 1
        assert set(data["activity"][0]) <= {
            "sequence",
            "kind",
            "name",
            "status",
            "latency_ms",
        }
        assert "private" not in str(data) and "_started" not in str(data)
    assert observed[0]["activity"][0]["status"] == "running"
    assert observed[1]["activity"][0]["status"] == "ok"
    # A late progress write cannot overwrite a completed response.
    control.update_job_activity(job, [])
    done = client.get(f"/api/jobs/{job}").json()
    assert done["state"] == "done" and done["result"]["answer"] == "180 events"
    assert done["result"]["activity"] == observed[1]["activity"]
    assert "_review" not in done["result"] and "_activity" not in done["result"]
    with control.control_connection() as conn:
        conn.execute(
            "UPDATE control.accounts SET role='user' WHERE username=%s", (USERNAME,)
        )
    assert client.get(f"/api/jobs/{job}").json()["activity"] == observed[1]["activity"]
    assert client.get(f"/api/jobs/{job}/review").status_code == 403
    assert TestClient(app).get(f"/api/jobs/{job}").status_code == 401


def test_progress_persistence_failure_does_not_break_inference(monkeypatch):
    from app import tracing

    async def fail(activity):
        raise RuntimeError("private connection details")

    async def run():
        trace_token = tracing.calls.set([])
        token = tracing.progress.set(fail)
        try:
            assert await tracing.tool("test", {}, lambda: 180) == 180
            assert tracing.activity()[0]["status"] == "ok"
        finally:
            tracing.calls.reset(trace_token)
            tracing.progress.reset(token)

    asyncio.run(run())
