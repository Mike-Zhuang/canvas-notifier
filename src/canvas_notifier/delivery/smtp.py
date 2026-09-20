import uuid
from datetime import timedelta
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

import aiosmtplib
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select, update

from canvas_notifier.config import read_secret
from canvas_notifier.db import Delivery, Reminder, Resource
from canvas_notifier.domain.normalize import complete
from canvas_notifier.domain.time import now_utc, parse_time
from canvas_notifier.scheduling.planner import context
from canvas_notifier.sync.state import health

ENV = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"), autoescape=select_autoescape()
)


def render_message(settings, delivery, *, payload=None):
    from canvas_notifier.delivery.presentation import present

    p = present(payload or delivery.payload)
    message = EmailMessage()
    subject_parts = [value for value in (p.get("course"), p["title"]) if value]
    message["Subject"] = (
        f"[Canvas·{p['label']}] " + "｜".join(subject_parts).replace("\r", " ").replace("\n", " ")[:220]
    )
    message["From"], message["To"] = settings.mail_from, delivery.recipient
    message["Message-ID"], message["Date"] = delivery.message_id, format_datetime(now_utc())
    message.set_content(ENV.get_template("message.txt").render(**p))
    message.add_alternative(ENV.get_template("message.html").render(**p), subtype="html")
    return message


async def send(settings, message):
    if settings.smtp_tls_mode == "none" and settings.smtp_host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("Unencrypted SMTP is allowed only on loopback")
    await aiosmtplib.send(
        message,
        hostname=settings.smtp_host,
        port=settings.smtp_port,
        use_tls=settings.smtp_tls_mode == "tls",
        start_tls=settings.smtp_tls_mode == "starttls",
        username=settings.smtp_username or None,
        password=read_secret(settings.smtp_password_file) or None,
        timeout=30,
    )


async def applicable(session, delivery, now):
    keys = delivery.payload.get("reminder_keys", [])
    if not keys:
        if delivery.payload.get("kind") == "test":
            return True, ""
        from canvas_notifier.delivery.queue import recipients
        from canvas_notifier.scheduling.rules import effective_rules

        resource = await session.get(Resource, delivery.resource_key) if delivery.resource_key else None
        key = resource.key if resource else ""
        if resource and resource.kind == "submission":
            key = f"assignment:{resource.course_id}:{resource.data.get('assignment_id')}"
        rule, _ = await effective_rules(session, resource.course_id if resource else "", key)
        if not rule.enabled or rule.events.get(delivery.payload.get("kind"), rule.default_event) == "off":
            return False, "事件已被当前规则关闭"
        if rule.recipients is not None and delivery.recipient not in recipients(",".join(rule.recipients)):
            return False, "收件人已从当前规则移除"
        if not rule.score_in_email and (
            delivery.payload.get("kind", "").startswith("grade_")
            or delivery.payload.get("kind") == "rubric_changed"
        ):
            delivery.payload = {**delivery.payload, "changes": {}, "score_in_email": False}
        return True, ""
    resource = await session.get(Resource, delivery.resource_key)
    if not resource:
        return False, "资源不存在"
    submission, rule, _ = await context(session, resource)
    if not rule.enabled or resource.availability != "visible":
        return False, "规则关闭或资源不可访问"
    if rule.only_incomplete and complete(
        resource.data, submission.data if submission else {}, resource.local
    ):
        return False, "已完成"
    jobs = list((await session.scalars(select(Reminder).where(Reminder.key.in_(keys)))).all())
    if not any(job.status == "queued" for job in jobs):
        return False, "提醒计划已取消"
    boundary = parse_time(delivery.payload.get("boundary"))
    if boundary and now >= boundary and delivery.payload["kind"] != "overdue":
        return False, "边界已过去"
    if rule.stale_source_policy == "pause" and now - min(
        resource.last_seen, submission.last_seen if submission else resource.last_seen
    ) > timedelta(minutes=rule.stale_after_minutes):
        return False, "等待新鲜数据"
    # 最后同步时间采用发送前的真实状态，不能保持入队时的旧文案。
    delivery.payload = {
        **delivery.payload,
        "last_synced_at": min(
            resource.last_seen, submission.last_seen if submission else resource.last_seen
        ).isoformat(),
    }
    return True, ""


async def deliver_batch(settings, sessions, *, sender=send, limit=30, now=None):
    from canvas_notifier.sync.leases import lease

    # 全局发送租约持续续期，避免慢 SMTP 使后续批次行租约过期后被另一 worker 领取。
    async with lease(sessions, "delivery") as acquired:
        if not acquired:
            return 0
        return await _deliver_claimed_batch(settings, sessions, sender=sender, limit=limit, now=now)


async def _deliver_claimed_batch(settings, sessions, *, sender=send, limit=30, now=None):
    now = now or now_utc()
    owner = uuid.uuid4().hex
    async with sessions() as session, session.begin():
        # 过期租约回收，发送和状态落库之间崩溃可能重复，Message-ID 保持稳定。
        await session.execute(
            update(Delivery)
            .where(Delivery.status == "sending", Delivery.lease_until < now)
            .values(status="retry", reason="发送租约过期，重新尝试")
        )
        candidates = list(
            (
                await session.scalars(
                    select(Delivery.id)
                    .where(Delivery.status.in_(["pending", "retry"]), Delivery.available_at <= now)
                    .order_by(Delivery.id)
                    .limit(limit)
                )
            ).all()
        )
    claimed = []
    for delivery_id in candidates:
        async with sessions() as session, session.begin():
            result = await session.execute(
                update(Delivery)
                .where(Delivery.id == delivery_id, Delivery.status.in_(["pending", "retry"]))
                .values(status="sending", lease_owner=owner, lease_until=now + timedelta(seconds=120))
            )
            if result.rowcount:
                claimed.append(delivery_id)
    # 摘要按收件人分组，普通即时事件仍独立投递。
    groups = {}
    async with sessions() as session, session.begin():
        for delivery_id in claimed:
            row = await session.get(Delivery, delivery_id)
            ok, reason = await applicable(session, row, now)
            if not ok:
                row.status, row.reason = ("retry" if reason == "等待新鲜数据" else "cancelled"), reason
                row.available_at = now + timedelta(minutes=5)
                continue
            group_key = ("digest", row.recipient) if row.payload.get("digest") else ("single", row.id)
            groups.setdefault(group_key, []).append(row)
    sent_count = 0
    for rows in groups.values():
        primary = rows[0]
        payload = None
        if primary.payload.get("digest"):
            payload = {
                **primary.payload,
                "label": "更新摘要",
                "title": f"{len(rows)} 项更新",
                "excerpt": "",
                "digest_items": [dict(r.payload) for r in rows],
                "course": "",
                "link": "",
                "file_size": None,
                "is_assignment": False,
                "points_possible": None,
                "due_at": None,
                "lock_at": None,
                "unlock_at": None,
                "submitted_at": None,
                "attachments": [],
                "changes": {
                    "items": [
                        {"title": r.payload["title"], "label": r.payload["label"], "link": r.payload["link"]}
                        for r in rows
                    ]
                },
            }
        error = None
        try:
            await sender(settings, render_message(settings, primary, payload=payload))
        except Exception as exc:
            # SMTP 错误可能回显邮箱或密码，日志只保留安全类型和 SMTP 状态码。
            error = type(exc).__name__ + (":" + str(exc.code) if hasattr(exc, "code") else "")
        async with sessions() as session, session.begin():
            for previous in rows:
                row = await session.get(Delivery, previous.id)
                if row.lease_owner != owner or row.status != "sending":
                    continue
                row.attempts += 1
                row.lease_until, row.lease_owner = None, None
                if error:
                    row.status = "failed" if row.attempts >= 8 else "retry"
                    row.reason = error
                    row.available_at = now + timedelta(seconds=min(3600, 30 * 2**row.attempts))
                else:
                    row.status, row.accepted_at, row.reason = (
                        "accepted",
                        now,
                        "SMTP 已接受；不等于收件箱已送达",
                    )
                    for key in row.payload.get("reminder_keys", []):
                        job = await session.get(Reminder, key)
                        if job:
                            job.status, job.reason = "sent", "SMTP 已接受"
                    sent_count += 1
            await health(session, "delivery", error or "ok")
    return sent_count
