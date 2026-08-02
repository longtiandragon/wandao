from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from wandao_core.browser import ExportError, ExportStopped
from wandao_core.report import finalize_report


MODULE_PATH = Path(__file__).resolve().parents[1] / "plugins" / "zhihu" / "backend" / "export_zhihu.py"
SPEC = importlib.util.spec_from_file_location("wandao_zhihu_export", MODULE_PATH)
assert SPEC and SPEC.loader
zhihu = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = zhihu
SPEC.loader.exec_module(zhihu)


ARTICLE_URL = "https://zhuanlan.zhihu.com/p/1987571598576874983"
ANSWER_URL = "https://www.zhihu.com/question/123456/answer/789012"
IMAGE_URL = "https://pic3.zhimg.com/v2-example_r.jpg"


class ZhihuUrlTests(unittest.TestCase):
    def test_article_url_is_normalized_and_query_is_not_persisted(self) -> None:
        source = zhihu.parse_zhihu_url(f"{ARTICLE_URL}?utm_source=copy#anchor")
        self.assertEqual(source.kind, "article")
        self.assertEqual(source.content_id, "1987571598576874983")
        self.assertEqual(source.canonical_url, ARTICLE_URL)

    def test_www_article_and_answer_are_supported(self) -> None:
        article = zhihu.parse_zhihu_url("https://www.zhihu.com/p/42")
        answer = zhihu.parse_zhihu_url(ANSWER_URL)
        self.assertEqual(article.canonical_url, "https://zhuanlan.zhihu.com/p/42")
        self.assertEqual(answer.kind, "answer")
        self.assertEqual(answer.question_id, "123456")
        self.assertEqual(answer.item_key, "zhihu:answer:789012:123456")

    def test_invalid_or_non_single_content_urls_are_rejected(self) -> None:
        values = [
            "http://zhuanlan.zhihu.com/p/1",
            "https://example.com/p/1",
            "https://www.zhihu.com/question/123456",
            "https://www.zhihu.com/people/someone",
            "https://www.zhihu.com/zvideo/123456",
            "https://zhuanlan.zhihu.com/p/not-an-id",
            "https://zhihu.com@evil.example/p/1",
        ]
        for value in values:
            with self.subTest(value=value), self.assertRaises(ExportError):
                zhihu.parse_zhihu_url(value)


class ZhihuMarkdownTests(unittest.TestCase):
    def test_renderer_keeps_content_structure_and_isolates_images(self) -> None:
        renderer = zhihu.ZhihuMarkdownRenderer(ARTICLE_URL)
        markdown = renderer.render(
            f"""
            <div class="Post-RichText">
              <h2>二级标题</h2>
              <p>普通 <strong>加粗</strong> 和 <em>强调</em>，还有 <a href="https://example.com/a">链接</a>。</p>
              <blockquote>引用内容<br>第二行</blockquote>
              <ol start="2"><li>第二项</li><li>第三项<ul><li>子项</li></ul></li></ol>
              <pre><code class="language-python">print('hello')</code></pre>
              <table><thead><tr><th>列一</th><th>列二</th></tr></thead><tbody><tr><td>A</td><td>B</td></tr></tbody></table>
              <img alt="示例图片" data-original="{IMAGE_URL}" src="https://pic3.zhimg.com/v2-example_1440w.jpg">
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
        self.assertEqual(renderer.images[0].source, IMAGE_URL)
        self.assertEqual(renderer.images[0].fallback_sources, ("https://pic3.zhimg.com/v2-example_1440w.jpg",))
        self.assertIn(renderer.images[0].token, markdown)

    def test_renderer_preserves_untrusted_remote_image_without_downloading_it(self) -> None:
        renderer = zhihu.ZhihuMarkdownRenderer(ARTICLE_URL)
        markdown = renderer.render('<p><img alt="远程" src="https://example.com/image.png"></p>')
        self.assertEqual(renderer.images, [])
        self.assertIn("![远程](https://example.com/image.png)", markdown)

    def test_page_expression_targets_article_body_not_catalog_or_comments(self) -> None:
        expression = zhihu.page_payload_expression(zhihu.parse_zhihu_url(ARTICLE_URL))
        self.assertIn("article .Post-RichTextContainer .Post-RichText", expression)
        self.assertIn("initial.content", expression)
        self.assertIn("html: initial.content || root?.innerHTML || ''", expression)
        self.assertIn("innerText", expression)
        self.assertNotIn("CommentItem", expression)
        self.assertNotIn("pageHint", expression)
        self.assertNotIn("bodyText.slice", expression)

    def test_page_url_must_match_the_requested_item(self) -> None:
        source = zhihu.parse_zhihu_url(ARTICLE_URL)
        self.assertTrue(zhihu.page_matches_source({"url": ARTICLE_URL}, source))
        self.assertFalse(zhihu.page_matches_source({"url": "https://zhuanlan.zhihu.com/p/1"}, source))
        self.assertFalse(zhihu.page_matches_source({"url": ANSWER_URL}, source))
        self.assertFalse(zhihu.page_matches_source({"url": "https://www.zhihu.com/signin"}, source))

    def test_navigation_waits_until_the_browser_reaches_the_target_item(self) -> None:
        class FakeCdp:
            def __init__(self) -> None:
                self.navigated: list[str] = []
                self.states = [
                    {"url": "https://zhuanlan.zhihu.com/p/1", "readyState": "complete"},
                    {"url": ARTICLE_URL, "readyState": "complete"},
                ]

            def navigate(self, url: str) -> None:
                self.navigated.append(url)

            def evaluate(self, _expression: str, timeout=0):
                self.assertEqual(timeout, 6)
                return self.states.pop(0)

            def assertEqual(self, actual, expected) -> None:
                self.test_case.assertEqual(actual, expected)

        cdp = FakeCdp()
        cdp.test_case = self
        args = zhihu.parse_args(["--request-delay", "0", "--request-jitter", "0"])
        with patch.object(zhihu, "throttle_request"), patch.object(zhihu.time, "sleep"):
            zhihu.open_zhihu_target(cdp, ARTICLE_URL, args)
        self.assertEqual(cdp.navigated, [ARTICLE_URL])
        self.assertEqual(cdp.states, [])

    def test_navigation_allows_the_login_homepage_without_a_content_id(self) -> None:
        class FakeCdp:
            def __init__(self) -> None:
                self.navigated: list[str] = []

            def navigate(self, url: str) -> None:
                self.navigated.append(url)

            def evaluate(self, _expression: str, timeout=0):
                self.test_case.assertEqual(timeout, 6)
                return {"url": zhihu.ENTRY_URL, "readyState": "complete"}

        cdp = FakeCdp()
        cdp.test_case = self
        args = zhihu.parse_args(["--request-delay", "0", "--request-jitter", "0"])
        with patch.object(zhihu, "throttle_request"), patch.object(zhihu.time, "sleep"):
            zhihu.open_zhihu_target(cdp, zhihu.ENTRY_URL, args)
        self.assertEqual(cdp.navigated, [zhihu.ENTRY_URL])


class ZhihuResourceTests(unittest.TestCase):
    def test_image_host_allowlist_and_redaction(self) -> None:
        self.assertTrue(zhihu.is_zhihu_image_url(IMAGE_URL))
        self.assertTrue(zhihu.is_zhihu_image_url("https://pica.zhimg.com/example.webp"))
        self.assertFalse(zhihu.is_zhihu_image_url("http://pic3.zhimg.com/example.jpg"))
        self.assertFalse(zhihu.is_zhihu_image_url("https://pic3.zhimg.com.evil.example/example.jpg"))
        self.assertEqual(
            zhihu.safe_resource_url(f"{IMAGE_URL}?token=secret#fragment"),
            IMAGE_URL,
        )

    def test_rewrite_images_saves_local_files_and_rewrites_markdown(self) -> None:
        source = zhihu.parse_zhihu_url(ARTICLE_URL)
        image = zhihu.ImageRef("__IMAGE__", IMAGE_URL, "示例")
        args = zhihu.parse_args(["--request-delay", "0", "--request-jitter", "0"])
        with tempfile.TemporaryDirectory() as temporary:
            md_path = Path(temporary) / "文章.md"
            with patch.object(zhihu, "download_image", return_value=(b"image", "image/png", IMAGE_URL)), \
                    patch.object(zhihu, "emit"):
                markdown, failures, saved = zhihu.rewrite_images("![示例](__IMAGE__)", [image], md_path, args, source)
            self.assertEqual(markdown, "![示例](文章_assets/image-001.png)")
            self.assertEqual(failures, [])
            self.assertEqual(saved, 1)
            self.assertEqual((Path(temporary) / "文章_assets" / "image-001.png").read_bytes(), b"image")

    def test_rewrite_failure_keeps_remote_image_and_marks_partial_result(self) -> None:
        source = zhihu.parse_zhihu_url(ARTICLE_URL)
        image = zhihu.ImageRef("__IMAGE__", IMAGE_URL, "示例")
        args = zhihu.parse_args(["--request-delay", "0", "--request-jitter", "0"])
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(zhihu, "download_image", side_effect=ExportError("HTTP 403")), \
                    patch.object(zhihu, "emit"):
                markdown, failures, saved = zhihu.rewrite_images(
                    "![示例](__IMAGE__)", [image], Path(temporary) / "文章.md", args, source
                )
        self.assertEqual(markdown, f"![示例]({IMAGE_URL})")
        self.assertEqual(saved, 0)
        self.assertEqual(failures[0]["source"], IMAGE_URL)
        result = finalize_report({"imageFailures": failures, "imageFailureCount": len(failures)})
        self.assertEqual(result["outcome"], "partial")

    def test_rewrite_uses_display_image_when_original_times_out(self) -> None:
        source = zhihu.parse_zhihu_url(ARTICLE_URL)
        fallback = "https://pic3.zhimg.com/v2-example_1440w.jpg"
        image = zhihu.ImageRef("__IMAGE__", IMAGE_URL, "示例", fallback_sources=(fallback,))
        args = zhihu.parse_args(["--request-delay", "0", "--request-jitter", "0"])
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(zhihu, "download_image", side_effect=[TimeoutError("slow"), (b"fallback", "image/jpeg", fallback)]), \
                    patch.object(zhihu, "emit"):
                markdown, failures, saved = zhihu.rewrite_images(
                    "![示例](__IMAGE__)", [image], Path(temporary) / "文章.md", args, source
                )
        self.assertEqual(markdown, "![示例](文章_assets/image-001.jpg)")
        self.assertEqual(failures, [])
        self.assertEqual(saved, 1)

    def test_read_limited_response_rejects_oversized_response(self) -> None:
        class FakeResponse:
            headers = {"Content-Length": "6"}

            @staticmethod
            def read(_size):
                return b"123456"

        with self.assertRaises(ExportError):
            zhihu.read_limited_response(FakeResponse(), max_bytes=5)

    def test_same_title_uses_a_unique_path_unless_the_source_matches(self) -> None:
        first = zhihu.parse_zhihu_url("https://zhuanlan.zhihu.com/p/1")
        second = zhihu.parse_zhihu_url("https://zhuanlan.zhihu.com/p/2")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            original = zhihu.markdown_path(output, "同名标题", first)
            original.write_text(zhihu.build_front_matter({"title": "同名标题"}, first), encoding="utf-8")
            self.assertEqual(zhihu.markdown_path(output, "同名标题", first), original)
            disambiguated = zhihu.markdown_path(output, "同名标题", second)
            self.assertNotEqual(disambiguated, original)
            self.assertEqual(disambiguated.name, "同名标题 [article-2].md")
            disambiguated.write_text(zhihu.build_front_matter({"title": "同名标题"}, second), encoding="utf-8")
            self.assertEqual(zhihu.markdown_path(output, "同名标题", second), disambiguated)


class ZhihuFlowTests(unittest.TestCase):
    def test_wait_for_page_payload_accepts_initial_state_fallback(self) -> None:
        class FakeCdp:
            @staticmethod
            def evaluate(_expression, timeout=0):
                self.assertEqual(timeout, 12)
                return {"url": ARTICLE_URL, "html": "<p>从初始状态读取的正文长度足够</p>", "textLength": 20, "blocked": False}

        args = zhihu.parse_args([])
        payload = zhihu.wait_for_page_payload(FakeCdp(), zhihu.parse_zhihu_url(ARTICLE_URL), args)
        self.assertIn("初始状态", payload["html"])

    def test_login_emits_valid_final_json_without_an_inline_prompt(self) -> None:
        class FakeCdp:
            @staticmethod
            def navigate(_url):
                return None

            @staticmethod
            def close():
                return None

        output = io.StringIO()
        with patch.object(zhihu, "connect_zhihu_browser", return_value=(FakeCdp(), None)), \
                patch.object(zhihu, "verify_login_session"), \
                patch.object(zhihu, "save_auth_summary", return_value={"authFile": "auth.json", "loggedIn": True}), \
                patch.object(zhihu, "emit"), \
                patch("builtins.input", return_value=""), \
                contextlib.redirect_stdout(output):
            self.assertEqual(zhihu.main(["--login"]), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["kind"], "wandao.result")
        self.assertTrue(payload["loggedIn"])

    def test_login_rejects_an_unverified_session(self) -> None:
        class FakeCdp:
            @staticmethod
            def evaluate(_expression, timeout=0):
                self.assertEqual(timeout, 10)
                return {"zhihuHost": True, "loggedIn": False}

        with self.assertRaises(ExportError):
            zhihu.verify_login_session(FakeCdp())

    def test_export_writes_markdown_resources_and_standard_report(self) -> None:
        class FakeCdp:
            @staticmethod
            def close():
                return None

        payload = {
            "title": "真实页面测试标题",
            "author": "测试作者",
            "html": f"<p>这是足够长的正文内容，用于验证 Markdown 导出结果。</p><img data-original=\"{IMAGE_URL}\">",
            "textLength": 28,
        }
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            args = zhihu.parse_args(["--source-url", ARTICLE_URL, "--output", str(output), "--request-delay", "0", "--request-jitter", "0"])
            with patch.object(zhihu, "connect_zhihu_browser", return_value=(FakeCdp(), None)), \
                    patch.object(zhihu, "wait_for_page_payload", return_value=payload), \
                    patch.object(zhihu, "download_image", return_value=(b"image", "image/jpeg", IMAGE_URL)), \
                    patch.object(zhihu, "emit"):
                report = zhihu.export_zhihu(args)
            md_path = output / "真实页面测试标题.md"
            self.assertEqual(report["kind"], "wandao.result")
            self.assertEqual(report["outcome"], "completed")
            self.assertEqual(report["exported"], 1)
            self.assertEqual(report["imageSuccess"], 1)
            self.assertTrue((output / "00-导出报告.json").is_file())
            content = md_path.read_text(encoding="utf-8")
            self.assertIn('source: "https://zhuanlan.zhihu.com/p/1987571598576874983"', content)
            self.assertIn("真实页面测试标题", content)
            self.assertIn("真实页面测试标题_assets/image-001.jpg", content)
            self.assertEqual((output / "真实页面测试标题_assets" / "image-001.jpg").read_bytes(), b"image")

    def test_incremental_does_not_skip_a_different_source_with_the_same_title(self) -> None:
        class FakeCdp:
            @staticmethod
            def close():
                return None

        payload = {
            "title": "同名标题",
            "html": "<p>这是足够长的正文内容，用于验证同名文件不会被错误跳过。</p>",
            "textLength": 30,
        }
        source = zhihu.parse_zhihu_url(ARTICLE_URL)
        different_source = zhihu.parse_zhihu_url("https://zhuanlan.zhihu.com/p/1")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            output.mkdir()
            (output / "同名标题.md").write_text(
                zhihu.build_front_matter({"title": "同名标题"}, different_source), encoding="utf-8"
            )
            args = zhihu.parse_args(["--source-url", ARTICLE_URL, "--output", str(output), "--incremental"])
            with patch.object(zhihu, "connect_zhihu_browser", return_value=(FakeCdp(), None)), \
                    patch.object(zhihu, "wait_for_page_payload", return_value=payload), \
                    patch.object(zhihu, "emit"):
                report = zhihu.export_zhihu(args)
            self.assertEqual(report["exported"], 1)
            self.assertEqual(report["skipped"], 0)
            self.assertTrue((output / "同名标题 [article-1987571598576874983].md").is_file())

    def test_stop_is_not_converted_into_a_success_report(self) -> None:
        class FakeCdp:
            @staticmethod
            def close():
                return None

        with tempfile.TemporaryDirectory() as temporary:
            args = zhihu.parse_args(["--source-url", ARTICLE_URL, "--output", temporary])
            with patch.object(zhihu, "connect_zhihu_browser", return_value=(FakeCdp(), None)), \
                    patch.object(zhihu, "wait_for_page_payload", side_effect=ExportStopped("用户已停止当前任务")), \
                    patch.object(zhihu, "emit"):
                with self.assertRaises(ExportStopped):
                    zhihu.export_zhihu(args)

    def test_stop_marks_checkpoint_stopped_and_releases_lease(self) -> None:
        class FakeCdp:
            @staticmethod
            def close():
                return None

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkpoint_file = root / "checkpoint.sqlite"
            args = zhihu.parse_args(
                [
                    "--source-url", ARTICLE_URL,
                    "--output", str(root / "output"),
                    "--checkpoint-file", str(checkpoint_file),
                    "--checkpoint-task-id", "zhihu-stop-contract",
                ]
            )
            with patch.object(zhihu, "connect_zhihu_browser", return_value=(FakeCdp(), None)), \
                    patch.object(zhihu, "wait_for_page_payload", side_effect=ExportStopped("用户已停止当前任务")), \
                    patch.object(zhihu, "emit"):
                with self.assertRaises(ExportStopped):
                    zhihu.export_zhihu(args)

            connection = sqlite3.connect(checkpoint_file)
            try:
                task = connection.execute(
                    "SELECT status, lease_id FROM tasks WHERE task_id = ?", ("zhihu-stop-contract",)
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(task, ("stopped", ""))


if __name__ == "__main__":
    unittest.main()
