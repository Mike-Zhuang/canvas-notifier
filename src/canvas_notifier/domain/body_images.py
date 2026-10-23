"""识别 Canvas 正文图片，保留稳定文件引用，不保存签名或外站跟踪地址。"""

import html
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit


class BodyImages(HTMLParser):
    def __init__(self, origin):
        super().__init__(convert_charrefs=False)
        self.origin = origin
        self.parts = []
        self.images = []

    def handle_starttag(self, tag, attrs):
        if tag != "img":
            self.parts.append(self.get_starttag_text())
            return
        values = dict(attrs)
        source = values.get("data-api-endpoint") or values.get("src") or ""
        try:
            parsed, base = urlsplit(urljoin(self.origin + "/", html.unescape(source))), urlsplit(self.origin)
            if (
                (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc)
                or parsed.username
                or parsed.password
            ):
                return
            match = re.fullmatch(
                r"/(?:api/v1/)?(?:courses/(\d+)/)?files/(\d+)(?:/(?:preview|download))?", parsed.path
            )
            if not match:
                return
        except ValueError:
            return
        course_id, file_id = match.groups()
        path = f"/courses/{course_id}/files/{file_id}" if course_id else f"/files/{file_id}"
        image = {"file_id": file_id, "url": self.origin + path, "name": values.get("alt") or "题目图片"}
        self.images.append(image)
        self.parts.append(
            f'<a href="{html.escape(image["url"], quote=True)}">图片：{html.escape(image["name"])}</a>'
        )

    def handle_startendtag(self, tag, attrs):
        if tag == "img":
            self.handle_starttag(tag, attrs)
        else:
            self.parts.append(self.get_starttag_text())

    def handle_endtag(self, tag):
        self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        self.parts.append(data)

    def handle_entityref(self, name):
        self.parts.append(f"&{name};")

    def handle_charref(self, name):
        self.parts.append(f"&#{name};")


def body_images(body, origin):
    parser = BodyImages(origin)
    parser.feed(body)
    return "".join(parser.parts), parser.images
