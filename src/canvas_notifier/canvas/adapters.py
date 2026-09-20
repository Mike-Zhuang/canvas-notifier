from datetime import timedelta
from urllib.parse import quote

from canvas_notifier.canvas.http import CanvasError
from canvas_notifier.domain.time import now_utc


async def courses(client):
    return await client.paginate(
        "/api/v1/courses",
        [("per_page", "100"), ("state[]", "available"), ("state[]", "completed"), ("include[]", "term")],
        kind="course",
    )


async def assignments(client, course_id):
    return await client.paginate(
        f"/api/v1/courses/{course_id}/assignments",
        [("per_page", "100"), ("include[]", "submission")],
        kind="assignment",
    )


async def submissions(client, course_id, assignment_items):
    result = await client.paginate(
        f"/api/v1/courses/{course_id}/students/submissions",
        [
            ("per_page", "100"),
            ("student_ids[]", "self"),
            ("include[]", "submission_comments"),
            ("include[]", "rubric_assessment"),
        ],
        kind="submission",
    )
    if not result.complete:
        return result
    merged = {
        str(item["assignment_id"]): item
        for item in result.items
        if str(item.get("user_id")) == client.user_id
    }
    # 逐项详情用于独立评论与 rubric，不依赖 assignment.updated_at 或 graded_since。
    for assignment in assignment_items:
        aid = str(assignment["id"])
        try:
            detail, _ = await client.get(
                f"/api/v1/courses/{course_id}/assignments/{aid}/submissions/self",
                [("include[]", "submission_comments"), ("include[]", "rubric_assessment")],
                kind="submission",
            )
            if not isinstance(detail, dict) or str(detail.get("user_id")) != client.user_id:
                raise CanvasError("submission_identity_mismatch")
            merged[aid] = {**merged.get(aid, {}), **detail, "assignment_id": aid}
            result.pages += 1
        except CanvasError as error:
            result.complete, result.error = False, error.code
    result.items = list(merged.values())
    return result


async def content_scopes(client, course_id):
    prefix = f"/api/v1/courses/{course_id}"
    for kind, endpoint, params in [
        ("announcement", "discussion_topics", [("only_announcements", "true")]),
        ("file", "files", []),
        ("folder", "folders", []),
        ("discussion", "discussion_topics", []),
        ("module", "modules", []),
        ("page", "pages", []),
    ]:
        result = await client.paginate(f"{prefix}/{endpoint}", [("per_page", "100"), *params], kind=kind)
        if kind == "discussion":
            result.items = [item for item in result.items if not item.get("is_announcement")]
        if kind == "page":
            for i, item in enumerate(result.items):
                try:
                    detail, _ = await client.get(f"{prefix}/pages/{quote(item['url'], safe='')}", kind="page")
                    result.items[i] = {**item, **detail, "id": item.get("page_id") or item["url"]}
                    result.pages += 1
                except (CanvasError, KeyError, TypeError) as error:
                    result.complete, result.error = False, getattr(error, "code", "schema_changed")
        yield kind, "", result
        if kind == "module":
            for item in result.items:
                nested = await client.paginate(
                    f"{prefix}/modules/{item['id']}/items", [("per_page", "100")], kind="module_item"
                )
                yield "module_item", str(item["id"]), nested
        if kind == "discussion":
            for item in result.items:
                topic = str(item["id"])
                entries = await client.paginate(
                    f"{prefix}/discussion_topics/{topic}/entries", [("per_page", "100")], kind="reply"
                )
                # recent_replies 是截断预览，必须继续请求完整 replies 子资源。
                yield "reply", topic, entries
                for entry in entries.items:
                    if entry.get("has_more_replies") or entry.get("recent_replies") or entry.get("replies"):
                        replies = await client.paginate(
                            f"{prefix}/discussion_topics/{topic}/entries/{entry['id']}/replies",
                            [("per_page", "100")],
                            kind="reply",
                        )
                        yield "reply", f"{topic}:{entry['id']}", replies


async def global_scopes(client):
    inbox = await client.paginate("/api/v1/conversations", [("per_page", "100")], kind="conversation")
    for i, item in enumerate(inbox.items):
        try:
            detail, _ = await client.get(
                f"/api/v1/conversations/{item['id']}", [("auto_mark_as_read", "false")], kind="conversation"
            )
            inbox.items[i] = {**item, **detail}
            inbox.pages += 1
        except (CanvasError, TypeError) as error:
            inbox.complete, inbox.error = False, getattr(error, "code", "schema_changed")
    yield "conversation", inbox
    now = now_utc()
    for kind, path, params in [
        ("calendar", "/api/v1/calendar_events", [("all_events", "true")]),
        (
            "planner",
            "/api/v1/planner/items",
            [
                ("start_date", (now - timedelta(days=7)).date().isoformat()),
                ("end_date", (now + timedelta(days=120)).date().isoformat()),
            ],
        ),
        ("planner_note", "/api/v1/planner_notes", []),
    ]:
        yield kind, await client.paginate(path, [("per_page", "100"), *params], kind=kind)
