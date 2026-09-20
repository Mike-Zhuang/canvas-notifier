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
