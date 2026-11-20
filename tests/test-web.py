import re

import httpx
from sqlalchemy import func, select

from canvas_notifier.db import Rule
from canvas_notifier.web.app import create_app


async def sign_in(client):
    page = await client.get("/login")
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
    response = await client.post(
        "/login",
        data={"csrf": csrf, "password": "synthetic-admin-password"},
        headers={"Origin": "http://127.0.0.1:8000"},
    )
    assert response.status_code == 303
    return csrf


async def test_auth_csrf_and_private_pages(settings, sessions):
    app = create_app(settings, sessions)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
    ) as client:
        assert (await client.get("/")).status_code == 303
        assert (await client.get("/api/reminders")).status_code == 401
        assert (await client.post("/login", data={"password": "synthetic-admin-password"})).status_code == 403
        await sign_in(client)
        for path in ["/", "/rules", "/history", "/health"]:
            response = await client.get(path)
            assert response.status_code == 200
            assert "synthetic-token" not in response.text
            assert "synthetic-admin-password" not in response.text
            assert response.headers["cache-control"] == "no-store"
            assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert (await client.post("/rules", data={"scope": "global"})).status_code == 403
        page = await client.get("/rules")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
        result = await client.post(
            "/rules",
            data={
                "csrf": csrf,
                "scope": "global",
                "mode": "json",
                "config": '{"due":["PT2H"],"events":{"grade_changed":"off"}}',
            },
        )
        assert result.status_code == 303
        async with sessions() as db:
            rule = await db.get(Rule, "global")
            assert rule.data["due"] == ["PT7200S"] and rule.data["events"]["grade_changed"] == "off"
        bad = await client.post(
            "/rules", data={"csrf": csrf, "scope": "global", "mode": "json", "config": '{"due":["P1M"]}'}
        )
        assert bad.status_code == 422
        async with sessions() as db:
            assert (await db.get(Rule, "global")).data["due"] == ["PT7200S"]


async def test_preview_does_not_modify_rules(settings, sessions):
    app = create_app(settings, sessions)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
    ) as client:
        await sign_in(client)
        page = await client.get("/rules")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
        response = await client.post(
            "/api/rules/preview",
            data={
                "csrf": csrf,
                "scope": "global",
                "due": "PT1H",
                "lock": "PT1H",
                "unlock": "",
                "personal": "",
                "enabled": "on",
                "only_incomplete": "on",
                "overdue_max": "2",
                "timezone": "Asia/Shanghai",
                "quiet_start": "23:00",
                "quiet_end": "08:00",
                "critical_policy": "advance",
                "stale_source_policy": "pause",
            },
        )
        assert response.status_code == 200
        async with sessions() as db:
            assert await db.scalar(select(func.count()).select_from(Rule)) == 0


async def test_login_rate_limit(settings, sessions):
    app = create_app(settings, sessions)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
    ) as client:
        page = await client.get("/login")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
        for i in range(8):
            assert (await client.post("/login", data={"csrf": csrf, "password": "wrong"})).status_code == 401
        assert (await client.post("/login", data={"csrf": csrf, "password": "wrong"})).status_code == 429


async def test_custom_ten_character_admin_password(settings, sessions):
    from canvas_notifier.config import write_secret

    write_secret(settings.admin_password_file, "DemoPass7!")
    app = create_app(settings, sessions)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
    ) as client:
        page = await client.get("/login")
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
        response = await client.post(
            "/login",
            data={"csrf": csrf, "password": "DemoPass7!"},
            headers={"Origin": "http://127.0.0.1:8000"},
        )
        assert response.status_code == 303
        assert (await client.get("/")).status_code == 200


async def test_overview_course_and_incomplete_filters(settings, sessions):
    from datetime import timedelta

    from canvas_notifier.db import Resource
    from canvas_notifier.domain.time import now_utc

    now = now_utc()
    past = (now - timedelta(days=1)).isoformat()
    future = (now + timedelta(days=1)).isoformat()
    earlier = (now - timedelta(days=2)).isoformat()
    later = (now + timedelta(days=2)).isoformat()
    submitted = {"submitted_at": past}
    cases = [
        ("pending", "1", {}, {}, "online_upload"),
        ("submitted", "1", submitted, {}, "online_upload"),
        ("excused", "1", {"excused": True}, {}, "online_upload"),
        ("local", "1", {}, {"completed": True}, "on_paper"),
        ("redo", "1", {**submitted, "redo_request": True}, {"completed": True}, "online_upload"),
        ("missing", "1", {**submitted, "missing": True}, {}, "online_upload"),
        ("external", "1", submitted, {}, "external_tool"),
        ("overdue", "1", {}, {"personal_at": future}, "online_upload"),
        ("closed", "1", {}, {}, "online_upload"),
        ("other", "2", {}, {}, "online_upload"),
        ("old", "3", {}, {}, "online_upload"),
        ("next_term", "4", {}, {}, "online_upload"),
        ("unknown_term", "5", {}, {}, "online_upload"),
    ]
    async with sessions() as db, db.begin():
        periods = [(past, future), (past, future), (earlier, past), (future, later), (None, None)]
        for index, (start, end) in enumerate(periods, 1):
            cid = str(index)
            db.add(
                Resource(
                    key=f"course::{cid}",
                    kind="course",
                    external_id=cid,
                    scope="courses",
                    data={"name": f"课程{cid}", "term": {"start_at": start, "end_at": end}},
                )
            )
        for name, cid, sub, local, submission_type in cases:
            data = {"name": name, "submission_types": [submission_type]}
            if name == "overdue":
                data.update(due_at=past, lock_at=future)
            if name == "closed":
                data["lock_at"] = past
            if name == "other":
                data["due_at"] = future
            db.add(Resource(key=f"assignment:{cid}:{name}", kind="assignment", external_id=name,
                            course_id=cid, scope=f"{cid}:assignments", data=data, local=local))
            db.add(Resource(key=f"submission:{cid}:{name}", kind="submission", external_id=name,
                            course_id=cid, scope=f"{cid}:submissions", data=sub))

    def task_names(response):
        assert response.status_code == 200
        return set(re.findall(r'<h3>([^<]+)</h3>', response.text)) & {c[0] for c in cases}

    app = create_app(settings, sessions)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
    ) as client:
        await sign_in(client)
        default = await client.get("/")
        assert task_names(default) == {"pending", "redo", "missing", "external", "other"}
        assert "需要重新提交" in default.text
        assert task_names(await client.get("/?course=2")) == {"other"}
        assert task_names(await client.get("/?course=1&status=all&q=submitted")) == {"submitted"}
        assert task_names(await client.get("/?course=1&show=future")) == {
            "pending", "redo", "missing", "external", "overdue"
        }
        assert task_names(await client.get("/?course=1&show=all")) == {
            "pending", "redo", "missing", "external", "overdue", "closed"
        }
        assert task_names(await client.get("/?course=3&term=all")) == {"old"}
        assert task_names(await client.get("/?status=all&show=all&term=all")) == {c[0] for c in cases}
        empty = await client.get("/?course=2&q=pending")
        assert task_names(empty) == set()
        assert "目前没有匹配的任务" in empty.text
        assert task_names(await client.get("/?status=invalid")) == task_names(default)
