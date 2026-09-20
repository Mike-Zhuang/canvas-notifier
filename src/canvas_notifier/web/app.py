import hmac
import json
import secrets
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import func, select
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from canvas_notifier.config import Settings, read_secret
from canvas_notifier.db import Delivery, Event, Health, Reminder, Resource, Rule, Scope, SyncRun, database
from canvas_notifier.delivery.queue import LABELS
from canvas_notifier.domain.time import now_utc, parse_time
from canvas_notifier.scheduling.planner import rebuild
from canvas_notifier.scheduling.rules import Rules, duration, effective_rules

ROOT = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=ROOT / "templates")


def create_app(settings=None, sessions=None):
    settings = settings or Settings()
    engine = None
    if sessions is None:
        engine, sessions = database(settings.database_url)
    session_key = read_secret(settings.session_secret_file)
    password = read_secret(settings.admin_password_file)
    if len(session_key) < 32 or len(password) < 12:
        raise ValueError("Run canvas-notifier init before starting web")

    @asynccontextmanager
    async def lifespan(app):
        yield
        if engine:
            await engine.dispose()

    app = FastAPI(title="Canvas 通知", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.sessions, app.state.settings = sessions, settings
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_key,
        session_cookie="canvas_admin",
        same_site="strict",
        https_only=settings.public_base_url.startswith("https:"),
        max_age=28800,
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "testserver", urlsplit(settings.public_base_url).hostname],
    )
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    attempts = {}

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "same-origin",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; form-action 'self'; base-uri 'self'",
            }
        )
        return response

    def csrf(request):
        if "csrf" not in request.session:
            request.session["csrf"] = secrets.token_urlsafe(32)
        return request.session["csrf"]

    def authenticated(request):
        return bool(request.session.get("authenticated"))

    async def form_data(request, require_auth=True):
        if require_auth and not authenticated(request):
            raise HTTPException(401, "请先登录")
        form = await request.form()
        if not hmac.compare_digest(str(form.get("csrf", "")), str(request.session.get("csrf", "missing"))):
            raise HTTPException(403, "表单已过期，请刷新后重试")
        origin = request.headers.get("origin")
        if (
            origin
            and origin.rstrip("/") != settings.public_base_url.rstrip("/")
            and origin != str(request.base_url).rstrip("/")
        ):
            raise HTTPException(403, "请求来源不匹配")
        return form

    async def base_context(request, active, **extra):
        async with sessions() as session:
            global_rule, _ = await effective_rules(session)
            sync = await session.get(Health, "sync")
        zone = ZoneInfo(global_rule.timezone)

        def local_time(value):
            try:
                dt = parse_time(value)
                return dt.astimezone(zone).strftime("%m-%d %H:%M") if dt else "未设置"
            except (ValueError, TypeError):
                return "时间异常"

        return {
            "request": request,
            "active": active,
            "csrf": csrf(request),
            "sync_health": sync,
            "zone": global_rule.timezone,
            "now": now_utc(),
            "labels": LABELS,
            "local_time": local_time,
            **extra,
        }

    @app.get("/healthz")
    async def healthz():
        try:
            async with sessions() as session:
                await session.execute(select(1))
            return {"status": "ok"}
        except Exception:
            return JSONResponse({"status": "database_unavailable"}, status_code=503)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        return TEMPLATES.TemplateResponse(
            request=request, name="login.html", context={"csrf": csrf(request), "error": ""}
        )

    @app.post("/login")
    async def login(request: Request):
        form = await form_data(request, False)
        address = request.client.host if request.client else "unknown"
        record = [stamp for stamp in attempts.get(address, []) if stamp > time.monotonic() - 300]
        if len(record) >= 8:
            raise HTTPException(429, "尝试次数过多，5 分钟后重试")
        if not hmac.compare_digest(str(form.get("password", "")).encode(), password.encode()):
            attempts[address] = [*record, time.monotonic()]
            return TEMPLATES.TemplateResponse(
                request=request,
                name="login.html",
                context={"csrf": csrf(request), "error": "密码不正确"},
                status_code=401,
            )
        request.session.clear()
        request.session.update({"authenticated": True, "csrf": secrets.token_urlsafe(32)})
        attempts.pop(address, None)
        return RedirectResponse("/", 303)

    @app.post("/logout")
    async def logout(request: Request):
        await form_data(request)
        request.session.clear()
        return RedirectResponse("/login", 303)

    @app.get("/", response_class=HTMLResponse)
    async def overview(request: Request, q: str = "", show: str = "future"):
        if not authenticated(request):
            return RedirectResponse("/login", 303)
        async with sessions() as session:
            tasks = list((await session.scalars(select(Resource).where(Resource.kind == "assignment"))).all())
            courses = {
                r.external_id: r
                for r in (await session.scalars(select(Resource).where(Resource.kind == "course"))).all()
            }
            submissions = {
                r.key: r
                for r in (await session.scalars(select(Resource).where(Resource.kind == "submission"))).all()
            }
            events = list((await session.scalars(select(Event).order_by(Event.id.desc()).limit(6))).all())
            scopes = list((await session.scalars(select(Scope))).all())
            pending = await session.scalar(
                select(func.count())
                .select_from(Delivery)
                .where(Delivery.status.in_(["pending", "retry", "failed"]))
            )
        rows = []
        for task in tasks:
            times = [parse_time(task.data.get(k)) for k in ("due_at", "lock_at") if task.data.get(k)]
            if task.local.get("personal_at"):
                times.append(parse_time(task.local["personal_at"]))
            upcoming = [dt for dt in times if dt >= now_utc()]
            if show == "future" and times and not upcoming:
                continue
            if (
                q
                and q.lower()
                not in (
                    task.data.get("name", "") + courses.get(task.course_id, task).data.get("name", "")
                ).lower()
            ):
                continue
            rows.append(
                {
                    "task": task,
                    "course": courses.get(task.course_id),
                    "submission": submissions.get(f"submission:{task.course_id}:{task.external_id}"),
                    "has_time": bool(times),
                    "next_time": min(upcoming)
                    if upcoming
                    else max(times)
                    if times
                    else now_utc() + timedelta(days=10000),
                }
            )
        rows.sort(key=lambda row: row["next_time"])
        return TEMPLATES.TemplateResponse(
            request=request,
            name="overview.html",
            context=await base_context(
                request,
                "overview",
                rows=rows,
                courses=courses,
                events=events,
                scopes=scopes,
                pending=pending,
                q=q,
                show=show,
                healthy=sum(s.status == "ok" for s in scopes),
                total=len(scopes),
            ),
        )

    @app.get("/rules", response_class=HTMLResponse)
    async def rules_page(request: Request, scope: str = "global", saved: str = ""):
        if not authenticated(request):
            return RedirectResponse("/login", 303)
        async with sessions() as session:
            courses = list((await session.scalars(select(Resource).where(Resource.kind == "course"))).all())
            tasks = list((await session.scalars(select(Resource).where(Resource.kind == "assignment"))).all())
            row = await session.get(Rule, scope)
            course_id = scope.split(":")[1] if ":" in scope else ""
            effective, sources = await effective_rules(
                session, course_id, scope if scope.startswith("assignment:") else ""
            )
            reminders = list(
                (
                    await session.scalars(
                        select(Reminder)
                        .where(Reminder.status == "pending")
                        .order_by(Reminder.scheduled_at)
                        .limit(30)
                    )
                ).all()
            )
            resource = await session.get(Resource, scope) if scope.startswith("assignment:") else None
        values = effective.model_dump()
        for key in ("due", "lock", "unlock", "personal"):

            def display_offset(value):
                seconds = duration(value)
                if seconds % 86400 == 0:
                    return f"P{seconds // 86400}D"
                if seconds % 3600 == 0:
                    return f"PT{seconds // 3600}H"
                if seconds % 60 == 0:
                    return f"PT{seconds // 60}M"
                return f"PT{seconds}S"

            values[key] = ", ".join(display_offset(v) for v in values[key])
        return TEMPLATES.TemplateResponse(
            request=request,
            name="rules.html",
            context=await base_context(
                request,
                "rules",
                courses=courses,
                tasks=tasks,
                scope=scope,
                values=values,
                sources=[
                    "全局默认"
                    if source == "global"
                    else "课程覆盖"
                    if source.startswith("course:")
                    else "单任务覆盖"
                    for source in sources
                ],
                overrides=row.data if row else {},
                reminders=reminders,
                task_names={t.key: t.data.get("name", "任务") for t in tasks},
                resource=resource,
                saved=saved,
                error="",
                advanced=json.dumps(effective.model_dump(), ensure_ascii=False, indent=2),
            ),
        )

    @app.post("/rules")
    async def save_rules(request: Request):
        form = await form_data(request)
        scope = str(form.get("scope", "global"))
        async with sessions() as session, session.begin():
            if scope != "global":
                target = await session.get(
                    Resource, "course::" + scope.split(":", 1)[1] if scope.startswith("course:") else scope
                )
                if not target:
                    raise HTTPException(400, "规则目标不存在")
            try:
                if form.get("mode") == "json":
                    raw = json.loads(str(form.get("config", "{}")))
                    validated = Rules.model_validate(raw)
                    data = validated.model_dump(exclude_unset=True)
                else:
                    raw = {
                        key: [v.strip() for v in str(form.get(key, "")).split(",") if v.strip()]
                        for key in ("due", "lock", "unlock", "personal")
                    }
                    raw.update(
                        {
                            key: form.get(key) == "on"
                            for key in (
                                "enabled",
                                "only_incomplete",
                                "score_in_email",
                                "quiet_enabled",
                                "overdue_enabled",
                                "pinned",
                            )
                        }
                    )
                    raw.update(
                        {
                            key: str(form[key])
                            for key in (
                                "timezone",
                                "quiet_start",
                                "quiet_end",
                                "critical_policy",
                                "stale_source_policy",
                            )
                        }
                    )
                    raw.update(
                        {key: int(str(form[key])) for key in ("digest_hour", "overdue_max", "history_days")}
                    )
                    raw["default_event"] = str(form["default_event"])
                    data = Rules.model_validate(raw).model_dump(exclude_unset=True)
                    old = await session.get(Rule, scope)
                    data = {**(old.data if old else {}), **data}
                # 清空覆盖显式恢复继承，不能用默认值伪装继承。
                old = await session.get(Rule, scope)
                if form.get("reset") == "true" and scope != "global":
                    if old:
                        await session.delete(old)
                elif old:
                    if old.data != data:
                        old.data, old.version, old.updated_at = data, old.version + 1, now_utc()
                else:
                    session.add(Rule(scope=scope, data=data))
                await session.flush()
                await rebuild(session)
            except (ValidationError, ValueError, TypeError):
                raise HTTPException(
                    422, "规则无效：请检查 ISO 时长、时区和数字范围。原规则未修改。"
                ) from None
        return RedirectResponse("/rules?scope=" + scope + "&saved=1", 303)

    @app.post("/tasks/local")
    async def local_task(request: Request):
        form = await form_data(request)
        async with sessions() as session, session.begin():
            resource = await session.get(Resource, str(form["key"]))
            if not resource or resource.kind != "assignment":
                raise HTTPException(404)
            try:
                personal = parse_time(str(form.get("personal_at") or ""))
                snooze = parse_time(str(form.get("snooze_until") or ""))
            except ValueError:
                raise HTTPException(422, "请使用含时区的 ISO 时间，例如 2026-10-01T18:00:00+08:00") from None
            resource.local = {
                "completed": form.get("completed") == "on",
                "personal_at": personal.isoformat() if personal else None,
                "snooze_until": snooze.isoformat() if snooze else None,
            }
            await rebuild(session)
        return RedirectResponse("/rules?scope=" + str(form["key"]) + "&saved=1", 303)

    @app.get("/history", response_class=HTMLResponse)
    async def history(request: Request, status: str = "", page: int = 1):
        if not authenticated(request):
            return RedirectResponse("/login", 303)
        page = max(1, page)
        async with sessions() as session:
            query = select(Delivery)
            if status:
                query = query.where(Delivery.status == status)
            deliveries = list(
                (
                    await session.scalars(
                        query.order_by(Delivery.id.desc()).offset((page - 1) * 50).limit(51)
                    )
                ).all()
            )
            events = list(
                (
                    await session.scalars(
                        select(Event).where(Event.disposition != "queued").order_by(Event.id.desc()).limit(30)
                    )
                ).all()
            )
            withheld_reminders = list(
                (
                    await session.scalars(
                        select(Reminder)
                        .where(Reminder.status.in_(["cancelled", "suppressed"]))
                        .order_by(Reminder.scheduled_at.desc())
                        .limit(30)
                    )
                ).all()
            )
        return TEMPLATES.TemplateResponse(
            request=request,
            name="history.html",
            context=await base_context(
                request,
                "history",
                deliveries=deliveries[:50],
                more=len(deliveries) > 50,
                events=events,
                withheld_reminders=withheld_reminders,
                status=status,
                page=page,
            ),
        )

    @app.post("/history/retry")
    async def retry(request: Request):
        form = await form_data(request)
        async with sessions() as session, session.begin():
            row = await session.get(Delivery, int(str(form["id"])))
            if row and row.status in ("retry", "failed"):
                row.status, row.available_at, row.attempts = "pending", now_utc(), 0
        return RedirectResponse("/history", 303)

    @app.get("/health", response_class=HTMLResponse)
    async def health_page(request: Request):
        if not authenticated(request):
            return RedirectResponse("/login", 303)
        async with sessions() as session:
            scopes = list((await session.scalars(select(Scope).order_by(Scope.course_id, Scope.kind))).all())
            health = list((await session.scalars(select(Health))).all())
            runs = list((await session.scalars(select(SyncRun).order_by(SyncRun.id.desc()).limit(10))).all())
            courses = {
                r.external_id: r.data.get("name")
                for r in (await session.scalars(select(Resource).where(Resource.kind == "course"))).all()
            }
        return TEMPLATES.TemplateResponse(
            request=request,
            name="health.html",
            context=await base_context(
                request,
                "health",
                scopes=scopes,
                health=health,
                runs=runs,
                courses=courses,
                credentials={
                    "Bearer": settings.canvas_token_file.exists(),
                    "Cookie": settings.canvas_cookie_file.exists(),
                },
                smtp_configured=bool(settings.smtp_host and settings.mail_to),
            ),
        )

    @app.post("/api/rules/preview")
    async def preview_rules(request: Request):
        from canvas_notifier.scheduling.planner import calculate_schedule

        form = await form_data(request)
        scope = str(form.get("scope", "global"))
        try:
            raw = {
                key: [v.strip() for v in str(form.get(key, "")).split(",") if v.strip()]
                for key in ("due", "lock", "unlock", "personal")
            }
            raw.update(
                {
                    key: form.get(key) == "on"
                    for key in ("enabled", "only_incomplete", "quiet_enabled", "overdue_enabled")
                }
            )
            raw.update(
                {
                    key: str(form[key])
                    for key in (
                        "timezone",
                        "quiet_start",
                        "quiet_end",
                        "critical_policy",
                        "stale_source_policy",
                    )
                }
            )
            raw["overdue_max"] = int(str(form["overdue_max"]))
            validated = Rules.model_validate(raw).model_dump(exclude_unset=True)
        except (ValueError, TypeError):
            raise HTTPException(422, "请检查 ISO 时长和时区") from None
        results = []
        now = now_utc()
        async with sessions() as session:
            query = select(Resource).where(Resource.kind == "assignment")
            if scope.startswith("assignment:"):
                query = query.where(Resource.key == scope)
            elif scope.startswith("course:"):
                query = query.where(Resource.course_id == scope.split(":", 1)[1])
            tasks = (await session.scalars(query)).all()
            for task in tasks:
                merged = {}
                for level in ("global", "course:" + task.course_id, task.key):
                    row = await session.get(Rule, level)
                    override = validated if level == scope else row.data if row else {}
                    merged.update(override)
                rule = Rules.model_validate(merged)
                sub = await session.get(Resource, f"submission:{task.course_id}:{task.external_id}")
                desired = calculate_schedule(task, sub.data if sub else {}, rule, now)
                coalesced = set()
                for label, boundary, trigger, offset in desired.values():
                    if trigger <= now:
                        group = (label, boundary)
                        if group in coalesced:
                            continue
                        coalesced.add(group)
                    results.append(
                        {
                            "at": max(trigger, now),
                            "label": {
                                "due": "正式截止",
                                "lock": "停止提交",
                                "unlock": "开放",
                                "personal": "个人计划",
                                "overdue": "逾期",
                            }[label],
                            "title": task.data.get("name", "任务"),
                            "timezone": rule.timezone,
                        }
                    )
        results.sort(key=lambda item: item["at"])
        return {
            "items": [
                {
                    "scheduled_at": item["at"]
                    .astimezone(ZoneInfo(item["timezone"]))
                    .strftime("%Y-%m-%d %H:%M"),
                    "label": item["label"],
                    "title": item["title"],
                }
                for item in results[:30]
            ]
        }

    @app.get("/api/reminders")
    async def preview(request: Request):
        if not authenticated(request):
            raise HTTPException(401)
        async with sessions() as session:
            jobs = (await session.scalars(select(Reminder).order_by(Reminder.scheduled_at).limit(200))).all()
        return [
            {
                "resource": j.resource_key,
                "boundary": j.boundary_type,
                "scheduled_at": j.scheduled_at,
                "status": j.status,
                "reason": j.reason,
            }
            for j in jobs
        ]

    return app
