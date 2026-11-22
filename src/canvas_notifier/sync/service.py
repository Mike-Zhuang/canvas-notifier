from collections import defaultdict
from datetime import timedelta

from sqlalchemy import select

from canvas_notifier.canvas import adapters
from canvas_notifier.canvas.http import CanvasError
from canvas_notifier.db import Account, Health, Resource, Rule, Scope, SyncRun
from canvas_notifier.delivery.queue import enqueue
from canvas_notifier.domain.time import now_utc, parse_time
from canvas_notifier.scheduling.planner import rebuild
from canvas_notifier.scheduling.rules import effective_rules
from canvas_notifier.sync.alerts import OWNER_ACTION, observe_sync
from canvas_notifier.sync.leases import canvas_session, lease, remaining_seconds
from canvas_notifier.sync.state import apply_scope, health


async def bind_identity(sessions, client):
    from canvas_notifier.auth.recovery import REAUTH_ERRORS, recover_session

    token_id = cookie_id = None
    errors = {}
    if client.settings.canvas_auth_mode == "token" or client.token:
        try:
            token_id = await client.identity("token")
        except CanvasError as error:
            errors["token"] = error
            client.health["token"] = error.code
    use_cookie = (
        client.settings.canvas_cookie_fallback
        or client.settings.canvas_auth_mode == "cookie"
        or client.settings.iam_auto_login
    )
    if use_cookie:
        try:
            cookie_id = await client.identity("cookie")
        except CanvasError as error:
            errors["cookie"] = error
            client.health["cookie"] = error.code
    if token_id and cookie_id and token_id != cookie_id:
        raise CanvasError("credential_identity_mismatch")
    if not token_id and not cookie_id:
        # 网络、限流和资源权限失败不是密码失效，不得触发反复登录。
        non_auth = next((error for error in errors.values() if error.code not in REAUTH_ERRORS), None)
        if non_auth:
            raise non_auth
        if client.settings.iam_auto_login:
            cookie_id = await recover_session(sessions, client)
        else:
            raise CanvasError("reauth_required")
    if client.settings.canvas_auth_mode == "token" and not token_id:
        if not (
            client.settings.canvas_cookie_fallback
            and "identity" in client.settings.canvas_cookie_resources.split(",")
        ):
            error = errors.get("token")
            if error and error.code not in REAUTH_ERRORS:
                raise error
            raise CanvasError("reauth_required")
        if "token" in errors and errors["token"].code not in REAUTH_ERRORS:
            raise errors["token"]
    if client.settings.canvas_auth_mode == "cookie" and not cookie_id and token_id:
        client.settings = client.settings.model_copy(update={"canvas_auth_mode": "token"})
    identity = token_id or cookie_id
    async with sessions() as session, session.begin():
        account = await session.get(Account, 1)
        if account and (account.user_id != identity or account.origin != client.origin):
            raise CanvasError("account_changed_requires_new_database")
        if not account:
            account = Account(id=1, origin=client.origin, user_id=identity)
            session.add(account)
        account.auth = client.health
    client.user_id, client.cookie_verified = identity, bool(cookie_id)


async def initial_digests(session, settings, now):
    courses = await session.scalars(
        select(Resource)
        .join(Scope, Scope.course_id == Resource.external_id)
        .where(Resource.kind == "course", Scope.kind == "assignment", Scope.baseline.is_(True))
    )
    for course in courses:
        key = f"initial_digest:{course.external_id}"
        if await session.get(Health, key):
            continue
        assignments = await session.scalars(
            select(Resource).where(
                Resource.kind == "assignment",
                Resource.course_id == course.external_id,
                Resource.availability == "visible",
            )
        )
        future = []
        for task in assignments:
            dates = [parse_time(task.data.get(field)) for field in ("due_at", "lock_at")]
            if any(date and date > now for date in dates):
                future.append({field: task.data.get(field) for field in ("name", "due_at", "lock_at")})
        state = await enqueue(
            session, settings, key, course, "baseline_digest", {"future_tasks": future}, now=now
        )
        # 无收件人或已关闭时不消耗首次摘要；下一轮仍可按该课程规则入队。
        if state == "queued":
            await health(session, key, "created")


async def sync_once(settings, sessions, *, force=True, include_history=False, transport=None):
    async with lease(sessions, "sync") as acquired:
        if not acquired:
            return {"status": "already_running"}
        async with canvas_session(settings, sessions, transport=transport) as client:
            if wait := remaining_seconds(client.retry_at):
                return {"status": "rate_limited", "retry_seconds": wait}
            async with sessions() as session, session.begin():
                run = SyncRun(status="running")
                session.add(run)
                await session.flush()
                run_id = run.id
                scopes_by_course = defaultdict(list)
                for scope in await session.scalars(select(Scope)):
                    client.scopes[scope.key] = scope
                    scopes_by_course[scope.course_id].append(scope)
            summary = {"complete_scopes": 0, "failed_scopes": 0}
            try:
                await bind_identity(sessions, client)

                async def apply(kind, course_id, result, suffix=""):
                    if result.skipped:
                        return client.scopes.get(f"{kind}:{course_id}:{suffix}")
                    async with sessions() as session, session.begin():
                        scope = await apply_scope(
                            session,
                            settings,
                            kind,
                            course_id,
                            result,
                            scope_suffix=suffix,
                            user_id=client.user_id,
                            sync_run_id=run_id,
                        )
                        if transport is None and scope.complete:
                            scope.verification = (
                                "observed_nonempty_on_tongji" if result.items else "observed_empty_on_tongji"
                            )
                        if scope.status != "unsupported":
                            summary["complete_scopes" if scope.complete else "failed_scopes"] += 1
                    client.check_retry_after()
                    return scope

                course_result = await adapters.courses(client)
                await apply("course", "", course_result)
                async with sessions() as session:
                    all_courses = list(
                        (await session.scalars(select(Resource).where(Resource.kind == "course"))).all()
                    )
                for course in all_courses:
                    cid = course.external_id
                    if not cid.isdigit():
                        continue
                    assignment_scope = client.scopes.get(f"assignment:{cid}:")
                    submission_scope = client.scopes.get(f"submission:{cid}:")
                    ended = course.data.get("workflow_state") == "completed"
                    term = course.data.get("term") or {}
                    end = parse_time(course.data.get("end_at") or term.get("end_at"))
                    now = now_utc()
                    if end and now >= end:
                        ended = True
                    if ended and end and not include_history:
                        async with sessions() as session:
                            rule, _ = await effective_rules(session, cid)
                            age = now - end
                            if not rule.pinned and age > timedelta(days=rule.history_days):
                                # 一个任务的延长关注仍需要扫描课程清单，才能可靠判断可见性。
                                task_rules = await session.scalars(
                                    select(Rule.data)
                                    .join(Resource, Resource.key == Rule.scope)
                                    .where(Resource.kind == "assignment", Resource.course_id == cid)
                                )
                                if not any(
                                    task.get("pinned", rule.pinned)
                                    or age <= timedelta(days=task.get("history_days", rule.history_days))
                                    for task in task_rules
                                ):
                                    continue
                    interval = settings.historical_poll_seconds if ended else settings.poll_seconds

                    def due_for(scope, seconds):
                        if scope and scope.status == "unsupported":
                            return not scope.next_attempt or now >= scope.next_attempt
                        return (
                            force
                            or not scope
                            or not scope.last_success
                            or scope.status != "ok"
                            or now - scope.last_success >= timedelta(seconds=seconds)
                        )

                    due = due_for(assignment_scope, interval)
                    if due:
                        assignment_result = await adapters.assignments(client, cid)
                        assignment_scope = await apply("assignment", cid, assignment_result)
                    if (
                        (due or due_for(submission_scope, interval))
                        and assignment_scope
                        and (assignment_scope.baseline or assignment_scope.status == "unsupported")
                    ):
                        async with sessions() as session:
                            assignments = (
                                await session.scalars(
                                    select(Resource.data).where(
                                        Resource.kind == "assignment",
                                        Resource.course_id == cid,
                                        Resource.availability == "visible",
                                    )
                                )
                            ).all()
                        submission_result = await adapters.submissions(client, cid, assignments)
                        await apply("submission", cid, submission_result)
                    content_scopes = [
                        scope
                        for scope in scopes_by_course[cid]
                        if scope.kind not in ("assignment", "submission")
                    ]
                    pending = [scope for scope in content_scopes if scope.cursor and scope.scan_started]
                    content_due = (
                        bool(pending)
                        or any(
                            due_for(client.scopes.get(f"{kind}:{cid}:"), settings.content_poll_seconds)
                            for kind in ("file", "folder", "discussion", "module", "page")
                        )
                        or any(
                            scope.baseline and scope.status not in ("ok", "unsupported")
                            for scope in content_scopes
                        )
                    )
                    if content_due:
                        async for kind, suffix, result in adapters.content_scopes(client, cid, pending):
                            await apply(kind, cid, result, suffix)
                    elif due:
                        announcements = await client.paginate(
                            f"/api/v1/courses/{cid}/discussion_topics",
                            [("only_announcements", "true"), ("per_page", "100")],
                            kind="announcement",
                            scope_key=f"announcement:{cid}:",
                        )
                        await apply("announcement", cid, announcements)
                async for kind, result in adapters.global_scopes(client):
                    await apply(kind, "", result)
                async with sessions() as session, session.begin():
                    await rebuild(session)
                    await initial_digests(session, settings, now_utc())
                # 本轮可能只执行高频资源；不能因跳过低频失败范围而把全局状态染绿。
                async with sessions() as session:
                    statuses = (await session.scalars(select(Scope.status))).all()
                    summary["unsupported_scopes"] = statuses.count("unsupported")
                    summary["unhealthy_scopes"] = sum(s not in ("ok", "unsupported") for s in statuses)
                status = "partial" if summary["unhealthy_scopes"] else "ok"
            except CanvasError as error:
                status = error.code
            except Exception as error:
                # 只记录错误类别，不记录可能含有带凭据 URL 的异常文本。
                status = "internal_error:" + type(error).__name__
            async with sessions() as session, session.begin():
                unhealthy = (
                    await session.scalars(
                        select(Scope).where(
                            Scope.status.not_in(("ok", "unsupported")), Scope.baseline.is_(True)
                        )
                    )
                ).all()
                failures = [{"scope": s.key, "status": s.status} for s in unhealthy]
                if status not in ("ok", "partial"):
                    failures = [{"scope": "authentication_or_sync", "status": status}]
                summary["retry_seconds"] = max(
                    await observe_sync(session, settings, failures), remaining_seconds(client.retry_at)
                )
                await health(session, "sync", status, summary)
                auth_status = (
                    "ok" if client.user_id else "reauth_required" if status in OWNER_ACTION else "degraded"
                )
                if client.cookie_verified:
                    token = client.health.get("token")
                    if token in ("auth_rejected", "bearer_expired_or_rejected", "login_html_returned"):
                        client.health["detail"] = "Cookie 通道正常，Token 已失效"
                    elif token == "bearer_missing":
                        client.health["detail"] = "Cookie 通道正常，未配置 Token"
                await health(session, "auth", auth_status, client.health)
                run = await session.get(SyncRun, run_id)
                run.status, run.summary, run.finished_at = status, summary, now_utc()
        return {"status": status, **summary}
