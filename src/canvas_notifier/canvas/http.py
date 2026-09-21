"""REST 行为移植自固定版本 tongji-canvas-mcp；来源和许可见 reference-manifest。"""

import asyncio
import json
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

import httpx

from canvas_notifier.config import Settings, read_secret

# GET 也可能有副作用，因此路径和参数必须一起检查，禁止任意 HAR 重放。
PATHS = [
    r"/api/v1/users/self",
    r"/api/v1/courses",
    r"/api/v1/courses/\d+",
    r"/api/v1/courses/\d+/(assignments|announcement_topics|discussion_topics|files|folders|modules|pages)",
    r"/api/v1/courses/\d+/assignments/\d+",
    r"/api/v1/courses/\d+/assignments/\d+/submissions/self",
    r"/api/v1/courses/\d+/students/submissions",
    r"/api/v1/courses/\d+/discussion_topics/\d+(/entries|/entries/\d+/replies)?",
    r"/api/v1/courses/\d+/modules/\d+/items",
    r"/api/v1/courses/\d+/pages/[A-Za-z0-9_%.-]+",
    r"/api/v1/folders/\d+/(files|folders)",
    r"/api/v1/(announcements|conversations|calendar_events|planner/items|planner_notes)",
    r"/api/v1/conversations/\d+",
]
PARAMS = {
    "per_page",
    "page",
    "include[]",
    "state[]",
    "enrollment_state",
    "student_ids[]",
    "context_codes[]",
    "start_date",
    "end_date",
    "all_events",
    "type",
    "only_announcements",
    "auto_mark_as_read",
    "order_by",
    "sort",
    "order",
    "include",
    "scope",
}
INCLUDES = {
    "term",
    "teachers",
    "course_progress",
    "submission",
    "submission_history",
    "submission_comments",
    "rubric_assessment",
    "total_scores",
    "items",
}


class CanvasError(Exception):
    def __init__(self, code: str, status: int | None = None):
        self.code, self.status = code, status
        super().__init__(code)


@dataclass
class PageResult:
    items: list[dict] = field(default_factory=list)
    pages: int = 0
    complete: bool = False
    cursor: str | None = None
    error: str | None = None


class CanvasClient:
    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.origin = settings.canvas_base_url
        self.client = httpx.AsyncClient(
            timeout=30, follow_redirects=False, trust_env=False, transport=transport
        )
        self.gate = asyncio.Lock()
        self.last_request = 0.0
        self.health = {}
        self.rate = {}
        self.cache = {}
        self.user_id: str | None = None
        self.cookie_verified = False
        self.token = read_secret(settings.canvas_token_file)
        self.cookies = json.loads(read_secret(settings.canvas_cookie_file) or "[]")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.client.aclose()

    def safe_url(self, path: str, params=None) -> str:
        url = urljoin(self.origin + "/", path)
        p, base = urlsplit(url), urlsplit(self.origin)
        if (p.scheme, p.netloc) != (base.scheme, base.netloc) or p.username or p.password or p.fragment:
            raise CanvasError("unsafe_url")
        if not any(re.fullmatch(pattern, p.path) for pattern in PATHS):
            raise CanvasError("path_not_allowed")
        pairs = parse_qsl(p.query, keep_blank_values=True) + list(params or [])
        for key, value in pairs:
            if key not in PARAMS:
                raise CanvasError("parameter_not_allowed")
            if key in ("include", "include[]") and value not in INCLUDES:
                raise CanvasError("include_not_allowed")
            if key == "auto_mark_as_read" and str(value).lower() != "false":
                raise CanvasError("read_side_effect_blocked")
            if key == "student_ids[]" and str(value) not in ("self", self.user_id):
                raise CanvasError("other_student_blocked")
        if re.fullmatch(r"/api/v1/conversations/\d+", p.path):
            pairs = [(k, v) for k, v in pairs if k != "auto_mark_as_read"] + [("auto_mark_as_read", "false")]
        return self.origin + p.path + ("?" + urlencode(pairs) if pairs else "")

    def headers(self, mode: str, url: str) -> dict:
        headers = {"Accept": "application/json", "User-Agent": "canvas-notifier/0.1"}
        if mode == "token":
            if not self.token:
                raise CanvasError("bearer_missing")
            headers["Authorization"] = f"Bearer {self.token}"
        else:
            target = urlsplit(url)
            pairs = []
            for cookie in self.cookies:
                domain = cookie.get("domain", "").lstrip(".")
                # 只发送精确 Canvas 主机的 Cookie，不保存 IAM 域族会话。
                if domain != target.hostname or not target.path.startswith(cookie.get("path", "/")):
                    continue
                if cookie.get("expires", -1) > 0 and cookie["expires"] <= time.time():
                    continue
                name, value = cookie["name"], cookie["value"]
                if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or any(c in value for c in "\r\n;"):
                    raise CanvasError("invalid_cookie")
                pairs.append(f"{name}={value}")
            if not pairs:
                raise CanvasError("cookie_missing")
            headers["Cookie"] = "; ".join(pairs)
        return headers

    async def _get(self, url: str, mode: str):
        headers = self.headers(mode, url)
        cache_key = (mode, url)
        if cache_key in self.cache:
            etag, _, _ = self.cache[cache_key]
            if etag:
                headers["If-None-Match"] = etag
        for attempt in range(4):
            async with self.gate:
                await asyncio.sleep(
                    max(0, self.settings.request_interval - (time.monotonic() - self.last_request))
                )
                try:
                    # 禁止 httpx 从其他认证通道继承 Set-Cookie。
                    self.client.cookies.clear()
                    response = await self.client.get(url, headers=headers)
                except httpx.RequestError:
                    if attempt == 3:
                        raise CanvasError("network_error") from None
                    response = None
                finally:
                    self.last_request = time.monotonic()
            if response is None or response.status_code == 429 or response.status_code >= 500:
                if attempt == 3:
                    raise CanvasError(
                        "rate_limited"
                        if response is not None and response.status_code == 429
                        else "server_error"
                    )
                delay = 2**attempt + random.random()
                if response is not None and response.headers.get("Retry-After"):
                    raw = response.headers["Retry-After"]
                    try:
                        delay = max(delay, float(raw))
                    except ValueError:
                        try:
                            delay = max(
                                delay,
                                (parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds(),
                            )
                        except (ValueError, TypeError):
                            pass
                if delay > 120:
                    raise CanvasError("rate_limited")
                await asyncio.sleep(delay)
                continue
            self.rate = {
                key: response.headers.get(key) for key in ("X-Request-Cost", "X-Rate-Limit-Remaining")
            }
            if response.status_code == 304:
                if cache_key not in self.cache:
                    raise CanvasError("cache_miss_304")
                _, data, next_url = self.cache[cache_key]
                return data, next_url
            if response.status_code == 401 or 300 <= response.status_code < 400:
                self.health[mode] = (
                    "bearer_expired_or_rejected" if mode == "token" else "cookie_expired_or_rejected"
                )
                raise CanvasError("auth_rejected", response.status_code)
            if response.status_code == 403:
                raise CanvasError("resource_forbidden", 403)
            if response.status_code in (404, 405):
                raise CanvasError("resource_unavailable", response.status_code)
            if not response.is_success:
                raise CanvasError("http_error", response.status_code)
            if "json" not in response.headers.get("Content-Type", ""):
                self.health[mode] = "login_html_returned"
                raise CanvasError("login_html_returned", response.status_code)
            try:
                data = response.json()
            except ValueError:
                raise CanvasError("invalid_json") from None
            next_url = response.links.get("next", {}).get("url")
            # 在返回结果前校验，绝不向恶意 next Link 发送凭据。
            if next_url:
                next_url = self.safe_url(next_url)
                if urlsplit(next_url).path != urlsplit(url).path:
                    raise CanvasError("pagination_path_changed")
            self.cache[cache_key] = (response.headers.get("ETag"), data, next_url)
            self.health[mode] = "bearer_ok" if mode == "token" else "cookie_ok"
            return data, next_url
        raise CanvasError("retry_exhausted")

    async def get(self, path, params=None, *, kind="", mode=None):
        url = self.safe_url(path, params)
        selected = mode or self.settings.canvas_auth_mode
        allowed = set(self.settings.canvas_cookie_resources.split(","))
        if (
            mode is None
            and selected == "token"
            and self.settings.canvas_cookie_fallback
            and self.cookie_verified
            and kind in allowed
            and self.health.get("token")
            in ("auth_rejected", "bearer_expired_or_rejected", "login_html_returned", "bearer_missing")
        ):
            return await self._get(url, "cookie")
        try:
            return await self._get(url, selected)
        except CanvasError as error:
            allowed = set(self.settings.canvas_cookie_resources.split(","))
            if (
                mode is None
                and selected == "token"
                and error.code in ("auth_rejected", "login_html_returned", "bearer_missing")
                and self.settings.canvas_cookie_fallback
                and self.cookie_verified
                and kind in allowed
            ):
                return await self._get(url, "cookie")
            raise

    async def identity(self, mode=None):
        data, _ = await self.get("/api/v1/users/self", mode=mode, kind="identity")
        if not isinstance(data, dict) or not data.get("id"):
            raise CanvasError("invalid_identity")
        return str(data["id"])

    async def paginate(self, path, params=None, *, kind="") -> PageResult:
        result = PageResult()
        url = self.safe_url(path, params)
        seen = set()
        while url:
            if url in seen:
                result.error, result.cursor = "pagination_loop", url
                return result
            if result.pages >= self.settings.max_pages:
                result.error, result.cursor = "page_budget", url
                return result
            seen.add(url)
            try:
                data, next_url = await self.get(url, kind=kind)
                if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
                    raise CanvasError("schema_changed")
            except CanvasError as error:
                result.error, result.cursor = error.code, url
                return result
            result.items.extend(data)
            result.pages += 1
            url = next_url
        result.complete = True
        return result
