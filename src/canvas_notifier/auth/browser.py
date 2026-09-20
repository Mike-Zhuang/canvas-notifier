"""用户交互登录入口；网络异常只返回安全分类，不回显 Playwright 的 Cookie 请求日志。"""

import asyncio
import json
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from canvas_notifier.canvas.http import CanvasClient, CanvasError
from canvas_notifier.config import write_secret
from canvas_notifier.db import Account, database


async def interactive_login(settings, *, remember_iam=False, timeout=600):
    try:
        from playwright.async_api import Error as BrowserError
        from playwright.async_api import async_playwright
    except ImportError:
        raise CanvasError("browser_dependency_missing") from None
    async with async_playwright() as runtime:
        browser = await runtime.chromium.launch(headless=False)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(settings.canvas_base_url)
            print("请在浏览器中完成同济登录和学校要求的验证；成功后程序会自动保存会话。")
            started = asyncio.get_running_loop().time()
            user_id = None
            while asyncio.get_running_loop().time() - started < timeout:
                if page.is_closed():
                    raise CanvasError("browser_closed_before_login")
                try:
                    response = await context.request.get(
                        settings.canvas_base_url + "/api/v1/users/self", max_redirects=0, timeout=10000
                    )
                    if response.status == 200 and "json" in response.headers.get("content-type", ""):
                        profile = await response.json()
                        if isinstance(profile, dict) and profile.get("id"):
                            user_id = str(profile["id"])
                            break
                except (BrowserError, ValueError):
                    # 页面跳转期间校验请求可能中断；不输出异常文本，其中可能带请求 Cookie。
                    pass
                await asyncio.sleep(3)
            if not user_id:
                raise CanvasError("browser_login_timeout")
            engine, sessions = database(settings.database_url)
            try:
                async with sessions() as session:
                    account = await session.get(Account, 1)
                    if account and (account.user_id != user_id or account.origin != settings.canvas_base_url):
                        raise CanvasError("credential_identity_mismatch")
            finally:
                await engine.dispose()
            host = urlsplit(settings.canvas_base_url).hostname
            cookies = [
                c for c in await context.cookies(settings.canvas_base_url) if c["domain"].lstrip(".") == host
            ]
            if not cookies:
                raise CanvasError("cookie_missing")
            with tempfile.TemporaryDirectory(prefix="canvas-cookie-check-") as directory:
                candidate_file = Path(directory) / "cookies.json"
                minimal = [c for c in cookies if c["name"] == "_canvas_middle_session"]
                for candidate in [minimal, cookies] if minimal else [cookies]:
                    write_secret(candidate_file, json.dumps(candidate))
                    async with CanvasClient(
                        settings.model_copy(update={"canvas_cookie_file": candidate_file})
                    ) as client:
                        try:
                            identity = await client.identity("cookie")
                        except CanvasError:
                            continue
                    if identity != user_id:
                        raise CanvasError("credential_identity_mismatch")
                    write_secret(settings.canvas_cookie_file, json.dumps(candidate))
                    iam_cookies = []
                    if remember_iam:
                        iam_cookies = [
                            c
                            for c in await context.cookies("https://iam.tongji.edu.cn")
                            if c["domain"].lstrip(".") == "iam.tongji.edu.cn"
                        ]
                        write_secret(settings.iam_cookie_file, json.dumps(iam_cookies))
                        from canvas_notifier.auth.recovery import credential_revision
                        from canvas_notifier.sync.state import health

                        state_engine, state_sessions = database(settings.database_url)
                        try:
                            async with state_sessions() as session, session.begin():
                                await health(
                                    session,
                                    "iam",
                                    "session_saved",
                                    {
                                        "credential_revision": credential_revision(settings),
                                        "failures": 0,
                                        "error": None,
                                        "note": "本人交互登录已完成，待后台 SSO 交换验证",
                                    },
                                )
                        finally:
                            await state_engine.dispose()
                    return {
                        "cookie_identity_valid": True,
                        "stored_cookie_count": len(candidate),
                        "iam_cookie_count": len(iam_cookies),
                    }
            raise CanvasError("browser_cookie_verification_failed")
        except BrowserError:
            raise CanvasError("browser_network_error") from None
        finally:
            await browser.close()
