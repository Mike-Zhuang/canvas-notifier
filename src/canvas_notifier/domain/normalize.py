import hashlib
import html
import json
import re
from functools import partial
from urllib.parse import urljoin, urlsplit

import bleach
from bleach.html5lib_shim import Filter

from canvas_notifier.domain.body_images import body_images
from canvas_notifier.domain.mail_metadata import (
    author_is_visible,
    feedback_metadata,
    is_secret_query_key,
    source_time,
)
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
        "allowed_attempts",
        "lock_explanation",
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
        "points_deducted",
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
    "calendar": {"all_day", "all_day_date", "location_name", "location_address", "context_code"},
    "planner": {"plannable_id", "plannable_type", "plannable_date", "plannable", "submissions"},
    "planner_note": {"todo_date", "details"},
}
TEXT_FIELDS = {"description", "body", "message", "details"}
BODY_RESOURCES = {"assignment", "announcement", "discussion", "page", "calendar", "planner_note"}
_FORMATTING_TAGS = {"sup", "sub", "s", "u", "hr"}
_TEXT_TAGS = {"p", "br", "b", "strong", "em", "ul", "ol", "li", "blockquote", "code"} | _FORMATTING_TAGS
_EMAIL_TAGS = _TEXT_TAGS | {
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
}
_BACKFILL_FIELDS = {
    "content_images",
    "links",
    "attachments",
    "rubric",
    "allowed_attempts",
    "lock_explanation",
    "location_address",
    "points_deducted",
}
_SUBMISSION_STATUS = {"missing", "late", "excused", "points_deducted"}
_NESTED_BACKFILL_FIELDS = {"attachments", "media_comment", "links", "content_images"}
_BACKFILL_FORMAT = re.compile(r"</?(?:" + "|".join(sorted(_FORMATTING_TAGS)) + r")>")


def _origin(url):
    port = url.port
    return url.scheme, url.hostname, 443 if port is None else port


def sanitize_link(value: str | None, origin: str) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    value = value.strip()
    if "\\" in value or re.search(r"[\x00-\x1f\x7f]", value):
        return ""
    try:
        parsed = urlsplit(urljoin(origin + "/", value))
        if (
            parsed.scheme == "https"
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
        ):
            same_origin = _origin(parsed) == _origin(urlsplit(origin))
        elif parsed.scheme == "mailto" and not parsed.netloc and parsed.path:
            same_origin = False
        else:
            return ""
    except ValueError:
        return ""

    # 百分号编码、大小写与分隔符不能让鉴权参数漏进邮件。
    terms = re.split(r"[&;]", parsed.query)
    public = [term for term in terms if not is_secret_query_key(term.partition("=")[0])]
    secret_fragment = any(
        "=" in term and is_secret_query_key(term.partition("=")[0])
        for term in re.split(r"[?&;]", parsed.fragment)
    )
    if len(public) != len(terms) or secret_fragment:
        if not same_origin:
            return ""
        return parsed._replace(
            query=parsed.query if len(public) == len(terms) else "&".join(public),
            fragment="" if secret_fragment else parsed.fragment,
        ).geturl()
    return parsed.geturl()


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
        tags=_TEXT_TAGS,
        attributes={},
        protocols=[],
        strip=True,
    )


class _LinkFilter(Filter):
    def __init__(self, source, *, origin, links):
        super().__init__(source)
        self.origin = origin
        self.origin_key = _origin(urlsplit(origin))
        self.links = links

    def __iter__(self):
        for token in self.source:
            if token["type"] in ("StartTag", "EmptyTag") and token["name"] == "a":
                attrs = token["data"]
                key = (None, "href")
                if key in attrs:
                    target = sanitize_link(html.unescape(attrs[key]), self.origin)
                    if target:
                        attrs[key] = target
                        if self.links is not None and _origin(urlsplit(target)) == self.origin_key:
                            self.links.add(target)
                    else:
                        del attrs[key]
            yield token


def _clean_email_html(value, origin, links=None):
    return bleach.Cleaner(
        tags=_EMAIL_TAGS,
        attributes={"a": ["href", "title"], "td": ["colspan", "rowspan"], "th": ["colspan", "rowspan"]},
        protocols=["https", "mailto"],
        strip=True,
        filters=[partial(_LinkFilter, origin=origin, links=links)],
    ).clean(value)


def clean_email_html(value: str, origin: str) -> str:
    return _clean_email_html(value, origin)


def _attachments(data):
    items = list(data.get("attachments") or [])
    if isinstance(data.get("attachment"), dict):
        items.append(data["attachment"])
    return sorted(
        (
            {
                "id": item.get("id"),
                "display_name": item.get("display_name") or item.get("filename"),
                "size": item.get("size"),
            }
            for item in items
            if isinstance(item, dict)
        ),
        key=lambda item: tuple(str(item[key]) for key in ("id", "display_name", "size")),
    )


def _rubric(criteria):
    return [
        {
            **{k: row.get(k) for k in ("id", "description", "long_description", "points")},
            "ratings": [
                {k: rating.get(k) for k in ("id", "description", "points")}
                for rating in row.get("ratings") or []
            ],
        }
        for row in criteria or []
    ]


def _author_name(data, author):
    author = author if isinstance(author, dict) else {}
    if not author_is_visible(data, author):
        return ""
    return (
        author.get("display_name")
        or data.get("user_name")
        or author.get("name")
        or author.get("full_name")
        or ""
    )


def _message_author_name(message, participants):
    author = message.get("author")
    author = author if isinstance(author, dict) else {}
    if not author_is_visible(message, author):
        return ""
    participant = participants.get(str(message.get("author_id")), {})
    if not author_is_visible(message, participant):
        return ""
    return (
        author.get("display_name")
        or participant.get("display_name")
        or participant.get("name")
        or participant.get("full_name")
        or message.get("user_name")
        or ""
    )


def _email_body(raw, origin, links=None):
    text, images = body_images(raw, origin)
    return _clean_email_html(text, origin, links), images


def normalize(kind: str, data: dict, origin: str, course_id="") -> dict:
    raw_body = data.get("message") or data.get("description") or data.get("body") or data.get("details") or ""
    links = set() if kind in BODY_RESOURCES else None
    email_body, images = _email_body(raw_body, origin, links)
    attachments = _attachments(data)
    fields = COMMON | FIELDS.get(kind, set())
    result = {k: v for k, v in data.items() if k in fields}
    for key, value in list(result.items()):
        if (key in TEXT_FIELDS or key == "lock_explanation") and isinstance(value, str):
            result[key] = clean_html(value)
            if links is not None and key in TEXT_FIELDS and value and value != raw_body:
                _clean_email_html(value, origin, links)
        if key.endswith("_at") or key in ("cached_due_date", "todo_date", "plannable_date"):
            if value is not None:
                result[key] = parse_time(value).isoformat()
        if key == "id" or key.endswith("_id"):
            result[key] = str(value) if value is not None else None
    if any(key in data for key in TEXT_FIELDS):
        result["content_images"] = images
    if links is not None:
        result["links"], result["attachments"] = sorted(links), attachments
    elif "attachments" in data:
        result["attachments"] = attachments
    if kind == "assignment" and "rubric" in data:
        result["rubric"] = _rubric(data["rubric"])
    email = result["_email"] = {
        "course_id": str(data.get("course_id") or "") if kind == "planner" else course_id,
        "source_created_at": source_time(data.get("created_at")),
        "source_updated_at": source_time(data.get("updated_at")),
        "source_modified_at": source_time(data.get("modified_at")),
        "source_published_at": source_time(data.get("posted_at")),
        "body": email_body,
        "attachments": attachments,
        "comments": {},
    }
    if kind in {"announcement", "discussion", "reply"}:
        email["author_name"] = _author_name(data, data.get("author"))
    if kind == "course":
        result["term"] = (
            {k: data["term"].get(k) for k in ("id", "start_at", "end_at")} if data.get("term") else None
        )
        # 身份与个人成绩不混进课程指纹。
        result.pop("enrollments", None)
    if kind == "submission" and "submission_comments" in data:
        comments = []
        for comment in data.get("submission_comments") or []:
            raw_comment = comment.get("comment") or ""
            body, content_images = _email_body(raw_comment, origin)
            comment_attachments = _attachments(comment)
            media = comment.get("media_comment")
            comments.append(
                {
                    **{
                        k: (str(v) if k in ("id", "author_id") else v)
                        for k, v in comment.items()
                        if k in {"id", "author_id", "created_at"}
                    },
                    **({"comment": clean_html(raw_comment)} if "comment" in comment else {}),
                    "attachments": comment_attachments,
                    "content_images": content_images,
                    "media_comment": {
                        "media_type": media.get("media_type") or media.get("content-type"),
                        "display_name": media.get("display_name"),
                    }
                    if isinstance(media, dict)
                    else None,
                }
            )
            if "id" in comment:
                email["comments"][str(comment["id"])] = {
                    **feedback_metadata(comment, origin),
                    "body": body,
                    "content_images": content_images,
                    "attachments": comment_attachments,
                }
        result["submission_comments"] = sorted(comments, key=lambda c: c.get("id", ""))
    if kind == "conversation":
        result.pop("workflow_state", None)  # 读/未读变化不等于新消息。
        if "messages" in data:
            participants = {str(p.get("id")): p for p in data.get("participants") or []}
            messages = []
            email["messages"] = {}
            for message in data["messages"] or []:
                message_id = str(message["id"])
                raw_message = message.get("body") or ""
                targets = set()
                body, content_images = _email_body(raw_message, origin, targets)
                messages.append(
                    {
                        **{
                            k: (str(v) if k in ("id", "author_id") else v)
                            for k, v in message.items()
                            if k in {"id", "author_id", "created_at"}
                        },
                        **({"body": clean_html(raw_message)} if "body" in message else {}),
                        "links": sorted(targets),
                        "attachments": _attachments(message),
                    }
                )
                email["messages"][message_id] = {
                    "author_name": _message_author_name(message, participants),
                    "body": body,
                    "content_images": content_images,
                }
            result["messages"] = sorted(messages, key=lambda m: m["id"])
    if kind == "planner" and isinstance(data.get("plannable"), dict):
        result["plannable"] = {k: v for k, v in data["plannable"].items() if k in {"id", "title", "due_at"}}
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


def _snapshot_diff(before, after, backfill_fields, *, old_format=False):
    # 第一次保存新快照结构只补基线，后续变化才需要通知。
    return {
        key: {"before": before.get(key), "after": value}
        for key, value in after.items()
        if key != "_email"
        and before.get(key) != value
        and not (key in backfill_fields and key not in before)
        and not (
            old_format
            and (key in TEXT_FIELDS or key == "comment")
            and isinstance(value, str)
            and before.get(key) == _BACKFILL_FORMAT.sub("", value)
        )
    }


def _nested_changed(before, after):
    return before is None or bool(
        _snapshot_diff(before, after, _NESTED_BACKFILL_FIELDS, old_format="attachments" not in before)
    )


def changes_for(kind, before, after, user_id=""):
    old_format = ("links" in after and "links" not in before) or (
        kind == "reply" and "author_name" not in before.get("_email", {})
    )
    diff = _snapshot_diff(before, after, _BACKFILL_FIELDS, old_format=old_format)
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
        status = {key: value for key, value in diff.items() if key in _SUBMISSION_STATUS}
        if before and status:
            events.append(("submission_status_changed", status))
        old_comments = {str(c["id"]): c for c in before.get("submission_comments", [])}
        for comment in after.get("submission_comments", []):
            if str(comment.get("author_id")) != user_id and _nested_changed(
                old_comments.get(str(comment["id"])), comment
            ):
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
            if str(message.get("author_id")) != user_id and _nested_changed(
                old_messages.get(str(message["id"])), message
            ):
                events.append(("conversation_message_added", {"message": message}))
    elif kind == "reply" and str(after.get("user_id")) == user_id:
        return []
    elif kind == "assignment":
        dates = {k: v for k, v in diff.items() if k in ("due_at", "lock_at", "unlock_at")}
        if dates:
            events.append(("assignment_dates_changed", dates))
        ordering = {k: v for k, v in diff.items() if k == "position"}
        access = {
            k: v
            for k, v in diff.items()
            if k in {"locked", "locked_for_user", "hidden", "hidden_for_user", "published", "workflow_state"}
        }
        content = {k: v for k, v in diff.items() if k not in dates and k not in ordering and k not in access}
        if content:
            events.append(("assignment_content_changed", content))
        if access:
            events.append(("assignment_access_changed", access))
        if ordering:
            events.append(("assignment_order_changed", ordering))
    else:
        events.append((kind + "_changed", diff))
    return events
