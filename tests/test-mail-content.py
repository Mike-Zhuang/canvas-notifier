from sqlalchemy import select

from canvas_notifier.db import Delivery, Resource
from canvas_notifier.delivery.batching import batch_payload
from canvas_notifier.delivery.presentation import present
from canvas_notifier.delivery.queue import enqueue
from canvas_notifier.delivery.smtp import render_message
from canvas_notifier.domain.normalize import changes_for, normalize
from canvas_notifier.domain.time import parse_time

NOW = parse_time("2030-01-02T05:06:00Z")
UPLOAD_NOTE = "系统发现时间不等于老师上传或修改时间。"
SUBMISSION_NOTE = "提交状态以最近同步为准。"
FOOTER = "本服务不代表学校官方通知，回复不会发给老师。"


def resource(settings, kind, raw):
    external_id = str(raw["assignment_id"] if kind == "submission" else raw["id"])
    return Resource(
        key=f"{kind}:1:{external_id}",
        kind=kind,
        external_id=external_id,
        course_id="1",
        scope=f"{kind}:1:",
        data=normalize(kind, raw, settings.canvas_base_url, "1"),
        first_seen=NOW,
        last_seen=NOW,
        local={},
    )


async def queued(db, settings, item, kind, changes=None):
    assert await enqueue(db, settings, kind, item, kind, changes, now=NOW) == "queued"
    return await db.scalar(select(Delivery).order_by(Delivery.id.desc()).limit(1))


def bodies(message):
    return [message.get_body(preferencelist=(kind,)).get_content() for kind in ("html", "plain")]


async def test_visible_grade_fact_matches_diff_and_stays_with_batch_item(settings, sessions):
    task = resource(settings, "assignment", {"id": 2, "name": "示例作业", "points_possible": 100})
    submission = resource(
        settings,
        "submission",
        {
            "id": 3,
            "assignment_id": 2,
            "user_id": 4,
            "score": 15,
            "grade": "不可外发",
            "published_score": 74.125,
            "published_grade": "B+",
        },
    )
    document = resource(settings, "file", {"id": 5, "display_name": "参考资料.pdf"})
    async with sessions() as db, db.begin():
        db.add_all([task, submission, document])
        await db.flush()
        changes = dict(changes_for("submission", {}, submission.data))["grade_published"]
        grade = await queued(db, settings, submission, "grade_published", changes)
        assert {key: grade.payload[key] for key in ("score", "grade")} == changes["grade"]["after"]
        view = present(grade.payload)
        assert ("得分", "74.125 / 100") in view["facts"]
        assert ("成绩", "B+") in view["facts"] and "分数：74.125" in view["changes_text"]
        file = await queued(db, settings, document, "file_created")
        batch = batch_payload([grade, file])
        assert "score" not in batch and "grade" not in batch
        assert batch["digest_items"][0]["score"] == 74.125
        for body in bodies(render_message(settings, grade, payload=batch)):
            assert body.count("74.125 / 100") == 1
            assert body.count(UPLOAD_NOTE) == body.count(SUBMISSION_NOTE) == body.count(FOOTER) == 1
            assert "不可外发" not in body

        submission.data = {**submission.data, "grade_hidden": True}
        hidden = await queued(db, settings, task, "assignment_content_changed")
        assert hidden.payload["score"] is None and hidden.payload["grade"] is None
        assert not ({"得分", "成绩"} & {label for label, _ in present(hidden.payload)["facts"]})
        for body in bodies(render_message(settings, hidden)):
            assert "不可外发" not in body and "74.125" not in body and "B+" not in body
            assert SUBMISSION_NOTE in body and UPLOAD_NOTE not in body
        for body in bodies(render_message(settings, file)):
            assert UPLOAD_NOTE in body and SUBMISSION_NOTE not in body and body.count(FOOTER) == 1


async def test_assignment_and_submission_events_explain_status_and_reminders(settings, sessions):
    task = resource(settings, "assignment", {"id": 2, "name": "示例作业"})
    submission = resource(settings, "submission", {"id": 3, "assignment_id": 2, "user_id": 4})
    async with sessions() as db, db.begin():
        db.add_all([task, submission])
        await db.flush()
        status = await queued(
            db,
            settings,
            submission,
            "submission_status_changed",
            {
                "missing": {"before": False, "after": True},
                "late": {"before": True, "after": False},
                "excused": {"before": False, "after": True},
                "points_deducted": {"before": 0, "after": 2.5},
            },
        )
        unavailable = await queued(
            db, settings, task, "assignment_unavailable", {"last_seen": NOW.isoformat()}
        )
        available = await queued(db, settings, task, "assignment_available_again")
        for body in bodies(render_message(settings, status)):
            assert all(
                value in body
                for value in (
                    "提交状态变化",
                    "缺交：否 → 是",
                    "迟交：是 → 否",
                    "豁免：否 → 是",
                    "扣分：0 → 2.5",
                    "查看提交与状态",
                )
            )
        for body in bodies(render_message(settings, unavailable)):
            assert all(
                value in body
                for value in (
                    "作业已不可见",
                    "老师可能撤回了发布或删除了作业",
                    "相关提醒已取消",
                    "最后一次看到作业：2030-01-02 13:06",
                    "查看课程",
                )
            )
        for body in bodies(render_message(settings, available)):
            assert "作业重新可见" in body and "提醒已按当前规则恢复" in body and "查看作业" in body


async def test_announcement_preserves_title_author_and_plain_before_after(settings, sessions):
    title = "这是一条较长的公告标题" * 25 + "标题结尾"
    raw = {
        "id": 2,
        "title": title,
        "message": "<p><strong>新正文</strong><br>补充一句</p>",
        "author": {"display_name": "可见教师"},
    }
    item = resource(settings, "announcement", raw)
    before = normalize("announcement", {**raw, "message": "<p>旧正文</p>"}, settings.canvas_base_url, "1")
    changes = dict(changes_for("announcement", before, item.data))["announcement_changed"]
    async with sessions() as db, db.begin():
        row = await queued(db, settings, item, "announcement_changed", changes)
        message = render_message(settings, row)
        for body in bodies(message):
            assert title in body and "可见教师" in body
            assert "修改前：旧正文\n修改后：新正文\n补充一句" in body
            assert UPLOAD_NOTE not in body and SUBMISSION_NOTE not in body and body.count(FOOTER) == 1
        assert len(str(message["Subject"])) <= 220 and "标题结尾" not in message["Subject"]


async def test_calendar_planner_and_rubric_show_new_item_details(settings, sessions):
    calendar = resource(
        settings,
        "calendar",
        {
            "id": 2,
            "title": "课堂日程",
            "start_at": "2030-01-03T00:00:00Z",
            "end_at": "2030-01-03T01:30:00Z",
            "location_name": "教学楼",
            "location_address": "一层报告厅",
        },
    )
    note = resource(
        settings,
        "planner_note",
        {"id": 3, "title": "个人待办", "todo_date": "2030-01-04T00:00:00Z", "details": "<p>带好课堂材料</p>"},
    )
    task = resource(
        settings,
        "assignment",
        {
            "id": 4,
            "name": "评分作业",
            "allowed_attempts": -1,
            "lock_explanation": "<p>等老师解锁</p>",
            "rubric": [
                {
                    "id": "criterion-id",
                    "description": "论证清晰度",
                    "points": 10,
                    "ratings": [{"id": "developing-id", "description": "有待改进", "points": 5}],
                }
            ],
        },
    )
    submission = resource(settings, "submission", {"id": 5, "assignment_id": 4, "user_id": 6})
    async with sessions() as db, db.begin():
        db.add_all([task, submission])
        await db.flush()
        for item, kind, expected in (
            (
                calendar,
                "calendar_created",
                ("开始时间", "2030-01-03 08:00", "结束时间", "09:30", "教学楼", "一层报告厅"),
            ),
            (note, "planner_note_created", ("待办日期", "2030-01-04 08:00", "带好课堂材料")),
            (task, "assignment_created", ("允许尝试次数", "不限", "锁定说明", "等老师解锁")),
        ):
            row = await queued(db, settings, item, kind)
            for body in bodies(render_message(settings, row)):
                assert all(value in body for value in expected)
        rubric = await queued(
            db,
            settings,
            submission,
            "rubric_changed",
            {
                "rubric_assessment": {
                    "before": None,
                    "after": {
                        "criterion-id": {"points": 5, "rating_id": "developing-id", "comments": "补充例子"}
                    },
                }
            },
        )
        for body in bodies(render_message(settings, rubric)):
            assert "论证清晰度：5 / 10 分；有待改进；补充例子" in body
            assert "criterion-id" not in body and "developing-id" not in body


async def test_reply_conversation_and_anonymous_discussion_authors(settings, sessions):
    reply = resource(settings, "reply", {"id": 2, "message": "讨论回复正文", "user_name": "可见回复者"})
    discussion = resource(
        settings,
        "discussion",
        {"id": 3, "title": "匿名讨论", "anonymous": True, "author": {"display_name": "不公开的作者"}},
    )
    conversation = resource(
        settings,
        "conversation",
        {
            "id": 4,
            "subject": "站内信标题",
            "participants": [{"id": 5, "display_name": "站内信作者"}],
            "messages": [{"id": 6, "author_id": 5, "body": "<p>站内信正文</p>"}],
        },
    )
    async with sessions() as db, db.begin():
        reply_row = await queued(db, settings, reply, "reply_created")
        anonymous_row = await queued(db, settings, discussion, "discussion_created")
        changes = dict(changes_for("conversation", {}, conversation.data))["conversation_message_added"]
        conversation_row = await queued(db, settings, conversation, "conversation_message_added", changes)
        for body in bodies(render_message(settings, reply_row)):
            assert "可见回复者" in body and "讨论回复正文" in body
        for body in bodies(render_message(settings, anonymous_row)):
            assert "匿名讨论" in body and "不公开的作者" not in body
        for body in bodies(render_message(settings, conversation_row)):
            assert "站内信作者" in body and "站内信正文" in body


async def test_image_only_feedback_uses_canvas_image_pipeline(settings, sessions):
    origin = settings.canvas_base_url
    image_url = origin + "/courses/1/files/9"
    item = resource(
        settings,
        "submission",
        {
            "id": 3,
            "assignment_id": 2,
            "user_id": 4,
            "submission_comments": [
                {"id": 5, "comment": f'<img src="{image_url}?verifier=hidden" alt="批注图片">'}
            ],
        },
    )
    async with sessions() as db, db.begin():
        changes = dict(changes_for("submission", {}, item.data))["submission_comment_added"]
        row = await queued(db, settings, item, "submission_comment_added", changes)
        png = b"\x89PNG\r\n\x1a\nfeedback"
        message = render_message(settings, row, inline_images={image_url: (png, "png")})
        html, plain = bodies(message)
        assert 'src="cid:' in html and image_url in plain and "批注图片" in plain
        assert "verifier" not in message.as_string()
        assert [
            part.get_payload(decode=True) for part in message.walk() if part.get_content_type() == "image/png"
        ] == [png]
        for body in bodies(render_message(settings, row)):
            assert "未能内嵌" in body and "批注图片" in body and image_url in body
