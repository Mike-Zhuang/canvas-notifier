from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import quote

from canvas_notifier.canvas.http import CanvasError, PageResult
from canvas_notifier.domain.time import now_utc


async def courses(client):
    return await client.paginate(
        "/api/v1/courses",
        [("per_page", "100"), ("state[]", "available"), ("state[]", "completed"), ("include[]", "term")],
        kind="course",
        scope_key="course::",
    )


async def assignments(client, course_id):
    return await client.paginate(
        f"/api/v1/courses/{course_id}/assignments",
        [("per_page", "100"), ("include[]", "submission")],
        kind="assignment",
        scope_key=f"assignment:{course_id}:",
    )


@dataclass
class SubmissionPageResult(PageResult):
    detail_ids: set[str] = field(default_factory=set)
    failed_detail_ids: set[str] = field(default_factory=set)


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
        scope_key=f"submission:{course_id}:",
    )
    if result.skipped:
        return result
    if result.continues:
        result.items = [item for item in result.items if str(item.get("user_id")) == client.user_id]
        return result
    result = SubmissionPageResult(**vars(result))
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
            if (
                not isinstance(detail, dict)
                or str(detail.get("user_id")) != client.user_id
                or str(detail.get("assignment_id", aid)) != aid
            ):
                raise CanvasError("submission_identity_mismatch")
            merged[aid] = {**merged.get(aid, {}), **detail, "assignment_id": aid}
            result.detail_ids.add(aid)
            result.pages += 1
        except CanvasError as error:
            result.complete = False
            result.error = result.error or error.code
            result.failed_detail_ids.add(aid)
    result.items = list(merged.values())
    return result


async def content_scopes(client, course_id, pending=()):
    prefix = f"/api/v1/courses/{course_id}"
    module_ids = set()
    topics = set()
    reply_entries = {}
    # 父清单已翻到下一批时，仍须继续先前发现的子清单。
    for scope in pending:
        kind, _, suffix = scope.key.split(":", 2)
        if kind == "module_item":
            module_ids.add(suffix)
        elif kind == "reply":
            if ":" in suffix:
                topic, entry = suffix.split(":", 1)
                reply_entries.setdefault(topic, set()).add(entry)
            else:
                topics.add(suffix)
    for kind, endpoint, params in [
        ("announcement", "discussion_topics", [("only_announcements", "true")]),
        ("file", "files", []),
        ("folder", "folders", []),
        ("discussion", "discussion_topics", []),
        ("module", "modules", []),
        ("page", "pages", []),
    ]:
        result = await client.paginate(
            f"{prefix}/{endpoint}",
            [("per_page", "100"), *params],
            kind=kind,
            scope_key=f"{kind}:{course_id}:",
        )
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
                    result.continues = False
        yield kind, "", result
        if kind == "module":
            module_ids.update(str(item["id"]) for item in result.items)
        elif kind == "discussion":
            topics.update(str(item["id"]) for item in result.items)
    for module_id in sorted(module_ids):
        nested = await client.paginate(
            f"{prefix}/modules/{module_id}/items",
            [("per_page", "100")],
            kind="module_item",
            scope_key=f"module_item:{course_id}:{module_id}",
        )
        yield "module_item", module_id, nested
    for topic in sorted(topics):
        entries = await client.paginate(
            f"{prefix}/discussion_topics/{topic}/entries",
            [("per_page", "100")],
            kind="reply",
            scope_key=f"reply:{course_id}:{topic}",
        )
        yield "reply", topic, entries
        for entry in entries.items:
            entry_id = str(entry["id"])
            if not (
                entry.get("has_more_replies") or entry.get("recent_replies") or entry.get("replies")
            ) and (
                entry.get("has_more_replies") is False
                or entry.get("recent_replies") == []
                or entry.get("replies") == []
            ):
                # 明确无回复也要建空基线，第一条后续回复才不是历史数据。
                yield "reply", f"{topic}:{entry_id}", PageResult(complete=True)
                reply_entries.get(topic, set()).discard(entry_id)
            else:
                reply_entries.setdefault(topic, set()).add(entry_id)
    for topic, entry_ids in sorted(reply_entries.items()):
        for entry_id in sorted(entry_ids):
            suffix = f"{topic}:{entry_id}"
            replies = await client.paginate(
                f"{prefix}/discussion_topics/{topic}/entries/{entry_id}/replies",
                [("per_page", "100")],
                kind="reply",
                scope_key=f"reply:{course_id}:{suffix}",
            )
            yield "reply", suffix, replies


async def global_scopes(client):
    inbox = await client.paginate(
        "/api/v1/conversations", [("per_page", "100")], kind="conversation", scope_key="conversation::"
    )
    for i, item in enumerate(inbox.items):
        try:
            detail, _ = await client.get(
                f"/api/v1/conversations/{item['id']}", [("auto_mark_as_read", "false")], kind="conversation"
            )
            inbox.items[i] = {**item, **detail}
            inbox.pages += 1
        except (CanvasError, TypeError) as error:
            inbox.complete, inbox.error = False, getattr(error, "code", "schema_changed")
            inbox.continues = False
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
        yield (
            kind,
            await client.paginate(path, [("per_page", "100"), *params], kind=kind, scope_key=f"{kind}::"),
        )
