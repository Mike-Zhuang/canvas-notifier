from datetime import timedelta

import httpx
from sqlalchemy import select

from canvas_notifier.canvas.adapters import content_scopes, submissions
from canvas_notifier.canvas.http import CanvasClient, PageResult
from canvas_notifier.db import Event, Resource
from canvas_notifier.domain.time import parse_time
from canvas_notifier.sync.state import apply_scope

NOW = parse_time("2026-10-01T00:00:00Z")


async def test_link_and_attachment_changes_are_business_events(sessions, settings):
    def assignment(file_id, size):
        return {
            "id": "10",
            "description": f'<p><a href="/files/{file_id}?verifier=secret">资料</a></p>',
            "attachment": {"id": "400", "display_name": "教材", "size": size},
        }

    async with sessions() as db, db.begin():
        for item in [assignment(20, 1), assignment(21, 1), assignment(21, 2)]:
            await apply_scope(db, settings, "assignment", "1", PageResult([item], 1, True), now=NOW)
        events = (await db.scalars(select(Event).order_by(Event.id))).all()
        assert [event.kind for event in events] == ["assignment_content_changed"] * 2
        assert events[0].changes == {
            "links": {
                "before": [f"{settings.canvas_base_url}/files/20"],
                "after": [f"{settings.canvas_base_url}/files/21"],
            }
        }
        assert events[1].changes["attachments"]["after"][0]["size"] == 2


async def test_submission_status_changes(sessions, settings):
    initial = {"assignment_id": "10", "missing": False, "late": True, "excused": False, "points_deducted": 0}
    changed = {**initial, "missing": True, "excused": True, "points_deducted": 1}
    async with sessions() as db, db.begin():
        for item in [initial, changed, changed]:
            await apply_scope(db, settings, "submission", "1", PageResult([item], 1, True), now=NOW)
        [event] = (await db.scalars(select(Event))).all()
        assert event.kind == "submission_status_changed"
        assert event.changes == {
            "missing": {"before": False, "after": True},
            "excused": {"before": False, "after": True},
            "points_deducted": {"before": 0, "after": 1},
        }


async def test_assignment_availability_changes_once_per_transition(sessions, settings):
    item = {"id": "10"}
    async with sessions() as db, db.begin():
        for i, (items, complete) in enumerate(
            [
                ([item], True),
                ([], False),
                ([], True),
                ([], True),
                ([item], True),
                ([item], True),
                ([], True),
                ([item], True),
            ]
        ):
            result = PageResult(items, 1, complete, error=None if complete else "network_error")
            await apply_scope(db, settings, "assignment", "1", result, now=NOW + timedelta(minutes=i))
        events = (await db.scalars(select(Event).order_by(Event.id))).all()
        assert [(event.kind, event.changes) for event in events] == [
            ("assignment_unavailable", {"last_seen": NOW.isoformat()}),
            ("assignment_available_again", {}),
            ("assignment_unavailable", {"last_seen": (NOW + timedelta(minutes=5)).isoformat()}),
            ("assignment_available_again", {}),
        ]
        assert len({event.unique_key for event in events}) == 4


async def test_failed_submission_detail_does_not_block_other_baseline(sessions, settings):
    grade = 3
    second_detail_fails = True

    def handler(request):
        path = request.url.path
        if path.endswith("/students/submissions"):
            return httpx.Response(200, json=[{"assignment_id": 20, "user_id": 7, "score": 0}])
        if path.endswith("/assignments/10/submissions/self"):
            return httpx.Response(
                200, json={"assignment_id": 10, "user_id": 7, "score": grade, "grade": str(grade)}
            )
        assert path.endswith("/assignments/20/submissions/self")
        if second_detail_fails:
            return httpx.Response(404, json={})
        return httpx.Response(200, json={"assignment_id": 20, "user_id": 7, "score": 100, "grade": "100"})

    async with CanvasClient(settings, httpx.MockTransport(handler)) as client, sessions() as db, db.begin():
        client.user_id = "7"
        for score in [3, 4]:
            grade = score
            result = await submissions(client, "1", [{"id": 10}, {"id": 20}])
            scope = await apply_scope(db, settings, "submission", "1", result, user_id="7", now=NOW)
            assert not scope.baseline and scope.status == "resource_unavailable"
            assert await db.get(Resource, "submission:1:20") is None
        second_detail_fails = False
        result = await submissions(client, "1", [{"id": 10}, {"id": 20}])
        scope = await apply_scope(db, settings, "submission", "1", result, user_id="7", now=NOW)
        assert scope.baseline and scope.complete
        [event] = (await db.scalars(select(Event))).all()
        assert event.kind == "grade_changed" and event.resource_key == "submission:1:10"
        assert event.changes["grade"] == {
            "before": {"score": 3, "grade": "3"},
            "after": {"score": 4, "grade": "4"},
        }


async def test_first_reply_after_confirmed_empty_baseline(sessions, settings):
    has_reply = False
    reply_reads = 0

    def handler(request):
        nonlocal reply_reads
        path = request.url.path
        if path.endswith("/discussion_topics") and not request.url.params.get("only_announcements"):
            return httpx.Response(200, json=[{"id": 10}])
        if path.endswith("/discussion_topics/10/entries"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 11,
                        "user_id": 7,
                        "has_more_replies": False,
                        "recent_replies": [{"id": 12}] if has_reply else [],
                    }
                ],
            )
        if path.endswith("/entries/11/replies"):
            reply_reads += 1
            return httpx.Response(200, json=[{"id": 12, "user_id": 8, "message": "第一条回复"}])
        return httpx.Response(200, json=[])

    async with CanvasClient(settings, httpx.MockTransport(handler)) as client, sessions() as db, db.begin():
        for has_reply in (False, True):
            async for kind, suffix, result in content_scopes(client, "1"):
                if kind == "reply":
                    scope = await apply_scope(
                        db, settings, kind, "1", result, scope_suffix=suffix, user_id="7", now=NOW
                    )
                    assert scope.baseline
        [event] = (await db.scalars(select(Event))).all()
        assert event.kind == "reply_created" and event.resource_key == "reply:1:10:11:12"
        assert reply_reads == 1


async def test_snapshot_schema_upgrade_is_silent_but_later_diffs_notify(sessions, settings):
    assignment = {
        "id": 10,
        "description": '<p>x<sup>2</sup><a href="/files/2">资料</a></p>',
        "allowed_attempts": 3,
        "lock_explanation": "已锁定",
        "rubric": [{"id": 1, "points": 4}],
        "attachment": {"id": 9, "display_name": "参考", "size": 1},
    }
    comment = {"id": 2, "author_id": 9, "comment": "批注", "attachment": {"id": 4, "size": 1}}
    submission = {"assignment_id": 10, "submission_comments": [comment], "points_deducted": 1}
    message = {"id": 5, "author_id": 9, "body": '<a href="/files/2">资料</a>', "attachments": [{"id": 6}]}
    conversation = {"id": 3, "messages": [message]}
    async with sessions() as db, db.begin():

        async def apply(kind, item):
            return await apply_scope(
                db, settings, kind, "1", PageResult([item], 1, True), user_id="7", now=NOW
            )

        for kind, item, resource_id, legacy in [
            ("assignment", assignment, "10", {"id": "10", "description": "<p>x2资料</p>"}),
            (
                "submission",
                submission,
                "10",
                {
                    "assignment_id": "10",
                    "submission_comments": [
                        {"id": "2", "author_id": "9", "comment": "批注"},
                    ],
                },
            ),
            (
                "conversation",
                conversation,
                "3",
                {
                    "id": "3",
                    "messages": [
                        {"id": "5", "author_id": "9", "body": "资料"},
                    ],
                },
            ),
        ]:
            await apply(kind, item)
            resource = await db.get(Resource, f"{kind}:1:{resource_id}")
            # 旧版已能保存规范化 CTA，仅新加的正文信息缺失。
            if resource.data.get("html_url"):
                legacy["html_url"] = resource.data["html_url"]
            resource.data = legacy
            await apply(kind, item)
        assert not (await db.scalars(select(Event))).all()

        await apply("assignment", {**assignment, "allowed_attempts": 2})
        await apply(
            "submission",
            {
                **submission,
                "submission_comments": [
                    {**comment, "attachment": {"id": 4, "size": 2}},
                ],
            },
        )
        await apply("conversation", {**conversation, "messages": [{**message, "attachments": [{"id": 7}]}]})
        assert [event.kind for event in (await db.scalars(select(Event).order_by(Event.id))).all()] == [
            "assignment_content_changed",
            "submission_comment_changed",
            "conversation_message_added",
        ]
