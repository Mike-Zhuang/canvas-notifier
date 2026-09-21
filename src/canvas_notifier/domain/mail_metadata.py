"""仅补充 API 实际可见的邮件信息；不额外请求作者资料，也不保存媒体鉴权地址。"""

from urllib.parse import parse_qsl, urljoin, urlsplit

from email_validator import EmailNotValidError, validate_email

from canvas_notifier.domain.time import parse_time

SECRET_QUERY_KEYS = {
    "access_token",
    "accesstoken",
    "token",
    "session_token",
    "code",
    "state",
    "verifier",
    "signature",
    "sig",
    "key",
}


def visible_url(value, origin):
    if not isinstance(value, str) or not value:
        return ""
    url = urljoin(origin + "/", value)
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            return ""
        if any(
            key.lower() in SECRET_QUERY_KEYS or key.lower().startswith(("x-amz-", "x-oss-"))
            for key, _ in parse_qsl(parsed.query)
        ):
            return ""
        if "/images/thumbnails/" in parsed.path:
            return ""
    except ValueError:
        return ""
    return url


def visible_email(value):
    if not isinstance(value, str) or "\n" in value or "\r" in value:
        return ""
    try:
        return validate_email(value, check_deliverability=False, test_environment=True).normalized
    except EmailNotValidError:
        return ""


def source_time(value):
    if value is not None and not isinstance(value, str):
        return None
    try:
        stamp = parse_time(value)
        return stamp.isoformat() if stamp else None
    except (TypeError, ValueError):
        return None


def feedback_metadata(comment, origin):
    author = comment.get("author") if isinstance(comment.get("author"), dict) else {}
    readable = (
        comment.get("can_read_author") is not False
        and not comment.get("anonymous")
        and not author.get("anonymous")
    )
    media = comment.get("media_comment") if isinstance(comment.get("media_comment"), dict) else {}
    content_type = media.get("media_type") or media.get("content-type") or ""
    media_kind = (
        "audio"
        if str(content_type).startswith("audio")
        else "video"
        if str(content_type).startswith("video")
        else "media"
        if media
        else ""
    )
    return {
        "author_name": (
            comment.get("author_name")
            or author.get("display_name")
            or author.get("short_name")
            or author.get("name")
        )
        if readable
        else None,
        "author_email": visible_email(comment.get("author_email") or author.get("email")) if readable else "",
        "author_profile_url": visible_url(author.get("html_url"), origin) if readable else "",
        "author_avatar_url": visible_url(author.get("avatar_image_url"), origin) if readable else "",
        "media_kind": media_kind,
        "media_name": media.get("display_name") if isinstance(media.get("display_name"), str) else "",
        "feedback_created_at": source_time(comment.get("created_at")),
        "feedback_edited_at": source_time(comment.get("edited_at")),
    }
