import httpx
import pytest

from canvas_notifier.canvas.http import CanvasClient, CanvasError
from canvas_notifier.config import write_secret


async def test_all_pages_and_repeated_parameters(settings):
    observed = []

    def handler(request):
        observed.append(request)
        page = int(request.url.params.get("page", "1"))
        link = (
            {"Link": f'<https://canvas.tongji.edu.cn/api/v1/courses?page={page + 1}>; rel="next"'}
            if page < 4
            else {}
        )
        return httpx.Response(200, json=[{"id": str((page - 1) * 40 + i)} for i in range(40)], headers=link)

    async with CanvasClient(settings, httpx.MockTransport(handler)) as client:
        result = await client.paginate("/api/v1/courses", [("include[]", "term"), ("include[]", "teachers")])
    assert result.complete and result.pages == 4 and len(result.items) == 160
    assert observed[0].url.params.get_list("include[]") == ["term", "teachers"]


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/api/v1/courses",
        "https://canvas.tongji.edu.cn/api/v1/courses/1/assignments?include[]=read_status",
        "https://canvas.tongji.edu.cn/checkin",
    ],
)
async def test_unsafe_next_never_requested(settings, url):
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(200, json=[{"id": "1"}], headers={"Link": f'<{url}>; rel="next"'})

    async with CanvasClient(settings, httpx.MockTransport(handler)) as client:
        result = await client.paginate("/api/v1/courses")
    assert count == 1 and not result.complete and result.error


@pytest.mark.parametrize(
    "status,content,error",
    [
        (401, {}, "auth_rejected"),
        (403, {}, "resource_forbidden"),
        (302, {}, "auth_rejected"),
        (200, "<html>login</html>", "login_html_returned"),
    ],
)
async def test_auth_not_empty(settings, status, content, error):
    def handler(request):
        return (
            httpx.Response(status, text=content, headers={"Content-Type": "text/html"})
            if isinstance(content, str)
            else httpx.Response(status, json=content)
        )

    async with CanvasClient(settings, httpx.MockTransport(handler)) as client:
        result = await client.paginate("/api/v1/courses")
    assert not result.complete and result.error == error


async def test_304_reuses_data_and_pagination(settings):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return (
            httpx.Response(200, json=[{"id": "1"}], headers={"ETag": "v1"})
            if calls == 1
            else httpx.Response(304)
        )

    async with CanvasClient(settings, httpx.MockTransport(handler)) as client:
        first = await client.paginate("/api/v1/courses")
        second = await client.paginate("/api/v1/courses")
    assert first.items == second.items and second.complete


async def test_page_budget_partial(settings):
    settings.max_pages = 2

    def handler(request):
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(
            200,
            json=[{"id": str(page)}],
            headers={"Link": f'<https://canvas.tongji.edu.cn/api/v1/courses?page={page + 1}>; rel="next"'},
        )

    async with CanvasClient(settings, httpx.MockTransport(handler)) as client:
        result = await client.paginate("/api/v1/courses")
    assert result.pages == 2 and result.error == "page_budget" and result.cursor and not result.complete


async def test_conversation_no_mark_read(settings):
    def handler(request):
        assert request.url.params["auto_mark_as_read"] == "false"
        return httpx.Response(200, json={"id": "1"})

    async with CanvasClient(settings, httpx.MockTransport(handler)) as client:
        await client.get("/api/v1/conversations/1")
        with pytest.raises(CanvasError):
            await client.get("/api/v1/conversations/1", [("auto_mark_as_read", "true")])
        with pytest.raises(CanvasError):
            await client.get(
                "/api/v1/courses/1/assignments/1/submissions/self", [("include[]", "read_status")]
            )


async def test_cookie_fallback_guard(settings):
    import json

    write_secret(
        settings.canvas_cookie_file,
        json.dumps(
            [{"name": "session", "value": "synthetic", "domain": "canvas.tongji.edu.cn", "path": "/"}]
        ),
    )
    settings.canvas_cookie_fallback = True
    settings.canvas_cookie_resources = "course"
    modes = []

    def handler(request):
        modes.append("token" if "Authorization" in request.headers else "cookie")
        return (
            httpx.Response(401, json={})
            if "Authorization" in request.headers
            else httpx.Response(200, json=[])
        )

    async with CanvasClient(settings, httpx.MockTransport(handler)) as client:
        rejected = await client.paginate("/api/v1/courses", kind="course")
        assert not rejected.complete
        client.cookie_verified = True
        ok = await client.paginate("/api/v1/courses", kind="course")
    assert ok.complete and modes == ["token", "cookie"]


async def test_429_retry_after(settings, monkeypatch):
    import canvas_notifier.canvas.http as module

    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={}, headers={"Retry-After": "4"})
        if calls == 2:
            return httpx.Response(503, json={})
        return httpx.Response(200, json=[])

    async with CanvasClient(settings, httpx.MockTransport(handler)) as client:
        result = await client.paginate("/api/v1/courses")
    assert result.complete and calls == 3 and 4 in delays


async def test_missing_token_fallback_still_requires_verified_allowed_cookie(settings):
    import json

    settings.canvas_token_file.unlink()
    settings.canvas_cookie_fallback = True
    settings.canvas_cookie_resources = "course"
    write_secret(
        settings.canvas_cookie_file,
        json.dumps(
            [{"name": "session", "value": "synthetic", "domain": "canvas.tongji.edu.cn", "path": "/"}]
        ),
    )
    requests = []

    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(200, json=[])

    async with CanvasClient(settings, httpx.MockTransport(handler)) as client:
        not_verified = await client.paginate("/api/v1/courses", kind="course")
        assert not_verified.error == "bearer_missing" and requests == []
        client.cookie_verified = True
        allowed = await client.paginate("/api/v1/courses", kind="course")
        denied = await client.paginate("/api/v1/courses/1/files", kind="file")
        assert allowed.complete and denied.error == "bearer_missing"
    assert requests == ["/api/v1/courses"]
