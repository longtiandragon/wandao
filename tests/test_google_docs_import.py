from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from plugins.google_docs.backend import google_oauth
from plugins.google_docs.backend import import_google_docs as google_import


PNG = b"\x89PNG\r\n\x1a\nwandao-import-image"
REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = (
    REPO_ROOT
    / "plugins"
    / "google_docs"
    / "providers"
    / "google-docs-import"
    / "provider.json"
)
PLUGIN_MANIFEST = REPO_ROOT / "plugins" / "google_docs" / "plugin.json"


class GoogleDocsImportManifestTests(unittest.TestCase):
    def test_import_provider_is_experimental_and_exposes_safe_workflow(self) -> None:
        plugin_manifest = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        fields = {field["name"]: field for field in manifest["fields"]}
        actions = {action["id"]: action for action in manifest["actions"]}

        self.assertEqual(plugin_manifest["version"], "1.0.1")
        self.assertEqual(plugin_manifest["core"]["minVersion"], "1.3.5")
        self.assertEqual(
            plugin_manifest["entrypoints"]["providers"],
            ["providers/google-docs-import/provider.json"],
        )
        self.assertEqual(manifest["group"], "import")
        self.assertEqual(manifest["status"], "experimental")
        self.assertTrue(manifest["capabilities"]["import"])
        self.assertEqual(manifest["fields"][0]["type"], "notice")
        notice_markdown = manifest["fields"][0]["markdown"]
        self.assertIn("首次授权一次，之后一步导入", notice_markdown)
        self.assertIn("**首次使用：** 选择", notice_markdown)
        self.assertIn("**以后导入：** 只需", notice_markdown)
        self.assertEqual(fields["source_dir"]["type"], "directory")
        self.assertEqual(fields["source_file"]["type"], "file")
        self.assertNotIn("prepare", actions)
        self.assertEqual(actions["importOne"]["args"], ["--import-one", "--yes"])
        self.assertEqual(actions["import"]["args"][:2], ["--import-all", "--yes"])
        self.assertEqual(actions["import"]["label"], "开始导入")


class GoogleDocsImportOAuthTests(unittest.TestCase):
    def test_oauth_prefers_configured_chromium_browser(self) -> None:
        process = mock.Mock()
        process.wait.side_effect = google_oauth.subprocess.TimeoutExpired(
            "chrome", 0.75
        )
        with (
            mock.patch.object(google_oauth, "find_chrome", return_value="chrome"),
            mock.patch.object(
                google_oauth.subprocess, "Popen", return_value=process
            ) as popen,
            mock.patch.object(google_oauth.webbrowser, "open") as system_open,
        ):
            selected = google_oauth.open_authorization_url(
                "https://accounts.google.com/o/oauth2/v2/auth"
            )

        self.assertEqual(selected, "chromium")
        popen.assert_called_once_with(
            ["chrome", "https://accounts.google.com/o/oauth2/v2/auth"],
            stdout=google_oauth.subprocess.DEVNULL,
            stderr=google_oauth.subprocess.DEVNULL,
        )
        system_open.assert_not_called()

    def test_oauth_falls_back_to_system_browser(self) -> None:
        with (
            mock.patch.object(google_oauth, "find_chrome", return_value="stale-chrome"),
            mock.patch.object(
                google_oauth.subprocess, "Popen", side_effect=OSError("gone")
            ),
            mock.patch.object(
                google_oauth.webbrowser, "open", return_value=True
            ) as system_open,
        ):
            selected = google_oauth.open_authorization_url(
                "https://accounts.google.com/o/oauth2/v2/auth"
            )

        self.assertEqual(selected, "system")
        system_open.assert_called_once()

    def test_import_requests_only_drive_file_scope(self) -> None:
        self.assertEqual(google_oauth.DRIVE_SCOPES, google_oauth.DRIVE_FILE_SCOPE)
        self.assertEqual(
            google_oauth.granted_scope_text({}), google_oauth.DRIVE_FILE_SCOPE
        )
        granted = google_oauth.granted_scope_text(
            {
                "scope": f"openid {google_oauth.DRIVE_FILE_SCOPE} https://www.googleapis.com/auth/drive.readonly"
            }
        )
        self.assertIn(google_oauth.DRIVE_FILE_SCOPE, granted.split())
        with self.assertRaisesRegex(google_oauth.GoogleDocsError, "导入所需"):
            google_oauth.granted_scope_text({"scope": "openid profile"})

    def test_load_oauth_client_accepts_only_desktop_application(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "client.json"
            client = {
                "client_id": "client-id",
                "client_secret": "client-secret",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
            source.write_text(json.dumps({"installed": client}), encoding="utf-8")
            self.assertEqual(google_oauth.load_oauth_client(source), client)

            source.write_text(json.dumps({"web": client}), encoding="utf-8")
            with self.assertRaisesRegex(google_oauth.GoogleDocsError, "桌面应用"):
                google_oauth.load_oauth_client(source)

            source.write_text(
                json.dumps(
                    {
                        "installed": {
                            **client,
                            "token_uri": "https://attacker.example/token",
                        }
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                google_oauth.GoogleDocsError, "Google 官方端点"
            ):
                google_oauth.load_oauth_client(source)

    def test_credentials_fallback_is_not_current_working_directory(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"WANDAO_PLUGIN_DATA_DIR": "", "WANDAO_DATA_DIR": ""},
            clear=False,
        ):
            path = google_oauth.credentials_path()
        self.assertNotEqual(path.parent, Path.cwd().resolve())
        self.assertEqual(path.name, google_oauth.TOKEN_FILE)

    def test_saved_credentials_accept_extra_scope_but_require_drive_file(self) -> None:
        payload = {
            "client_id": "client-id",
            "client_secret": "client-secret",
            "token_uri": google_oauth.GOOGLE_TOKEN_URI,
            "refresh_token": "refresh-token",
            "scope": f"{google_oauth.DRIVE_FILE_SCOPE} https://www.googleapis.com/auth/drive.readonly",
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                google_oauth,
                "credentials_path",
                return_value=Path(tmp) / google_oauth.TOKEN_FILE,
            ),
        ):
            google_oauth.credentials_path().write_text(
                json.dumps(payload), encoding="utf-8"
            )
            self.assertEqual(
                google_oauth.load_saved_credentials()["client_id"], "client-id"
            )

            payload["scope"] = "https://www.googleapis.com/auth/drive.readonly"
            google_oauth.credentials_path().write_text(
                json.dumps(payload), encoding="utf-8"
            )
            with self.assertRaisesRegex(google_oauth.GoogleDocsError, "重新授权"):
                google_oauth.load_saved_credentials()

    def test_saved_credentials_reject_tampered_token_endpoint(self) -> None:
        payload = {
            "client_id": "client-id",
            "client_secret": "client-secret",
            "token_uri": "https://attacker.example/token",
            "refresh_token": "refresh-token",
            "scope": google_oauth.DRIVE_FILE_SCOPE,
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                google_oauth,
                "credentials_path",
                return_value=Path(tmp) / google_oauth.TOKEN_FILE,
            ),
        ):
            google_oauth.credentials_path().write_text(
                json.dumps(payload), encoding="utf-8"
            )
            with self.assertRaisesRegex(
                google_oauth.GoogleDocsError, "Google 官方地址"
            ):
                google_oauth.load_saved_credentials()


class PandocRuntimeTests(unittest.TestCase):
    def test_cached_binary_requires_matching_digest_and_exact_version(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                google_import,
                "managed_pandoc_digest_path",
                return_value=Path(tmp) / "pandoc.sha256",
            ),
            mock.patch.object(
                google_import, "pandoc_version", return_value="pandoc 3.10.2"
            ),
        ):
            executable = Path(tmp) / "pandoc.exe"
            executable.write_bytes(b"verified-pandoc")
            google_import.managed_pandoc_digest_path().write_text(
                google_import._file_sha256(executable), encoding="ascii"
            )
            self.assertTrue(google_import._managed_pandoc_is_valid(executable))

            executable.write_bytes(b"tampered-pandoc")
            self.assertFalse(google_import._managed_pandoc_is_valid(executable))

    def test_copy_archive_binary_extracts_only_expected_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive_path = root / "pandoc.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("pandoc-version/pandoc.exe", b"binary")
                archive.writestr("pandoc-version/ignored.txt", b"ignored")
            destination = root / "pandoc.exe"

            google_import._copy_archive_binary(archive_path, "zip", destination)

            self.assertEqual(destination.read_bytes(), b"binary")
            self.assertFalse((root / "ignored.txt").exists())

    def test_download_rejects_digest_mismatch(self) -> None:
        class Response(io.BytesIO):
            headers: dict[str, str] = {}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                google_import.urllib.request,
                "urlopen",
                return_value=Response(b"not-pandoc"),
            ),
        ):
            destination = Path(tmp) / "download"
            with self.assertRaisesRegex(google_import.GoogleDocsImportError, "SHA-256"):
                google_import._download_file(
                    "https://example.invalid/pandoc.zip", destination, "0" * 64
                )


class MarkdownResourceStagingTests(unittest.TestCase):
    @staticmethod
    def ast_for(target: str) -> dict:
        return {
            "pandoc-api-version": [1, 23, 1],
            "meta": {},
            "blocks": [
                {
                    "t": "Para",
                    "c": [
                        {
                            "t": "Image",
                            "c": [
                                ["", [], []],
                                [{"t": "Str", "c": "image"}],
                                [target, ""],
                            ],
                        }
                    ],
                }
            ],
        }

    def test_math_normalization_skips_all_markdown_code_forms(self) -> None:
        markdown = (
            "Outside $ x $ becomes math.\n\n"
            "`inline $ y $` and ``code with ` plus $ z $``.\n\n"
            "```javascript\n"
            "<div> ${j} x ${i} = ${j * i} </div>\n"
            "```\n\n"
            "~~~text\n"
            "$ fenced $\n"
            "~~~\n\n"
            "    indented $ code $\n"
        )

        normalized = google_import.normalize_markdown_math(markdown)

        self.assertIn("Outside $x$ becomes math.", normalized)
        self.assertIn("`inline $ y $`", normalized)
        self.assertIn("``code with ` plus $ z $``", normalized)
        self.assertIn("<div> ${j} x ${i} = ${j * i} </div>", normalized)
        self.assertIn("$ fenced $", normalized)
        self.assertIn("    indented $ code $", normalized)

    def test_local_asset_is_copied_and_ast_target_is_rewritten(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as stage_tmp,
        ):
            root = Path(tmp).resolve()
            assets = root / "note.assets"
            assets.mkdir()
            (assets / "image.png").write_bytes(PNG)
            source = root / "note.md"
            source.write_text("![image](note.assets/image.png)", encoding="utf-8")
            ast = self.ast_for("note.assets/image.png")

            failures = google_import.stage_ast_images(
                ast, source, root, Path(stage_tmp)
            )

            self.assertEqual(failures, [])
            target = ast["blocks"][0]["c"][0]["c"][2][0]
            self.assertRegex(target, r"^image-0001-[0-9a-f]{12}\.png$")
            self.assertEqual((Path(stage_tmp) / target).read_bytes(), PNG)

    def test_failed_resource_references_are_safe_and_still_actionable(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as stage_tmp,
        ):
            root = Path(tmp).resolve()
            source = root / "note.md"
            source.write_text("test", encoding="utf-8")
            targets = {
                (
                    "https://user:password@example.com/private.png"
                    "?X-Amz-Signature=secret#fragment"
                ): "https://example.com/private.png",
                (
                    "//user:password@example.com/protocol-relative.png"
                    "?token=secret#fragment"
                ): "//example.com/protocol-relative.png",
                (
                    "https://user:password@[invalid/private.png"
                    "?token=secret#fragment"
                ): "https://[invalid/private.png",
                "../outside.png": "../outside.png",
                "data:image/png;base64,NOT_A_SECRET": (
                    "data:image/png;base64,[内容已省略]"
                ),
            }
            for target, expected_reference in targets.items():
                with self.subTest(target=target):
                    ast = self.ast_for(target)
                    failures = google_import.stage_ast_images(
                        ast, source, root, Path(stage_tmp)
                    )
                    self.assertEqual(len(failures), 1)
                    self.assertEqual(failures[0]["resource"], expected_reference)
                    self.assertEqual(
                        ast["blocks"][0]["c"][0]["c"][2][0], "wandao-missing-image"
                    )

    def test_ast_image_staging_handles_deep_nesting_without_recursion(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as stage_tmp,
        ):
            root = Path(tmp).resolve()
            source = root / "note.md"
            source.write_text("test", encoding="utf-8")
            image = self.ast_for("missing.png")["blocks"][0]["c"][0]
            nested = image
            for _ in range(2_000):
                nested = [nested]
            ast = {"blocks": nested}

            failures = google_import.stage_ast_images(
                ast, source, root, Path(stage_tmp)
            )

        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["resource"], "missing.png")
        self.assertEqual(image["c"][2][0], "wandao-missing-image")

    def test_forged_image_and_svg_external_reference_are_rejected(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as stage_tmp,
        ):
            root = Path(tmp).resolve()
            source = root / "note.md"
            source.write_text("test", encoding="utf-8")
            (root / "fake.png").write_bytes(b"not an image")
            (root / "remote.svg").write_text(
                '<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.com/x.png"/></svg>',
                encoding="utf-8",
            )
            for target in ("fake.png", "remote.svg"):
                with self.subTest(target=target):
                    ast = self.ast_for(target)
                    failures = google_import.stage_ast_images(
                        ast, source, root, Path(stage_tmp)
                    )
                    self.assertEqual(len(failures), 1)
                    self.assertEqual(
                        ast["blocks"][0]["c"][0]["c"][2][0], "wandao-missing-image"
                    )

    def test_markdown_to_docx_uses_ast_pipeline_and_preserves_resource_failures(
        self,
    ) -> None:
        ast = self.ast_for("missing.png")
        commands: list[list[str]] = []

        def fake_run(command, *, cwd, input_text=None):
            commands.append(command)
            if "--to=json" in command:
                self.assertIn("$x^2$", input_text)
                return json.dumps(ast)
            output_arg = next(arg for arg in command if arg.startswith("--output="))
            Path(output_arg.removeprefix("--output=")).write_bytes(b"docx")
            return ""

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(google_import, "_run_pandoc", side_effect=fake_run),
        ):
            root = Path(tmp).resolve()
            source = root / "note.md"
            source.write_text(
                "# Note\n\n```python\nprint(1)\n```\n\n$x^2$\n\n![x](missing.png)",
                encoding="utf-8",
            )
            destination = root / "result.docx"

            failures = google_import.markdown_to_docx(
                Path("pandoc"), source, root, destination
            )

            self.assertEqual(destination.read_bytes(), b"docx")
            self.assertEqual(len(failures), 1)
            self.assertEqual(len(commands), 2)
            self.assertIn("--from=json", commands[1])
            self.assertIn("--syntax-highlighting=default", commands[1])

    def test_markdown_to_docx_retries_invalid_yaml_as_plain_markdown(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command, *, cwd, input_text=None):
            calls.append(command)
            if len(calls) == 1:
                raise google_import.GoogleDocsImportError(
                    "Pandoc 转换失败：Error parsing YAML metadata: Non-string keys are not supported"
                )
            if "--to=json" in command:
                return json.dumps(
                    {"pandoc-api-version": [1, 23, 1], "meta": {}, "blocks": []}
                )
            output_arg = next(arg for arg in command if arg.startswith("--output="))
            Path(output_arg.removeprefix("--output=")).write_bytes(b"docx")
            return ""

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(google_import, "_run_pandoc", side_effect=fake_run),
        ):
            root = Path(tmp).resolve()
            source = root / "hexo-template.md"
            source.write_text("---\ntitle:\n  default: value\n---\n", encoding="utf-8")
            google_import.markdown_to_docx(
                Path("pandoc"), source, root, root / "result.docx"
            )

        self.assertIn(
            "--from=gfm+tex_math_dollars-raw_html-yaml_metadata_block", calls[1]
        )

    def test_markdown_to_docx_wraps_deep_json_parse_error(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(google_import, "_run_pandoc", return_value="{}"),
            mock.patch.object(
                google_import.json, "loads", side_effect=RecursionError("too deep")
            ),
        ):
            root = Path(tmp).resolve()
            source = root / "deep.md"
            source.write_text("# Deep", encoding="utf-8")

            with self.assertRaisesRegex(
                google_import.GoogleDocsImportError, "中间文档嵌套过深"
            ):
                google_import.markdown_to_docx(
                    Path("pandoc"), source, root, root / "result.docx"
                )

    def test_markdown_to_docx_wraps_deep_json_serialization_error(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                google_import, "_run_pandoc", return_value='{"blocks": []}'
            ),
            mock.patch.object(
                google_import.json, "dumps", side_effect=RecursionError("too deep")
            ),
        ):
            root = Path(tmp).resolve()
            source = root / "deep.md"
            source.write_text("# Deep", encoding="utf-8")

            with self.assertRaisesRegex(
                google_import.GoogleDocsImportError, "中间文档嵌套过深"
            ):
                google_import.markdown_to_docx(
                    Path("pandoc"), source, root, root / "result.docx"
                )

    def test_markdown_to_docx_removes_ooxml_forbidden_control_characters(self) -> None:
        seen_inputs: list[str] = []

        def fake_run(command, *, cwd, input_text=None):
            if "--to=json" in command:
                seen_inputs.append(input_text)
                return json.dumps(
                    {"pandoc-api-version": [1, 23, 1], "meta": {}, "blocks": []}
                )
            output_arg = next(arg for arg in command if arg.startswith("--output="))
            Path(output_arg.removeprefix("--output=")).write_bytes(b"docx")
            return ""

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(google_import, "_run_pandoc", side_effect=fake_run),
        ):
            root = Path(tmp).resolve()
            source = root / "control.md"
            source.write_text("before\x0bafter", encoding="utf-8")
            google_import.markdown_to_docx(
                Path("pandoc"), source, root, root / "result.docx"
            )

        self.assertEqual(seen_inputs, ["before after"])

    def test_spaced_latex_delimiters_are_normalized_but_prices_are_unchanged(
        self,
    ) -> None:
        markdown = (
            "选择 $a_i-xb_i $ 作为权值。\n"
            "$ \\max \\frac{a_i}{b_i} $\n"
            "价格写作 $ 100 $，不应当作公式。"
        )

        normalized = google_import.normalize_markdown_math(markdown)

        self.assertIn("$a_i-xb_i$", normalized)
        self.assertIn("$\\max \\frac{a_i}{b_i}$", normalized)
        self.assertIn("$ 100 $", normalized)


class MarkdownSourceScanTests(unittest.TestCase):
    def test_scan_recurses_but_ignores_dependency_and_cache_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "notes" / "nested").mkdir(parents=True)
            (root / "notes" / "nested" / "keep.md").write_text(
                "# Keep", encoding="utf-8"
            )
            (root / "node_modules" / "pkg").mkdir(parents=True)
            (root / "node_modules" / "pkg" / "README.md").write_text(
                "# Skip", encoding="utf-8"
            )
            (root / ".git").mkdir()
            (root / ".git" / "message.md").write_text("# Skip", encoding="utf-8")

            files = google_import.markdown_files(root)

        self.assertEqual(
            [path.relative_to(root).as_posix() for path in files],
            ["notes/nested/keep.md"],
        )


class GoogleDriveImportTests(unittest.TestCase):
    def test_cached_access_token_refreshes_when_forced(self) -> None:
        issued: list[str] = []

        def refresh() -> str:
            token = f"token-{len(issued) + 1}"
            issued.append(token)
            return token

        cache = google_import.CachedAccessToken(refresh)
        self.assertEqual(cache(), "token-1")
        self.assertEqual(cache(), "token-1")
        self.assertEqual(cache(True), "token-2")
        self.assertEqual(issued, ["token-1", "token-2"])

    def test_unauthorized_drive_request_refreshes_and_rechecks_duplicate(self) -> None:
        calls: list[bool] = []
        existing = {
            "id": "existing",
            "name": "Note",
            "mimeType": google_import.GOOGLE_DOC_MIME,
        }

        def token(force: bool) -> str:
            calls.append(force)
            return "fresh" if force else "expired"

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                google_import,
                "find_existing_document",
                side_effect=[
                    google_import.GoogleDriveRequestError(401, "expired"),
                    existing,
                ],
            ),
            mock.patch.object(google_import, "upload_docx") as upload,
        ):
            created, skipped = google_import._find_or_upload_document(
                token, Path(tmp) / "document.docx", "Note", "a" * 64, "b" * 64
            )

        self.assertEqual(created["id"], "existing")
        self.assertTrue(skipped)
        self.assertEqual(calls, [False, True])
        upload.assert_not_called()

    def test_drive_rate_limit_reason_retries_after_403(self) -> None:
        existing = {
            "id": "existing",
            "name": "Note",
            "mimeType": google_import.GOOGLE_DOC_MIME,
        }
        limited = google_import.GoogleDriveRequestError(
            403, "rate limited", reason="rateLimitExceeded"
        )
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                google_import, "find_existing_document", side_effect=[limited, existing]
            ) as find,
            mock.patch.object(google_import, "upload_docx") as upload,
            mock.patch.object(google_import.time, "sleep") as sleep,
        ):
            created, skipped = google_import._find_or_upload_document(
                "token", Path(tmp) / "document.docx", "Note", "a" * 64, "b" * 64
            )

        self.assertEqual(created["id"], "existing")
        self.assertTrue(skipped)
        self.assertEqual(find.call_count, 2)
        sleep.assert_called_once_with(1)
        upload.assert_not_called()

    def test_stable_docx_hash_ignores_generation_time_but_not_content(self) -> None:
        core_template = (
            b'<cp:coreProperties xmlns:cp="urn:cp" xmlns:dcterms="urn:dcterms">'
            b'<dcterms:created xsi:type="dcterms:W3CDTF">%s</dcterms:created>'
            b'<dcterms:modified xsi:type="dcterms:W3CDTF">%s</dcterms:modified>'
            b"</cp:coreProperties>"
        )

        def write_docx(path: Path, timestamp: bytes, document: bytes) -> None:
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "docProps/core.xml", core_template % (timestamp, timestamp)
                )
                archive.writestr("word/document.xml", document)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, second, changed = (
                root / "first.docx",
                root / "second.docx",
                root / "changed.docx",
            )
            write_docx(first, b"2026-08-31T10:00:00Z", b"same")
            write_docx(second, b"2026-08-31T11:00:00Z", b"same")
            write_docx(changed, b"2026-08-31T11:00:00Z", b"different")

            self.assertEqual(
                google_import.stable_docx_hash(first),
                google_import.stable_docx_hash(second),
            )
            self.assertNotEqual(
                google_import.stable_docx_hash(first),
                google_import.stable_docx_hash(changed),
            )

    def test_upload_requests_native_google_doc_conversion(self) -> None:
        captured: dict[str, object] = {}

        def fake_request(request, *, timeout=120):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["body"] = request.data
            return {
                "id": "google-id",
                "name": "示例",
                "mimeType": google_import.GOOGLE_DOC_MIME,
                "webViewLink": "https://docs.example/id",
            }

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                google_import, "_drive_json_request", side_effect=fake_request
            ),
        ):
            docx = Path(tmp) / "note.docx"
            docx.write_bytes(b"PK-docx")
            result = google_import.upload_docx(
                "token", docx, "示例", "a" * 64, "b" * 64
            )

        self.assertEqual(result["id"], "google-id")
        self.assertIn("uploadType=multipart", str(captured["url"]))
        body = bytes(captured["body"])
        self.assertIn(google_import.GOOGLE_DOC_MIME.encode(), body)
        self.assertIn(google_import.DOCX_MIME.encode(), body)
        self.assertIn(b"wandaoContentSha256", body)
        self.assertIn(b"wandaoSourcePathSha256", body)
        self.assertIn(b"PK-docx", body)

    def test_large_docx_uses_resumable_upload(self) -> None:
        captured: dict[str, object] = {}

        def fake_request(request, *, timeout=120):
            captured["method"] = request.method
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["size"] = len(request.data)
            return {
                "id": "google-id",
                "name": "Large",
                "mimeType": google_import.GOOGLE_DOC_MIME,
            }

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                google_import,
                "_start_resumable_upload",
                return_value="https://www.googleapis.com/upload/drive/v3/files?upload_id=safe",
            ) as start,
            mock.patch.object(
                google_import, "_drive_json_request", side_effect=fake_request
            ),
        ):
            docx = Path(tmp) / "large.docx"
            docx.write_bytes(b"x" * (google_import.MULTIPART_UPLOAD_LIMIT + 1))
            result = google_import.upload_docx(
                "token", docx, "Large", "b" * 64, "c" * 64
            )

        self.assertEqual(result["id"], "google-id")
        self.assertEqual(captured["method"], "PUT")
        self.assertIn("upload_id=safe", str(captured["url"]))
        self.assertEqual(captured["size"], google_import.MULTIPART_UPLOAD_LIMIT + 1)
        start_request = start.call_args.args[0]
        self.assertIn("uploadType=resumable", start_request.full_url)
        self.assertEqual(
            start_request.get_header("X-upload-content-type"), google_import.DOCX_MIME
        )

    def test_docx_over_google_conversion_limit_is_rejected(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(google_import, "MAX_DOCX_BYTES", 4),
        ):
            docx = Path(tmp) / "too-large.docx"
            docx.write_bytes(b"12345")
            with self.assertRaisesRegex(google_import.GoogleDocsImportError, "50 MB"):
                google_import.upload_docx("token", docx, "Large", "b" * 64, "c" * 64)

    def test_batch_honors_stop_marker_before_next_document(self) -> None:
        stop_event = threading.Event()
        stop_event.set()
        args = google_import.argparse.Namespace(stop_event=stop_event)
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                google_import, "pandoc_version", return_value="pandoc 3.10.2"
            ),
        ):
            root = Path(tmp).resolve()
            source = root / "note.md"
            source.write_text("# Note", encoding="utf-8")
            report = google_import.import_files(
                "token", Path("pandoc"), [source], root, progress_every=0, args=args
            )

        self.assertTrue(report["stopped"])
        self.assertEqual(report["outcome"], "stopped")
        self.assertEqual(report["processedCount"], 0)

    def test_repeated_docx_hash_is_skipped_without_upload(self) -> None:
        existing = {
            "id": "existing",
            "name": "Note",
            "mimeType": google_import.GOOGLE_DOC_MIME,
            "webViewLink": "https://docs.example/existing",
        }

        def fake_convert(_executable, _source, _root, destination):
            destination.write_bytes(b"same-docx")
            return [{"resource": "missing.png", "error": "missing"}]

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                google_import, "markdown_to_docx", side_effect=fake_convert
            ),
            mock.patch.object(
                google_import, "stable_docx_hash", return_value="a" * 64
            ),
            mock.patch.object(
                google_import, "find_existing_document", return_value=existing
            ),
            mock.patch.object(google_import, "upload_docx") as upload,
            mock.patch.object(
                google_import, "pandoc_version", return_value="pandoc 3.10.2"
            ),
        ):
            root = Path(tmp).resolve()
            source = root / "note.md"
            source.write_text("# Note", encoding="utf-8")
            result = google_import.import_files(
                "token", Path("pandoc"), [source], root, progress_every=0
            )

        upload.assert_not_called()
        self.assertEqual(result["skippedCount"], 1)
        self.assertEqual(result["successCount"], 1)
        self.assertTrue(result["imported"][0]["skippedDuplicate"])
        self.assertEqual(result["imported"][0]["relativePath"], "note.md")
        self.assertNotIn("path", result["imported"][0])
        self.assertEqual(result["resourceFailureCount"], 1)
        self.assertEqual(result["outcome"], "partial")

    def test_resource_failures_survive_a_later_upload_failure(self) -> None:
        def fake_convert(_executable, _source, _root, destination):
            destination.write_bytes(b"docx")
            return [{"resource": "missing.png", "error": "missing"}]

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                google_import, "markdown_to_docx", side_effect=fake_convert
            ),
            mock.patch.object(google_import, "stable_docx_hash", return_value="a" * 64),
            mock.patch.object(
                google_import,
                "_find_or_upload_document",
                side_effect=google_import.GoogleDocsImportError("upload failed"),
            ),
            mock.patch.object(
                google_import, "pandoc_version", return_value="pandoc 3.10.2"
            ),
            mock.patch.object(google_import, "emit"),
        ):
            root = Path(tmp).resolve()
            source = root / "note.md"
            source.write_text("# Note\n\n![missing](missing.png)", encoding="utf-8")

            result = google_import.import_files(
                "token", Path("pandoc"), [source], root, progress_every=0
            )

        self.assertEqual(result["failureCount"], 1)
        self.assertEqual(result["resourceFailureCount"], 1)
        self.assertEqual(result["resourceFailures"][0]["relativePath"], "note.md")
        self.assertEqual(result["resourceFailures"][0]["resource"], "missing.png")
        self.assertEqual(result["outcome"], "partial")

    def test_deep_document_failure_does_not_stop_later_documents(self) -> None:
        created = {
            "id": "created-document",
            "name": "Created Document",
            "mimeType": google_import.GOOGLE_DOC_MIME,
        }

        def fake_convert(_executable, source, _root, destination):
            if source.name == "deep.md":
                raise google_import.GoogleDocsImportError(
                    "Pandoc 中间文档嵌套过深"
                )
            destination.write_bytes(b"docx")
            return []

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                google_import, "markdown_to_docx", side_effect=fake_convert
            ),
            mock.patch.object(google_import, "stable_docx_hash", return_value="a" * 64),
            mock.patch.object(
                google_import,
                "_find_or_upload_document",
                return_value=(created, False),
            ),
            mock.patch.object(
                google_import, "pandoc_version", return_value="pandoc 3.10.2"
            ),
            mock.patch.object(google_import, "emit"),
        ):
            root = Path(tmp).resolve()
            before = root / "before.md"
            deep = root / "deep.md"
            later = root / "later.md"
            before.write_text("# Before", encoding="utf-8")
            deep.write_text("# Deep", encoding="utf-8")
            later.write_text("# Later", encoding="utf-8")

            result = google_import.import_files(
                "token",
                Path("pandoc"),
                [before, deep, later],
                root,
                progress_every=0,
            )

        self.assertEqual(result["processedCount"], 3)
        self.assertEqual(result["failureCount"], 1)
        self.assertEqual(result["successCount"], 2)
        self.assertEqual(result["failures"][0]["relativePath"], "deep.md")
        self.assertEqual(
            [item["relativePath"] for item in result["imported"]],
            ["before.md", "later.md"],
        )

    def test_identical_content_from_different_source_paths_creates_two_documents(
        self,
    ) -> None:
        created = [
            {"id": "first", "name": "First", "mimeType": google_import.GOOGLE_DOC_MIME},
            {
                "id": "second",
                "name": "Second",
                "mimeType": google_import.GOOGLE_DOC_MIME,
            },
        ]

        def fake_convert(_executable, _source, _root, destination):
            destination.write_bytes(b"same-docx")
            return []

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                google_import, "markdown_to_docx", side_effect=fake_convert
            ),
            mock.patch.object(google_import, "stable_docx_hash", return_value="a" * 64),
            mock.patch.object(
                google_import, "find_existing_document", return_value=None
            ) as find,
            mock.patch.object(
                google_import, "upload_docx", side_effect=created
            ) as upload,
            mock.patch.object(
                google_import, "pandoc_version", return_value="pandoc 3.10.2"
            ),
        ):
            root = Path(tmp).resolve()
            first = root / "first.md"
            second = root / "second.md"
            first.write_text("# Same", encoding="utf-8")
            second.write_text("# Same", encoding="utf-8")
            result = google_import.import_files(
                "token", Path("pandoc"), [first, second], root, progress_every=0
            )

        self.assertEqual(result["importedCount"], 2)
        self.assertEqual(result["skippedCount"], 0)
        self.assertEqual(find.call_count, 2)
        self.assertEqual(upload.call_count, 2)
        source_hashes = [call.args[2] for call in find.call_args_list]
        self.assertNotEqual(source_hashes[0], source_hashes[1])


class GoogleDocsImportCliTests(unittest.TestCase):
    def test_scan_source_returns_only_a_bounded_file_sample(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(google_import, "emit_json") as emit_json,
        ):
            source_dir = Path(tmp)
            for index in range(25):
                (source_dir / f"note-{index:02d}.md").write_text(
                    f"# Note {index}", encoding="utf-8"
                )

            exit_code = google_import.main(
                ["--scan-source", "--source-dir", str(source_dir)]
            )

        plan = emit_json.call_args.args[0]
        self.assertEqual(exit_code, 0)
        self.assertEqual(plan["totalDocs"], 25)
        self.assertEqual(len(plan["sampleFiles"]), google_import.SCAN_SAMPLE_LIMIT)
        self.assertEqual(plan["sampleFiles"][0], "note-00.md")
        self.assertEqual(plan["sampleFiles"][-1], "note-19.md")
        self.assertNotIn("files", plan)

    def test_setup_failure_report_keeps_resolved_document_count(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                google_import, "refresh_access_token", return_value="token"
            ),
            mock.patch.object(google_import, "emit_json") as emit_json,
            mock.patch.object(google_import.sys, "stderr", io.StringIO()),
        ):
            source = Path(tmp) / "note.md"
            source.write_text("# Note", encoding="utf-8")
            missing_pandoc = Path(tmp) / "missing-pandoc"

            exit_code = google_import.main(
                [
                    "--import-one",
                    "--yes",
                    "--source-file",
                    str(source),
                    "--pandoc",
                    str(missing_pandoc),
                ]
            )

        report = emit_json.call_args.args[0]
        self.assertEqual(exit_code, 1)
        self.assertEqual(report["totalDocs"], 1)
        self.assertEqual(report["processedCount"], 0)
        self.assertEqual(report["successCount"], 0)
        self.assertEqual(report["failureCount"], 1)
        self.assertEqual(report["failures"][0]["relativePath"], "note.md")

    def test_batch_setup_failure_marks_every_resolved_document_failed(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                google_import, "refresh_access_token", return_value="token"
            ),
            mock.patch.object(google_import, "emit_json") as emit_json,
            mock.patch.object(google_import.sys, "stderr", io.StringIO()),
        ):
            source_dir = Path(tmp)
            (source_dir / "first.md").write_text("# First", encoding="utf-8")
            (source_dir / "second.md").write_text("# Second", encoding="utf-8")
            missing_pandoc = source_dir / "missing-pandoc"

            exit_code = google_import.main(
                [
                    "--import-all",
                    "--yes",
                    "--source-dir",
                    str(source_dir),
                    "--pandoc",
                    str(missing_pandoc),
                ]
            )

        report = emit_json.call_args.args[0]
        self.assertEqual(exit_code, 1)
        self.assertEqual(report["totalDocs"], 2)
        self.assertEqual(report["processedCount"], 0)
        self.assertEqual(report["successCount"], 0)
        self.assertEqual(report["failureCount"], 2)
        self.assertEqual(len(report["failures"]), 1)

    def test_stopped_batch_returns_cooperative_stop_exit_code(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                google_import, "refresh_access_token", return_value="token"
            ),
            mock.patch.object(
                google_import, "ensure_pandoc", return_value=Path("pandoc")
            ),
            mock.patch.object(
                google_import,
                "import_files",
                return_value={"failureCount": 0, "stopped": True},
            ),
        ):
            source = Path(tmp) / "note.md"
            source.write_text("# Note", encoding="utf-8")
            exit_code = google_import.main(
                ["--import-one", "--yes", "--source-file", str(source)]
            )

        self.assertEqual(exit_code, 130)


if __name__ == "__main__":
    unittest.main()
