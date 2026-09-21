from datetime import timedelta
from types import SimpleNamespace

from canvas_notifier.delivery.presentation import present
from canvas_notifier.delivery.smtp import render_message
from canvas_notifier.domain.normalize import clean_email_html
from canvas_notifier.domain.time import now_utc


def payload(**values):
    return {
        "kind": "announcement_created",
        "label": "新公告",
        "title": "合成公告",
        "course": "示例课程",
        "timezone": "Asia/Shanghai",
        "last_synced_at": now_utc().isoformat(),
        "manage_url": "https://canvas.example.org/rules",
        "link": "https://canvas.tongji.edu.cn/courses/1/discussion_topics/2",
        "excerpt": "<p>完整正文</p><table><tr><td>表格内容</td></tr></table>",
        "attachments": [
            {"name": "课程资料.pdf", "size": 2048, "link": "https://canvas.tongji.edu.cn/courses/1/files/3"}
        ],
        **values,
    }


def test_canvas_parity_and_extra_information(settings):
    p = payload(author="示例教师", observed_at=now_utc().isoformat(), rule_text="即时事件通知")
    message = render_message(
        settings,
        SimpleNamespace(payload=p, recipient="student@example.test", message_id="<test@example.test>"),
    )
    html = message.get_body(preferencelist=("html",)).get_content()
    text = message.get_body(preferencelist=("plain",)).get_content()
    for value in (
        "示例课程",
        "合成公告",
        "完整正文",
        "表格内容",
        "课程资料.pdf",
        "2.0 KB",
        "示例教师",
        "最近成功同步",
        "本次规则",
        "管理提醒",
    ):
        assert value in html and value in text
    assert "<table>" in html and "<img" not in html


def test_deadline_details_and_grade_privacy(settings):
    now = now_utc()
    p = present(
        payload(
            kind="lock",
            label="停止提交提醒",
            boundary=(now + timedelta(hours=2)).isoformat(),
            no_due=True,
            is_assignment=True,
        ),
        now=now,
    )
    assert "2 小时" in p["remaining"] and ("正式截止", "老师未设置") in p["facts"]
    grade = present(payload(kind="grade_published", score_in_email=False))
    assert "不展示具体分数" in grade["explanation"]


def test_mail_html_drops_tracking_images_and_credential_links():
    html = clean_email_html(
        '<script>x()</script><img src="https://evil.test/pixel"><a href="javascript:alert(1)">bad</a><a href="https://canvas.tongji.edu.cn/files/1?verifier=secret">private</a><a href="https://example.org/course">normal</a>'
    )
    assert "<script" not in html and "<img" not in html and "javascript:" not in html and "secret" not in html
    assert 'href="https://example.org/course"' in html


def test_relative_canvas_body_links_are_absolute():
    rendered = present(payload(excerpt='<p><a href="/courses/1/files/3">课程资料</a></p>'))
    assert 'href="https://canvas.tongji.edu.cn/courses/1/files/3"' in rendered["excerpt"]


async def test_file_source_times_are_distinct_from_discovery(settings, sessions):
    from sqlalchemy import select

    from canvas_notifier.db import Delivery, Resource
    from canvas_notifier.delivery.queue import enqueue
    from canvas_notifier.domain.normalize import normalize
    from canvas_notifier.domain.time import parse_time

    discovered = parse_time("2030-01-02T10:00:00Z")
    raw = {
        "id": "11",
        "display_name": "Synthetic file.pdf",
        "size": 1024,
        "created_at": "2030-01-01T05:29:00Z",
        "modified_at": "2030-01-01T06:30:00Z",
        "updated_at": "2030-01-01T07:00:00Z",
    }
    async with sessions() as db, db.begin():
        item = Resource(
            key="file:1:11",
            kind="file",
            external_id="11",
            course_id="1",
            scope="file:1:",
            data=normalize("file", raw, settings.canvas_base_url, "1"),
            version=1,
            first_seen=discovered,
            last_seen=discovered,
            local={},
        )
        db.add(item)
        await enqueue(db, settings, "file-times", item, "file_created", now=discovered)
        row = await db.scalar(select(Delivery))
        view = present(row.payload, now=discovered)
        assert ("上传/创建时间", "2030-01-01 13:29") in view["facts"]
        assert ("文件修改时间", "2030-01-01 14:30") in view["facts"]
        assert ("文件信息更新", "2030-01-01 15:00") in view["facts"]
        assert view["observed_display"] == "2030-01-02 18:00"
        assert not row.payload["digest"] and row.available_at == discovered


async def test_visible_feedback_and_media_reach_both_email_parts(settings, sessions):
    from sqlalchemy import select

    from canvas_notifier.db import Delivery, Resource
    from canvas_notifier.delivery.queue import enqueue
    from canvas_notifier.domain.normalize import normalize

    now = now_utc()
    comment = {
        "id": "3",
        "author_id": "7",
        "author_name": "示例教师",
        "author": {
            "id": "7",
            "email": "teacher@example.edu",
            "html_url": "https://canvas.tongji.edu.cn/courses/1/users/7",
            "avatar_image_url": "https://canvas.tongji.edu.cn/images/messages/avatar-50.png",
        },
        "comment": "<p>请听音频说明</p>",
        "created_at": "2030-01-01T01:00:00Z",
        "edited_at": "2030-01-01T02:00:00Z",
        "media_comment": {
            "media_type": "audio",
            "display_name": "音频反馈",
            "url": "https://example.edu/media?token=do-not-save",
        },
        "attachments": [{"id": "12", "display_name": "反馈附件.pdf", "size": 2048}],
    }
    raw = {"id": "9", "assignment_id": "2", "user_id": "99", "submission_comments": [comment]}
    async with sessions() as db, db.begin():
        task = Resource(
            key="assignment:1:2",
            kind="assignment",
            external_id="2",
            course_id="1",
            scope="assignment:1:",
            data={"name": "示例任务", "points_possible": 100},
            first_seen=now,
            last_seen=now,
            local={},
        )
        submission = Resource(
            key="submission:1:2",
            kind="submission",
            external_id="2",
            course_id="1",
            scope="submission:1:",
            data=normalize("submission", raw, settings.canvas_base_url, "1"),
            first_seen=now,
            last_seen=now,
            local={},
        )
        db.add_all([task, submission])
        await enqueue(
            db,
            settings,
            "feedback-meta",
            submission,
            "submission_comment_added",
            {"comment": submission.data["submission_comments"][0]},
        )
        row = await db.scalar(select(Delivery))
        message = render_message(settings, row)
        html = message.get_body(preferencelist=("html",)).get_content()
        text = message.get_body(preferencelist=("plain",)).get_content()
        for value in (
            "teacher@example.edu",
            "示例教师",
            "音频",
            "反馈附件.pdf",
            "/assignments/2/submissions/99",
            "反馈创建时间",
            "反馈编辑时间",
        ):
            assert value in html and value in text
        assert "do-not-save" not in str(submission.data) and "do-not-save" not in html
        assert "<img" not in html


def test_hidden_author_and_temporary_avatar_links_are_not_exposed():
    from canvas_notifier.domain.mail_metadata import feedback_metadata, source_time

    hidden = feedback_metadata(
        {
            "can_read_author": False,
            "author_name": "Hidden",
            "author": {"email": "hidden@example.edu", "html_url": "https://example.edu/user"},
        },
        "https://canvas.tongji.edu.cn",
    )
    assert not hidden["author_name"] and not hidden["author_email"] and not hidden["author_profile_url"]
    unsafe = feedback_metadata(
        {
            "author": {
                "avatar_image_url": "https://example.edu/avatar?X-Amz-Signature=private",
                "email": "bad\r\nBcc:bad@example.edu",
            }
        },
        "https://canvas.tongji.edu.cn",
    )
    assert not unsafe["author_avatar_url"] and not unsafe["author_email"]
    assert source_time(123456) is None


def test_official_like_layout_retains_details_and_client_fallbacks(settings):
    message = render_message(
        settings,
        SimpleNamespace(
            payload=payload(), recipient="student@example.test", message_id="<layout@example.test>"
        ),
    )
    html = message.get_body(preferencelist=("html",)).get_content()
    assert "max-width:600px" in html and "[if mso]" in html and 'width="600"' in html
    assert ".ExternalClass" in html and "max-width:620px" in html and "16px !important" in html
    assert 'dir="auto"' in html and "background:#ffffff" in html
    assert "#883253" not in html and "border-radius" not in html
    assert "系统发现时间" in html and "邮件生成时间" in html and "本次规则" in html


def test_plain_text_keeps_body_links_and_table_rows():
    rendered = present(
        payload(
            excerpt='<p>说明</p><table><tr><td>材料</td><td>要求</td></tr><tr><td>报告</td><td>PDF</td></tr></table><p><a href="/courses/1/files/3">下载</a></p>'
        )
    )
    assert "https://canvas.tongji.edu.cn/courses/1/files/3" in rendered["excerpt_text"]
    assert "材料 | 要求\n" in rendered["excerpt_text"]
    assert "\n | 报告 | PDF" in rendered["excerpt_text"]


def test_missing_source_time_is_never_filled_with_detection_time():
    now = now_utc()
    view = present(payload(kind="file_created", observed_at=now.isoformat()))
    assert ("上传/创建时间", "Canvas 未提供有效时间") in view["facts"]


def test_feedback_does_not_confuse_grade_post_time_with_comment_time():
    view = present(
        payload(
            kind="submission_comment_added",
            resource_kind="submission",
            source_published_at="2030-01-01T00:00:00Z",
            feedback_created_at="2030-01-02T00:00:00Z",
        )
    )
    assert not any(label in ("Canvas 发布时间", "成绩记录发布时间") for label, _ in view["facts"])
    assert ("反馈创建时间", "2030-01-02 08:00") in view["facts"]
