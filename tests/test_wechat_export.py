from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wandao_core.browser import ExportError, ExportStopped
from wandao_core.report import finalize_report


MODULE_PATH = Path(__file__).resolve().parents[1] / "plugins" / "wechat" / "backend" / "export_wechat.py"
SPEC = importlib.util.spec_from_file_location("wandao_wechat_export", MODULE_PATH)
assert SPEC and SPEC.loader
wechat = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = wechat
SPEC.loader.exec_module(wechat)


ARTICLE_URL = "https://mp.weixin.qq.com/s?__biz=MzI4NjAxNjY4Nw%3D%3D&mid=2650238548&idx=1&sn=98272891260c9492f0f6dfb9d14bde8a&chksm=f2b6330c"
TRACKED_URL = f"{ARTICLE_URL}&scene=27&from=timeline#wechat_redirect"
IMAGE_URL = "https://mmbiz.qpic.cn/mmbiz_jpg/example/640?wx_fmt=jpeg&from=appmsg"
FALLBACK_IMAGE_URL = "https://mmbiz.qpic.cn/sz_mmbiz_jpg/fallback/640?wx_fmt=other"


def page_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "url": ARTICLE_URL,
        "title": "微信公众号测试文章",
        "author": "测试公众号",
        "publishedAt": "2026-08-03 12:00",
        "copyright": "原创",
        "html": "<p>这是足够长的微信公众号正文，用于验证 Markdown 导出结果。</p>",
        "textLength": 28,
        "clientRequired": False,
        "verificationRequired": False,
        "loginRequired": False,
    }
    payload.update(overrides)
    return payload


class WeChatUrlTests(unittest.TestCase):
    def test_article_url_is_canonicalized_without_tracking_parameters(self) -> None:
        source = wechat.parse_wechat_url(TRACKED_URL)

        self.assertEqual(source.canonical_url, ARTICLE_URL)
        self.assertEqual(len(source.article_key), 20)
        self.assertEqual(source.item_key, f"wechat:article:{source.article_key}")

    def test_short_article_url_is_supported(self) -> None:
        source = wechat.parse_wechat_url("https://mp.weixin.qq.com/s/AbCdEf_123")

        self.assertEqual(source.canonical_url, "https://mp.weixin.qq.com/s/AbCdEf_123")

    def test_non_article_and_unsafe_urls_are_rejected(self) -> None:
        values = [
            "http://mp.weixin.qq.com/s?__biz=a&mid=1",
            "https://mp.weixin.qq.com/profile?__biz=a&mid=1",
            "https://mp.weixin.qq.com/s?__biz=a",
            "https://mp.weixin.qq.com@evil.example/s?__biz=a&mid=1",
            "https://example.com/s?__biz=a&mid=1",
        ]
        for value in values:
            with self.subTest(value=value), self.assertRaises(ExportError):
                wechat.parse_wechat_url(value)


class WeChatMarkdownTests(unittest.TestCase):
    def test_renderer_preserves_structure_and_prefers_wechat_data_source_image(self) -> None:
        renderer = wechat.WeChatMarkdownRenderer(ARTICLE_URL)
        markdown = renderer.render(
            f"""
            <section>
              <h2>二级标题</h2>
              <p>普通 <strong>加粗</strong> 和 <em>强调</em>，还有 <a href="https://example.com/a">链接</a>。</p>
              <blockquote>引用内容<br>第二行</blockquote>
              <ol start="2"><li>第二项</li><li>第三项<ul><li>子项</li></ul></li></ol>
              <pre><code class="language-python">print('hello')</code></pre>
              <table><thead><tr><th>列一</th><th>列二</th></tr></thead><tbody><tr><td>A</td><td>B</td></tr></tbody></table>
              <img alt="示例图片" data-src="{IMAGE_URL}" src="{FALLBACK_IMAGE_URL}">
              <script>不应导出</script>
            </section>
            """
        )

        self.assertIn("## 二级标题", markdown)
        self.assertIn("**加粗**", markdown)
        self.assertIn("*强调*", markdown)
        self.assertIn("[链接](https://example.com/a)", markdown)
        self.assertIn("> 引用内容", markdown)
        self.assertIn("2. 第二项", markdown)
        self.assertIn("  - 子项", markdown)
        self.assertIn("```python\nprint('hello')\n```", markdown)
        self.assertIn("| 列一 | 列二 |", markdown)
        self.assertNotIn("不应导出", markdown)
        self.assertEqual(len(renderer.images), 1)
        self.assertEqual(renderer.images[0].source, IMAGE_URL)
        self.assertEqual(renderer.images[0].fallback_sources, (FALLBACK_IMAGE_URL,))

    def test_only_wechat_body_image_host_is_downloaded(self) -> None:
        renderer = wechat.WeChatMarkdownRenderer(ARTICLE_URL)
        markdown = renderer.render('<p><img alt="远程" src="https://example.com/image.png"></p>')

        self.assertEqual(renderer.images, [])
        self.assertIn("![远程](https://example.com/image.png)", markdown)
        self.assertTrue(wechat.is_wechat_image_url(IMAGE_URL))
        self.assertFalse(wechat.is_wechat_image_url("https://mmbiz.qpic.cn.evil.example/image.png"))

    def test_page_expression_isolates_article_body_and_access_states(self) -> None:
        expression = wechat.page_payload_expression()

        self.assertIn("#js_content", expression)
        self.assertIn("#activity-name", expression)
        self.assertIn("verificationRequired", expression)
        self.assertIn("loginRequired", expression)
        self.assertIn("clientRequired", expression)
        self.assertIn("环境异常", expression)
        self.assertNotIn("comment", expression.lower())

    def test_direct_html_parser_uses_article_dom_contract(self) -> None:
        source = wechat.parse_wechat_url(ARTICLE_URL)
        payload = wechat.extract_payload_from_html(
            """
            <html><head><title>页面标题</title></head><body>
              <h1 id="activity-name">直连文章</h1>
              <span id="js_name">测试账号</span>
              <em id="publish_time">2026-08-03</em>
              <div id="js_content"><p>直连正文 <strong>加粗</strong>。</p><img data-src="https://mmbiz.qpic.cn/mmbiz_jpg/example/640?wx_fmt=jpeg"></div>
            </body></html>
            """,
            source,
            ARTICLE_URL,
        )

        self.assertEqual(payload["title"], "直连文章")
        self.assertEqual(payload["author"], "测试账号")
        self.assertEqual(payload["publishedAt"], "2026-08-03")
        self.assertIn("直连正文", payload["html"])
        self.assertGreater(payload["textLength"], 0)


class WeChatAccessFlowTests(unittest.TestCase):
    def test_browser_selects_the_article_page(self) -> None:
        pages = [{"type": "page", "url": ARTICLE_URL, "webSocketDebuggerUrl": "ws://127.0.0.1/article"}]
        with patch.object(wechat, "http_json", return_value=pages):
            page = wechat.page_for_wechat(9258, ARTICLE_URL)

        self.assertEqual(page, pages[0])

    def test_verification_waits_then_resumes_same_article(self) -> None:
        class FakeCdp:
            def __init__(self) -> None:
                self.payloads = [page_payload(html="", textLength=0, verificationRequired=True), page_payload()]

            def evaluate(self, _expression: str, timeout: int = 0) -> dict[str, object]:
                self.test_case.assertEqual(timeout, 12)
                return self.payloads.pop(0)

        cdp = FakeCdp()
        cdp.test_case = self
        args = wechat.parse_args(["--verification-wait-seconds", "30"])
        with patch.object(wechat.time, "sleep"), patch.object(wechat, "emit") as emit:
            result = wechat.wait_for_page_payload(cdp, wechat.parse_wechat_url(ARTICLE_URL), args)

        self.assertEqual(result["title"], "微信公众号测试文章")
        self.assertEqual(emit.call_args_list[0].kwargs["event"], "auth.verification.required")

    def test_login_waits_then_resumes_same_article(self) -> None:
        class FakeCdp:
            def __init__(self) -> None:
                self.payloads = [page_payload(html="", textLength=0, loginRequired=True), page_payload()]

            def evaluate(self, _expression: str, timeout: int = 0) -> dict[str, object]:
                self.test_case.assertEqual(timeout, 12)
                return self.payloads.pop(0)

        cdp = FakeCdp()
        cdp.test_case = self
        args = wechat.parse_args(["--verification-wait-seconds", "30"])
        with patch.object(wechat.time, "sleep"), patch.object(wechat, "emit") as emit:
            result = wechat.wait_for_page_payload(cdp, wechat.parse_wechat_url(ARTICLE_URL), args)

        self.assertEqual(result["url"], ARTICLE_URL)
        self.assertEqual(emit.call_args_list[0].kwargs["event"], "auth.login.required")

    def test_client_only_article_fails_with_a_specific_diagnostic(self) -> None:
        class FakeCdp:
            @staticmethod
            def evaluate(_expression: str, timeout: int = 0) -> dict[str, object]:
                self.assertEqual(timeout, 12)
                return page_payload(html="", textLength=0, clientRequired=True)

        with self.assertRaisesRegex(ExportError, "微信客户端"):
            wechat.wait_for_page_payload(FakeCdp(), wechat.parse_wechat_url(ARTICLE_URL), wechat.parse_args([]))


class WeChatExportTests(unittest.TestCase):
    def test_images_are_saved_and_partial_failure_keeps_remote_url(self) -> None:
        source = wechat.parse_wechat_url(ARTICLE_URL)
        image = wechat.ImageRef("__IMAGE__", IMAGE_URL, "示例")
        args = wechat.parse_args(["--request-delay", "0", "--request-jitter", "0"])
        with tempfile.TemporaryDirectory() as temporary:
            md_path = Path(temporary) / "文章.md"
            with patch.object(wechat, "download_image", return_value=(b"image", "image/png", IMAGE_URL)), patch.object(wechat, "emit"):
                markdown, failures, saved = wechat.rewrite_images("![示例](__IMAGE__)", [image], md_path, args, source)
            self.assertEqual(markdown, "![示例](文章_assets/image-001.png)")
            self.assertEqual((Path(temporary) / "文章_assets" / "image-001.png").read_bytes(), b"image")
            self.assertEqual((failures, saved), ([], 1))

            with patch.object(wechat, "download_image", side_effect=ExportError("HTTP 403")), patch.object(wechat, "emit"):
                markdown, failures, saved = wechat.rewrite_images("![示例](__IMAGE__)", [image], md_path, args, source)
            self.assertEqual(markdown, f"![示例]({IMAGE_URL})")
            self.assertEqual(saved, 0)
            self.assertEqual(finalize_report({"imageFailures": failures})["outcome"], "partial")

    def test_export_writes_front_matter_and_resources(self) -> None:
        class FakeCdp:
            @staticmethod
            def close() -> None:
                return None

        payload = page_payload(html=f'<p>这是足够长的正文内容，用于验证 Markdown 导出结果。</p><img data-src="{IMAGE_URL}">')
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            args = wechat.parse_args(["--source-url", ARTICLE_URL, "--output", str(output), "--request-delay", "0", "--request-jitter", "0"])
            with patch.object(wechat, "connect_wechat_browser", return_value=(FakeCdp(), None)), \
                    patch.object(wechat, "wait_for_page_payload", return_value=payload), \
                    patch.object(wechat, "download_image", return_value=(b"image", "image/jpeg", IMAGE_URL)), \
                    patch.object(wechat, "emit"):
                report = wechat.export_wechat(args)

            content = (output / "微信公众号测试文章.md").read_text(encoding="utf-8")
            self.assertEqual(report["outcome"], "completed")
            self.assertEqual(report["imageSuccess"], 1)
            self.assertIn(f'source: "{ARTICLE_URL}"', content)
            self.assertIn('author: "测试公众号"', content)
            self.assertIn("微信公众号测试文章_assets/image-001.jpg", content)

    def test_incremental_does_not_skip_a_different_source_with_the_same_title(self) -> None:
        source = wechat.parse_wechat_url(ARTICLE_URL)
        other = wechat.parse_wechat_url("https://mp.weixin.qq.com/s?__biz=Other&mid=2&idx=1&sn=abcdef")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            output.mkdir()
            (output / "同名标题.md").write_text(wechat.build_front_matter({"title": "同名标题"}, other), encoding="utf-8")
            args = wechat.parse_args(["--source-url", ARTICLE_URL, "--output", str(output), "--incremental"])
            with patch.object(wechat, "connect_wechat_browser", return_value=(None, None)), \
                    patch.object(wechat, "wait_for_page_payload", return_value=page_payload(title="同名标题")), \
                    patch.object(wechat, "emit"):
                report = wechat.export_wechat(args)

            self.assertEqual(report["exported"], 1)
            self.assertEqual(len(list(output.glob("同名标题*.md"))), 2)

    def test_stop_is_not_reported_as_success(self) -> None:
        class FakeCdp:
            @staticmethod
            def close() -> None:
                return None

        with tempfile.TemporaryDirectory() as temporary:
            args = wechat.parse_args(["--source-url", ARTICLE_URL, "--output", temporary])
            with patch.object(wechat, "connect_wechat_browser", return_value=(FakeCdp(), None)), \
                    patch.object(wechat, "wait_for_page_payload", side_effect=ExportStopped("用户已停止当前任务")), \
                    patch.object(wechat, "emit"):
                with self.assertRaises(ExportStopped):
                    wechat.export_wechat(args)

    def test_browser_start_failure_uses_public_direct_fallback(self) -> None:
        payload = page_payload(html="<p>直连正文内容足够长，用于验证浏览器失败后的公开页面回退。</p>")
        with tempfile.TemporaryDirectory() as temporary:
            args = wechat.parse_args(["--source-url", ARTICLE_URL, "--output", temporary, "--request-delay", "0", "--request-jitter", "0"])
            with patch.object(wechat, "connect_wechat_browser", side_effect=ExportError("没有可用浏览器")), \
                    patch.object(wechat, "fetch_article_payload_direct", return_value=payload) as fetch_direct, \
                    patch.object(wechat, "emit"):
                report = wechat.export_wechat(args)

            fetch_direct.assert_called_once()
            self.assertEqual(report["outcome"], "completed")
            self.assertTrue((Path(temporary) / "微信公众号测试文章.md").exists())


if __name__ == "__main__":
    unittest.main()
