from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wandao_core.browser import ExportError, ExportStopped
from wandao_core.report import finalize_report


MODULE_PATH = Path(__file__).resolve().parents[1] / "plugins" / "csdn" / "backend" / "export_csdn.py"
SPEC = importlib.util.spec_from_file_location("wandao_csdn_export", MODULE_PATH)
assert SPEC and SPEC.loader
csdn = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = csdn
SPEC.loader.exec_module(csdn)


ARTICLE_URL = "https://blog.csdn.net/liqngjun123/article/details/149118632"
SUBDOMAIN_URL = "https://liqngjun123.blog.csdn.net/article/details/149118632"
IMAGE_URL = "https://i-blog.csdnimg.cn/direct/110764ad2d064508aa56ce39a1a67723.png#pic_center"
FALLBACK_IMAGE_URL = "https://img-blog.csdnimg.cn/direct/110764ad2d064508aa56ce39a1a67723.png"


def page_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "url": ARTICLE_URL,
        "title": "真实页面测试标题",
        "author": "测试作者",
        "html": "<p>这是足够长的正文内容，用于验证 CSDN Markdown 导出结果。</p>",
        "textLength": 28,
        "verificationRequired": False,
        "loginRequired": False,
    }
    payload.update(overrides)
    return payload


class CsdnUrlTests(unittest.TestCase):
    def test_standard_article_url_is_normalized_without_tracking(self) -> None:
        source = csdn.parse_csdn_url(f"{ARTICLE_URL}?utm_source=copy#anchor")

        self.assertEqual(source.content_id, "149118632")
        self.assertEqual(source.author_slug, "liqngjun123")
        self.assertEqual(source.canonical_url, ARTICLE_URL)
        self.assertEqual(source.item_key, "csdn:article:149118632")

    def test_author_subdomain_article_url_is_supported(self) -> None:
        source = csdn.parse_csdn_url(SUBDOMAIN_URL)

        self.assertEqual(source.content_id, "149118632")
        self.assertEqual(source.canonical_url, SUBDOMAIN_URL)

    def test_non_article_or_unsafe_urls_are_rejected(self) -> None:
        values = [
            "http://blog.csdn.net/liqngjun123/article/details/149118632",
            "https://blog.csdn.net/liqngjun123",
            "https://blog.csdn.net/liqngjun123/article/details/not-an-id",
            "https://blog.csdn.net@evil.example/liqngjun123/article/details/149118632",
            "https://example.com/liqngjun123/article/details/149118632",
        ]
        for value in values:
            with self.subTest(value=value), self.assertRaises(ExportError):
                csdn.parse_csdn_url(value)


class CsdnMarkdownTests(unittest.TestCase):
    def test_renderer_preserves_content_and_normalizes_csdn_image_fragment(self) -> None:
        renderer = csdn.CsdnMarkdownRenderer(ARTICLE_URL)
        markdown = renderer.render(
            f"""
            <div class="markdown_views">
              <h2>二级标题</h2>
              <p>普通 <strong>加粗</strong> 和 <em>强调</em>，还有 <a href="https://example.com/a">链接</a>。</p>
              <blockquote>引用内容<br>第二行</blockquote>
              <ol start="2"><li>第二项</li><li>第三项<ul><li>子项</li></ul></li></ol>
              <pre><code class="language-python">print('hello')</code></pre>
              <table><thead><tr><th>列一</th><th>列二</th></tr></thead><tbody><tr><td>A</td><td>B</td></tr></tbody></table>
              <img alt="示例图片" data-original="{IMAGE_URL}" src="{FALLBACK_IMAGE_URL}">
              <script>不应导出</script>
            </div>
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
        self.assertEqual(renderer.images[0].source, IMAGE_URL.removesuffix("#pic_center"))
        self.assertEqual(renderer.images[0].fallback_sources, (FALLBACK_IMAGE_URL,))

    def test_only_csdn_image_hosts_are_downloaded(self) -> None:
        renderer = csdn.CsdnMarkdownRenderer(ARTICLE_URL)
        markdown = renderer.render('<p><img alt="远程" src="https://example.com/image.png"></p>')

        self.assertEqual(renderer.images, [])
        self.assertIn("![远程](https://example.com/image.png)", markdown)
        self.assertTrue(csdn.is_csdn_image_url(IMAGE_URL))
        self.assertTrue(csdn.is_csdn_image_url(FALLBACK_IMAGE_URL))
        self.assertFalse(csdn.is_csdn_image_url("https://i-blog.csdnimg.cn.evil.example/image.png"))

    def test_page_expression_isolates_article_content_and_classifies_access_state(self) -> None:
        expression = csdn.page_payload_expression(csdn.parse_csdn_url(ARTICLE_URL))

        self.assertIn("#content_views", expression)
        self.assertIn("#article_content .markdown_views", expression)
        self.assertIn("verificationRequired", expression)
        self.assertIn("loginRequired", expression)
        self.assertIn("安全验证", expression)
        self.assertIn("登录后", expression)
        self.assertNotIn("comments", expression.lower())


class CsdnAccessFlowTests(unittest.TestCase):
    def test_browser_can_attach_to_the_csdn_login_page_after_a_redirect(self) -> None:
        pages = [
            {
                "type": "page",
                "url": "https://passport.csdn.net/login?from=blog",
                "webSocketDebuggerUrl": "ws://127.0.0.1/login",
            }
        ]
        with patch.object(csdn, "http_json", return_value=pages):
            page = csdn.page_for_csdn(9255, ARTICLE_URL)

        self.assertEqual(page["url"], pages[0]["url"])

    def test_target_navigation_allows_a_login_redirect_to_wait_for_user(self) -> None:
        class FakeCdp:
            def __init__(self) -> None:
                self.navigated: list[str] = []

            def navigate(self, url: str) -> None:
                self.navigated.append(url)

            @staticmethod
            def evaluate(_expression: str, timeout: int = 0) -> dict[str, str]:
                self.assertEqual(timeout, 6)
                return {"url": "https://passport.csdn.net/login", "readyState": "complete"}

        cdp = FakeCdp()
        args = csdn.parse_args(["--request-delay", "0", "--request-jitter", "0"])
        with patch.object(csdn, "throttle_request"), patch.object(csdn.time, "sleep"):
            csdn.open_csdn_target(cdp, ARTICLE_URL, args)

        self.assertEqual(cdp.navigated, [ARTICLE_URL])

    def test_security_verification_waits_then_resumes_same_article(self) -> None:
        class FakeCdp:
            def __init__(self) -> None:
                self.payloads = [
                    page_payload(html="", textLength=0, verificationRequired=True),
                    page_payload(),
                ]

            def evaluate(self, _expression: str, timeout: int = 0) -> dict[str, object]:
                self.assertEqual(timeout, 12)
                return self.payloads.pop(0)

            def assertEqual(self, actual: object, expected: object) -> None:
                self.test_case.assertEqual(actual, expected)

        cdp = FakeCdp()
        cdp.test_case = self
        args = csdn.parse_args(["--verification-wait-seconds", "30"])
        with patch.object(csdn.time, "sleep"), patch.object(csdn, "emit") as emit:
            result = csdn.wait_for_page_payload(cdp, csdn.parse_csdn_url(ARTICLE_URL), args)

        self.assertEqual(result["title"], "真实页面测试标题")
        self.assertEqual(emit.call_args_list[0].kwargs["event"], "auth.verification.required")

    def test_login_waits_then_resumes_same_article(self) -> None:
        class FakeCdp:
            def __init__(self) -> None:
                self.payloads = [
                    page_payload(url="https://passport.csdn.net/login", html="", textLength=0, loginRequired=True),
                    page_payload(),
                ]

            def evaluate(self, _expression: str, timeout: int = 0) -> dict[str, object]:
                self.assertEqual(timeout, 12)
                return self.payloads.pop(0)

            def assertEqual(self, actual: object, expected: object) -> None:
                self.test_case.assertEqual(actual, expected)

        cdp = FakeCdp()
        cdp.test_case = self
        args = csdn.parse_args(["--verification-wait-seconds", "30"])
        with patch.object(csdn.time, "sleep"), patch.object(csdn, "emit") as emit:
            result = csdn.wait_for_page_payload(cdp, csdn.parse_csdn_url(ARTICLE_URL), args)

        self.assertEqual(result["url"], ARTICLE_URL)
        self.assertEqual(emit.call_args_list[0].kwargs["event"], "auth.login.required")

    def test_access_wait_timeout_reports_the_correct_required_action(self) -> None:
        class FakeCdp:
            @staticmethod
            def evaluate(_expression: str, timeout: int = 0) -> dict[str, object]:
                self.assertEqual(timeout, 12)
                return page_payload(html="", textLength=0, verificationRequired=True)

        values = iter([0, 0, 31])
        args = csdn.parse_args(["--verification-wait-seconds", "30"])
        with patch.object(csdn.time, "time", side_effect=lambda: next(values)), patch.object(csdn.time, "sleep"), patch.object(csdn, "emit"):
            with self.assertRaisesRegex(ExportError, "安全验证"):
                csdn.wait_for_page_payload(FakeCdp(), csdn.parse_csdn_url(ARTICLE_URL), args)

    def test_login_action_rejects_unverified_session(self) -> None:
        class FakeCdp:
            @staticmethod
            def evaluate(_expression: str, timeout: int = 0) -> dict[str, object]:
                self.assertEqual(timeout, 10)
                return {"csdnHost": True, "loggedIn": False}

        with self.assertRaises(ExportError):
            csdn.verify_login_session(FakeCdp())

    def test_login_action_produces_a_standard_result(self) -> None:
        class FakeCdp:
            @staticmethod
            def navigate(_url: str) -> None:
                return None

            @staticmethod
            def close() -> None:
                return None

        output = io.StringIO()
        with patch.object(csdn, "connect_csdn_browser", return_value=(FakeCdp(), None)), \
                patch.object(csdn, "verify_login_session"), \
                patch.object(csdn, "save_auth_summary", return_value={"authFile": "auth.json", "loggedIn": True}), \
                patch.object(csdn, "emit"), \
                patch("builtins.input", return_value=""), \
                contextlib.redirect_stdout(output):
            self.assertEqual(csdn.main(["--login"]), 0)

        self.assertTrue(json.loads(output.getvalue())["loggedIn"])


class CsdnExportTests(unittest.TestCase):
    def test_images_are_saved_and_partial_failure_keeps_remote_url(self) -> None:
        source = csdn.parse_csdn_url(ARTICLE_URL)
        image = csdn.ImageRef("__IMAGE__", IMAGE_URL.removesuffix("#pic_center"), "示例")
        args = csdn.parse_args(["--request-delay", "0", "--request-jitter", "0"])
        with tempfile.TemporaryDirectory() as temporary:
            md_path = Path(temporary) / "文章.md"
            with patch.object(csdn, "download_image", return_value=(b"image", "image/png", IMAGE_URL)), patch.object(csdn, "emit"):
                markdown, failures, saved = csdn.rewrite_images("![示例](__IMAGE__)", [image], md_path, args, source)
            self.assertEqual(markdown, "![示例](文章_assets/image-001.png)")
            self.assertEqual((Path(temporary) / "文章_assets" / "image-001.png").read_bytes(), b"image")
            self.assertEqual((failures, saved), ([], 1))

            with patch.object(csdn, "download_image", side_effect=ExportError("HTTP 403")), patch.object(csdn, "emit"):
                markdown, failures, saved = csdn.rewrite_images("![示例](__IMAGE__)", [image], md_path, args, source)
            self.assertEqual(markdown, f"![示例]({IMAGE_URL.removesuffix('#pic_center')})")
            self.assertEqual(saved, 0)
            self.assertEqual(finalize_report({"imageFailures": failures})["outcome"], "partial")

    def test_export_writes_source_metadata_copyright_and_resources(self) -> None:
        class FakeCdp:
            @staticmethod
            def close() -> None:
                return None

        payload = page_payload(
            html=f'<p>这是足够长的正文内容，用于验证 Markdown 导出结果。</p><img data-original="{IMAGE_URL}">',
            copyright="本内容遵循CC 4.0 BY-SA版权协议",
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            args = csdn.parse_args(["--source-url", ARTICLE_URL, "--output", str(output), "--request-delay", "0", "--request-jitter", "0"])
            with patch.object(csdn, "connect_csdn_browser", return_value=(FakeCdp(), None)), \
                    patch.object(csdn, "wait_for_page_payload", return_value=payload), \
                    patch.object(csdn, "download_image", return_value=(b"image", "image/jpeg", IMAGE_URL)), \
                    patch.object(csdn, "emit"):
                report = csdn.export_csdn(args)

            content = (output / "真实页面测试标题.md").read_text(encoding="utf-8")
            self.assertEqual(report["outcome"], "completed")
            self.assertEqual(report["imageSuccess"], 1)
            self.assertIn(f'source: "{ARTICLE_URL}"', content)
            self.assertIn('copyright: "本内容遵循CC 4.0 BY-SA版权协议"', content)
            self.assertIn("真实页面测试标题_assets/image-001.jpg", content)

    def test_incremental_does_not_skip_a_different_source_with_the_same_title(self) -> None:
        source = csdn.parse_csdn_url(ARTICLE_URL)
        different_source = csdn.parse_csdn_url("https://blog.csdn.net/other_author/article/details/149118633")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            output.mkdir()
            (output / "同名标题.md").write_text(csdn.build_front_matter({"title": "同名标题"}, different_source), encoding="utf-8")
            args = csdn.parse_args(["--source-url", ARTICLE_URL, "--output", str(output), "--incremental"])
            payload = page_payload(title="同名标题")
            with patch.object(csdn, "connect_csdn_browser", return_value=(None, None)), \
                    patch.object(csdn, "wait_for_page_payload", return_value=payload), \
                    patch.object(csdn, "emit"):
                report = csdn.export_csdn(args)

            self.assertEqual(report["exported"], 1)
            self.assertTrue((output / "同名标题 [article-149118632].md").is_file())
            self.assertEqual(source.item_key, "csdn:article:149118632")

    def test_stop_is_not_reported_as_success(self) -> None:
        class FakeCdp:
            @staticmethod
            def close() -> None:
                return None

        with tempfile.TemporaryDirectory() as temporary:
            args = csdn.parse_args(["--source-url", ARTICLE_URL, "--output", temporary])
            with patch.object(csdn, "connect_csdn_browser", return_value=(FakeCdp(), None)), \
                    patch.object(csdn, "wait_for_page_payload", side_effect=ExportStopped("用户已停止当前任务")), \
                    patch.object(csdn, "emit"):
                with self.assertRaises(ExportStopped):
                    csdn.export_csdn(args)


if __name__ == "__main__":
    unittest.main()
