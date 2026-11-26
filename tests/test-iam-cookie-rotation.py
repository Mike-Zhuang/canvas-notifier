import base64
import json
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from canvas_notifier.auth import browser
from canvas_notifier.auth.iam import CALLBACK, CANVAS, CANVAS_HOST, IAM, IAM_HOST, IAMLogin
from canvas_notifier.canvas.http import CanvasClient
from canvas_notifier.config import read_secret, write_secret
from canvas_notifier.db import create_schema, database

AUTHORIZE = IAM + "/idp/oauth2/authorize?response_type=code&redirect_uri=" + CALLBACK + "&state=rotation"
SSO = IAM + "/idp/profile/OAUTH2/AuthorizationCode/SSO"
FUTURE_EXPIRY = 2_145_916_800


def redirect(url, *cookies):
    return httpx.Response(302, headers=[("Location", url), *(("Set-Cookie", c) for c in cookies)])


def request_cookies(request):
    header = request.headers.get("cookie")
    return header.split("; ") if header else []


def canvas_request(request):
    path = request.url.path
    if path == "/login/openid_connect":
        assert request_cookies(request) == []
        return redirect(
            AUTHORIZE,
            "sid=canvas; Secure; Path=/",
            "poison=invalid; Domain=tongji.edu.cn; Path=/",
            f"sid=; Domain={IAM_HOST}; Path=/; Max-Age=0",
        )
    if path == "/login/oauth2/callback":
        assert request_cookies(request) == ["sid=canvas"]
        return redirect(CANVAS + "/", "session=verified; Secure; HttpOnly; Path=/")
    if path in {"/", "/api/v1/users/self"}:
        assert set(request_cookies(request)) == {"sid=canvas", "session=verified"}
        return httpx.Response(200, json={"id": 42})
    raise AssertionError(path)


async def test_legacy_saved_cookie_is_replaced_after_password_login(settings):
    settings.iam_use_saved_session = True
    write_secret(
        settings.iam_cookie_file,
        json.dumps(
            [
                {"name": "sid", "value": "legacy", "domain": IAM_HOST},
                {"name": "sid", "value": "narrow", "domain": IAM_HOST, "path": "/idp/profile"},
                {"name": "expired", "value": "invalid", "domain": IAM_HOST, "expires": 0},
                {"name": "parent", "value": "invalid", "domain": ".tongji.edu.cn"},
                {"name": "child", "value": "invalid", "domain": f"sub.{IAM_HOST}"},
            ]
        ),
    )
    public_key = base64.b64encode(
        rsa.generate_private_key(public_exponent=65537, key_size=1024)
        .public_key()
        .public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    ).decode()

    def school(request):
        if request.url.host == CANVAS_HOST:
            return canvas_request(request)
        if request.url.path == httpx.URL(SSO).path:
            assert request_cookies(request) == ["sid=narrow", "sid=rotated"]
            return redirect(CALLBACK + "?code=synthetic&state=rotation")
        assert request_cookies(request) == ["sid=legacy"]
        match request.method, request.url.path:
            case "GET", "/idp/oauth2/authorize":
                return httpx.Response(
                    200,
                    text=(
                        '<form id="authen1Form" action="/idp/AuthnEngine?authnLcKey=fresh">'
                        '<input name="j_username"><input name="j_password">'
                        '<input name="authnLcKey" value="fresh"><input name="op" value="login">'
                        '<input name="spAuthChainCode" value="chain"></form>'
                    ),
                )
            case "POST", "/idp/displayVerificationCode.do":
                return httpx.Response(200, json=False)
            case "GET", "/idp/themes/default/js/main/crypt.js":
                return httpx.Response(200, text=f"encrypt.setPublicKey('{public_key}')")
            case "POST", "/idp/authcenter/ActionAuthChain":
                assert b"j_password=" in request.content
                return httpx.Response(200, json={"loginFailed": False})
            case "POST", "/idp/AuthnEngine":
                return redirect(
                    SSO,
                    f"sid=rotated; Domain={IAM_HOST}; Secure; HttpOnly; Path=/; Max-Age=3600",
                    f"sid=; Domain={CANVAS_HOST}; Path=/; Max-Age=0",
                )
            case _:
                raise AssertionError(request.url.path)

    result = await IAMLogin(settings, httpx.MockTransport(school)).login("synthetic-user", "synthetic-password")
    assert result.user_id == "42"
    assert {c["name"] for c in result.cookies} == {"sid", "session"}
    assert len(result.iam_cookies) == 2
    root = next(c for c in result.iam_cookies if c["path"] == "/")
    assert root["domain"] == f".{IAM_HOST}" and root["value"] == "rotated"
    assert root["secure"] and root["httpOnly"] and root["expires"] > 0

    write_secret(settings.iam_cookie_file, json.dumps(result.iam_cookies))

    def replay(request):
        if request.url.host == CANVAS_HOST:
            return canvas_request(request)
        if request.url.path == "/idp/oauth2/authorize":
            assert request_cookies(request) == ["sid=rotated"]
            return redirect(SSO)
        assert str(request.url) == SSO
        assert request_cookies(request) == ["sid=narrow", "sid=rotated"]
        return redirect(CALLBACK + "?code=synthetic&state=rotation")

    reused = await IAMLogin(settings, httpx.MockTransport(replay)).login()
    assert reused.user_id == "42"
    assert next(c for c in reused.iam_cookies if c["path"] == "/") == root


@pytest.mark.parametrize("deletion", ["Max-Age=0", "Expires=Thu, 01 Jan 1970 00:00:00 GMT"])
async def test_cookie_rotations_and_deletions_are_applied_in_response_order(settings, deletion):
    settings.iam_use_saved_session = True
    write_secret(
        settings.iam_cookie_file,
        json.dumps(
            [
                {"name": "sid", "value": "old-copy", "domain": IAM_HOST, "path": "/"},
                {"name": "sid", "value": "new-copy", "domain": f".{IAM_HOST}", "expires": -1},
                {
                    "name": "sid",
                    "value": "keep",
                    "domain": f".{IAM_HOST}",
                    "path": "/idp",
                    "expires": FUTURE_EXPIRY,
                    "secure": True,
                    "httpOnly": True,
                },
            ]
        ),
    )

    def school(request):
        if request.url.host == CANVAS_HOST:
            return canvas_request(request)
        path = request.url.path
        if path == "/idp/oauth2/authorize":
            assert request_cookies(request) == ["sid=keep", "sid=new-copy"]
            return redirect(
                IAM + "/idp/AuthnEngine",
                "sid=host-only; Path=/; Secure",
                "short-lived=first; Path=/idp",
                f"short-lived=; Domain={IAM_HOST}; Path=/idp; {deletion}",
                f"late=; Path=/idp; {deletion}",
                f"late=last; Domain={IAM_HOST}; Path=/idp",
            )
        if path == "/idp/AuthnEngine":
            assert request_cookies(request) == ["sid=keep", "late=last", "sid=host-only"]
            return redirect(
                IAM + "/idp/authcenter/ActionAuthChain",
                f"sid=domain-scope; Domain={IAM_HOST}; Path=/",
                "defaulted=kept; Secure; hTtPoNlY",
                "too-narrow=invalid-here; Path=/idp/profile/OAUTH2/AuthorizationCode/SS",
            )
        if path == "/idp/authcenter/ActionAuthChain":
            cookies = request_cookies(request)
            assert cookies[-1] == "sid=domain-scope"
            assert sorted(cookies[:-1]) == ["defaulted=kept", "late=last", "sid=keep"]
            return redirect(SSO, f"sid=; Path=/; {deletion}")
        assert str(request.url) == SSO
        assert sorted(request_cookies(request)) == ["defaulted=kept", "late=last", "sid=keep"]
        return redirect(CALLBACK + "?code=synthetic&state=rotation")

    result = await IAMLogin(settings, httpx.MockTransport(school)).login()
    assert result.user_id == "42"
    by_name = {c["name"]: c for c in result.iam_cookies}
    assert len(result.iam_cookies) == len(by_name) == 4
    assert by_name["sid"] == {
        "name": "sid",
        "value": "keep",
        "domain": f".{IAM_HOST}",
        "path": "/idp",
        "expires": FUTURE_EXPIRY,
        "secure": True,
        "httpOnly": True,
    }
    assert by_name["defaulted"]["path"] == "/idp"
    assert by_name["defaulted"]["httpOnly"] is True
    assert by_name["late"]["value"] == "last"


async def test_browser_saves_iam_path_scopes_after_canvas_verification(settings, monkeypatch):
    engine, _ = database(settings.database_url)
    try:
        await create_schema(engine)
    finally:
        await engine.dispose()
    canvas = {"name": "_canvas_middle_session", "value": "canvas-only", "domain": CANVAS_HOST, "path": "/"}
    scoped = {
        "name": "sid",
        "value": "path-scoped",
        "domain": f".{IAM_HOST}",
        "path": "/idp",
        "expires": FUTURE_EXPIRY,
        "secure": True,
        "httpOnly": True,
    }

    async def cookies(*urls):
        if urls == (CANVAS,):
            return [canvas]
        assert urls == ()
        return [canvas, scoped, {**scoped, "domain": ".tongji.edu.cn"}, {**scoped, "expires": 0}]

    def verified_canvas(request):
        assert request.url.path == "/api/v1/users/self"
        assert request_cookies(request) == ["_canvas_middle_session=canvas-only"]
        return httpx.Response(200, json={"id": 42})

    page = SimpleNamespace(goto=AsyncMock(), is_closed=lambda: False)
    profile = SimpleNamespace(status=200, headers={"content-type": "application/json"}, json=AsyncMock(return_value={"id": 42}))
    context = SimpleNamespace(new_page=AsyncMock(return_value=page), request=SimpleNamespace(get=AsyncMock(return_value=profile)), cookies=cookies)
    chromium = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())

    @asynccontextmanager
    async def playwright():
        yield SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=chromium)))

    class BrowserError(Exception):
        pass

    monkeypatch.setitem(sys.modules, "playwright.async_api", SimpleNamespace(Error=BrowserError, async_playwright=playwright))
    monkeypatch.setattr(browser, "CanvasClient", lambda cfg: CanvasClient(cfg, httpx.MockTransport(verified_canvas)))
    result = await browser.interactive_login(settings, remember_iam=True)
    assert result == {"cookie_identity_valid": True, "stored_cookie_count": 1, "iam_cookie_count": 1}
    assert json.loads(read_secret(settings.iam_cookie_file)) == [scoped]
