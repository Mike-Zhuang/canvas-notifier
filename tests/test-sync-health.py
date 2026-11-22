import asyncio
import json
from datetime import timedelta

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from typer.testing import CliRunner

from canvas_notifier.cli import app
from canvas_notifier.config import write_secret
from canvas_notifier.db import Delivery, Health, Resource, Rule, Scope, database
from canvas_notifier.domain.time import now_utc, parse_time
from canvas_notifier.sync.service import sync_once


def response(request, **paths):
    data = paths.get(request.url.path, {"id": "1"} if request.url.path.endswith("/users/self") else [])
    return httpx.Response(200, json=data)


async def test_unsupported_is_healthy_and_reprobe_is_deferred(settings, sessions):
    page_requests = 0

    def handler(request):
        nonlocal page_requests
        if request.url.path == "/api/v1/courses/1/pages":
            page_requests += 1
            return httpx.Response(404, json={}) if page_requests == 1 else httpx.Response(200, json=[])
        return response(request, **{"/api/v1/courses": [{"id": "1", "name": "课程甲"}]})

    transport = httpx.MockTransport(handler)
    for _ in range(2):
        summary = await sync_once(settings, sessions, transport=transport)
        assert summary["status"] == "ok" and summary["unsupported_scopes"] == 1
        assert summary["failed_scopes"] == summary["unhealthy_scopes"] == 0
    assert page_requests == 1
    async with sessions() as db, db.begin():
        scope = await db.get(Scope, "page:1:")
        assert scope.status == "unsupported" and scope.next_attempt - scope.last_attempt == timedelta(days=1)
        assert (await db.get(Health, "sync")).status == "ok"
        assert (await db.get(Health, "sync_alert")).status == "ok"
        scope.next_attempt = now_utc() - timedelta(seconds=1)
    assert (await sync_once(settings, sessions, transport=transport))["unsupported_scopes"] == 0
    assert page_requests == 2


async def test_cookie_is_healthy_after_bearer_rejection(settings, sessions):
    settings.canvas_cookie_fallback = True
    settings.canvas_cookie_resources = "identity,course,conversation,calendar,planner,planner_note"
    write_secret(
        settings.canvas_cookie_file,
        json.dumps([{"name": "session", "value": "synthetic", "domain": "canvas.tongji.edu.cn"}]),
    )

    def handler(request):
        if "authorization" in request.headers:
            return httpx.Response(401, json={})
        assert request.headers["cookie"] == "session=synthetic"
        return response(request)

    assert (await sync_once(settings, sessions, transport=httpx.MockTransport(handler)))["status"] == "ok"
    async with sessions() as db:
        auth = await db.get(Health, "auth")
        assert auth.status == "ok" and auth.details["detail"] == "Cookie 通道正常，Token 已失效"
        assert not (await db.scalars(select(Delivery))).all()


async def test_initial_digests_wait_for_each_baseline_and_recipients(settings, sessions):
    settings.mail_to = ""
    assignments_ready = False
    due = (now_utc() + timedelta(days=1)).isoformat()

    def handler(request):
        path = request.url.path
        if path == "/api/v1/courses/2/assignments" and not assignments_ready:
            return httpx.Response(403, json={})
        return response(
            request,
            **{
                "/api/v1/courses": [{"id": "1", "name": "第一门"}, {"id": "2", "name": "第二门"}],
                "/api/v1/courses/1/assignments": [{"id": "11", "name": "任务甲", "due_at": due}],
                "/api/v1/courses/2/assignments": [{"id": "22", "name": "任务乙", "lock_at": due}],
                "/api/v1/courses/1/assignments/11/submissions/self": {"user_id": "1"},
                "/api/v1/courses/2/assignments/22/submissions/self": {"user_id": "1"},
            },
        )

    transport = httpx.MockTransport(handler)
    await sync_once(settings, sessions, transport=transport)
    async with sessions() as db, db.begin():
        assert not await db.get(Health, "initial_digest:1")
        assert not await db.get(Health, "initial_digest:2")
        assert not (await db.scalars(select(Delivery))).all()
        db.add(Rule(scope="course:1", data={"recipients": ["course@example.test"]}))
    await sync_once(settings, sessions, transport=transport)
    async with sessions() as db:
        assert (await db.get(Health, "initial_digest:1")).status == "created"
        assert not await db.get(Health, "initial_digest:2")
    assignments_ready = True
    settings.mail_to = "student@example.test"
    for _ in range(2):
        assert (await sync_once(settings, sessions, transport=transport))["status"] == "ok"
    async with sessions() as db:
        assert (await db.get(Health, "initial_digest:2")).status == "created"
        rows = (await db.scalars(select(Delivery).order_by(Delivery.id))).all()
        digests = [d for d in rows if d.payload["kind"] == "baseline_digest"]
        assert [
            (d.payload["course_id"], d.recipient, d.payload["changes"]["future_tasks"][0]["name"])
            for d in digests
        ] == [("1", "course@example.test", "任务甲"), ("2", "student@example.test", "任务乙")]


async def test_retry_after_is_shared_across_rounds(settings, sessions, monkeypatch):
    from canvas_notifier.canvas import http
    from canvas_notifier.sync import leases

    started = now_utc()
    clock = started
    monkeypatch.setattr(http, "now_utc", lambda: clock)
    monkeypatch.setattr(leases, "now_utc", lambda: clock)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/api/v1/courses" and clock == started:
            return httpx.Response(429, json={}, headers={"Retry-After": "600"})
        return response(request)

    transport = httpx.MockTransport(handler)
    first = await sync_once(settings, sessions, transport=transport)
    assert first["status"] == "rate_limited" and first["retry_seconds"] == 600
    assert calls == ["/api/v1/users/self", "/api/v1/courses"]
    async with sessions() as db:
        wait = await db.get(Health, "canvas_retry_after")
        assert parse_time(wait.details["next_attempt"]) == started + timedelta(seconds=600)
    clock += timedelta(seconds=599)
    assert await sync_once(settings, sessions, transport=transport) == {
        "status": "rate_limited",
        "retry_seconds": 1,
    }
    assert len(calls) == 2
    clock += timedelta(seconds=1)
    assert (await sync_once(settings, sessions, transport=transport))["status"] == "ok"
    assert calls[2] == "/api/v1/users/self"


async def test_paginated_scope_resumes_without_marking_seen_items_missing(settings, sessions):
    path = "/api/v1/courses/1/files"
    pages = []
    ids = [1, 2, 3, 4]

    def handler(request):
        if request.url.path == path:
            page = int(request.url.params.get("page", "1"))
            pages.append(page)
            headers = {"Link": f"<?page={page + 1}>; rel=next"} if page < len(ids) else {}
            return httpx.Response(200, json=[{"id": ids[page - 1]}], headers=headers)
        return response(request, **{"/api/v1/courses": [{"id": 1}]})

    transport = httpx.MockTransport(handler)
    assert (await sync_once(settings, sessions, transport=transport))["status"] == "ok"
    settings.max_pages = 1
    ids = [1, 2, 4]
    pages.clear()
    for start in range(1, 4):
        result = await sync_once(settings, sessions, force=start == 1, transport=transport)
        assert result["status"] == ("partial" if start < 3 else "ok")
        async with sessions() as db:
            scope = await db.get(Scope, "file:1:")
            assert scope.complete is (start == 3)
            assert bool(scope.cursor) is (start < 3)
            file = await db.get(Resource, "file:1:3")
            assert file.availability == ("visible" if start < 3 else "missing_unconfirmed")
    assert pages == [1, 2, 3]
    async with sessions() as db:
        for key in ("1", "2", "4"):
            assert (await db.get(Resource, f"file:1:{key}")).availability == "visible"


@pytest.mark.parametrize("override", [{"pinned": True}, {"history_days": 500}, None])
async def test_task_history_override_and_manual_history(settings, sessions, override):
    end = (now_utc() - timedelta(days=370)).isoformat()
    requests = []
    async with sessions() as db, db.begin():
        db.add(
            Resource(
                key="assignment:1:11",
                kind="assignment",
                external_id="11",
                course_id="1",
                scope="assignment:1:",
                data={"name": "旧作业"},
            )
        )
        if override:
            db.add(Rule(scope="assignment:1:11", data=override))

    def handler(request):
        requests.append(request.url.path)
        return response(
            request,
            **{
                "/api/v1/courses": [{"id": 1, "workflow_state": "completed", "end_at": end}],
                "/api/v1/courses/1/assignments": [{"id": 11, "name": "新名称"}],
                "/api/v1/courses/1/assignments/11/submissions/self": {"user_id": "1"},
            },
        )

    transport = httpx.MockTransport(handler)
    assert (await sync_once(settings, sessions, transport=transport))["status"] == "ok"
    assert ("/api/v1/courses/1/assignments" in requests) is bool(override)
    if not override:
        assert (await sync_once(settings, sessions, include_history=True, transport=transport))[
            "status"
        ] == "ok"
        assert "/api/v1/courses/1/assignments" in requests
    async with sessions() as db:
        assert (await db.get(Resource, "assignment:1:11")).data["name"] == "新名称"


def test_cli_sync_accepts_include_history_without_failing_for_unsupported(settings, monkeypatch):
    async def run(settings, sessions, *, include_history):
        assert include_history is True
        return {"status": "ok", "unsupported_scopes": 2}

    monkeypatch.setattr("canvas_notifier.cli.sync_once", run)
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    result = CliRunner().invoke(app, ["sync", "--once", "--include-history"])
    assert result.exit_code == 0, result.exception
    assert json.loads(result.output)["unsupported_scopes"] == 2


async def test_migration_removes_only_the_removed_grade_preference(settings, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    config = Config("alembic.ini")

    await asyncio.to_thread(command.upgrade, config, "f2b55311b3a2")
    engine, sessions = database(settings.database_url)
    rows = {
        "global": {"score_in_email": True, "enabled": False, "recipients": ["test@example.test"]},
        "course:1": {"score_in_email": None, "events": {"grade_changed": "off"}},
        "assignment:1:2": {"pinned": True, "score_in_email": False},
        "course:3": {"history_days": 120},
    }
    try:
        async with sessions() as db, db.begin():
            db.add_all(Rule(scope=key, data=value) for key, value in rows.items())
        await asyncio.to_thread(command.upgrade, config, "head")
        async with sessions() as db:
            assert await db.scalar(text("SELECT version_num FROM alembic_version")) == "b2f780df8f31"
            migrated = (await db.scalars(select(Rule))).all()
        assert {row.scope: row.data for row in migrated} == {
            key: {k: v for k, v in data.items() if k != "score_in_email"} for key, data in rows.items()
        }
    finally:
        await engine.dispose()
