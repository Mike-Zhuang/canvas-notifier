import hashlib
import json
import re
from urllib.parse import urlsplit

import bleach

from canvas_notifier.domain.mail_metadata import feedback_metadata, source_time
from canvas_notifier.domain.time import parse_time

COMMON = {
    "id",
    "name",
    "title",
    "description",
    "body",
    "message",
    "workflow_state",
    "published",
    "hidden",
    "hidden_for_user",
    "locked",
    "locked_for_user",
    "due_at",
    "lock_at",
    "unlock_at",
    "start_at",
    "end_at",
    "posted_at",
    "html_url",
    "position",
}
FIELDS = {
    "course": {"course_code", "start_at", "end_at", "term", "enrollments"},
    "assignment": {
        "submission_types",
        "points_possible",
        "has_overrides",
        "only_visible_to_overrides",
        "is_quiz_assignment",
        "quiz_id",
    },
    "submission": {
        "assignment_id",
        "user_id",
        "submitted_at",
        "score",
        "grade",
        "published_score",
        "published_grade",
        "graded_at",
        "attempt",
        "excused",
        "missing",
        "late",
        "redo_request",
        "grade_matches_current_submission",
        "cached_due_date",
        "submission_type",
        "submission_comments",
        "rubric_assessment",
        "grade_hidden",
        "hidden",
    },
    "file": {"display_name", "filename", "size", "content-type", "modified_at", "updated_at", "folder_id"},
    "folder": {"full_name", "parent_folder_id", "files_count", "folders_count"},
    "conversation": {"subject", "last_message_at", "message_count", "messages"},
    "discussion": {"last_reply_at", "discussion_subentry_count", "is_announcement"},
    "reply": {"user_id", "parent_id", "created_at", "updated_at"},
    "module": {
        "prerequisite_module_ids",
        "require_sequential_progress",
        "state",
        "completed_at",
        "items_count",
    },
    "module_item": {"type", "content_id", "completion_requirement"},
    "page": {"url", "updated_at", "editing_roles"},
    "calendar": {"all_day", "all_day_date", "location_name", "context_code"},
    "planner": {"plannable_id", "plannable_type", "plannable_date", "plannable", "submissions"},
    "planner_note": {"todo_date", "details"},
}
TEXT_FIELDS = {"description", "body", "message", "details"}


def safe_link(value: str | None, origin: str) -> str:
    if not value:
        return ""
    p, o = urlsplit(value), urlsplit(origin)
    if (p.scheme, p.netloc) != (o.scheme, o.netloc) or p.username or p.password or p.fragment:
        return ""
    if not re.fullmatch(
        r"/(courses(?:/[A-Za-z0-9_./%-]*)?|conversations|calendar|dashboard|files/\d+)", p.path
    ):
        return ""
    # 通知链接只保留业务路径，签名、SSO 码及查询参数不落库。
    return origin + p.path


def clean_html(value: str) -> str:
    return bleach.clean(
        value,
        tags={"p", "br", "b", "strong", "em", "ul", "ol", "li", "blockquote", "code"},
        attributes={},
        protocols=[],
        strip=True,
    )


def clean_email_html(value: str) -> str:
    from urllib.parse import parse_qsl

    def allow_attribute(tag, name, value):
        if tag == "a" and name == "href":
            parsed = urlsplit(value)
            return (
                not parsed.username
                and not parsed.password
                and not any(
                    k.lower()
                    in {"access_token", "token", "session_token", "code", "state", "verifier", "signature"}
                    for k, _ in parse_qsl(parsed.query)
                )
            )
        return (tag == "a" and name == "title") or (tag in {"td", "th"} and name in {"colspan", "rowspan"})

    return bleach.clean(
        value,
        tags={
            "p",
            "br",
            "b",
            "strong",
            "em",
            "ul",
            "ol",
            "li",
            "blockquote",
            "code",
            "pre",
            "h2",
            "h3",
            "h4",
            "table",
            "thead",
            "tbody",
            "tr",
            "th",
            "td",
            "caption",
            "a",
        },
        attributes=allow_attribute,
        protocols=["https", "mailto"],
        strip=True,
    )


def normalize(kind: str, data: dict, origin: str, course_id="") -> dict:
    result = {k: v for k, v in data.items() if k in COMMON | FIELDS.get(kind, set())}
    for key, value in list(result.items()):
        if key in TEXT_FIELDS and isinstance(value, str):
            result[key] = clean_html(value)
        if key.endswith("_at") or key in ("cached_due_date", "todo_date", "plannable_date"):
            if value is not None:
                result[key] = parse_time(value).isoformat()
        if key == "id" or key.endswith("_id"):
            result[key] = str(value) if value is not None else None
    if kind == "course":
        result["term"] = (
            {k: data["term"].get(k) for k in ("id", "start_at", "end_at")} if data.get("term") else None
        )
        # 身份与个人成绩不混进课程指纹。
        result.pop("enrollments", None)
    if kind == "submission" and "submission_comments" in data:
        result["submission_comments"] = sorted(
            [
                {
                    k: (str(v) if k in ("id", "author_id") else clean_html(v) if k == "comment" else v)
                    for k, v in comment.items()
                    if k in {"id", "author_id", "comment", "created_at"}
                }
                for comment in data["submission_comments"] or []
            ],
            key=lambda c: c.get("id", ""),
        )
    if kind == "conversation":
        result.pop("workflow_state", None)  # 读/未读变化不等于新消息。
        if "messages" in data:
            result["messages"] = sorted(
                [
                    {
                        k: str(v) if k in ("id", "author_id") else clean_html(v) if k == "body" else v
                        for k, v in m.items()
                        if k in {"id", "author_id", "body", "created_at"}
                    }
                    for m in data["messages"]
                ],
                key=lambda m: m["id"],
            )
    if kind == "planner" and isinstance(data.get("plannable"), dict):
        result["plannable"] = {k: v for k, v in data["plannable"].items() if k in {"id", "title", "due_at"}}
    if "attachments" in data:
        result["attachments"] = [
            {k: a.get(k) for k in ("id", "display_name", "size")} for a in data["attachments"] or []
        ]
    attachments = list(data.get("attachments") or [])
    if isinstance(data.get("attachment"), dict):
        attachments.append(data["attachment"])
    result["_email"] = {
        "source_created_at": source_time(data.get("created_at")),
        "source_updated_at": source_time(data.get("updated_at")),
        "source_modified_at": source_time(data.get("modified_at")),
        "source_published_at": source_time(data.get("posted_at")),
        "body": clean_email_html(data.get("message") or data.get("description") or data.get("body") or ""),
        "attachments": [{k: a.get(k) for k in ("id", "display_name", "size")} for a in attachments],
        "comments": {
            str(c["id"]): {
                **feedback_metadata(c, origin),
                "body": clean_email_html(c.get("comment") or ""),
                "attachments": [
                    {k: a.get(k) for k in ("id", "display_name", "size")} for a in c.get("attachments") or []
                ],
            }
            for c in data.get("submission_comments") or []
            if "id" in c
        },
    }
    link = safe_link(data.get("html_url"), origin)
    if not link and course_id.isdigit() and str(data.get("id", "")).isdigit() and kind == "assignment":
        link = f"{origin}/courses/{course_id}/assignments/{data['id']}"
    if link:
        result["html_url"] = link
    else:
        result.pop("html_url", None)
    return result


def fingerprint(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class GradeVisibilityPolicy:
    @staticmethod
    def visible(data: dict):
        if data.get("grade_hidden") or data.get("hidden"):
            return None
        # 以学生可见响应为依据；明确存在的 published_* 优先，不能用 posted_at 猜测。
        if "published_score" in data or "published_grade" in data:
            score, grade = data.get("published_score"), data.get("published_grade")
        else:
            score, grade = data.get("score"), data.get("grade")
        return {"score": score, "grade": grade} if score is not None or grade is not None else None


def complete(assignment: dict, submission: dict, local: dict) -> bool:
    if submission.get("redo_request"):
        return False
    if local.get("completed") or submission.get("excused"):
        return True
    types = assignment.get("submission_types", [])
    if not any(
        t in types
        for t in (
            "online_upload",
            "online_text_entry",
            "online_quiz",
            "online_url",
            "media_recording",
            "student_annotation",
        )
    ):
        return False
    return bool(submission.get("submitted_at")) and not submission.get("missing", False)


def changes_for(kind, before, after, user_id=""):
    diff = {
        k: {"before": before.get(k), "after": after.get(k)}
        for k in after
        if k != "_email" and before.get(k) != after.get(k)
    }
    if not diff:
        return []
    events = []
    if kind == "submission":
        old_grade, new_grade = GradeVisibilityPolicy.visible(before), GradeVisibilityPolicy.visible(after)
        if new_grade is not None and old_grade != new_grade:
            events.append(
                (
                    "grade_published" if old_grade is None else "grade_changed",
                    {"grade": {"before": old_grade, "after": new_grade}},
                )
            )
        old_comments = {str(c["id"]): c for c in before.get("submission_comments", [])}
        for comment in after.get("submission_comments", []):
            if str(comment.get("author_id")) != user_id and old_comments.get(str(comment["id"])) != comment:
                events.append(
                    (
                        "submission_comment_added"
                        if str(comment["id"]) not in old_comments
                        else "submission_comment_changed",
                        {"comment": comment},
                    )
                )
        if "rubric_assessment" in diff:
            events.append(("rubric_changed", {"rubric_assessment": diff["rubric_assessment"]}))
        if after.get("redo_request") and not before.get("redo_request"):
            events.append(("resubmission_requested", {}))
        if after.get("submitted_at") and before.get("submitted_at") != after.get("submitted_at"):
            events.append(("submission_confirmed", {"submitted_at": diff["submitted_at"]}))
    elif kind == "conversation":
        old_messages = {str(m["id"]): m for m in before.get("messages", [])}
        for message in after.get("messages", []):
            if str(message.get("author_id")) != user_id and old_messages.get(str(message["id"])) != message:
                events.append(("conversation_message_added", {"message": message}))
    elif kind == "reply" and str(after.get("user_id")) == user_id:
        return []
    elif kind == "assignment":
        dates = {k: v for k, v in diff.items() if k in ("due_at", "lock_at", "unlock_at")}
        if dates:
            events.append(("assignment_dates_changed", dates))
        content = {k: v for k, v in diff.items() if k not in ("due_at", "lock_at", "unlock_at")}
        if content:
            events.append(("assignment_content_changed", content))
    else:
        events.append((kind + "_changed", diff))
    return events
