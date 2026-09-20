from datetime import timedelta

from sqlalchemy import select

from canvas_notifier.canvas.http import CanvasClient, CanvasError, PageResult
from canvas_notifier.db import Reminder, Resource
from canvas_notifier.domain.time import now_utc
from canvas_notifier.scheduling.planner import plan_resource
from canvas_notifier.sync.leases import lease
from canvas_notifier.sync.service import bind_identity
from canvas_notifier.sync.state import apply_scope, health


async def refresh_due(settings, sessions):
    """只复核即将触发的任务，不把全站轮询一起提频；失败按陈旧数据策略处理。"""
    now = now_utc()
    async with sessions() as session:
        keys = list(
            (
                await session.scalars(
                    select(Reminder.resource_key)
                    .where(
                        Reminder.status.in_(["pending", "queued"]),
                        Reminder.scheduled_at <= now + timedelta(minutes=2),
                        Reminder.boundary_at > now,
                    )
                    .distinct()
                )
            ).all()
        )
        candidates = []
        for key in keys:
            resource = await session.get(Resource, key)
            if resource and now - resource.last_seen > timedelta(seconds=60):
                candidates.append(resource)
    if not candidates:
        return
    async with lease(sessions, "sync") as acquired:
        if not acquired:
            return
        async with CanvasClient(settings) as client:
            try:
                await bind_identity(sessions, client)
                for resource in candidates:
                    prefix = f"/api/v1/courses/{resource.course_id}/assignments/{resource.external_id}"
                    assignment, _ = await client.get(prefix, [("include[]", "submission")], kind="assignment")
                    submission, _ = await client.get(
                        prefix + "/submissions/self",
                        [("include[]", "submission_comments"), ("include[]", "rubric_assessment")],
                        kind="submission",
                    )
                    if str(submission.get("user_id")) != client.user_id:
                        raise CanvasError("submission_identity_mismatch")
                    async with sessions() as session, session.begin():
                        for kind, item in [("assignment", assignment), ("submission", submission)]:
                            await apply_scope(
                                session,
                                settings,
                                kind,
                                resource.course_id,
                                PageResult([item], 1, True),
                                user_id=client.user_id,
                                now=now,
                                targeted=True,
                            )
                        current = await session.get(Resource, resource.key)
                        await plan_resource(session, current, now)
                state = "ok"
            except CanvasError as error:
                state = error.code
            async with sessions() as session, session.begin():
                await health(session, "preflight", state)
