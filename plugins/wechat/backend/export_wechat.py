#!/usr/bin/env python3
"""Export one authorized WeChat Official Account article to Markdown.

Only the rendered public article page is read. The exporter does not call
WeChat private APIs, collect article keys, or extract browser cookies.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import mimetypes
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from wandao_core.browser import (
    CDPClient,
    ExportError,
    ExportStopped,
    check_stopped,
    chrome_debug_available,
    default_data_dir,
    emit,
    http_json,
    open_tab,
    sanitize_filename,
    start_chrome,
    throttle_request,
    wait_for_debug_port,
)
from wandao_core.checkpoint import add_checkpoint_args, open_checkpoint_from_args
from wandao_core.report import finalize_report


PLUGIN_ID = "wechat"
PROVIDER_ID = "wechat-export"
ENTRY_URL = "https://mp.weixin.qq.com/"
WECHAT_HOST = "mp.weixin.qq.com"
DEFAULT_PORT = 9258
DEFAULT_PROFILE = ".wechat-article-chrome-profile"
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_HTML_BYTES = 16 * 1024 * 1024
IMAGE_TIMEOUT_SECONDS = 15
DEFAULT_ACCESS_WAIT_SECONDS = 300
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
IMAGE_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/avif": ".avif",
}
CANONICAL_QUERY_KEYS = {"__biz", "mid", "idx", "sn", "chksm"}


@dataclass(frozen=True)
class WeChatSource:
    canonical_url: str
    article_key: str

    @property
    def item_key(self) -> str:
        return f"wechat:article:{self.article_key}"


@dataclass
class HtmlNode:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[HtmlNode | str] = field(default_factory=list)


@dataclass(frozen=True)
class ImageRef:
    token: str
    source: str
    alt: str
    fallback_sources: tuple[str, ...] = ()


class HtmlTreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = HtmlNode("root")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        name = tag.lower()
        node = HtmlNode(name, {str(key).lower(): str(value or "") for key, value in attrs})
        self.stack[-1].children.append(node)
        if name not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        name = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == name:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1].children.append(data)


class WeChatMarkdownRenderer:
    """Convert the article body HTML while keeping executable content inert."""

    def __init__(self, source_url: str) -> None:
        self.source_url = source_url
        self.images: list[ImageRef] = []
        self._image_tokens: dict[str, str] = {}

    def render(self, source_html: str) -> str:
        parser = HtmlTreeParser()
        parser.feed(source_html or "")
        parser.close()
        return self._normalize(self._render_children(parser.root))

    def _render_children(self, node: HtmlNode, *, in_pre: bool = False) -> str:
        values: list[str] = []
        for child in node.children:
            if isinstance(child, str):
                values.append(child if in_pre else self._inline_text(child))
            else:
                values.append(self._render_node(child, in_pre=in_pre))
        return "".join(values)

    def _render_node(self, node: HtmlNode, *, in_pre: bool = False) -> str:
        tag = node.tag
        if tag in {"script", "style", "noscript", "button", "svg", "path", "canvas", "form", "input", "iframe"}:
            return ""
        if tag == "br":
            return "\n"
        if tag == "hr":
            return "\n\n---\n\n"
        if tag == "img":
            return self._render_image(node)
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            value = self._inline(self._render_children(node)).strip()
            return f"\n\n{'#' * int(tag[1:])} {value}\n\n" if value else ""
        if tag == "p":
            value = self._inline(self._render_children(node)).strip()
            return f"\n\n{value}\n\n" if value else ""
        if tag == "blockquote":
            value = self._normalize(self._render_children(node)).strip()
            quoted = "\n".join(">" if not line else f"> {line}" for line in value.splitlines())
            return f"\n\n{quoted}\n\n" if quoted else ""
        if tag == "pre":
            content = self._text(node).strip("\n")
            if not content:
                return ""
            fence = "```"
            while fence in content:
                fence += "`"
            return f"\n\n{fence}{self._code_language(node)}\n{content}\n{fence}\n\n"
        if tag == "code":
            if in_pre:
                return self._text(node)
            value = self._text(node).strip()
            if not value:
                return ""
            fence = "``" if "`" in value else "`"
            return f"{fence}{value}{fence}"
        if tag in {"strong", "b"}:
            value = self._inline(self._render_children(node)).strip()
            return f"**{value}**" if value else ""
        if tag in {"em", "i"}:
            value = self._inline(self._render_children(node)).strip()
            return f"*{value}*" if value else ""
        if tag in {"del", "s", "strike"}:
            value = self._inline(self._render_children(node)).strip()
            return f"~~{value}~~" if value else ""
        if tag == "a":
            label = self._inline(self._render_children(node)).strip() or self._inline_text(node.attrs.get("title", ""))
            target = self._safe_link(node.attrs.get("href", ""))
            return f"[{label or target}]({self._markdown_url(target)})" if target else label
        if tag in {"ul", "ol"}:
            return self._render_list(node, ordered=tag == "ol")
        if tag == "table":
            return self._render_table(node)
        if tag in {"div", "article", "section", "main", "figure", "figcaption"}:
            value = self._render_children(node, in_pre=in_pre)
            return f"\n\n{value}\n\n" if tag in {"figure", "figcaption"} and value.strip() else value
        return self._render_children(node, in_pre=in_pre)

    def _render_list(self, node: HtmlNode, *, ordered: bool) -> str:
        items = [item for item in node.children if isinstance(item, HtmlNode) and item.tag == "li"]
        if not items:
            return ""
        try:
            start = max(1, int(node.attrs.get("start", "1") or "1"))
        except ValueError:
            start = 1
        lines: list[str] = []
        for index, item in enumerate(items):
            inline_parts: list[str] = []
            nested_parts: list[str] = []
            for child in item.children:
                if isinstance(child, HtmlNode) and child.tag in {"ul", "ol"}:
                    nested_parts.append(self._render_list(child, ordered=child.tag == "ol").strip())
                elif isinstance(child, HtmlNode):
                    inline_parts.append(self._render_node(child))
                else:
                    inline_parts.append(self._inline_text(child))
            text = self._inline("".join(inline_parts)).strip()
            marker = f"{start + index}. " if ordered else "- "
            lines.append(marker + text if text else marker.rstrip())
            for nested in nested_parts:
                lines.extend("  " + line for line in nested.splitlines() if line.strip())
        return "\n\n" + "\n".join(lines) + "\n\n"

    def _render_table(self, node: HtmlNode) -> str:
        rows: list[tuple[list[HtmlNode], bool]] = []
        for child in node.children:
            if not isinstance(child, HtmlNode):
                continue
            if child.tag == "tr":
                cells = [item for item in child.children if isinstance(item, HtmlNode) and item.tag in {"th", "td"}]
                rows.append((cells, any(cell.tag == "th" for cell in cells)))
            elif child.tag in {"thead", "tbody", "tfoot"}:
                rows.extend(self._table_rows(child))
        if not rows:
            return ""
        rendered = [[self._inline(self._render_children(cell)).replace("|", "\\|").strip() for cell in cells] for cells, _ in rows]
        rendered = [row for row in rendered if row]
        if not rendered:
            return ""
        width = max(len(row) for row in rendered)
        normalized = [row + [""] * (width - len(row)) for row in rendered]
        lines = ["| " + " | ".join(normalized[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
        lines.extend("| " + " | ".join(row) + " |" for row in normalized[1:])
        return "\n\n" + "\n".join(lines) + "\n\n"

    def _table_rows(self, node: HtmlNode) -> list[tuple[list[HtmlNode], bool]]:
        rows: list[tuple[list[HtmlNode], bool]] = []
        for child in node.children:
            if isinstance(child, HtmlNode) and child.tag == "tr":
                cells = [item for item in child.children if isinstance(item, HtmlNode) and item.tag in {"th", "td"}]
                rows.append((cells, any(cell.tag == "th" for cell in cells)))
            elif isinstance(child, HtmlNode) and child.tag in {"thead", "tbody", "tfoot"}:
                rows.extend(self._table_rows(child))
        return rows

    def _render_image(self, node: HtmlNode) -> str:
        candidates: list[str] = []
        for key in ("data-src", "data-original", "data-actualsrc", "src", "srcset"):
            candidate = str(node.attrs.get(key, "") or "").strip()
            if key == "srcset" and candidate:
                candidate = candidate.split(",", 1)[0].strip().split(" ", 1)[0]
            if candidate.startswith("//"):
                candidate = f"https:{candidate}"
            if candidate:
                candidates.append(normalize_image_url(candidate))
        alt = self._inline_text(node.attrs.get("alt", "")) or "微信公众号图片"
        trusted = list(dict.fromkeys(value for value in candidates if is_wechat_image_url(value)))
        if trusted:
            source = trusted[0]
            token = self._image_tokens.get(source)
            if not token:
                token = f"__WANDAO_WECHAT_IMAGE_{len(self.images) + 1:03d}__"
                self._image_tokens[source] = token
                self.images.append(ImageRef(token, source, alt, tuple(trusted[1:])))
            return f"\n\n![{alt}]({token})\n\n"
        remote = self._safe_link(candidates[0] if candidates else "")
        return f"\n\n![{alt}]({self._markdown_url(remote)})\n\n" if remote else ""

    def _safe_link(self, value: str) -> str:
        source = html.unescape(str(value or "")).strip()
        if not source or any(char in source for char in "\r\n"):
            return ""
        parsed = urllib.parse.urlsplit(source)
        if parsed.scheme.lower() in {"https", "http", "mailto"}:
            return source
        if source.startswith("/") and not source.startswith("//"):
            return urllib.parse.urljoin(self.source_url, source)
        return ""

    @staticmethod
    def _markdown_url(value: str) -> str:
        return value.replace("\\", "/").replace(" ", "%20").replace("(", "%28").replace(")", "%29")

    @staticmethod
    def _inline_text(value: str) -> str:
        return re.sub(r"\s+", " ", html.unescape(str(value or "")).replace("\xa0", " "))

    @staticmethod
    def _inline(value: str) -> str:
        text = re.sub(r"[ \t]+\n", "\n", value)
        return re.sub(r"\n{3,}", "\n\n", text)

    @staticmethod
    def _normalize(value: str) -> str:
        text = value.replace("\r\n", "\n").replace("\r", "\n")
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return f"{text}\n" if text else ""

    def _text(self, node: HtmlNode) -> str:
        return "".join(child if isinstance(child, str) else "\n" if child.tag == "br" else self._text(child) for child in node.children)

    @staticmethod
    def _code_language(node: HtmlNode) -> str:
        for value in node.attrs.get("class", "").split():
            match = re.search(r"(?:language|lang)-([A-Za-z0-9_+-]+)", value)
            if match:
                return match.group(1).lower()
        for child in node.children:
            if isinstance(child, HtmlNode):
                language = WeChatMarkdownRenderer._code_language(child)
                if language:
                    return language
        return ""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def default_profile_path() -> Path:
    override = os.environ.get("WECHAT_PROFILE_DIR", "").strip()
    return Path(override).expanduser().resolve() if override else default_data_dir() / DEFAULT_PROFILE


def parse_wechat_url(value: str) -> WeChatSource:
    raw = html.unescape(str(value or "")).strip()
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ExportError("微信公众号链接端口格式无效。") from exc
    if parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() != WECHAT_HOST or parsed.username or parsed.password or port not in {None, 443}:
        raise ExportError("仅支持 HTTPS 的 mp.weixin.qq.com 单篇文章链接。")
    path = urllib.parse.unquote(parsed.path or "").rstrip("/") or "/"
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    identity = {key: value for key, value in query if key in CANONICAL_QUERY_KEYS and value}
    if path == "/s":
        if not identity.get("__biz") or not identity.get("mid"):
            raise ExportError("微信公众号文章链接需要包含 __biz 与 mid 参数。")
        ordered = [(key, identity[key]) for key in ("__biz", "mid", "idx", "sn", "chksm") if identity.get(key)]
        canonical = f"https://{WECHAT_HOST}/s?{urllib.parse.urlencode(ordered)}"
        article_key = "-".join(identity.get(key, "") for key in ("__biz", "mid", "idx", "sn") if identity.get(key))
    elif re.fullmatch(r"/s/[A-Za-z0-9_-]{6,128}", path):
        canonical = f"https://{WECHAT_HOST}{path}"
        article_key = path.rsplit("/", 1)[-1]
    else:
        raise ExportError("当前仅支持单篇微信公众号文章链接（/s?... 或 /s/短标识），不支持公众号主页、搜索和合集页。")
    safe_key = hashlib.sha256(article_key.encode("utf-8")).hexdigest()[:20]
    return WeChatSource(canonical, safe_key)


def page_matches_source(payload: dict[str, Any], source: WeChatSource) -> bool:
    try:
        return parse_wechat_url(str(payload.get("url") or "")).canonical_url == source.canonical_url
    except ExportError:
        return False


def is_wechat_image_url(value: str) -> bool:
    parsed = urllib.parse.urlsplit(str(value or ""))
    return parsed.scheme == "https" and (parsed.hostname or "").lower() == "mmbiz.qpic.cn"


def normalize_image_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(str(value or ""))
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def find_node_by_id(node: HtmlNode, node_id: str) -> HtmlNode | None:
    """Find an article element without relying on a browser DOM."""
    if node.attrs.get("id") == node_id:
        return node
    for child in node.children:
        if isinstance(child, HtmlNode):
            found = find_node_by_id(child, node_id)
            if found:
                return found
    return None


def serialize_html_node(node: HtmlNode) -> str:
    """Serialize the parsed node so the existing Markdown renderer stays in charge."""
    if node.tag == "root":
        return "".join(serialize_html_child(child) for child in node.children)
    attrs = "".join(f' {key}="{html.escape(value, quote=True)}"' for key, value in node.attrs.items())
    if node.tag in VOID_TAGS:
        return f"<{node.tag}{attrs}>"
    inner = "".join(serialize_html_child(child) for child in node.children)
    return f"<{node.tag}{attrs}>{inner}</{node.tag}>"


def serialize_html_child(child: HtmlNode | str) -> str:
    return html.escape(child, quote=False) if isinstance(child, str) else serialize_html_node(child)


def extract_payload_from_html(raw_html: str, source: WeChatSource, final_url: str = "") -> dict[str, Any]:
    """Extract the same contract as the browser path from a public HTML response."""
    parser = HtmlTreeParser()
    parser.feed(raw_html or "")
    parser.close()
    text_renderer = WeChatMarkdownRenderer(source.canonical_url)
    root = find_node_by_id(parser.root, "js_content")
    body_text = re.sub(r"\s+", " ", text_renderer._text(parser.root)).strip()
    title_node = find_node_by_id(parser.root, "activity-name")
    author_node = find_node_by_id(parser.root, "js_name")
    published_node = find_node_by_id(parser.root, "publish_time")
    title = text_renderer._text(title_node).strip() if title_node else ""
    author = text_renderer._text(author_node).strip() if author_node else ""
    published_at = text_renderer._text(published_node).strip() if published_node else ""
    original_node = find_node_by_id(parser.root, "js_original_link")
    copyright_text = text_renderer._text(original_node).strip() if original_node else ""
    client_required = root is None and bool(re.search(r"请在微信客户端(?:打开|查看)|请在微信中(?:打开|查看)|微信客户端打开", body_text))
    verification_required = root is None and not client_required and bool(re.search(r"环境异常|访问过于频繁|操作过于频繁|安全验证|人机验证|滑动验证|验证码", body_text))
    login_required = root is None and not client_required and not verification_required and bool(re.search(r"请登录|登录后(?:查看|继续)|扫码登录", body_text))
    if final_url and not page_matches_source({"url": final_url}, source):
        raise ExportError("微信公众号直连页面已跳转到非目标内容，未导出该页面。")
    if client_required:
        raise ExportError("此文章要求在微信客户端内打开。当前插件不能读取微信客户端 WebView 内容。")
    if verification_required:
        raise ExportError("微信公众号直连页面要求完成安全验证，请在插件打开的浏览器中完成验证后重试。")
    if login_required:
        raise ExportError("微信公众号直连页面要求登录，请在插件打开的浏览器中完成登录后重试。")
    if root is None:
        raise ExportError("微信公众号直连响应没有找到正文区域。")
    html_body = serialize_html_node(root)
    return {
        "url": final_url or source.canonical_url,
        "title": title or "微信公众号文章",
        "author": author,
        "publishedAt": published_at,
        "copyright": copyright_text,
        "html": html_body,
        "textLength": len(re.sub(r"\s+", " ", text_renderer._text(root)).strip()),
        "clientRequired": False,
        "verificationRequired": False,
        "loginRequired": False,
    }


def fetch_article_payload_direct(source: WeChatSource, args: argparse.Namespace) -> dict[str, Any]:
    """Fetch only public HTML directly; no cookies, credentials, or proxy are used."""
    check_stopped(args)
    throttle_request(args)
    request = urllib.request.Request(
        source.canonical_url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
            "Referer": ENTRY_URL,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if content_type and content_type != "text/html":
                raise ExportError(f"微信公众号直连响应类型无效：{content_type}")
            raw = read_limited_response(response, MAX_HTML_BYTES)
            charset = response.headers.get_content_charset() or "utf-8"
            final_url = str(response.geturl() or source.canonical_url)
    except urllib.error.HTTPError as exc:
        raise ExportError(f"微信公众号直连读取返回 HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ExportError(f"微信公众号直连读取失败：{exc.reason}") from exc
    return extract_payload_from_html(raw.decode(charset, errors="replace"), source, final_url)


def safe_resource_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(str(value or ""))
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def page_for_wechat(port: int, preferred_url: str = "") -> dict[str, Any] | None:
    pages = http_json(f"http://127.0.0.1:{port}/json/list", timeout=5)
    fallback = None
    for page in pages:
        if page.get("type") != "page":
            continue
        host = (urllib.parse.urlsplit(str(page.get("url") or "")).hostname or "").lower()
        if host != WECHAT_HOST:
            continue
        if preferred_url:
            try:
                if parse_wechat_url(str(page.get("url") or "")).canonical_url == parse_wechat_url(preferred_url).canonical_url:
                    return page
            except ExportError:
                pass
        fallback = fallback or page
    return fallback


def find_available_debug_port(start: int) -> int:
    for port in range(max(1024, start), max(1024, start) + 40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise ExportError("没有可用的微信公众号浏览器调试端口，请关闭占用端口的浏览器后重试。")


def open_wechat_target(cdp: CDPClient, target_url: str, args: argparse.Namespace) -> None:
    throttle_request(args)
    cdp.navigate(target_url)
    for _ in range(30):
        check_stopped(args)
        state = cdp.evaluate("({url: location.href, readyState: document.readyState})", timeout=6)
        if isinstance(state, dict):
            current_host = (urllib.parse.urlsplit(str(state.get("url") or "")).hostname or "").lower()
            if current_host == WECHAT_HOST and str(state.get("readyState") or "") in {"interactive", "complete"}:
                return
        time.sleep(0.5)
    raise ExportError("微信公众号目标页面打开超时，请确认浏览器中能正常访问该链接后重试。")


def connect_wechat_browser(args: argparse.Namespace, initial_url: str) -> tuple[CDPClient, Any | None]:
    process = None
    port = int(args.port)
    page = page_for_wechat(port, initial_url) if chrome_debug_available(port) else None
    if not page and chrome_debug_available(port):
        port = find_available_debug_port(port + 1)
        args.port = port
    if not chrome_debug_available(port):
        profile = Path(args.profile_dir).expanduser().resolve() if args.profile_dir else default_profile_path()
        process = start_chrome(port, profile, initial_url, getattr(args, "browser_path", "") or None)
        wait_for_debug_port(port, timeout=30)
    page = page_for_wechat(port, initial_url)
    if not page:
        open_tab(port, initial_url)
        time.sleep(1)
        page = page_for_wechat(port, initial_url)
    if not page or not page.get("webSocketDebuggerUrl"):
        raise ExportError("无法找到或创建微信公众号网页标签页。")
    client = CDPClient(str(page["webSocketDebuggerUrl"]))
    client.connect()
    client.send("Runtime.enable")
    client.send("Page.enable")
    open_wechat_target(client, initial_url, args)
    return client, process


def page_payload_expression() -> str:
    return """
(() => {
  const trim = (value) => String(value || '').replace(/\\s+/g, ' ').trim();
  const readable = (node) => trim(node?.innerText || node?.textContent || '');
  const root = document.querySelector('#js_content');
  const bodyText = trim(document.body?.innerText || '');
  const clientRequired = !root && /请在微信客户端(?:打开|查看)|请在微信中(?:打开|查看)|微信客户端打开/.test(bodyText);
  const verificationRequired = !root && !clientRequired && /环境异常|访问过于频繁|操作过于频繁|安全验证|人机验证|滑动验证|验证码/.test(bodyText);
  const loginRequired = !root && !clientRequired && !verificationRequired && /请登录|登录后(?:查看|继续)|扫码登录/.test(bodyText);
  const original = readable(document.querySelector('#js_original_link'));
  return {
    url: location.href,
    title: readable(document.querySelector('#activity-name')) || trim(document.title).replace(/\\s*[-_]?\\s*微信公众平台\\s*$/, ''),
    author: readable(document.querySelector('#js_name')),
    publishedAt: readable(document.querySelector('#publish_time')),
    copyright: original,
    html: root?.innerHTML || '',
    textLength: trim(root?.innerText).length,
    clientRequired,
    verificationRequired,
    loginRequired
  };
})()
"""


def wait_for_page_payload(cdp: CDPClient, source: WeChatSource, args: argparse.Namespace) -> dict[str, Any]:
    wait_seconds = max(30, int(getattr(args, "verification_wait_seconds", DEFAULT_ACCESS_WAIT_SECONDS) or DEFAULT_ACCESS_WAIT_SECONDS))
    deadline = time.time() + wait_seconds
    waiting_state = ""
    last_payload: dict[str, Any] = {}
    expression = page_payload_expression()
    while time.time() < deadline:
        check_stopped(args)
        try:
            payload = cdp.evaluate(expression, timeout=12)
        except ExportError as exc:
            last_payload = {"error": str(exc)}
        else:
            if isinstance(payload, dict):
                last_payload = payload
                if payload.get("clientRequired"):
                    raise ExportError("此文章要求在微信客户端内打开。当前插件只导出可在浏览器正常访问的公开单篇文章，不能绕过该平台限制。")
                if payload.get("verificationRequired"):
                    if waiting_state != "verification":
                        waiting_state = "verification"
                        emit(args, "微信公众号页面正在要求安全验证。请在本插件打开的浏览器中完成验证；完成后保持或返回该文章页，插件会自动继续。", event="auth.verification.required", level="warn")
                    time.sleep(0.5)
                    continue
                if payload.get("loginRequired"):
                    if waiting_state != "login":
                        waiting_state = "login"
                        emit(args, "微信公众号页面要求登录。请在本插件打开的浏览器中完成登录；完成后保持或返回该文章页，插件会自动继续。", event="auth.login.required", level="warn")
                    time.sleep(0.5)
                    continue
                if not page_matches_source(payload, source):
                    last_payload["targetMismatch"] = True
                elif len(str(payload.get("html") or "").strip()) >= 8 and int(payload.get("textLength") or 0) > 0:
                    return payload
        time.sleep(0.5)
    if waiting_state == "verification":
        raise ExportError(f"等待微信公众号安全验证超过 {wait_seconds} 秒。请完成验证后重新导出。")
    if waiting_state == "login":
        raise ExportError(f"等待微信公众号登录超过 {wait_seconds} 秒。请完成登录后重新导出。")
    if last_payload.get("targetMismatch"):
        raise ExportError("微信公众号页面已跳转到非目标内容，未导出该页面。请返回原文章链接后重试。")
    raise ExportError(f"微信公众号正文没有在 {wait_seconds} 秒内加载完成。请确认链接公开可访问，或完成页面要求的验证后重试。")


def read_limited_response(response: Any, max_bytes: int = MAX_IMAGE_BYTES) -> bytes:
    try:
        declared_size = int(str(response.headers.get("Content-Length") or "0") or "0")
    except ValueError:
        declared_size = 0
    if declared_size > max_bytes:
        raise ExportError(f"图片超过大小限制（{max_bytes // 1024 // 1024} MB）")
    raw = response.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ExportError(f"图片超过大小限制（{max_bytes // 1024 // 1024} MB）")
    return raw


def download_image(source: str, page_url: str) -> tuple[bytes, str, str]:
    current = source
    opener = urllib.request.build_opener(NoRedirect())
    for _ in range(4):
        if not is_wechat_image_url(current):
            raise ExportError("图片跳转到了不受信任的域名")
        request = urllib.request.Request(
            current,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                "Referer": page_url,
            },
        )
        try:
            with opener.open(request, timeout=IMAGE_TIMEOUT_SECONDS) as response:
                content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                if content_type not in IMAGE_CONTENT_TYPES:
                    raise ExportError(f"图片响应类型无效：{content_type or '未知类型'}")
                return read_limited_response(response), content_type, current
        except urllib.error.HTTPError as exc:
            if exc.code not in {301, 302, 303, 307, 308}:
                raise ExportError(f"图片下载返回 HTTP {exc.code}") from exc
            location = str(exc.headers.get("Location") or "")
            if not location:
                raise ExportError(f"图片下载返回 HTTP {exc.code}，但没有跳转地址") from exc
            current = urllib.parse.urljoin(current, location)
    raise ExportError("图片重定向次数过多")


def image_extension(content_type: str, source: str) -> str:
    if content_type in IMAGE_CONTENT_TYPES:
        return IMAGE_CONTENT_TYPES[content_type]
    suffix = Path(urllib.parse.urlsplit(source).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return mimetypes.guess_extension(content_type) or ".img"


def rewrite_images(markdown: str, images: list[ImageRef], md_path: Path, args: argparse.Namespace, source: WeChatSource, checkpoint: Any | None = None) -> tuple[str, list[dict[str, str]], int]:
    rewritten = markdown
    failures: list[dict[str, str]] = []
    saved = 0
    assets_dir = md_path.with_name(f"{md_path.stem}_assets")
    for index, image in enumerate(images, start=1):
        check_stopped(args)
        resource_key = f"{source.item_key}:image:{hashlib.sha256(image.source.encode('utf-8')).hexdigest()[:20]}"
        if not getattr(args, "download_images", False):
            rewritten = rewritten.replace(image.token, WeChatMarkdownRenderer._markdown_url(image.source))
            continue
        try:
            throttle_request(args)
            if checkpoint:
                checkpoint.upsert_resource(source.item_key, resource_key, "image", safe_resource_url(image.source), metadata={"index": index})
                checkpoint.start_resource(resource_key)
            raw = content_type = final_url = None
            last_error: Exception | None = None
            for candidate in (image.source, *image.fallback_sources):
                try:
                    raw, content_type, final_url = download_image(candidate, source.canonical_url)
                    break
                except ExportStopped:
                    raise
                except Exception as exc:  # noqa: BLE001 - retain a usable remote link on individual failures.
                    last_error = exc
            if raw is None or content_type is None or final_url is None:
                assert last_error is not None
                raise last_error
            assets_dir.mkdir(parents=True, exist_ok=True)
            target = assets_dir / f"image-{index:03d}{image_extension(content_type, final_url)}"
            target.write_bytes(raw)
            relative = f"{assets_dir.name}/{target.name}"
            rewritten = rewritten.replace(image.token, WeChatMarkdownRenderer._markdown_url(relative))
            saved += 1
            if checkpoint:
                checkpoint.complete_resource(resource_key, local_path=str(target), target=relative, metadata={"contentType": content_type})
            emit(args, f"微信公众号图片已保存：{target.name}", event="resource.download.completed", resource={"type": "image", "index": index, "path": str(target)})
        except ExportStopped:
            if checkpoint:
                checkpoint.fail_resource(resource_key, "stopped")
            raise
        except Exception as exc:  # noqa: BLE001 - do not discard an otherwise complete article.
            rewritten = rewritten.replace(image.token, WeChatMarkdownRenderer._markdown_url(image.source))
            failure = {"source": safe_resource_url(image.source), "error": str(exc), "index": str(index)}
            failures.append(failure)
            if checkpoint:
                checkpoint.fail_resource(resource_key, str(exc))
            emit(args, f"微信公众号图片下载失败，已保留远程链接：{exc}", event="resource.download.failed", level="warn", resource={"type": "image", "index": index}, error={"type": type(exc).__name__, "message": str(exc)})
    return rewritten, failures, saved


def build_front_matter(payload: dict[str, Any], source: WeChatSource) -> str:
    fields = [("title", str(payload.get("title") or "微信公众号文章")), ("source", source.canonical_url), ("content_type", "article")]
    for key, payload_key in (("author", "author"), ("published_at", "publishedAt"), ("copyright", "copyright")):
        value = str(payload.get(payload_key) or "").strip()
        if value:
            fields.append((key, value))
    fields.append(("exported_at", time.strftime("%Y-%m-%dT%H:%M:%S%z")))
    return "---\n" + "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in fields) + "\n---\n\n"


def markdown_matches_source(path: Path, source: WeChatSource) -> bool:
    try:
        prefix = path.read_text(encoding="utf-8", errors="replace")[:8192]
    except OSError:
        return False
    if not prefix.startswith("---\n"):
        return False
    header_end = prefix.find("\n---\n", 4)
    if header_end < 0:
        return False
    for line in prefix[4:header_end].splitlines():
        if line.startswith("source: "):
            try:
                return str(json.loads(line[len("source: ") :])) == source.canonical_url
            except json.JSONDecodeError:
                return False
    return False


def markdown_path(output: Path, title: str, source: WeChatSource) -> Path:
    filename = sanitize_filename(str(title or "微信公众号文章"), fallback="微信公众号文章", max_len=110)
    candidate = output / f"{filename}.md"
    if not candidate.exists() or markdown_matches_source(candidate, source):
        return candidate
    for index in range(1, 1000):
        suffix = "" if index == 1 else f" ({index})"
        candidate = output / f"{filename} [wechat-{source.article_key[:8]}]{suffix}.md"
        if not candidate.exists() or markdown_matches_source(candidate, source):
            return candidate
    raise ExportError("无法为微信公众号导出文件分配安全的唯一文件名。")


def export_wechat(args: argparse.Namespace) -> dict[str, Any]:
    source = parse_wechat_url(args.source_url)
    output = Path(args.output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    started = time.time()
    checkpoint = open_checkpoint_from_args(args, PROVIDER_ID, "export")
    cdp: CDPClient | None = None
    process = None
    exported = skipped = image_success = 0
    failures: list[dict[str, str]] = []
    image_failures: list[dict[str, str]] = []
    md_path: Path | None = None
    try:
        if checkpoint:
            checkpoint.start_task({"source": source.canonical_url, "outputDir": str(output), "totalDocs": 1, "resume": bool(args.resume)})
            checkpoint.upsert_item(source.item_key, title=source.article_key, source_url=source.canonical_url, source_id=source.article_key)
            status = checkpoint.item_status(source.item_key)
            if args.resume and status == "completed":
                skipped = 1
                emit(args, "微信公众号单篇内容已完成，继续任务时跳过。", event="task.skipped")
                return finish_export(output, source, started, exported, skipped, image_success, failures, image_failures, checkpoint, md_path, args)
            checkpoint.start_item(source.item_key, "content")
        emit(args, "正在打开微信公众号文章并读取正文…", event="document.export.started", doc={"id": source.article_key, "type": "article"})
        try:
            cdp, process = connect_wechat_browser(args, source.canonical_url)
        except ExportError as browser_error:
            # The direct path is intentionally a fallback for public HTML only.
            # It does not carry browser cookies and cannot unlock client-only pages.
            emit(
                args,
                f"浏览器不可用，尝试直连读取公开文章：{browser_error}",
                event="document.export.direct_fallback",
                level="warn",
            )
            payload = fetch_article_payload_direct(source, args)
        else:
            payload = wait_for_page_payload(cdp, source, args)
        title = str(payload.get("title") or "微信公众号文章").strip()
        md_path = markdown_path(output, title, source)
        if args.incremental and md_path.exists() and markdown_matches_source(md_path, source):
            skipped = 1
            if checkpoint:
                checkpoint.complete_item(source.item_key, local_path=str(md_path), metadata={"skippedExisting": True})
            emit(args, f"目标文件已存在，已跳过：{md_path.name}", event="document.export.skipped", doc={"id": source.article_key, "path": str(md_path)})
        else:
            renderer = WeChatMarkdownRenderer(source.canonical_url)
            body = renderer.render(str(payload.get("html") or ""))
            if len(body.strip()) < 20:
                raise ExportError("微信公众号页面没有返回可导出的正文。")
            markdown = build_front_matter(payload, source) + f"# {title}\n\n" + body
            markdown, image_failures, image_success = rewrite_images(markdown, renderer.images, md_path, args, source, checkpoint)
            md_path.write_text(markdown, encoding="utf-8", newline="\n")
            exported = 1
            if checkpoint:
                if image_failures:
                    checkpoint.fail_item(source.item_key, f"{len(image_failures)} 个图片下载失败")
                else:
                    checkpoint.complete_item(source.item_key, local_path=str(md_path), metadata={"images": image_success})
            emit(args, f"微信公众号内容导出完成：{md_path.name}", event="document.export.completed", level="success" if not image_failures else "warn", doc={"id": source.article_key, "type": "article", "path": str(md_path)}, stats={"imageSuccess": image_success, "imageFailureCount": len(image_failures)})
    except ExportStopped:
        if checkpoint:
            checkpoint.fail_item(source.item_key, "stopped")
            checkpoint.fail_task("stopped", status="stopped")
            checkpoint.close()
        raise
    except Exception as exc:  # noqa: BLE001 - produce a task-center report for every failed article.
        failures.append({"source": source.canonical_url, "title": source.article_key, "error": str(exc)})
        if checkpoint:
            checkpoint.fail_item(source.item_key, str(exc))
        emit(args, f"微信公众号内容导出失败：{exc}", event="document.export.failed", level="error", doc={"id": source.article_key, "type": "article"}, error={"type": type(exc).__name__, "message": str(exc)})
    finally:
        if cdp:
            cdp.close()
        if process and args.close_started_chrome:
            process.terminate()
    return finish_export(output, source, started, exported, skipped, image_success, failures, image_failures, checkpoint, md_path, args)


def finish_export(output: Path, source: WeChatSource, started: float, exported: int, skipped: int, image_success: int, failures: list[dict[str, str]], image_failures: list[dict[str, str]], checkpoint: Any | None, md_path: Path | None, args: argparse.Namespace) -> dict[str, Any]:
    try:
        report_path = output / "00-导出报告.json"
        report = finalize_report({
            "platform": "wechat",
            "sourceUrl": source.canonical_url,
            "contentType": "article",
            "total": 1,
            "exported": exported,
            "skipped": skipped,
            "imageSuccess": image_success,
            "imageFailureCount": len(image_failures),
            "failures": failures,
            "imageFailures": image_failures,
            "elapsedSeconds": round(time.time() - started, 2),
            "checkpoint": checkpoint.stats() if checkpoint else {},
        }, provider=PROVIDER_ID, mode="export", report_file=report_path, output=output)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if checkpoint:
            if failures or image_failures:
                checkpoint.fail_task(f"{len(failures)} 个内容失败，{len(image_failures)} 个图片失败")
            else:
                checkpoint.complete_task(report)
        emit(args, "微信公众号导出完成" if not failures else "微信公众号导出完成，但正文读取失败。", event="task.completed", level="success" if not failures and not image_failures else "warn", reportFile=str(report_path), stats={"exportedDocs": exported, "skippedDocs": skipped, "imageSuccess": image_success, "failureCount": len(failures), "imageFailureCount": len(image_failures)})
        return report
    finally:
        if checkpoint:
            checkpoint.close()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="导出单篇微信公众号文章为 Markdown")
    parser.add_argument("--gui", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--source-url", default="", help="微信公众号单篇文章链接")
    parser.add_argument("--output", default=str(default_data_dir() / "exports" / "wechat"), help="输出目录")
    parser.add_argument("--download-images", dest="download_images", action="store_true", default=True, help="下载正文图片到本地")
    parser.add_argument("--no-download-images", dest="download_images", action="store_false", help="保留正文图片的远程链接")
    parser.add_argument("--incremental", action="store_true", help="目标 Markdown 已存在时跳过")
    add_checkpoint_args(parser)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="微信公众号专用 Chrome 调试端口")
    parser.add_argument("--profile-dir", default=str(default_profile_path()), help="微信公众号专用浏览器配置目录")
    parser.add_argument("--browser-path", default="", help="Chrome/Edge/Chromium 可执行文件路径")
    parser.add_argument("--progress-every", type=int, default=1, help="保留统一任务接口的进度参数")
    parser.add_argument("--request-delay", type=float, default=0.8, help="页面和图片请求延迟秒")
    parser.add_argument("--request-jitter", type=float, default=0.2, help="请求随机浮动秒")
    parser.add_argument("--verification-wait-seconds", type=int, default=DEFAULT_ACCESS_WAIT_SECONDS, help="等待用户完成验证或登录的秒数")
    parser.add_argument("--close-started-chrome", action="store_true", help="任务结束后关闭本插件启动的浏览器")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        if not args.source_url:
            raise ExportError("导出前请先填写微信公众号单篇文章链接。")
        result = export_wechat(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if not result.get("failures") else 1
    except ExportStopped as exc:
        emit(args, f"微信公众号导出已停止：{exc}", event="task.stopped", level="warn")
        print(str(exc), file=sys.stderr, flush=True)
        return 130
    except ExportError as exc:
        emit(args, f"微信公众号导出失败：{exc}", event="task.failed", level="error", error={"type": type(exc).__name__, "message": str(exc)})
        print(str(exc), file=sys.stderr, flush=True)
        return 1
    except Exception as exc:  # noqa: BLE001 - final CLI boundary for the desktop task runner.
        emit(args, f"微信公众号导出失败：{exc}", event="task.failed", level="error", error={"type": type(exc).__name__, "message": str(exc)})
        print(f"微信公众号导出失败：{exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
