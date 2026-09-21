from datetime import timedelta

from sqlalchemy import select

from canvas_notifier.canvas import adapters
from canvas_notifier.canvas.http import CanvasClient, CanvasError
from canvas_notifier.db import Account, Delivery, Health, Resource, Scope, SyncRun
from canvas_notifier.delivery.queue import enqueue
from canvas_notifier.domain.time import now_utc, parse_time
from canvas_notifier.scheduling.planner import rebuild
from canvas_notifier.scheduling.rules import effective_rules
from canvas_notifier.sync.leases import lease
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
            raise errors.get("token") or errors.get("cookie") or CanvasError("reauth_required")
    if client.settings.canvas_auth_mode == "token" and not token_id:
        if not (
            client.settings.canvas_cookie_fallback
            and "identity" in client.settings.canvas_cookie_resources.split(",")
        ):
            raise errors.get("token") or CanvasError("reauth_required")
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


async def sync_once(settings, sessions, *, force=True, transport=None):
    async with lease(sessions, "sync") as acquired:
        if not acquired:
            return {"status": "already_running"}
        async with sessions() as session, session.begin():
            run = SyncRun(status="running")
            session.add(run)
            await session.flush()
            run_id = run.id
        summary = {"complete_scopes": 0, "failed_scopes": 0}
        async with CanvasClient(settings, transport=transport) as client:
            try:
                await bind_identity(sessions, client)

                async def apply(kind, course_id, result, suffix=""):
                    async with sessions() as session, session.begin():
                        scope = await apply_scope(
                            session,
                            settings,
                            kind,
                            course_id,
                            result,
                            scope_suffix=suffix,
                            user_id=client.user_id,
                        )
                        if transport is None and scope.complete:
                            scope.verification = (
                                "observed_nonempty_on_tongji" if result.items else "observed_empty_on_tongji"
                            )
                        summary["complete_scopes" if scope.complete else "failed_scopes"] += 1

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
                    async with sessions() as session:
                        rule, _ = await effective_rules(session, cid)
                        assignments_scope = await session.get(Scope, f"assignment:{cid}:")
                        file_scope = await session.get(Scope, f"file:{cid}:")
                    ended = course.data.get("workflow_state") == "completed"
                    term = course.data.get("term") or {}
                    end = parse_time(course.data.get("end_at") or term.get("end_at"))
                    if end and now_utc() > end:
                        ended = True
                    if (
                        ended
                        and end
                        and now_utc() - end > timedelta(days=rule.history_days)
                        and not rule.pinned
                    ):
                        continue
                    interval = settings.historical_poll_seconds if ended else settings.poll_seconds
                    due = (
                        force
                        or not assignments_scope
                        or not assignments_scope.last_success
                        or now_utc() - assignments_scope.last_success >= timedelta(seconds=interval)
                    )
                    if due:
                        assignment_result = await adapters.assignments(client, cid)
                        await apply("assignment", cid, assignment_result)
                        submission_result = await adapters.submissions(client, cid, assignment_result.items)
                        await apply("submission", cid, submission_result)
                    content_due = (
                        force
                        or not file_scope
                        or not file_scope.last_success
                        or now_utc() - file_scope.last_success
                        >= timedelta(seconds=settings.content_poll_seconds)
                    )
                    if content_due:
                        async for kind, suffix, result in adapters.content_scopes(client, cid):
                            await apply(kind, cid, result, suffix)
                    elif due:
                        announcements = await client.paginate(
                            f"/api/v1/courses/{cid}/discussion_topics",
                            [("only_announcements", "true"), ("per_page", "100")],
                            kind="announcement",
                        )
                        await apply("announcement", cid, announcements)
                async for kind, result in adapters.global_scopes(client):
                    await apply(kind, "", result)
                async with sessions() as session, session.begin():
                    await rebuild(session)
                    initial = await session.get(Health, "initial_digest")
                    if not initial and summary["complete_scopes"] > 0:
                        rows = (
                            await session.scalars(select(Resource).where(Resource.kind == "assignment"))
                        ).all()
                        future = [
                            r
                            for r in rows
                            if any(
                                r.data.get(k) and parse_time(r.data[k]) > now_utc()
                                for k in ("due_at", "lock_at")
                            )
                        ]
                        await enqueue(
                            session,
                            settings,
                            "initial_digest",
                            None,
                            "baseline_digest",
                            {
                                "future_tasks": [
                                    {
                                        "name": r.data.get("name"),
                                        "due_at": r.data.get("due_at"),
                                        "lock_at": r.data.get("lock_at"),
                                    }
                                    for r in future
                                ]
                            },
                        )
                        await health(session, "initial_digest", "created")
                # 本轮可能只执行高频资源；不能因跳过低频失败范围而把全局状态染绿。
                async with sessions() as session:
                    unhealthy = (await session.scalars(select(Scope).where(Scope.status != "ok"))).all()
                    summary["unhealthy_scopes"] = len(unhealthy)
                status = "partial" if unhealthy else "ok"
            except CanvasError as error:
                status = error.code
            except Exception as error:
                # 只记录错误类别，不记录可能含有带凭据 URL 的异常文本。
                status = "internal_error:" + type(error).__name__
            async with sessions() as session, session.begin():
                previous = await session.get(Health, "sync")
                if (previous is None and status != "ok") or (previous and previous.status != status):
                    stale = (
                        await session.scalars(
                            select(Delivery).where(
                                Delivery.status.in_(["pending", "retry"]),
                                Delivery.unique_key.startswith("sync-state:"),
                            )
                        )
                    ).all()
                    for pending in stale:
                        pending.status, pending.reason = "cancelled", "同步状态已经变化"
                    await enqueue(
                        session,
                        settings,
                        f"sync-state:{now_utc().isoformat()}",
                        None,
                        "sync_recovered" if status == "ok" else "sync_interrupted",
                        {"status": status},
                    )
                await health(session, "sync", status, summary)
                auth_status = (
                    "degraded"
                    if client.cookie_verified
                    and client.health.get("token")
                    in (
                        "auth_rejected",
                        "bearer_expired_or_rejected",
                        "login_html_returned",
                        "bearer_missing",
                    )
                    else "ok"
                    if client.user_id
                    else "reauth_required"
                )
                previous_auth = await session.get(Health, "auth")
                if auth_status == "degraded" and (not previous_auth or previous_auth.status != "degraded"):
                    await enqueue(
                        session,
                        settings,
                        f"auth-degraded:{now_utc().isoformat()}",
                        None,
                        "auth_degraded",
                        {"status": "Bearer 不可用，正在使用已验证的 Cookie 备用通道"},
                    )
                await health(session, "auth", auth_status, client.health)
                run = await session.get(SyncRun, run_id)
                run.status, run.summary, run.finished_at = status, summary, now_utc()
        return {"status": status, **summary}
