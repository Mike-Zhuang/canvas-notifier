from datetime import timedelta

from sqlalchemy import select

from canvas_notifier.canvas.adapters import SubmissionPageResult
from canvas_notifier.db import Event, Health, Resource, Scope
from canvas_notifier.delivery.queue import enqueue
from canvas_notifier.domain.normalize import changes_for, normalize
from canvas_notifier.domain.time import now_utc


async def emit(session, settings, resource, kind, changes, suffix="", now=None, sync_run_id=None):
    unique = f"{resource.key}:{resource.version}:{kind}:{suffix}"
    if await session.scalar(select(Event.id).where(Event.unique_key == unique)):
        return
    state = await enqueue(
        session, settings, unique, resource, kind, changes, now=now, sync_run_id=sync_run_id
    )
    session.add(
        Event(
            unique_key=unique,
            resource_key=resource.key,
            kind=kind,
            changes=changes,
            disposition=state,
            created_at=now or now_utc(),
        )
    )


async def apply_scope(
    session,
    settings,
    kind,
    course_id,
    result,
    *,
    scope_suffix="",
    user_id="",
    now=None,
    targeted=False,
    sync_run_id=None,
):
    now = now or now_utc()
    scope_key = f"{kind}:{course_id}:{scope_suffix}"
    scope = await session.get(Scope, scope_key)
    if not scope:
        scope = Scope(key=scope_key, kind=kind, course_id=course_id, baseline=False)
        session.add(scope)
    resumed = not targeted and result.resumed and scope.scan_started is not None
    if not targeted:
        scope.last_attempt, scope.pages, scope.count = now, result.pages, len(result.items)
        scope.complete, scope.status = result.complete, result.error or "ok"
        if (
            not scope.baseline
            and not resumed
            and not result.pages
            and not result.items
            and result.error in {"resource_unavailable", "404"}
        ):
            scope.status = "unsupported"
        if not resumed:
            scope.scan_started = now
    submissions = result if isinstance(result, SubmissionPageResult) else None
    seen = set()
    for raw in result.items:
        external = raw.get("assignment_id") if kind == "submission" else raw.get("id")
        if external is None and kind == "planner":
            external = f"{raw.get('plannable_type')}:{raw.get('plannable_id')}"
        if external is None and kind == "page":
            external = raw.get("page_id") or raw.get("url")
        if external is None:
            scope.complete, scope.status = False, "schema_changed"
            continue
        external = str(external)
        # 子资源 ID 在不同父资源下允许重复。
        key = f"{kind}:{course_id}:{scope_suffix + ':' if scope_suffix else ''}{external}"
        seen.add(key)
        if (
            kind == "submission"
            and not targeted
            and (external in submissions.failed_detail_ids if submissions else not result.complete)
        ) or (kind == "page" and not result.complete and "body" not in raw):
            # 轻量清单可证明项目存在，但不能代替详情或为失败的详情建立基线。
            resource = await session.get(Resource, key)
            if resource and not targeted:
                resource.last_listed = scope.scan_started
            continue
        try:
            data = normalize(kind, raw, settings.canvas_base_url, course_id)
        except (ValueError, TypeError):
            scope.complete, scope.status = False, "data_quality_error"
            continue
        resource = await session.get(Resource, key)
        if not resource:
            resource = Resource(
                key=key,
                kind=kind,
                external_id=external,
                course_id=course_id,
                scope=scope_key,
                data=data,
                version=1,
                first_seen=now,
                last_seen=now,
                local={},
                availability="visible",
            )
            session.add(resource)
            if scope.baseline:
                if kind in ("submission", "conversation"):
                    events = changes_for(kind, {}, data, user_id)
                elif kind == "reply" and str(data.get("user_id")) == user_id:
                    events = []
                else:
                    events = [(kind + "_created", {})]
                for i, (event, changes) in enumerate(events):
                    await emit(session, settings, resource, event, changes, str(i), now, sync_run_id)
        else:
            available_again = kind == "assignment" and resource.availability == "missing_unconfirmed"
            # 缺字段不等于 null；轻量响应不能抹掉以前的完整详情。
            merged = {**resource.data, **data}
            before = resource.data
            semantic_before = {k: v for k, v in before.items() if k != "_email"}
            semantic_after = {k: v for k, v in merged.items() if k != "_email"}
            if semantic_before != semantic_after or available_again:
                resource.version += 1
                resource.data = merged
                if scope.baseline or (submissions and external in submissions.detail_ids):
                    for i, (event, changes) in enumerate(changes_for(kind, before, merged, user_id)):
                        await emit(session, settings, resource, event, changes, str(i), now, sync_run_id)
                if available_again:
                    await emit(
                        session,
                        settings,
                        resource,
                        "assignment_available_again",
                        {},
                        now=now,
                        sync_run_id=sync_run_id,
                    )
            resource.data = merged
            resource.last_seen, resource.missing_count, resource.availability = now, 0, "visible"
        if not targeted:
            resource.last_listed = scope.scan_started
    if targeted:
        return scope
    if scope.complete:
        rows = (await session.scalars(select(Resource).where(Resource.scope == scope_key))).all()
        for resource in rows:
            if resource.key not in seen and (not resumed or resource.last_listed != scope.scan_started):
                resource.missing_count += 1
                was_visible = resource.availability == "visible"
                resource.availability = "missing_unconfirmed"
                # 清单缺失不足以证明删除；保留快照，永不直接发删除通知。
                if kind == "assignment" and was_visible:
                    resource.version += 1
                    await emit(
                        session,
                        settings,
                        resource,
                        "assignment_unavailable",
                        {"last_seen": resource.last_seen.isoformat()},
                        now=now,
                        sync_run_id=sync_run_id,
                    )
        scope.baseline, scope.last_success = True, now
    if result.continues and scope.status not in {"schema_changed", "data_quality_error", "unsupported"}:
        scope.cursor, scope.scan_urls = result.cursor, result.scan_urls
    else:
        scope.cursor, scope.scan_urls, scope.scan_started = None, None, None
    delay = (
        timedelta(hours=24)
        if scope.status == "unsupported"
        else timedelta(
            seconds=settings.poll_seconds if scope.status == "ok" else max(settings.poll_seconds, 300)
        )
    )
    scope.next_attempt = now + delay
    return scope


async def health(session, name, status, details=None):
    row = await session.get(Health, name)
    if not row:
        row = Health(name=name, status=status)
        session.add(row)
    row.status, row.details, row.updated_at = status, details or {}, now_utc()
