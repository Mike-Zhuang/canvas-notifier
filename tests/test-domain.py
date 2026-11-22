import json
from pathlib import Path

import pytest

from canvas_notifier.domain.mail_metadata import visible_url
from canvas_notifier.domain.normalize import (
    GradeVisibilityPolicy,
    changes_for,
    clean_email_html,
    complete,
    normalize,
    safe_link,
    sanitize_link,
)
from canvas_notifier.domain.time import parse_time
from canvas_notifier.scheduling.rules import Rules, duration, quiet_adjust

ORIGIN = "https://canvas.tongji.edu.cn"


def test_real_har_shapes():
    bundle = json.loads(Path("tests/fixtures/har-sanitized.json").read_text())
    quiz = [a for a in bundle["assignments"] if a["submission_types"] == ["online_quiz"]]
    upload = [a for a in bundle["assignments"] if a["submission_types"] == ["online_upload"]]
    assert len(quiz) == 49 and sum(a["due_at"] is None for a in quiz) == 48
    assert all(a["lock_at"] for a in quiz)
    assert len(upload) == 3 and all(a["due_at"] and not a["lock_at"] for a in upload)
    assert [len(page) for page in bundle["file_pages"]] == [20, 20, 20, 10]
    assert len(bundle["submissions"]) == 52 and len(bundle["sources"]) == 6
    detail = bundle["graded_detail"]
    assert parse_time(detail["posted_at"]) < parse_time(detail["graded_at"])
    assert GradeVisibilityPolicy.visible(detail)


def test_grade_zero_and_visibility():
    assert GradeVisibilityPolicy.visible({"score": 0, "grade": "0"}) == {"score": 0, "grade": "0"}
    assert GradeVisibilityPolicy.visible({"score": 99, "grade_hidden": True}) is None
    assert (
        GradeVisibilityPolicy.visible({"score": 99, "published_score": None, "published_grade": None}) is None
    )
    assert changes_for("submission", {}, {"score": 0})[0][0] == "grade_published"
    assert changes_for("submission", {"score": 0}, {"score": 1})[0][0] == "grade_changed"
    assert (
        changes_for(
            "submission", {"score": 1, "posted_at": None}, {"score": 1, "posted_at": "2026-01-01T00:00:00Z"}
        )
        == []
    )


def test_completion_is_personal_and_not_grade():
    online = {"submission_types": ["online_quiz"], "has_submitted_submissions": True}
    assert not complete(online, {"workflow_state": "graded", "score": 0}, {})
    assert complete(online, {"submitted_at": "2026-01-01T00:00:00Z"}, {})
    assert not complete(online, {"submitted_at": "2026-01-01T00:00:00Z", "redo_request": True}, {})
    assert not complete({"submission_types": ["on_paper"]}, {"workflow_state": "graded"}, {})
    assert complete({"submission_types": ["on_paper"]}, {}, {"completed": True})


def test_comments_and_rubric_independent():
    events = changes_for(
        "submission",
        {"score": 1},
        {
            "score": 1,
            "submission_comments": [
                {"id": "1", "author_id": "teacher", "comment": "new"},
                {"id": "2", "author_id": "self", "comment": "mine"},
            ],
            "rubric_assessment": {"criterion": {"points": 1}},
        },
        "self",
    )
    assert [e[0] for e in events] == ["submission_comment_added", "rubric_changed"]


@pytest.mark.parametrize("bad", ["P1M", "PT0S", "-PT1H", "P", "invalid"])
def test_invalid_offsets(bad):
    with pytest.raises(ValueError):
        duration(bad)


def test_offsets_timezone_and_quiet_boundary():
    rule = Rules(due=["PT24H", "P1D", "PT1H"], quiet_enabled=True)
    assert rule.due == ["PT86400S", "PT3600S"]
    midnight = parse_time("2026-09-19T00:30:00+08:00")
    boundary = parse_time("2026-09-19T02:00:00+08:00")
    assert quiet_adjust(midnight, boundary, rule) == midnight
    advance = rule.model_copy(update={"critical_policy": "advance"})
    assert quiet_adjust(midnight, boundary, advance) < midnight
    skip = rule.model_copy(update={"critical_policy": "digest_only"})
    assert quiet_adjust(midnight, boundary, skip) is None
    with pytest.raises(ValueError):
        parse_time("2026-09-19T02:00:00")
    assert parse_time("2026-07-01T12:00:00-04:00").hour == 16


def test_sanitization_and_link_secrets():
    data = normalize(
        "assignment",
        {
            "id": 1,
            "name": "normal",
            "description": '<img src="https://evil.test/a"><script>alert(1)</script><b>ok</b>',
            "html_url": "https://canvas.tongji.edu.cn/courses/1/assignments/1?access_token=secret",
            "due_at": None,
        },
        "https://canvas.tongji.edu.cn",
        "1",
    )
    assert "<img" not in data["description"] and "<script" not in data["description"]
    assert "?" not in data["html_url"]
    assert not safe_link("https://evil.test/courses/1", "https://canvas.tongji.edu.cn")


def test_canvas_links_keep_public_parameters_and_fragments():
    target = "/courses/1/files/2?verifier=secret&amp;download=1&amp;%58-Amz-Signature=secret#preview"
    raw = (
        f'<p>H<sub>2</sub>O x<sup>2</sup><s>旧</s><u>新</u></p><hr><a href="{target}">文件</a>'
        '<a href="https://external.test/file?X-Amz-Signature=secret">签名</a>'
        '<a href="https://external.test/file?tab=all">外链</a><a href="javascript:alert(1)">脚本</a>'
        '<img src="/equation_images/x" alt="x &lt; y"><img src="https://external.test/pixel">'
    )
    snapshot = normalize("assignment", {"id": 1, "description": raw}, ORIGIN, "1")
    link = f"{ORIGIN}/courses/1/files/2?download=1#preview"
    assert snapshot["links"] == [link]
    assert "<sub>2</sub>" in snapshot["description"] and "<sup>2</sup>" in snapshot["description"]
    cleaned = snapshot["_email"]["body"]
    assert f'href="{link}"' in cleaned and '<a href="https://external.test/file?tab=all">' in cleaned
    assert "<s>旧</s><u>新</u></p><hr>" in cleaned
    assert "x &lt; y" in cleaned and "[图片]" in cleaned
    assert "secret" not in cleaned and "javascript:" not in cleaned and "<img" not in cleaned
    assert sanitize_link("/files/2?state=secret;part=1#preview", ORIGIN) == f"{ORIGIN}/files/2?part=1#preview"
    assert sanitize_link("/files/2#access_token=secret", ORIGIN) == f"{ORIGIN}/files/2"
    assert not visible_url("https://external.test/avatar?Key-Pair-Id=signed", ORIGIN)
    assert clean_email_html('<a href="#preview">章节</a>', ORIGIN) == f'<a href="{ORIGIN}/#preview">章节</a>'


def test_conversation_author_priority_and_privacy():
    participant = {"id": 7, "display_name": "参与者"}
    message = {"id": 8, "author_id": 7, "author": {"display_name": "直接作者"}, "user_name": "备用作者"}
    for extra, participants, expected in (
        ({}, [participant], "直接作者"),
        ({"author": {}}, [participant], "参与者"),
        ({"author": {}}, [], "备用作者"),
        ({"can_read_author": False}, [participant], ""),
        ({"author": {"anonymous": True}}, [participant], ""),
    ):
        raw = {"id": 1, "participants": participants, "messages": [{**message, **extra}]}
        assert normalize("conversation", raw, ORIGIN)["_email"]["messages"]["8"]["author_name"] == expected


def test_content_fields_are_safe_business_data():
    attachment = {
        "id": 5,
        "display_name": "答题纸",
        "size": 10,
        "url": "https://external.test/?signature=secret",
    }
    summary = {"id": 5, "display_name": "答题纸", "size": 10}
    rubric = {
        "id": "criterion",
        "description": "说明",
        "long_description": "长说明",
        "points": 2,
        "ratings": [{"id": "full", "description": "完成", "points": 2, "extra": "ignored"}],
    }
    assignment = normalize(
        "assignment",
        {
            "id": 1,
            "allowed_attempts": 2,
            "lock_explanation": "已锁定",
            "rubric": [rubric],
            "description": "",
            "attachment": attachment,
        },
        ORIGIN,
    )
    assert assignment["attachments"] == [summary]
    assert assignment["allowed_attempts"] == 2 and assignment["lock_explanation"] == "已锁定"
    assert assignment["rubric"] == [
        {**rubric, "ratings": [{"id": "full", "description": "完成", "points": 2}]}
    ]

    conversation = normalize(
        "conversation",
        {
            "id": 1,
            "participants": [{"id": 7, "display_name": "老师"}],
            "messages": [
                {"id": 8, "author_id": 7, "body": '<a href="/files/2">附件</a>', "attachments": [attachment]}
            ],
        },
        ORIGIN,
    )
    assert conversation["messages"][0]["author_id"] == "7"
    assert conversation["messages"][0]["attachments"] == [summary]
    assert conversation["messages"][0]["links"] == [f"{ORIGIN}/files/2"]
    assert conversation["_email"]["messages"]["8"]["author_name"] == "老师"

    submission = normalize(
        "submission",
        {
            "assignment_id": 1,
            "submission_comments": [
                {
                    "id": 2,
                    "author_id": 7,
                    "comment": '<img src="/files/4" alt="作业批注">',
                    "attachments": [attachment],
                    "media_comment": {
                        "media_type": "audio",
                        "display_name": "录音",
                        "url": "https://external.test/?token=secret",
                    },
                }
            ],
        },
        ORIGIN,
    )
    comment = submission["submission_comments"][0]
    assert comment["attachments"] == [summary]
    assert comment["media_comment"] == {"media_type": "audio", "display_name": "录音"}
    assert submission["_email"]["comments"]["2"]["content_images"][0]["file_id"] == "4"
    assert "secret" not in json.dumps([assignment, conversation, submission])


@pytest.mark.parametrize("kind", ["announcement", "discussion", "reply"])
def test_anonymous_author_names_not_exposed(kind):
    raw = {"id": 1, "author": {"display_name": "老师"}, "user_name": "其他名字"}
    assert normalize(kind, raw, ORIGIN)["_email"]["author_name"] == "老师"
    for private in ({"anonymous": True}, {"anonymous_state": "full"}, {"can_read_author": False}):
        assert normalize(kind, {**raw, **private}, ORIGIN)["_email"]["author_name"] == ""


def test_calendar_and_planner_note_fields():
    start, end = "2026-10-01T01:00:00Z", "2026-10-01T02:00:00Z"
    calendar = normalize(
        "calendar",
        {
            "id": 1,
            "start_at": start,
            "end_at": end,
            "location_name": "教学楼",
            "location_address": "第一教室",
        },
        ORIGIN,
    )
    assert (calendar["start_at"], calendar["end_at"]) == (
        parse_time(start).isoformat(),
        parse_time(end).isoformat(),
    )
    assert (calendar["location_name"], calendar["location_address"]) == ("教学楼", "第一教室")
    note = normalize(
        "planner_note", {"id": 1, "todo_date": start, "details": '<a href="/calendar">日历</a>'}, ORIGIN
    )
    assert note["todo_date"] == parse_time(start).isoformat() and note["details"] == "日历"
    assert note["links"] == [f"{ORIGIN}/calendar"]


def test_authoritative_assignment_dates_win_over_submission_cache():
    from types import SimpleNamespace

    from canvas_notifier.scheduling.planner import calculate_schedule

    now = parse_time("2026-10-01T00:00:00Z")
    task = SimpleNamespace(
        key="assignment:1:2",
        availability="visible",
        local={},
        data={"due_at": "2026-10-02T00:00:00Z", "submission_types": ["online_upload"]},
    )
    schedule = calculate_schedule(task, {"cached_due_date": None}, Rules(due=["PT1H"], lock=[]), now)
    assert len(schedule) == 1
    assert next(iter(schedule.values()))[1] == parse_time("2026-10-02T00:00:00Z")
    task.data["due_at"] = None
    assert not calculate_schedule(task, {"cached_due_date": "2026-10-02T00:00:00Z"}, Rules(), now)
    del task.data["due_at"]
    assert calculate_schedule(task, {"cached_due_date": "2026-10-02T00:00:00Z"}, Rules(), now)
