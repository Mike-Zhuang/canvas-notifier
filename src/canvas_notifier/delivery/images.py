"""将受信任 Canvas 文件图片内嵌到邮件；鉴权和临时下载参数不进入邮件或数据库。"""

import asyncio
import hashlib
import html
import re
from urllib.parse import urljoin, urlsplit

import httpx

from canvas_notifier.canvas.http import CanvasClient, CanvasError

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_MAIL_IMAGE_BYTES = 20 * 1024 * 1024


def image_path(url, origin):
    parsed, base = urlsplit(url), urlsplit(origin)
    if (
        (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc)
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise CanvasError("image_url_blocked")
    if not re.fullmatch(r"/(?:courses/\d+/)?files/\d+(?:/(?:download|preview))?", parsed.path):
        raise CanvasError("image_path_blocked")
    return parsed


def image_type(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    raise CanvasError("image_type_blocked")


def references(payload):
    for image in payload.get("content_images", []):
        yield image
    for key in ("digest_items", "related_items"):
        for item in payload.get(key, []):
            yield from references(item)


async def download_image(client, url, mode):
    parsed = image_path(url, client.origin)
    url = client.origin + re.sub(r"/(?:download|preview)$", "", parsed.path) + "/download"
    for _ in range(5):
        image_path(url, client.origin)
        headers = client.headers(mode, url)
        headers["Accept"] = "image/png,image/jpeg,image/gif,image/webp"
        client.client.cookies.clear()
        async with client.client.stream("GET", url, headers=headers, timeout=10) as response:
            if (
                response.status_code == 401
                or response.status_code == 302
                and "/login" in response.headers.get("location", "")
            ):
                raise CanvasError("image_auth_rejected")
            if 300 <= response.status_code < 400:
                # 逐跳检查域名和文件路径，绝不向外站发送 Canvas Cookie/Bearer。
                url = urljoin(url, response.headers.get("location", ""))
                image_path(url, client.origin)
                continue
            if response.status_code != 200:
                raise CanvasError("image_unavailable")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > MAX_IMAGE_BYTES:
                    raise CanvasError("image_too_large")
                data.extend(chunk)
            content = bytes(data)
            return content, image_type(content)
    raise CanvasError("image_redirect_limit")


async def load_images(settings, payload):
    wanted = {image["url"]: image for image in references(payload)}
    if not wanted:
        return {}
    loaded, total = {}, 0
    try:
        async with asyncio.timeout(30), CanvasClient(settings) as client:
            for url in wanted:
                if total >= MAX_MAIL_IMAGE_BYTES:
                    break
                modes = [settings.canvas_auth_mode]
                if settings.canvas_auth_mode == "token" and settings.canvas_cookie_fallback:
                    modes.append("cookie")
                for mode in modes:
                    try:
                        content, subtype = await download_image(client, url, mode)
                        if total + len(content) <= MAX_MAIL_IMAGE_BYTES:
                            loaded[url] = (content, subtype)
                            total += len(content)
                        break
                    except CanvasError as error:
                        # 只在认证拒绝/未配置时尝试备用凭据，权限和资源失败不换身份。
                        if error.code not in ("image_auth_rejected", "bearer_missing", "cookie_missing"):
                            break
                    except httpx.RequestError:
                        break
    except TimeoutError:
        pass
    return loaded


def embed_images(view, loaded):
    used = {}
    for image in view.get("content_images", []):
        url = image["url"]
        if url not in loaded:
            continue
        cid = hashlib.sha256(url.encode()).hexdigest() + "@canvas-notifier.local"
        if cid in used:
            continue
        used[cid] = loaded[url]
        anchor = f'<a href="{html.escape(url, quote=True)}">'
        picture = f'<img src="cid:{cid}" alt="{html.escape(image["name"], quote=True)}" style="display:block;max-width:100%;height:auto;border:0"><br>'
        view["excerpt"] = view["excerpt"].replace(anchor, picture + anchor)
    for key in ("digest_items", "related_items"):
        for item in view.get(key, []):
            used.update(embed_images(item, loaded))
    view["image_note"] = (
        "正文包含图片，已附在邮件中；若客户端不显示图片，可点击图片链接在 Canvas 查看。"
        if view.get("content_images") and all(image["url"] in loaded for image in view["content_images"])
        else "正文包含图片；本次未能内嵌全部图片，请点击正文图片链接在 Canvas 查看。"
        if view.get("content_images")
        else ""
    )
    return used
