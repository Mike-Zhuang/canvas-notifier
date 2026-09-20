import asyncio
import json
import secrets
import tempfile
from pathlib import Path

import typer
from sqlalchemy import select, text

from canvas_notifier.canvas.http import CanvasClient, CanvasError
from canvas_notifier.config import Settings, write_secret
from canvas_notifier.db import Delivery, Health, Reminder, create_schema, database
from canvas_notifier.delivery.queue import enqueue, recipients
from canvas_notifier.domain.time import now_utc
from canvas_notifier.sync.service import sync_once

app = typer.Typer(
    no_args_is_help=True, pretty_exceptions_show_locals=False, help="Canvas 个人通知服务，只读采集。"
)
auth_app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False)
mail_app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False)
reminder_app = typer.Typer(no_args_is_help=True, pretty_exceptions_show_locals=False)
app.add_typer(auth_app, name="auth")
app.add_typer(mail_app, name="mail")
app.add_typer(reminder_app, name="reminders")


def output(value):
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def run_with_db(function):
    async def main():
        settings = Settings()
        engine, sessions = database(settings.database_url)
        try:
            return await function(settings, sessions)
        finally:
            await engine.dispose()

    return asyncio.run(main())


@app.command()
def init():
    """创建本地管理凭据；数据库结构使用 alembic upgrade head。"""
    settings = Settings()
    Path(".local").mkdir(exist_ok=True, mode=0o700)
    for path in (settings.admin_password_file, settings.session_secret_file):
        if not path.exists():
            write_secret(path, secrets.token_urlsafe(32))
    output(
        {
            "credentials": "created_or_preserved",
            "admin_password_file": str(settings.admin_password_file),
            "next": "uv run alembic upgrade head",
        }
    )


@auth_app.command("token-test")
def token_test(
    watch: bool = False,
    interval: int = typer.Option(300, min=1),
    max_checks: int = typer.Option(0, min=0),
    mode: str = "token",
):
    """验证身份；watch 记录真实生命周期，不输出 Token、姓名或响应正文。"""
    if mode not in ("token", "cookie"):
        raise typer.BadParameter("mode must be token or cookie")

    async def main():
        settings = Settings()
        checks = 0
        path = Path(".local/auth-watch.jsonl")
        path.parent.mkdir(exist_ok=True, mode=0o700)
        async with CanvasClient(settings) as client:
            while True:
                row = {"checked_at": now_utc().isoformat(), "mode": mode}
                try:
                    identity = await client.identity(mode)
                    row.update({"ok": bool(identity), "state": client.health.get(mode), "status": 200})
                except CanvasError as error:
                    row.update({"ok": False, "state": error.code, "status": error.status})
                with path.open("a") as file:
                    file.write(json.dumps(row) + "\n")
                path.chmod(0o600)
                output(row)
                checks += 1
                if not watch or not row["ok"] or (max_checks and checks >= max_checks):
                    return bool(row["ok"])
                await asyncio.sleep(interval)

    if not asyncio.run(main()):
        raise typer.Exit(1)


@auth_app.command("iam-test")
def iam_test(force: bool = False):
    """验证 IAM 恢复；--force 明确发起新的登录交换，但不绕过验证码/增强认证。"""
    from canvas_notifier.auth.recovery import recover_session

    async def main(settings, sessions):
        async with CanvasClient(settings) as client:
            try:
                identity = await recover_session(sessions, client, force=force)
                return {
                    "ok": bool(identity),
                    "cookie_verified": client.cookie_verified,
                    "state": "recovered_or_reused",
                }
            except CanvasError as error:
                return {"ok": False, "state": error.code}

    result = run_with_db(main)
    output(result)
    if not result["ok"]:
        raise typer.Exit(1)


@auth_app.command("login")
def login(timeout: int = typer.Option(600, min=30, max=3600), remember_iam: bool = False):
    """用户交互登录；--remember-iam 保存经本人授权的 IAM 会话以供 SSO 恢复。"""
    from canvas_notifier.auth.browser import interactive_login

    try:
        output(asyncio.run(interactive_login(Settings(), remember_iam=remember_iam, timeout=timeout)))
    except CanvasError as error:
        output({"ok": False, "state": error.code})
        raise typer.Exit(1) from None


@app.command()
def doctor():
    """检查数据库、凭据通道与 worker 心跳；不发邮件。"""

    async def main(settings, sessions):
        result = {"database": "ok", "channels": {}}
        async with sessions() as session:
            await session.execute(text("SELECT 1"))
            result["health"] = {
                row.name: {"status": row.status, "updated_at": row.updated_at}
                for row in (await session.scalars(select(Health))).all()
            }
        async with CanvasClient(settings) as client:
            for mode in ("token", "cookie"):
                try:
                    result["channels"][mode] = "ok" if await client.identity(mode) else "invalid"
                except CanvasError as error:
                    result["channels"][mode] = error.code
        result["mail"] = {
            "host_configured": bool(settings.smtp_host),
            "recipient_count": len(recipients(settings.mail_to)),
        }
        return result

    output(run_with_db(main))


@app.command()
def sync(once: bool = True, dry_run: bool = False):
    """只读采集；dry-run 使用临时数据库，不改正式状态，不发送邮件。"""

    async def main():
        settings = Settings()
        if dry_run:
            with tempfile.TemporaryDirectory(prefix="canvas-dry-run-") as directory:
                engine, sessions = database("sqlite+aiosqlite:///" + directory + "/state.db")
                await create_schema(engine)
                try:
                    result = await sync_once(settings.model_copy(update={"mail_to": ""}), sessions)
                finally:
                    await engine.dispose()
        else:
            engine, sessions = database(settings.database_url)
            try:
                result = await sync_once(settings, sessions)
            finally:
                await engine.dispose()
        output({**result, "dry_run": dry_run})
        return result["status"]

    if asyncio.run(main()) not in ("ok", "already_running"):
        raise typer.Exit(1)


@reminder_app.command("preview")
def reminder_preview():
    async def main(settings, sessions):
        async with sessions() as session:
            rows = (await session.scalars(select(Reminder).order_by(Reminder.scheduled_at))).all()
            return [
                {
                    "task": row.resource_key,
                    "boundary": row.boundary_type,
                    "trigger": row.scheduled_at,
                    "status": row.status,
                    "reason": row.reason,
                }
                for row in rows
            ]

    output(run_with_db(main))


@mail_app.command("test")
def mail_test():
    """仅发送到明确配置的 MAIL_TEST_TO。"""

    async def main(settings, sessions):
        if not settings.mail_test_to:
            raise typer.BadParameter("Set MAIL_TEST_TO before sending a test")
        unique = "test:" + secrets.token_hex(12)
        async with sessions() as session, session.begin():
            await enqueue(
                session,
                settings,
                unique,
                None,
                "test",
                force_recipient=settings.mail_test_to,
                mode_override="immediate",
            )
        # 只投递此次测试，不顺便发送已有真实通知。
        from canvas_notifier.delivery.smtp import render_message, send

        async with sessions() as session:
            rows = (
                await session.scalars(select(Delivery).where(Delivery.unique_key.startswith(unique)))
            ).all()
            for row in rows:
                try:
                    await send(settings, render_message(settings, row))
                    row.status, row.accepted_at, row.reason, row.attempts = (
                        "accepted",
                        now_utc(),
                        "SMTP 已接受，收件箱需另行验证",
                        1,
                    )
                except Exception as error:
                    row.status, row.reason, row.attempts = "retry", type(error).__name__, 1
            await session.commit()
            return {"test_deliveries": [{"id": r.id, "status": r.status, "reason": r.reason} for r in rows]}

    output(run_with_db(main))


@app.command()
def worker(once: bool = False):
    """常驻同步、提醒和投递三个独立环节。"""
    from canvas_notifier.worker import tick
    from canvas_notifier.worker import worker as serve_worker

    async def main(settings, sessions):
        if once:
            await tick(settings, sessions)
        else:
            await serve_worker(settings, sessions)

    run_with_db(main)


@app.command()
def web(host: str = "127.0.0.1", port: int = 8000):
    """启动有认证的本地管理站点。"""
    import uvicorn

    from canvas_notifier.web.app import create_app

    uvicorn.run(create_app(), host=host, port=port, access_log=False)


@app.command()
def demo():
    """只允许在独立 demo 数据库导入合成的未来任务。"""

    async def main(settings, sessions):
        if "demo" not in settings.database_url:
            raise typer.BadParameter("Set DATABASE_URL=sqlite+aiosqlite:///.local/demo.db")
        from datetime import timedelta

        from canvas_notifier.canvas.http import PageResult
        from canvas_notifier.scheduling.planner import rebuild
        from canvas_notifier.sync.state import apply_scope

        now = now_utc()
        async with sessions() as session, session.begin():
            await apply_scope(
                session,
                settings,
                "course",
                "",
                PageResult([{"id": "9001", "name": "示例课程 · 数据与系统"}], 1, True),
            )
            await apply_scope(
                session,
                settings,
                "assignment",
                "9001",
                PageResult(
                    [
                        {
                            "id": "9002",
                            "name": "实验报告：状态与事件",
                            "due_at": (now + timedelta(hours=20)).isoformat(),
                            "lock_at": (now + timedelta(days=2)).isoformat(),
                            "submission_types": ["online_upload"],
                        },
                        {
                            "id": "9003",
                            "name": "章节自测：事务与一致性",
                            "due_at": None,
                            "lock_at": (now + timedelta(hours=8)).isoformat(),
                            "submission_types": ["online_quiz"],
                        },
                    ],
                    1,
                    True,
                ),
            )
            await rebuild(session)
        return {"demo": "ready"}

    settings = Settings()
    if "demo" in settings.database_url:

        async def schema():
            engine, _ = database(settings.database_url)
            await create_schema(engine)
            await engine.dispose()

        asyncio.run(schema())
    output(run_with_db(main))


if __name__ == "__main__":
    app()
