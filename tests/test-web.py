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
