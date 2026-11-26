"""同济 Canvas 的受限 IAM/OIDC 登录交换，独立于业务只读 HTTP 客户端。"""

import asyncio
import base64
import hmac
import json
import re
import time
from dataclasses import dataclass, field
from email.message import Message
from html.parser import HTMLParser
from http.cookiejar import Cookie, CookieJar
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from canvas_notifier.canvas.http import CanvasError
from canvas_notifier.config import Settings, read_secret

CANVAS_HOST = "canvas.tongji.edu.cn"
IAM_HOST = "iam.tongji.edu.cn"
CANVAS = f"https://{CANVAS_HOST}"
IAM = f"https://{IAM_HOST}"
CALLBACK = CANVAS + "/login/oauth2/callback"
PASSWORD_FORM = "authen1Form"
_SSO_PATH = "/idp/profile/OAUTH2/AuthorizationCode/SSO"
_SSO_NETWORK_MESSAGE = re.compile(r"\s*网络异常\s*[,，]\s*请稍后再试\s*")
GET_PATHS = {
    CANVAS: {"/", "/login", "/login/openid_connect", "/login/oauth2/callback", "/api/v1/users/self"},
    IAM: {
        "/idp/oauth2/authorize",
        "/idp/AuthnEngine",
        "/idp/authcenter/ActionAuthChain",
        _SSO_PATH,
        "/idp/themes/default/js/main/crypt.js",
    },
}
POST_PATHS = {"/idp/displayVerificationCode.do", "/idp/authcenter/ActionAuthChain", "/idp/AuthnEngine"}


@dataclass(repr=False)
class LoginResult:
    user_id: str
    cookies: list[dict] = field(repr=False)
    trace: list[dict] = field(default_factory=list)
    iam_cookies: list[dict] = field(default_factory=list, repr=False)


class LoginFormParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.action = ""
        self.fields = {}
        self.in_form = False
        self.found = False

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "form":
            self.in_form = values.get("id") == PASSWORD_FORM
            if self.in_form:
                self.found = True
                self.action = values.get("action", "")
        if tag == "input" and self.in_form and values.get("name"):
            self.fields[values["name"]] = values.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.in_form = False


class _SSOErrorParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.network_error = False
        self.ignored = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "template"):
            self.ignored.append(tag)

    def handle_endtag(self, tag):
        if self.ignored and tag == self.ignored[-1]:
            self.ignored.pop()

    def handle_data(self, data):
        if not self.ignored and _SSO_NETWORK_MESSAGE.fullmatch(data):
            self.network_error = True


def _check_terminal_sso_page(response):
    if response.url.host != IAM_HOST or response.url.path != _SSO_PATH:
        return
    content_type = response.headers.get("Content-Type", "").partition(";")[0].strip().lower()
    # 只对已知 SSO 端点的 HTTP 200 HTML 识别已观察到的错误提示。
    if response.status_code == 200 and content_type in ("text/html", "application/xhtml+xml"):
        parser = _SSOErrorParser()
        parser.feed(response.text)
        parser.close()
        if parser.network_error:
            raise CanvasError("iam_sso_network_error")
    raise CanvasError("iam_schema_changed")


def parse_form(html, current_url):
    parser = LoginFormParser()
    parser.feed(html)
    if not parser.found or not {"j_username", "j_password", "authnLcKey"} <= parser.fields.keys():
        raise CanvasError("iam_schema_changed")
    fields = parser.fields
    # authnLcKey 和认证链均来自本次登录页，绝不重用 HAR 中的历史值。
    chain = fields.get("spAuthChainCode")
    if not chain:
        match = re.search(r"""\$\(\s*["']#spAuthChainCode1["']\s*\)\.val\(\s*["']([a-fA-F0-9]+)["']""", html)
        if not match:
            raise CanvasError("iam_schema_changed")
        chain = match[1]
    action = urljoin(current_url, parser.action)
    query = parse_qs(urlsplit(action).query)
    if query.get("authnLcKey") != [fields["authnLcKey"]] or fields.get("op") != "login":
        raise CanvasError("iam_schema_changed")
    return action, {
        "op": "login",
        "spAuthChainCode": chain,
        "authnLcKey": fields["authnLcKey"],
        "j_checkcode": fields.get("j_checkcode", ""),
    }


def encrypt_password(script, password):
    # 公钥由受信任 IAM HTTPS 端点实时提供；去掉已注释掉的旧 key，不执行页面 JS。
    active = re.sub(
        r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|/\*.*?\*/|//[^\n]*",
        lambda match: match[0] if match[0].startswith(('"', "'")) else "",
        script,
        flags=re.S,
    )
    keys = re.findall(r"""\.setPublicKey\(\s*["']([A-Za-z0-9+/=\s]+)["']\s*\)""", active)
    if len(keys) != 1:
        raise CanvasError("iam_schema_changed")
    try:
        key = serialization.load_der_public_key(base64.b64decode(re.sub(r"\s+", "", keys[0]), validate=True))
        if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 1024:
            raise ValueError("unsupported key")
        encrypted = key.encrypt(password.encode(), padding.PKCS1v15())
    except (ValueError, TypeError):
        raise CanvasError("iam_encryption_failed") from None
    return base64.b64encode(encrypted).decode("ascii")


def scoped_cookie_records(records, host):
    """Select live cookies for one host; the last saved copy of a scope wins."""
    selected = {}
    now = time.time()
    for record in records:
        if record.get("domain") not in (host, f".{host}"):
            continue
        expires = record.get("expires")
        if expires not in (None, -1) and expires <= now:
            continue
        selected[record.get("path") or "/", record["name"]] = record
    return list(selected.values())


def _saved_cookie(record):
    domain = record["domain"]
    dotted = domain.startswith(".")
    path = record.get("path") or "/"
    expires = record.get("expires")
    if expires == -1:
        expires = None
    return Cookie(
        version=0,
        name=record["name"],
        value=record["value"],
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=dotted,
        domain_initial_dot=dotted,
        path=path,
        path_specified=True,
        secure=record.get("secure", False),
        expires=expires,
        discard=expires is None,
        comment=None,
        comment_url=None,
        rest={"HttpOnly": None} if record.get("httpOnly") else {},
    )


class _SetCookieHeader:
    def __init__(self, value):
        self.headers = Message()
        self.headers["Set-Cookie"] = value

    def info(self):
        return self.headers


class _SchoolCookieJar(CookieJar):
    _response_host = None

    def extract_cookies(self, response, request):
        host = urlsplit(request.get_full_url()).hostname
        if host not in (CANVAS_HOST, IAM_HOST):
            return
        domains = (host, f".{host}")
        self._response_host = host
        try:
            # CookieJar applies expirations while parsing, so process each header
            # in order instead of parsing the response and then storing all cookies.
            for header in response.info().get_all("Set-Cookie", ()):
                for cookie in self.make_cookies(_SetCookieHeader(header), request):
                    if cookie.domain in domains:
                        self.set_cookie_if_ok(cookie, request)
        finally:
            self._response_host = None

    def set_cookie(self, cookie):
        # The dotted form records Domain scope, but is not a separate identity
        # for the same exact school host, path and cookie name.
        other_domain = cookie.domain[1:] if cookie.domain.startswith(".") else f".{cookie.domain}"
        self._clear_if_present(other_domain, cookie.path, cookie.name)
        super().set_cookie(cookie)

    def clear(self, domain=None, path=None, name=None):
        host = self._response_host
        if host is None:
            return super().clear(domain, path, name)
        # CookieJar calls clear() for an expired Set-Cookie before policy checks.
        # It must not delete cookies for another host, and either form can match.
        domains = (host, f".{host}")
        if domain in domains:
            for alias in domains:
                self._clear_if_present(alias, path, name)

    def _clear_if_present(self, domain, path, name):
        try:
            super().clear(domain, path, name)
        except KeyError:
            pass


def cookie_records(jar, host=CANVAS_HOST):
    now = time.time()
    return [
        {
            "name": c.name,
            "value": c.value,
            "domain": c.domain,
            "path": c.path or "/",
            "expires": c.expires if c.expires is not None else -1,
            "secure": c.secure,
            "httpOnly": any(key.lower() == "httponly" for key in c._rest),
        }
        for c in jar
        if c.domain in (host, f".{host}") and not c.is_expired(now)
    ]


class IAMLogin:
    def __init__(self, settings: Settings, transport=None):
        if settings.canvas_base_url != CANVAS:
            raise CanvasError("iam_canvas_origin_not_supported")
        self.settings = settings
        self.transport = transport
        self.state = None
        self.callback_seen = False
        self.trace = []

    def check_url(self, url, method="GET"):
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.hostname}"
        if p.username or p.password or p.fragment or p.port not in (None, 443) or origin not in GET_PATHS:
            raise CanvasError("iam_redirect_blocked")
        if method == "POST":
            if origin != IAM or p.path not in POST_PATHS:
                raise CanvasError("iam_post_blocked")
        elif p.path not in GET_PATHS[origin]:
            raise CanvasError("iam_redirect_blocked")
        query = parse_qs(p.query)
        if origin == IAM and p.path == "/idp/oauth2/authorize":
            if query.get("redirect_uri") != [CALLBACK] or query.get("response_type") != ["code"]:
                raise CanvasError("iam_oauth_target_mismatch")
            state = query.get("state", [""])[0]
            if not state or (self.state and not hmac.compare_digest(self.state, state)):
                raise CanvasError("iam_state_mismatch")
            self.state = state
        if origin == CANVAS and p.path == "/login/oauth2/callback":
            if (
                not self.state
                or not query.get("code")
                or not hmac.compare_digest(self.state, query.get("state", [""])[0])
            ):
                raise CanvasError("iam_state_mismatch")
            self.callback_seen = True
        return url

    async def request(self, client, method, url, **kwargs):
        self.check_url(url, method)
        try:
            response = await client.request(method, url, **kwargs)
        except httpx.RequestError:
            raise CanvasError("iam_network_error") from None
        self.trace.append(
            {
                "method": method,
                "host": urlsplit(url).hostname,
                "path": urlsplit(url).path,
                "status": response.status_code,
            }
        )
        if response.status_code == 429:
            raise CanvasError("iam_rate_limited")
        if response.status_code >= 500:
            raise CanvasError("iam_server_error")
        if response.status_code in (401, 403):
            raise CanvasError("iam_rejected", response.status_code)
        if response.status_code >= 400:
            raise CanvasError("iam_http_error", response.status_code)
        return response

    async def follow(self, client, response):
        for _ in range(16):
            if response.status_code not in (301, 302, 303, 307, 308):
                return response
            location = response.headers.get("Location")
            if not location:
                raise CanvasError("iam_schema_changed")
            # 不让 307/308 将带密码的 POST 重放到下一跳。
            if response.status_code in (307, 308) and response.request.method != "GET":
                raise CanvasError("iam_post_redirect_blocked")
            response = await self.request(client, "GET", urljoin(str(response.url), location))
        raise CanvasError("iam_redirect_limit")

    async def login(self, username: str | None = None, password: str | None = None) -> LoginResult:
        try:
            async with asyncio.timeout(self.settings.iam_timeout_seconds):
                return await self.exchange(username, password)
        except TimeoutError:
            raise CanvasError("iam_timeout") from None

    async def verify_canvas(self, client):
        identity = await self.request(
            client, "GET", CANVAS + "/api/v1/users/self", headers={"Accept": "application/json"}
        )
        try:
            profile = identity.json()
        except ValueError:
            raise CanvasError("iam_canvas_verification_failed") from None
        if not isinstance(profile, dict) or not profile.get("id"):
            raise CanvasError("iam_canvas_verification_failed")
        cookies = cookie_records(client.cookies.jar)
        if not cookies:
            raise CanvasError("iam_canvas_cookie_missing")
        return LoginResult(
            str(profile["id"]), cookies, self.trace, cookie_records(client.cookies.jar, IAM_HOST)
        )

    async def exchange(self, username, password):
        # 已保存的 IAM 会话优先尝试 SSO；仅在密码页才读取本地密码。
        jar = _SchoolCookieJar()
        async with httpx.AsyncClient(
            timeout=20,
            follow_redirects=False,
            trust_env=False,
            cookies=jar,
            transport=self.transport,
            headers={"User-Agent": "Mozilla/5.0 CanvasNotifier/0.1", "Accept-Language": "zh-CN,zh;q=0.9"},
        ) as client:
            try:
                saved = (
                    json.loads(read_secret(self.settings.iam_cookie_file) or "[]")
                    if self.settings.iam_use_saved_session
                    else []
                )
            except (ValueError, TypeError):
                raise CanvasError("iam_cookie_file_invalid") from None
            for cookie in scoped_cookie_records(saved, IAM_HOST):
                jar.set_cookie(_saved_cookie(cookie))
            response = await self.follow(
                client, await self.request(client, "GET", CANVAS + "/login/openid_connect")
            )
            if self.callback_seen and urlsplit(str(response.url)).hostname == "canvas.tongji.edu.cn":
                return await self.verify_canvas(client)
            if urlsplit(str(response.url)).hostname != "iam.tongji.edu.cn" or not self.state:
                raise CanvasError("iam_login_page_missing")
            _check_terminal_sso_page(response)
            action, fields = parse_form(response.text, str(response.url))
            self.check_url(action, "POST")
            if username is None:
                username = read_secret(self.settings.iam_username_file)
            if password is None:
                password = read_secret(self.settings.iam_password_file)
            if not username or not password:
                raise CanvasError("iam_credentials_missing")
            headers = {"Origin": IAM, "Referer": str(response.url), "X-Requested-With": "XMLHttpRequest"}
            check = await self.request(
                client,
                "POST",
                IAM + "/idp/displayVerificationCode.do",
                data={
                    "j_username": username,
                    "j_authMethodID": "1",
                    "spAuthChainCode": fields["spAuthChainCode"],
                },
                headers=headers,
            )
            try:
                captcha = check.json()
            except ValueError:
                raise CanvasError("iam_schema_changed") from None
            if captcha is True or captcha == "true":
                raise CanvasError("iam_interaction_required")
            if captcha is not False and captcha != "false":
                raise CanvasError("iam_schema_changed")
            script = await self.request(client, "GET", IAM + "/idp/themes/default/js/main/crypt.js")
            encrypted = encrypt_password(script.text, password)
            # IAM 网页会把 RSA Base64 直接替换进序列化表单；保持 HAR 中未二次转义 '+' 的协议。
            form = {**fields, "j_username": username}
            body = urlencode(form) + "&j_password=" + encrypted
            ajax_url = (
                IAM + "/idp/authcenter/ActionAuthChain?" + urlencode({"authnLcKey": fields["authnLcKey"]})
            )
            submitted = await self.request(
                client,
                "POST",
                ajax_url,
                content=body,
                headers={
                    **headers,
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "Accept": "application/json",
                },
            )
            try:
                result = submitted.json()
            except ValueError:
                raise CanvasError("iam_schema_changed") from None
            if not isinstance(result, dict):
                raise CanvasError("iam_schema_changed")
            view = result.get("view")
            if view == "biometrics":
                raise CanvasError("iam_interaction_required")
            if view not in (None, "", "none"):
                raise CanvasError("iam_schema_changed")
            login_failed = str(result.get("loginFailed")).lower()
            if login_failed == "true":
                raise CanvasError("iam_rejected")
            if login_failed != "false":
                raise CanvasError("iam_schema_changed")
            # 密码已验证；HAR 的后续 AuthnEngine POST 不再含密码，只完成当前会话的认证跳转。
            finished = await self.request(
                client,
                "POST",
                action,
                data={"op": "login", "spAuthChainCode": fields["spAuthChainCode"]},
                headers={"Origin": IAM, "Referer": str(response.url)},
            )
            final = await self.follow(client, finished)
            if not self.callback_seen or urlsplit(str(final.url)).hostname != "canvas.tongji.edu.cn":
                _check_terminal_sso_page(final)
                raise CanvasError("iam_schema_changed")
            return await self.verify_canvas(client)
