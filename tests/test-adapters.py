import httpx
import pytest

from canvas_notifier.canvas.adapters import content_scopes, global_scopes
from canvas_notifier.canvas.http import CanvasClient
from canvas_notifier.domain.normalize import changes_for
from canvas_notifier.sync.service import bind_identity


@pytest.mark.parametrize("auth_mode", ["token", "cookie"])
async def test_nested_resources_nonempty_and_pagination(settings, auth_mode):
    import json

    from canvas_notifier.config import write_secret

    settings.canvas_auth_mode = auth_mode
    if auth_mode == "cookie":
        write_secret(
            settings.canvas_cookie_file,
            json.dumps(
                [
                    {
                        "name": "session",
                        "value": "synthetic-cookie",
                        "domain": "canvas.tongji.edu.cn",
                        "path": "/",
                    }
                ]
            ),
        )
    requests = []

    def handler(request):
        path = request.url.path
        requests.append(str(request.url))
        if path.endswith("/discussion_topics"):
            return httpx.Response(
                200,
                json=[] if request.url.params.get("only_announcements") else [{"id": "10", "title": "topic"}],
            )
        if path.endswith("/discussion_topics/10/entries"):
            return httpx.Response(
                200, json=[{"id": "11", "user_id": "2", "message": "hello", "has_more_replies": True}]
            )
        if path.endswith("/entries/11/replies"):
            page = request.url.params.get("page", "1")
            return httpx.Response(
                200,
                json=[{"id": "12" if page == "1" else "13", "user_id": "3", "message": "reply"}],
                headers={
                    "Link": '<https://canvas.tongji.edu.cn/api/v1/courses/1/discussion_topics/10/entries/11/replies?page=2>; rel="next"'
                }
                if page == "1"
                else {},
            )
        if path.endswith("/modules"):
            return httpx.Response(200, json=[{"id": "20", "name": "module"}])
        if path.endswith("/modules/20/items"):
            return httpx.Response(200, json=[{"id": "21", "title": "item", "type": "Page"}])
        if path.endswith("/pages"):
            return httpx.Response(200, json=[{"page_id": "30", "url": "intro", "title": "Introduction"}])
        if path.endswith("/pages/intro"):
            return httpx.Response(
                200,
                json={"page_id": "30", "url": "intro", "title": "Introduction", "body": "<p>Full page</p>"},
            )
        return httpx.Response(200, json=[])

    async with CanvasClient(settings, httpx.MockTransport(handler)) as client:
        results = {(kind, suffix): result async for kind, suffix, result in content_scopes(client, "1")}
    assert results["reply", "10:11"].pages == 2 and len(results["reply", "10:11"].items) == 2
    assert results["module_item", "20"].complete
    assert results["page", ""].items[0]["body"] == "<p>Full page</p>"
    assert all(r.complete for r in results.values())


async def test_inbox_detail_never_marks_read(settings):
    def handler(request):
        if request.url.path == "/api/v1/conversations":
            return httpx.Response(200, json=[{"id": "40", "subject": "Message"}])
        if request.url.path == "/api/v1/conversations/40":
            assert request.url.params["auto_mark_as_read"] == "false"
            return httpx.Response(
                200, json={"id": "40", "messages": [{"id": "41", "author_id": "2", "body": "hello"}]}
            )
        return httpx.Response(200, json=[])

    async with CanvasClient(settings, httpx.MockTransport(handler)) as client:
        scopes = {kind: result async for kind, result in global_scopes(client)}
    assert len(scopes["conversation"].items[0]["messages"]) == 1
    assert (
        changes_for(
            "conversation", {}, {"messages": [{"id": "1", "author_id": "self", "body": "mine"}]}, "self"
        )
        == []
    )
    assert (
        changes_for(
            "conversation", {}, {"messages": [{"id": "2", "author_id": "teacher", "body": "hello"}]}, "self"
        )[0][0]
        == "conversation_message_added"
    )


async def test_credential_identity_mismatch_is_blocked(settings, sessions):
    import json

    import pytest

    from canvas_notifier.canvas.http import CanvasError
    from canvas_notifier.config import write_secret

    write_secret(
        settings.canvas_cookie_file,
        json.dumps(
            [{"name": "session", "value": "synthetic", "domain": "canvas.tongji.edu.cn", "path": "/"}]
        ),
    )
    settings.canvas_cookie_fallback = True

    def handler(request):
        return httpx.Response(200, json={"id": "1" if "Authorization" in request.headers else "2"})

    async with CanvasClient(settings, httpx.MockTransport(handler)) as client:
        with pytest.raises(CanvasError, match="credential_identity_mismatch"):
            await bind_identity(sessions, client)
