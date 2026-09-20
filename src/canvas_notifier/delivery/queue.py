import re

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import select

from canvas_notifier.db import Delivery, Resource
from canvas_notifier.delivery.presentation import resource_link
from canvas_notifier.domain.normalize import fingerprint
from canvas_notifier.domain.time import now_utc
from canvas_notifier.scheduling.rules import digest_time, effective_rules, quiet_adjust

LABELS = {
    "assignment_created": "新任务",
    "assignment_content_changed": "任务内容变化",
    "assignment_dates_changed": "任务时间变化",
    "grade_published": "成绩发布",
    "grade_changed": "成绩变化",
    "submission_comment_added": "新的教师反馈",
    "submission_comment_changed": "教师反馈更新",
    "rubric_changed": "评分量表更新",
    "submission_confirmed": "检测到在线提交",
    "resubmission_requested": "需要重新提交",
    "announcement_created": "新公告",
    "announcement_changed": "公告更新",
    "file_created": "新资料",
    "file_changed": "资料更新",
    "due": "正式截止提醒",
    "lock": "停止提交提醒",
    "unlock": "任务开放提醒",
    "personal": "个人计划提醒",
    "overdue": "已过正式截止",
    "test": "通知测试",
    "sync_interrupted": "同步中断",
    "sync_recovered": "同步恢复",
    "auth_degraded": "认证降级",
    "iam_recovered": "IAM 已恢复登录",
    "iam_login_failed": "IAM 登录需要处理",
    "baseline_digest": "首次同步未来任务摘要",
    "course_created": "新课程",
    "course_changed": "课程更新",
    "folder_created": "新资料目录",
    "folder_changed": "资料目录更新",
    "conversation_created": "新站内信",
    "conversation_changed": "站内信更新",
    "conversation_message_added": "收到站内信回复",
    "discussion_created": "新讨论",
    "discussion_changed": "讨论更新",
    "reply_created": "新讨论回复",
    "reply_changed": "讨论回复更新",
    "module_created": "新模块",
    "module_changed": "模块更新",
    "module_item_created": "新模块内容",
    "module_item_changed": "模块内容更新",
    "page_created": "新课程页面",
    "page_changed": "课程页面更新",
    "calendar_created": "新日程",
    "calendar_changed": "日程更新",
    "planner_created": "新学习计划",
    "planner_changed": "学习计划更新",
    "planner_note_created": "新个人待办",
    "planner_note_changed": "个人待办更新",
}


def recipients(value: str) -> list[str]:
    result = []
    for address in value.split(","):
        if not address.strip():
            continue
        try:
            result.append(
                validate_email(address.strip(), check_deliverability=False, test_environment=True).normalized
            )
        except EmailNotValidError:
            raise ValueError("Invalid recipient address") from None
    return sorted(set(result))


async def enqueue(
    session,
    settings,
    source_key,
    resource,
    kind,
    changes=None,
    *,
    boundary=None,
    reminder_keys=None,
    force_recipient=None,
    now=None,
    mode_override=None,
):
    now = now or now_utc()
    rule_key = resource.key if resource else ""
    if resource and resource.kind == "submission":
        rule_key = f"assignment:{resource.course_id}:{resource.data.get('assignment_id')}"
    rule, sources = await effective_rules(session, resource.course_id if resource else "", rule_key)
    mode = mode_override or rule.events.get(kind, rule.default_event)
    if kind != "test" and (not rule.enabled or mode == "off"):
        return "suppressed_by_rule"
    targets = recipients(
        force_recipient
        if force_recipient is not None
        else ",".join(rule.recipients)
        if rule.recipients is not None
        else settings.mail_to
    )
    if not targets:
        return "no_recipient"
    data = resource.data if resource else {}
    course = (
        await session.get(Resource, f"course::{resource.course_id}")
        if resource and resource.course_id
        else None
    )
    title = (
        data.get("name")
        or data.get("title")
        or data.get("subject")
        or data.get("display_name")
        or "Canvas 更新"
    )
    assignment = resource if resource and resource.kind == "assignment" else None
    if resource and resource.kind == "submission":
        assignment = await session.get(
            Resource, f"assignment:{resource.course_id}:{data.get('assignment_id')}"
        )
        if assignment:
            title, data = (
                assignment.data.get("name", title),
                {**data, "html_url": assignment.data.get("html_url", "")},
            )
    link = resource_link(assignment or resource, settings.canvas_base_url) if resource else ""
    submission = resource if resource and resource.kind == "submission" else None
    if assignment and submission is None:
        submission = await session.get(
            Resource, f"submission:{assignment.course_id}:{assignment.external_id}"
        )
    task_data = assignment.data if assignment else {}
    submission_data = submission.data if submission else {}
    feedback = (changes or {}).get("comment", {})
    email_data = data.get("_email", {})
    comment_data = email_data.get("comments", {}).get(str(feedback.get("id")), {})
    attachments = [
        *email_data.get("attachments", data.get("attachments", [])),
        *comment_data.get("attachments", feedback.get("attachments", [])),
    ]
    attachments = [
        {
            "name": a.get("display_name") or "附件",
            "size": a.get("size"),
            "link": f"{settings.canvas_base_url}/courses/{resource.course_id}/files/{a['id']}"
            if resource and resource.course_id.isdigit() and str(a.get("id", "")).isdigit()
            else "",
        }
        for a in attachments
    ]
    rule_text = "仅提醒未确认完成的任务" if rule.only_incomplete else "按配置持续提醒"
    if reminder_keys:
        rule_text += "；" + LABELS.get(kind, kind)
    else:
        rule_text = "每日摘要" if mode == "digest" else "即时事件通知"
    payload = {
        "kind": kind,
        "label": LABELS.get(kind, kind.replace("_", " ")),
        "title": re.sub(r"[\r\n]+", " ", title)[:200],
        "course": course.data.get("name", "") if course else "",
        "link": link,
        "observed_at": now.isoformat(),
        "is_assignment": assignment is not None,
        "due_at": task_data.get("due_at"),
        "lock_at": task_data.get("lock_at"),
        "unlock_at": task_data.get("unlock_at"),
        "personal_at": assignment.local.get("personal_at") if assignment else None,
        "local_completed": assignment.local.get("completed", False) if assignment else False,
        "submitted_at": submission_data.get("submitted_at"),
        "excused": submission_data.get("excused", False),
        "redo_request": submission_data.get("redo_request", False),
        "points_possible": task_data.get("points_possible"),
        "file_size": data.get("size") if resource and resource.kind == "file" else None,
        "author": comment_data.get("author_name") or feedback.get("author_name"),
        "attachments": attachments,
        "rule_text": rule_text,
        "last_synced_at": resource.last_seen.isoformat() if resource else None,
        "boundary": boundary.isoformat() if boundary else None,
        "timezone": rule.timezone,
        "no_due": kind == "lock" and not data.get("due_at"),
        "changes": changes or {},
        "score_in_email": rule.score_in_email,
        "sources": sources,
        "reminder_keys": reminder_keys or [],
        "excerpt": comment_data.get("body")
        or feedback.get("comment")
        or email_data.get("body")
        or data.get("message")
        or data.get("description")
        or data.get("body")
        or "",
        "manage_url": settings.public_base_url.rstrip("/") + "/rules",
    }
    # 默认分数不进入邮件载荷；保留私有事件审计中的原始差异。
    if not rule.score_in_email and (kind.startswith("grade_") or kind == "rubric_changed"):
        payload["changes"] = {}
    available = (
        now
        if kind == "test"
        else digest_time(now, rule)
        if mode == "digest"
        else quiet_adjust(now, boundary, rule)
    )
    if available is None:
        return "suppressed_quiet_hours"
    payload["digest"] = mode == "digest"
    for recipient in targets:
        unique = source_key + ":" + fingerprint(recipient)[:16]
        existing = await session.scalar(select(Delivery).where(Delivery.unique_key == unique))
        if existing:
            if existing.status == "cancelled" and reminder_keys:
                existing.status, existing.reason = "pending", "提醒条件恢复"
                existing.payload, existing.available_at = payload, available
            continue
        session.add(
            Delivery(
                unique_key=unique,
                resource_key=resource.key if resource else "",
                recipient=recipient,
                payload=payload,
                available_at=available,
                message_id=f"<{fingerprint(unique)}@canvas-notifier.local>",
            )
        )
    return "queued"
