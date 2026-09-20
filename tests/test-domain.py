import json
from pathlib import Path

import pytest

from canvas_notifier.domain.normalize import (
    GradeVisibilityPolicy,
    changes_for,
    complete,
    normalize,
    safe_link,
)
from canvas_notifier.domain.time import parse_time
from canvas_notifier.scheduling.rules import Rules, duration, quiet_adjust


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
