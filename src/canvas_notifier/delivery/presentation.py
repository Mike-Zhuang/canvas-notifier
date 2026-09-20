"""邮件信息模型：保留 Canvas 通知要素，并补充本服务的时间、状态与审计信息。"""

import html
import re
from urllib.parse import quote, urljoin, urlsplit
from zoneinfo import ZoneInfo

import bleach

from canvas_notifier.delivery.formatting import format_changes
from canvas_notifier.domain.normalize import clean_email_html, safe_link
from canvas_notifier.domain.time import now_utc, parse_time


def resource_link(resource, origin):
    data = resource.data
    existing = safe_link(data.get("html_url"), origin)
    if existing:
        return existing
    cid, rid = resource.course_id, resource.external_id
    if resource.kind == "course" and rid.isdigit():
        return f"{origin}/courses/{rid}"
    if resource.kind == "conversation":
        return origin + "/conversations"
    if not cid.isdigit():
        return ""
    segment = {
        "assignment": "assignments",
        "file": "files",
        "announcement": "discussion_topics",
        "discussion": "discussion_topics",
    }.get(resource.kind)
    if segment and rid.isdigit():
        return f"{origin}/courses/{cid}/{segment}/{rid}"
    if resource.kind in ("module", "module_item"):
        return f"{origin}/courses/{cid}/modules"
    if resource.kind == "folder":
        return f"{origin}/courses/{cid}/files"
    if (
        resource.kind == "page"
        and data.get("url")
        and "/" not in data["url"]
        and data["url"] not in (".", "..")
    ):
        return f"{origin}/courses/{cid}/pages/{quote(data['url'], safe='')}"
    return f"{origin}/courses/{cid}"


def file_size(value):
    if value is None:
        return "大小未提供"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


def submission_status(data):
    if data.get("redo_request"):
        return "老师要求重新提交"
    if data.get("local_completed"):
        return "已在本服务标记完成（不代表已向 Canvas 提交）"
    if data.get("excused"):
        return "已豁免"
    if data.get("submitted_at"):
        return "已检测到在线提交"
    return "尚未确认完成，请以 Canvas 当前状态为准"


def present(payload, *, now=None):
    now = now or now_utc()
    p = dict(payload)
    zone = ZoneInfo(p.get("timezone", "Asia/Shanghai"))

    def timestamp(value):
        dt = parse_time(value)
        return dt.astimezone(zone).strftime("%Y-%m-%d %H:%M") if dt else "未设置"

    boundary = parse_time(p.get("boundary"))
    p["remaining"] = ""
    if boundary:
        seconds = int((boundary - now).total_seconds())
        hours, minutes = divmod(abs(seconds) // 60, 60)
        duration = (
            f"{hours // 24} 天 {hours % 24} 小时"
            if hours >= 24
            else f"{hours} 小时 {minutes} 分钟"
            if hours
            else f"{minutes} 分钟"
        )
        p["remaining"] = ("已超过 " if seconds < 0 else "剩余 ") + duration
    p["boundary_display"] = timestamp(p.get("boundary")) if boundary else ""
    p["last_synced_display"] = (
        timestamp(p.get("last_synced_at")) if p.get("last_synced_at") else "本地测试 / 运维通知"
    )
    p["observed_display"] = timestamp(p.get("observed_at") or now.isoformat())
    p["changes_text"] = format_changes(p.get("changes") or {}, p.get("timezone", "Asia/Shanghai"))
    origin = p.get("canvas_origin") or "https://" + (
        urlsplit(p.get("link") or "https://canvas.tongji.edu.cn").netloc
    )
    p["excerpt"] = clean_email_html(p.get("excerpt") or "")
    # 上游邮件会把课程正文中的相对链接补成绝对地址，邮箱中不能保留站内相对路径。
    p["excerpt"] = re.sub(
        r'href="([^"]*)"',
        lambda match: (
            'href="' + html.escape(urljoin(origin + "/", html.unescape(match[1])), quote=True) + '"'
        ),
        p["excerpt"],
    )
    p["excerpt_text"] = bleach.clean(p["excerpt"], tags=set(), strip=True)
    kind = p.get("kind", "")
    descriptions = {
        "assignment_created": "课程发布了一项新的作业或测验。请查看任务说明和适用时间。",
        "assignment_content_changed": "这项任务的内容发生了变化。请留意老师更新后的要求。",
        "assignment_dates_changed": "任务时间已经调整，提醒计划会按新的时间重新计算。",
        "grade_published": "这项任务的成绩已对你可见。",
        "grade_changed": "学生可见成绩发生了变化。",
        "announcement_created": "课程发布了一条新公告。",
        "announcement_changed": "这条课程公告已更新。",
        "file_created": "老师在课程中添加了新文件。",
        "file_changed": "课程文件的名称、位置或元数据发生了变化。",
        "submission_comment_added": "老师或其他可见作者添加了新的提交反馈。",
        "submission_comment_changed": "提交反馈已更新。",
        "submission_confirmed": "检测到你已经向 Canvas 提交了这项任务。",
        "resubmission_requested": "老师要求重新提交，请再次检查作业要求。",
        "due": "任务即将到达正式截止时间。",
        "lock": "任务即将停止接受提交，请留意可用期结束时间。",
        "unlock": "任务即将开放。",
        "personal": "这是你在本服务设置的个人计划时间。",
        "overdue": "任务已超过正式截止，最近同步尚未确认完成。",
        "test": "这是邮件模板与投递链路的测试，内容为合成示例，不代表真实课程更新。",
        "baseline_digest": "以下是首次同步后需要留意的未来任务。历史内容已归档。",
        "iam_recovered": "IAM 已重新确认本人身份，Canvas 会话恢复。",
        "iam_login_failed": "IAM 自动登录需要处理，请查看连接与健康页面。",
    }
    p["explanation"] = descriptions.get(kind, "检测到课程内容或服务状态发生以下变化。")
    if kind.startswith("grade_") and not p.get("score_in_email"):
        p["explanation"] += " 按当前隐私设置，邮件不展示具体分数。"
    p["facts"] = []
    for key, label in [
        ("due_at", "正式截止"),
        ("lock_at", "停止提交"),
        ("unlock_at", "开放时间"),
        ("personal_at", "个人计划"),
        ("submitted_at", "提交时间"),
    ]:
        if p.get(key):
            p["facts"].append((label, timestamp(p[key])))
    if p.get("is_assignment"):
        if not p.get("due_at"):
            p["facts"].append(("正式截止", "老师未设置"))
        p["facts"].append(("提交状态", submission_status(p)))
    if p.get("points_possible") is not None:
        p["facts"].append(("满分", str(p["points_possible"])))
    if p.get("file_size") is not None:
        p["facts"].append(("文件大小", file_size(p["file_size"])))
    if p.get("author"):
        p["facts"].append(("作者", p["author"]))
    p["attachments"] = [
        {**item, "size_display": file_size(item.get("size"))} for item in p.get("attachments", [])
    ]
    p["digest_items"] = [present(item, now=now) for item in p.get("digest_items", [])]
    p["preheader"] = " · ".join(
        v for v in (p.get("label"), p.get("course"), p.get("title"), p["remaining"]) if v
    )
    p["rule_text"] = p.get("rule_text") or "按当前事件通知规则发送"
    return p
