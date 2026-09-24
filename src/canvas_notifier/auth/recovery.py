"""双方凭据确实失效后才发起 IAM，使用数据库租约和持久冷却避免登录风暴。"""

import json
from datetime import timedelta

from canvas_notifier.auth.iam import IAMLogin
from canvas_notifier.canvas.http import CanvasError
from canvas_notifier.config import read_secret, write_secret
from canvas_notifier.db import Account, Health
from canvas_notifier.delivery.queue import enqueue
from canvas_notifier.domain.time import now_utc, parse_time
from canvas_notifier.sync.leases import lease

REAUTH_ERRORS = {"auth_rejected", "login_html_returned", "bearer_missing", "cookie_missing"}
BLOCKED_ERRORS = {
    "iam_rejected",
    "iam_interaction_required",
    "iam_schema_changed",
    "iam_state_mismatch",
    "iam_redirect_blocked",
    "iam_post_blocked",
    "iam_post_redirect_blocked",
    "iam_identity_mismatch",
    "iam_oauth_target_mismatch",
    "iam_encryption_failed",
}


def credential_revision(settings):
    # 只记录文件修改时间，不能把密码摘要变成数据库里的可离线猜测凭据。
    return [
        path.stat().st_mtime_ns if path.exists() else 0
        for path in (settings.iam_username_file, settings.iam_password_file, settings.iam_cookie_file)
    ]


async def recover_session(sessions, client, *, force=False, login_factory=IAMLogin):
    settings = client.settings
    if not settings.iam_auto_login and not force:
        raise CanvasError("iam_disabled")
    if settings.canvas_auth_mode != "cookie" and not (
        settings.canvas_cookie_fallback and "identity" in settings.canvas_cookie_resources.split(",")
    ):
        raise CanvasError("iam_cookie_fallback_not_enabled")
    username, password = read_secret(settings.iam_username_file), read_secret(settings.iam_password_file)
    if not username or not password:
        raise CanvasError("iam_credentials_missing")
    async with lease(sessions, "iam-login", seconds=settings.iam_timeout_seconds + 30) as acquired:
        if not acquired:
            raise CanvasError("iam_login_in_progress")
        # 等待锁期间另一 worker 可能已更新文件；先复用它，不再次提交密码。
        if not force:
            client.cookies = json.loads(read_secret(settings.canvas_cookie_file) or "[]")
            client.cache.clear()
            try:
                current = await client.identity("cookie")
            except CanvasError as error:
                if error.code not in REAUTH_ERRORS:
                    raise
            else:
                async with sessions() as session:
                    account = await session.get(Account, 1)
                    if account and (account.user_id != current or account.origin != client.origin):
                        raise CanvasError("iam_identity_mismatch")
                client.cookie_verified = True
                return current
        now = now_utc()
        revision = credential_revision(settings)
        async with sessions() as session, session.begin():
            account = await session.get(Account, 1)
            expected = account.user_id if account else None
            if account and account.origin != client.origin:
                raise CanvasError("account_changed_requires_new_database")
            state = await session.get(Health, "iam")
            previous_status = state.status if state else None
            previous = dict(state.details) if state else {}
            unchanged = previous.get("credential_revision") == revision
            if not force and unchanged:
                if state.status == "interaction_required" or state.status == "blocked":
                    raise CanvasError(previous.get("error", "iam_interaction_required"))
                next_attempt = parse_time(previous.get("next_attempt"))
                if next_attempt and now < next_attempt:
                    raise CanvasError("iam_retry_cooldown")
            failures = int(previous.get("failures", 0)) if unchanged else 0
            details = {
                "credential_revision": revision,
                "failures": failures,
                "last_attempt": now.isoformat(),
                "next_attempt": (now + timedelta(seconds=settings.iam_retry_seconds)).isoformat(),
                "last_success": previous.get("last_success"),
            }
            if not state:
                state = Health(name="iam", status="attempting", details=details)
                session.add(state)
            state.status, state.details, state.updated_at = "attempting", details, now
        try:
            result = await login_factory(settings).login(username, password)
            if expected is not None and result.user_id != expected:
                raise CanvasError("iam_identity_mismatch")
            # 再用普通 Canvas 客户端验证待保存 Cookie，不能只信任登录回调。
            old = client.cookies
            client.cookies, client.cache = result.cookies, {}
            try:
                verified = await client.identity("cookie")
                if verified != result.user_id:
                    raise CanvasError("iam_identity_mismatch")
            except Exception:
                client.cookies = old
                raise
            write_secret(settings.canvas_cookie_file, json.dumps(result.cookies))
            write_secret(settings.iam_cookie_file, json.dumps(result.iam_cookies))
            client.cookie_verified, client.health["iam"] = True, "recovered"
            finished = now_utc()
            details.update(
                {
                    "credential_revision": credential_revision(settings),
                    "failures": 0,
                    "error": None,
                    # 成功后不保留失败冷却；新会话再次被拒绝时应立即恢复。
                    "next_attempt": None,
                    "last_success": finished.isoformat(),
                    "hop_count": len(result.trace),
                    "password_submitted": any(
                        h["method"] == "POST" and h["path"].endswith("/ActionAuthChain") for h in result.trace
                    ),
                }
            )
            async with sessions() as session, session.begin():
                state = await session.get(Health, "iam")
                state.status, state.details, state.updated_at = "ok", details, finished
                # 自动续登成功只更新健康状态；需要本人处理的失败才发邮件。
            return verified
        except Exception as error:
            code = error.code if isinstance(error, CanvasError) else "iam_internal_error"
            status = (
                "interaction_required"
                if code == "iam_interaction_required"
                else "blocked"
                if code in BLOCKED_ERRORS
                else "retry_wait"
            )
            failures += 1
            details.update(
                {
                    "error": code,
                    "failures": failures,
                    "next_attempt": (
                        now_utc()
                        + timedelta(
                            seconds=min(21600, settings.iam_retry_seconds * 2 ** min(failures - 1, 6))
                        )
                    ).isoformat(),
                }
            )
            async with sessions() as session, session.begin():
                state = await session.get(Health, "iam")
                state.status, state.details, state.updated_at = status, details, now_utc()
                if previous_status != status:
                    await enqueue(
                        session,
                        settings,
                        "iam-failed:" + now.isoformat(),
                        None,
                        "iam_login_failed",
                        {"status": code},
                    )
            raise CanvasError(code) from None
