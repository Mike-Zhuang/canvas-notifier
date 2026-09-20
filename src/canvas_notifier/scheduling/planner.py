from datetime import timedelta

from sqlalchemy import select

from canvas_notifier.db import Delivery, Reminder, Resource
from canvas_notifier.delivery.queue import enqueue
from canvas_notifier.domain.normalize import complete, fingerprint
from canvas_notifier.domain.time import now_utc, parse_time
from canvas_notifier.scheduling.rules import duration, effective_rules, quiet_adjust


async def context(session, resource):
    submission = await session.get(Resource, f"submission:{resource.course_id}:{resource.external_id}")
    rule, sources = await effective_rules(session, resource.course_id, resource.key)
    return submission, rule, sources


def calculate_schedule(resource, sub, rule, now):
    done = complete(resource.data, sub, resource.local)
    desired = {}
    suppressed = not rule.enabled or (rule.only_incomplete and done) or resource.availability != "visible"
    # assignment API 默认应用本人日期覆盖；cached_due_date 只在字段缺失时兜底，缓存不能覆盖权威 null 或新日期。
    dates = {kind: resource.data.get(kind + "_at") for kind in ("due", "lock", "unlock")}
    if "due_at" not in resource.data and "cached_due_date" in sub:
        dates["due"] = sub["cached_due_date"]
    dates["personal"] = resource.local.get("personal_at")
    if not suppressed:
        for kind, value in dates.items():
            boundary = parse_time(value)
            if not boundary:
                continue
            offsets = [duration(item) for item in getattr(rule, kind)]
            schedule = [(kind, seconds, boundary - timedelta(seconds=seconds)) for seconds in offsets]
            if kind == "due" and rule.overdue_enabled:
                schedule += [
                    (
                        "overdue",
                        -i * duration(rule.overdue_repeat),
                        boundary + timedelta(seconds=i * duration(rule.overdue_repeat)),
                    )
                    for i in range(1, rule.overdue_max + 1)
                ]
            for label, seconds, trigger in schedule:
                if label != "overdue" and boundary <= now:
                    continue
                actual = quiet_adjust(trigger, boundary if label != "overdue" else None, rule)
                if actual is None:
                    continue
                snooze = parse_time(resource.local.get("snooze_until"))
                if snooze and actual < snooze:
                    actual = snooze
                # 等价保存规则不能改变 key；不同边界之间独立，改 due 不会重发 lock。
                key = fingerprint([resource.key, label, boundary.isoformat(), actual.isoformat()])
                desired[key] = (label, boundary, actual, seconds)
    return desired


async def plan_resource(session, resource: Resource, now=None):
    now = now or now_utc()
    submission, rule, _ = await context(session, resource)
    sub = submission.data if submission else {}
    desired = calculate_schedule(resource, sub, rule, now)
    old_jobs = list(
        (await session.scalars(select(Reminder).where(Reminder.resource_key == resource.key))).all()
    )
    for job in old_jobs:
        if job.key not in desired and job.status in ("pending", "queued"):
            job.status, job.reason = "cancelled", "已完成、规则变化、日期变化或不可访问"
        elif job.key in desired and job.status == "cancelled":
            # 取消后恢复允许重建未投递任务；已接受邮件永不恢复发送。
            sent = await session.scalar(
                select(Delivery.id).where(
                    Delivery.resource_key == resource.key,
                    Delivery.status == "accepted",
                    Delivery.payload["reminder_keys"].as_string().contains(job.key),
                )
            )
            if not sent:
                job.status, job.reason = "pending", ""
    known = {job.key for job in old_jobs}
    for key, (kind, boundary, trigger, seconds) in desired.items():
        if key not in known:
            session.add(
                Reminder(
                    key=key,
                    resource_key=resource.key,
                    boundary_type=kind,
                    boundary_at=boundary,
                    scheduled_at=trigger,
                    offset_seconds=seconds,
                    rule_fingerprint=fingerprint(rule.model_dump()),
                )
            )
    # 发件前仍会复查；这里立即取消尚未发送的旧计划。
    pending = (
        await session.scalars(
            select(Delivery).where(
                Delivery.resource_key == resource.key, Delivery.status.in_(["pending", "retry"])
            )
        )
    ).all()
    for delivery in pending:
        keys = delivery.payload.get("reminder_keys", [])
        if keys and not any(key in desired for key in keys):
            delivery.status, delivery.reason = "cancelled", "提醒计划已失效"


async def rebuild(session, now=None):
    for resource in (await session.scalars(select(Resource).where(Resource.kind == "assignment"))).all():
        await plan_resource(session, resource, now)


async def dispatch_reminders(session, settings, now=None):
    now = now or now_utc()
    jobs = list(
        (
            await session.scalars(
                select(Reminder)
                .where(Reminder.status == "pending", Reminder.scheduled_at <= now)
                .order_by(Reminder.scheduled_at)
            )
        ).all()
    )
    groups = {}
    for job in jobs:
        resource = await session.get(Resource, job.resource_key)
        if not resource:
            continue
        submission, rule, _ = await context(session, resource)
        reason = ""
        if not rule.enabled or (
            rule.only_incomplete
            and complete(resource.data, submission.data if submission else {}, resource.local)
        ):
            reason = "已完成或规则关闭"
        elif resource.availability != "visible":
            reason = "资源当前不可访问"
        elif job.boundary_type != "overdue" and now >= job.boundary_at:
            reason = "提醒边界已过去，不补发过时提醒"
        elif (
            job.boundary_type == "overdue"
            and resource.data.get("lock_at")
            and now >= parse_time(resource.data["lock_at"])
        ):
            reason = "已停止提交"
        if reason:
            job.status, job.reason = "cancelled", reason
            continue
        if rule.stale_source_policy == "pause" and now - min(
            resource.last_seen, submission.last_seen if submission else resource.last_seen
        ) > timedelta(minutes=rule.stale_after_minutes):
            job.reason = "等待新鲜同步数据"
            continue
        groups.setdefault((resource.key, job.boundary_type, job.boundary_at), []).append(job)
    for (key, kind, boundary), group in groups.items():
        resource = await session.get(Resource, key)
        keys = sorted(job.key for job in group)
        state = await enqueue(
            session,
            settings,
            "reminder:" + fingerprint(keys),
            resource,
            kind,
            {"coalesced_count": len(group)},
            boundary=boundary,
            reminder_keys=keys,
            now=now,
            mode_override="immediate",
        )
        for job in group:
            # 未配置收件人时保持待处理，配置好后可以继续投递。
            job.status = (
                "pending" if state == "no_recipient" else "queued" if state == "queued" else "suppressed"
            )
            job.reason = "停机错过的提醒点合并" if len(group) > 1 and state == "queued" else state
