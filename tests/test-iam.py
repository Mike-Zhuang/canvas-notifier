import asyncio
import base64
import json
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from canvas_notifier.auth.iam import CALLBACK, CANVAS, IAM, IAMLogin, LoginResult
from canvas_notifier.auth.recovery import recover_session
from canvas_notifier.canvas.http import CanvasClient, CanvasError
from canvas_notifier.config import read_secret, write_secret
from canvas_notifier.db import Account, Health
from canvas_notifier.sync.service import bind_identity


class School:
    def __init__(self, *, captcha=False, view=None, wrong_state=False, foreign=False, rejected=False):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        self.captcha, self.view, self.wrong_state, self.foreign, self.rejected = (
            captcha,
            view,
            wrong_state,
            foreign,
            rejected,
        )
        self.calls = []
        self.password_posts = 0

    def __call__(self, request):
        self.calls.append((request.method, request.url.host, request.url.path))
        path = request.url.path
        if path == "/login/openid_connect":
            return httpx.Response(
                302,
                headers={
                    "Location": IAM
                    + "/idp/oauth2/authorize?client_id=canvas&response_type=code&redirect_uri="
                    + CALLBACK
                    + "&state=fresh-state"
                },
            )
        if path == "/idp/oauth2/authorize":
            if "iam-sso=accepted" in request.headers.get("cookie", ""):
                return httpx.Response(
                    302, headers={"Location": IAM + "/idp/profile/OAUTH2/AuthorizationCode/SSO"}
                )
            location = (
                "https://evil.example/idp/AuthnEngine"
                if self.foreign
                else IAM + ":443/idp/AuthnEngine?authnLcKey=new-key"
            )
            return httpx.Response(302, headers={"Location": location})
        if path == "/idp/AuthnEngine" and request.method == "GET":
            return httpx.Response(
                302, headers={"Location": IAM + "/idp/authcenter/ActionAuthChain?authnLcKey=new-key"}
            )
        if path == "/idp/authcenter/ActionAuthChain" and request.method == "GET":
            page = """<form id="authen1Form" action="/idp/AuthnEngine?authnLcKey=new-key&amp;currentAuth=password"><input name="j_username"><input type="password" name="j_password"><input name="authnLcKey" value="new-key"><input name="j_checkcode" value="请输入验证码"><input name="op" value="login"><input name="spAuthChainCode" value=""></form><script>$("#spAuthChainCode1").val('abc123');</script>"""
            return httpx.Response(200, text=page)
        if path == "/idp/displayVerificationCode.do":
            assert parse_qs(request.content.decode())["j_authMethodID"] == ["1"]
            return httpx.Response(200, json=self.captcha)
        if path.endswith("/crypt.js"):
            key = base64.b64encode(
                self.key.public_key().public_bytes(
                    serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
                )
            ).decode()
            return httpx.Response(
                200, text="// encrypt.setPublicKey('old-key');\nencrypt.setPublicKey('" + key + "');"
            )
        if path == "/idp/authcenter/ActionAuthChain" and request.method == "POST":
            self.password_posts += 1
            form = parse_qs(request.content.decode())
            assert form["j_checkcode"] == ["请输入验证码"]
            assert form["authnLcKey"] == ["new-key"]
            encrypted = base64.b64decode(form["j_password"][0].replace(" ", "+"))
            assert self.key.decrypt(encrypted, padding.PKCS1v15()) == b"synthetic-password"
            assert b"synthetic-password" not in request.content
            if self.view:
                return httpx.Response(200, json={"loginFailed": "popViewException", "view": self.view})
            return httpx.Response(200, json={"loginFailed": "true" if self.rejected else "false"})
        if path == "/idp/AuthnEngine" and request.method == "POST":
            assert "j_password" not in parse_qs(request.content.decode())
            return httpx.Response(
                302,
                headers={
                    "Location": IAM + "/idp/profile/OAUTH2/AuthorizationCode/SSO",
                    "Set-Cookie": "iam-sso=accepted; Path=/; Secure; HttpOnly",
                },
            )
        if path == "/idp/profile/OAUTH2/AuthorizationCode/SSO":
            return httpx.Response(
                302,
                headers={
                    "Location": CALLBACK
                    + "?code=single-use-code&state="
                    + ("wrong" if self.wrong_state else "fresh-state")
                },
            )
        if path == "/login/oauth2/callback":
            return httpx.Response(
                302,
                headers={
                    "Location": CANVAS + "/",
                    "Set-Cookie": "session=new-session; Path=/; Secure; HttpOnly",
                },
            )
        if path == "/":
            return httpx.Response(200, text="Canvas")
        if path == "/api/v1/users/self":
            assert "session=new-session" in request.headers.get("cookie", "")
            return httpx.Response(200, json={"id": "42"})
        raise AssertionError("Unexpected request")


async def test_password_exchange_and_safe_trace(settings):
    school = School()
    result = await IAMLogin(settings, httpx.MockTransport(school)).login(
        "synthetic-user", "synthetic-password"
    )
    assert result.user_id == "42" and school.password_posts == 1
    assert {c["domain"] for c in result.cookies} == {"canvas.tongji.edu.cn"}
    assert {c["domain"] for c in result.iam_cookies} == {"iam.tongji.edu.cn"}
    trace = json.dumps(result.trace)
    assert all(
        secret not in trace
        for secret in ("synthetic-user", "synthetic-password", "new-key", "single-use-code", "new-session")
    )


async def test_saved_iam_sso_requires_no_password_submission(settings):
    write_secret(
        settings.iam_cookie_file,
        json.dumps(
            [
                {
                    "name": "iam-sso",
                    "value": "accepted",
                    "domain": "iam.tongji.edu.cn",
                    "path": "/",
                    "expires": -1,
                }
            ]
        ),
    )
    school = School()
    result = await IAMLogin(settings, httpx.MockTransport(school)).login(
        "synthetic-user", "synthetic-password"
    )
    assert result.user_id == "42" and school.password_posts == 0


@pytest.mark.parametrize(
    "options,error,posts",
    [
        ({"captcha": True}, "iam_interaction_required", 0),
        ({"view": "biometrics"}, "iam_interaction_required", 1),
        ({"rejected": True}, "iam_rejected", 1),
        ({"wrong_state": True}, "iam_state_mismatch", 1),
        ({"foreign": True}, "iam_redirect_blocked", 0),
    ],
)
async def test_fail_closed(settings, options, error, posts):
    school = School(**options)
    with pytest.raises(CanvasError, match=error):
        await IAMLogin(settings, httpx.MockTransport(school)).login("synthetic-user", "synthetic-password")
    assert school.password_posts == posts
    assert all(host != "evil.example" for _, host, _ in school.calls)
    if options.get("wrong_state"):
        assert not any(path == "/login/oauth2/callback" for _, _, path in school.calls)


@pytest.fixture
def recovery_settings(settings):
    settings.iam_auto_login = True
    settings.canvas_cookie_fallback = True
    settings.canvas_cookie_resources = "identity,course,assignment,submission"
    write_secret(settings.iam_username_file, "synthetic-user")
    write_secret(settings.iam_password_file, "synthetic-password")
    write_secret(settings.canvas_cookie_file, "[]")
    return settings


def canvas_response(request):
    return (
        httpx.Response(200, json={"id": "42"})
        if "session=new-session" in request.headers.get("cookie", "")
        else httpx.Response(401, json={})
    )


class SuccessfulLogin:
    count = 0

    def __init__(self, settings):
        pass

    async def login(self, username, password):
        type(self).count += 1
        await asyncio.sleep(0.01)
        return LoginResult(
            "42",
            [
                {
                    "name": "session",
                    "value": "new-session",
                    "domain": "canvas.tongji.edu.cn",
                    "path": "/",
                    "expires": -1,
                }
            ],
            [],
        )


async def test_recovery_verified_before_save_and_reused(recovery_settings, sessions):
    SuccessfulLogin.count = 0
    async with sessions() as db, db.begin():
        db.add(Account(id=1, origin=CANVAS, user_id="42"))
    async with CanvasClient(recovery_settings, httpx.MockTransport(canvas_response)) as client:
        assert await recover_session(sessions, client, login_factory=SuccessfulLogin) == "42"
        assert client.cookie_verified
        assert await recover_session(sessions, client, login_factory=SuccessfulLogin) == "42"
    assert SuccessfulLogin.count == 1
    assert "new-session" in read_secret(recovery_settings.canvas_cookie_file)
    assert recovery_settings.canvas_cookie_file.stat().st_mode & 0o077 == 0
    async with sessions() as db:
        assert (await db.get(Health, "iam")).status == "ok"


async def test_mismatch_does_not_overwrite_cookie_file(recovery_settings, sessions):
    async with sessions() as db, db.begin():
        db.add(Account(id=1, origin=CANVAS, user_id="99"))
    before = read_secret(recovery_settings.canvas_cookie_file)
    async with CanvasClient(recovery_settings, httpx.MockTransport(canvas_response)) as client:
        with pytest.raises(CanvasError, match="iam_identity_mismatch"):
            await recover_session(sessions, client, login_factory=SuccessfulLogin)
    assert read_secret(recovery_settings.canvas_cookie_file) == before


@pytest.mark.parametrize("code", ["iam_rejected", "iam_interaction_required", "iam_network_error"])
async def test_persistent_block_or_cooldown(recovery_settings, sessions, code):
    class FailedLogin:
        count = 0

        def __init__(self, settings):
            pass

        async def login(self, username, password):
            FailedLogin.count += 1
            raise CanvasError(code)

    for _ in range(2):
        async with CanvasClient(recovery_settings, httpx.MockTransport(canvas_response)) as client:
            with pytest.raises(CanvasError):
                await recover_session(sessions, client, login_factory=FailedLogin)
    assert FailedLogin.count == 1
    assert read_secret(recovery_settings.canvas_cookie_file) == "[]"


@pytest.mark.parametrize("masked_cookie_failure", [False, True])
async def test_bind_recovers_only_when_both_auth_channels_failed(
    recovery_settings, sessions, monkeypatch, masked_cookie_failure
):
    import canvas_notifier.auth.iam as module

    calls = []

    async def login(self, username, password):
        calls.append(1)
        return LoginResult(
            "42",
            [{"name": "session", "value": "new-session", "domain": "canvas.tongji.edu.cn", "path": "/"}],
            [],
        )

    monkeypatch.setattr(module.IAMLogin, "login", login)
    if masked_cookie_failure:
        write_secret(
            recovery_settings.canvas_cookie_file,
            json.dumps(
                [
                    {
                        "name": "session",
                        "value": "expired-session",
                        "domain": "canvas.tongji.edu.cn",
                        "path": "/",
                    }
                ]
            ),
        )

    def handler(request):
        if (
            masked_cookie_failure
            and request.url.path == "/api/v1/users/self"
            and "session=expired-session" in request.headers.get("cookie", "")
        ):
            return httpx.Response(404, json={})
        return canvas_response(request)

    async with CanvasClient(recovery_settings, httpx.MockTransport(handler)) as client:
        await bind_identity(sessions, client)
        assert client.user_id == "42"
    assert len(calls) == 1


@pytest.mark.parametrize("status", [403, 503])
async def test_http_failures_do_not_trigger_password_login(recovery_settings, sessions, monkeypatch, status):
    import canvas_notifier.auth.iam as module
    import canvas_notifier.canvas.http as http_module

    async def forbidden(*args):
        raise AssertionError("Must not log in")

    async def fast_sleep(seconds):
        pass

    monkeypatch.setattr(module.IAMLogin, "login", forbidden)
    monkeypatch.setattr(http_module.asyncio, "sleep", fast_sleep)
    async with CanvasClient(
        recovery_settings, httpx.MockTransport(lambda request: httpx.Response(status, json={}))
    ) as client:
        with pytest.raises(CanvasError):
            await bind_identity(sessions, client)


async def test_valid_bearer_does_not_require_iam(recovery_settings, sessions, monkeypatch):
    import canvas_notifier.auth.iam as module

    async def forbidden(*args):
        raise AssertionError("Must not log in")

    monkeypatch.setattr(module.IAMLogin, "login", forbidden)

    def handler(request):
        return (
            httpx.Response(200, json={"id": "42"})
            if "authorization" in request.headers
            else httpx.Response(401, json={})
        )

    async with CanvasClient(recovery_settings, httpx.MockTransport(handler)) as client:
        await bind_identity(sessions, client)
        assert client.user_id == "42" and not client.cookie_verified


async def test_single_flight_iam_login(recovery_settings, sessions):
    SuccessfulLogin.count = 0
    async with (
        CanvasClient(recovery_settings, httpx.MockTransport(canvas_response)) as first,
        CanvasClient(recovery_settings, httpx.MockTransport(canvas_response)) as second,
    ):
        results = await asyncio.gather(
            recover_session(sessions, first, login_factory=SuccessfulLogin),
            recover_session(sessions, second, login_factory=SuccessfulLogin),
            return_exceptions=True,
        )
    assert SuccessfulLogin.count == 1
    assert any(result == "42" for result in results)
    assert all(
        result == "42" or isinstance(result, CanvasError) and result.code == "iam_login_in_progress"
        for result in results
    )


def test_unexpected_login_target_and_method_are_blocked(settings):
    login = IAMLogin(settings)
    for url, method in [
        ("http://iam.tongji.edu.cn/idp/AuthnEngine", "GET"),
        ("https://iam.tongji.edu.cn:8443/idp/AuthnEngine", "GET"),
        (CANVAS + "/login/oauth2/callback", "POST"),
        (IAM + "/idp/changePassword", "POST"),
    ]:
        with pytest.raises(CanvasError):
            login.check_url(url, method)


def test_comment_parser_preserves_slashes_inside_public_key():
    from pathlib import Path

    from canvas_notifier.auth.iam import encrypt_password

    script = Path("tests/fixtures/iam-public-key.js").read_text()
    encrypted = encrypt_password(script, "synthetic-password")
    assert len(base64.b64decode(encrypted)) == 128


async def test_password_only_recovery_can_fetch_resources_without_bearer_file(
    recovery_settings, sessions, monkeypatch
):
    import canvas_notifier.auth.iam as module

    recovery_settings.canvas_token_file.unlink()
    calls = []

    async def login(self, username, password):
        calls.append("iam-login")
        return LoginResult(
            "42",
            [{"name": "session", "value": "new-session", "domain": "canvas.tongji.edu.cn", "path": "/"}],
            [],
        )

    monkeypatch.setattr(module.IAMLogin, "login", login)

    def handler(request):
        assert "authorization" not in request.headers
        if "session=new-session" not in request.headers.get("cookie", ""):
            return httpx.Response(401, json={})
        if request.url.path == "/api/v1/users/self":
            return httpx.Response(200, json={"id": "42"})
        if request.url.path == "/api/v1/courses":
            return httpx.Response(200, json=[{"id": "1", "name": "Synthetic course"}])
        return httpx.Response(200, json=[{"id": "2", "name": "Synthetic assignment"}])

    async with CanvasClient(recovery_settings, httpx.MockTransport(handler)) as client:
        await bind_identity(sessions, client)
        courses = await client.paginate("/api/v1/courses", kind="course")
        assignments = await client.paginate("/api/v1/courses/1/assignments", kind="assignment")
    assert courses.complete and len(courses.items) == 1
    assert assignments.complete and len(assignments.items) == 1
    assert calls == ["iam-login"]
