#!/usr/bin/env python3
"""Import local Markdown into Google Docs through a managed Pandoc DOCX conversion."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable

from wandao_core.browser import (
    ExportStopped,
    check_stopped,
    default_data_dir,
    stop_requested,
)
from wandao_core.logging import emit_legacy
from wandao_core.report import finalize_report
from wandao_core.source_paths import (
    inspect_local_reference,
    iter_regular_files_under_root,
)

try:
    from .google_oauth import (
        DRIVE_FILES_URL,
        DRIVE_FILE_SCOPE,
        GOOGLE_DOC_MIME,
        CachedAccessToken,
        GoogleDocsError,
        oauth_login,
        refresh_access_token,
    )
except ImportError:  # Executed directly by the Plugin v1 runtime.
    from google_oauth import (  # type: ignore[no-redef]
        DRIVE_FILES_URL,
        DRIVE_FILE_SCOPE,
        GOOGLE_DOC_MIME,
        CachedAccessToken,
        GoogleDocsError,
        oauth_login,
        refresh_access_token,
    )


PROVIDER_ID = "google-docs-import"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PANDOC_VERSION = "3.10.2"
MAX_DOWNLOAD_BYTES = 55 * 1024 * 1024
MAX_PANDOC_BINARY_BYTES = 300 * 1024 * 1024
MAX_MARKDOWN_BYTES = 10 * 1024 * 1024
MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_DOCUMENT_IMAGE_BYTES = 500 * 1024 * 1024
MULTIPART_UPLOAD_LIMIT = 5_000_000
MAX_DOCX_BYTES = 50_000_000
PANDOC_TIMEOUT_SECONDS = 180
DRIVE_RETRY_STATUSES = {429, 500, 502, 503, 504}
DRIVE_RETRY_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}
DRIVE_RETRY_ATTEMPTS = 3
LOCAL_IMAGE_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
}
IGNORED_SOURCE_DIRECTORY_NAMES = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
    "venv",
}
DATA_IMAGE_RE = re.compile(
    r"^data:(image/[a-z0-9.+-]+);base64,([A-Za-z0-9+/]+={0,2})$", re.IGNORECASE
)
INVALID_XML_CHARACTER_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
SINGLE_DOLLAR_SPAN_RE = re.compile(r"(?<!\\)\$(?!\$)([^$\n]+?)\$(?!\$)")
DOCX_CORE_TIMESTAMP_RE = re.compile(
    rb"(<dcterms:(?:created|modified)\b[^>]*>)[^<]*(</dcterms:(?:created|modified)>)"
)

PANDOC_RELEASES: dict[tuple[str, str], dict[str, str]] = {
    ("windows", "x86_64"): {
        "archive": f"pandoc-{PANDOC_VERSION}-windows-x86_64.zip",
        "sha256": "52487faaa63f8cef5363d5a771097da001228d61c6f44f32ed41b27a98c0278c",
        "kind": "zip",
    },
    ("darwin", "x86_64"): {
        "archive": f"pandoc-{PANDOC_VERSION}-x86_64-macOS.zip",
        "sha256": "437d378af72e9648f6fb42c170031218a3c2f31cf5089234cf2d0413f91481d0",
        "kind": "zip",
    },
    ("darwin", "arm64"): {
        "archive": f"pandoc-{PANDOC_VERSION}-arm64-macOS.zip",
        "sha256": "a30bd546062f0b29c25f45a71f951b7a1cf4f998d5b43974ea2c2416133f2e99",
        "kind": "zip",
    },
    ("linux", "x86_64"): {
        "archive": f"pandoc-{PANDOC_VERSION}-linux-amd64.tar.gz",
        "sha256": "c7edd535941c48be6a362081a748272837de81ae11777202d9c341d3d8261c9a",
        "kind": "tar.gz",
    },
    ("linux", "arm64"): {
        "archive": f"pandoc-{PANDOC_VERSION}-linux-arm64.tar.gz",
        "sha256": "1c4d69f2a092bd47cb180e58a4aab7b9637101ced928252458c7d41a7f7fa71d",
        "kind": "tar.gz",
    },
}


class GoogleDocsImportError(GoogleDocsError):
    """User-facing Google Docs import error."""


class GoogleDriveRequestError(GoogleDocsImportError):
    """Google Drive request failure that keeps its HTTP status for recovery."""

    def __init__(self, status: int, message: str, *, reason: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason


def emit_json(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False), flush=True)


def emit(
    message: str, *, event: str = "log.message", level: str = "info", **fields: Any
) -> None:
    emit_legacy(PROVIDER_ID, message, event=event, level=level, **fields)


def _normalized_platform() -> tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    architecture = (
        "arm64"
        if machine in {"arm64", "aarch64"}
        else "x86_64" if machine in {"amd64", "x86_64"} else machine
    )
    return system, architecture


def pandoc_root() -> Path:
    return default_data_dir() / "tools" / "pandoc" / PANDOC_VERSION


def managed_pandoc_path() -> Path:
    return pandoc_root() / ("pandoc.exe" if os.name == "nt" else "pandoc")


def managed_pandoc_digest_path() -> Path:
    return pandoc_root() / "pandoc.sha256"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _managed_pandoc_is_valid(executable: Path) -> bool:
    digest_path = managed_pandoc_digest_path()
    if (
        not executable.is_file()
        or executable.is_symlink()
        or not digest_path.is_file()
        or digest_path.is_symlink()
        or executable.stat().st_size > MAX_PANDOC_BINARY_BYTES
    ):
        return False
    try:
        expected = digest_path.read_text(encoding="ascii").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            return False
        if not secrets.compare_digest(_file_sha256(executable), expected):
            return False
        return pandoc_version(executable) == f"pandoc {PANDOC_VERSION}"
    except (GoogleDocsImportError, OSError, UnicodeError, subprocess.SubprocessError):
        return False


def _download_file(url: str, destination: Path, expected_sha256: str) -> None:
    request = urllib.request.Request(
        url, headers={"User-Agent": "Wandao-Google-Docs-Plugin/0.1"}
    )
    digest = hashlib.sha256()
    total = 0
    try:
        with (
            urllib.request.urlopen(request, timeout=120) as response,
            destination.open("wb") as output,
        ):
            length = response.headers.get("Content-Length")
            if length and int(length) > MAX_DOWNLOAD_BYTES:
                raise GoogleDocsImportError("Pandoc 下载包超过安全大小限制")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise GoogleDocsImportError("Pandoc 下载包超过安全大小限制")
                output.write(chunk)
                digest.update(chunk)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        if isinstance(exc, GoogleDocsImportError):
            raise
        raise GoogleDocsImportError(f"下载 Pandoc 失败：{exc}") from exc
    if not secrets.compare_digest(digest.hexdigest(), expected_sha256):
        raise GoogleDocsImportError("Pandoc 下载包 SHA-256 校验失败，已拒绝运行")


def _copy_archive_binary(archive_path: Path, kind: str, destination: Path) -> None:
    if kind == "zip":
        with zipfile.ZipFile(archive_path) as archive:
            candidates = [
                item
                for item in archive.infolist()
                if not item.is_dir()
                and Path(item.filename).name.lower() == destination.name.lower()
            ]
            if (
                len(candidates) != 1
                or candidates[0].file_size > MAX_PANDOC_BINARY_BYTES
            ):
                raise GoogleDocsImportError("Pandoc ZIP 的文件结构不符合预期")
            with (
                archive.open(candidates[0]) as source,
                destination.open("wb") as target,
            ):
                shutil.copyfileobj(source, target)
        return
    if kind == "tar.gz":
        with tarfile.open(archive_path, "r:gz") as archive:
            candidates = [
                item
                for item in archive.getmembers()
                if item.isfile() and Path(item.name).name == "pandoc"
            ]
            if len(candidates) != 1 or candidates[0].size > MAX_PANDOC_BINARY_BYTES:
                raise GoogleDocsImportError("Pandoc TAR 的文件结构不符合预期")
            source = archive.extractfile(candidates[0])
            if source is None:
                raise GoogleDocsImportError("无法从下载包读取 Pandoc")
            with source, destination.open("wb") as target:
                shutil.copyfileobj(source, target)
        return
    raise GoogleDocsImportError("未知的 Pandoc 下载包格式")


def ensure_pandoc(override: str | Path | None = None) -> Path:
    if override:
        candidate = Path(override).expanduser().resolve()
        if not candidate.is_file() or candidate.is_symlink():
            raise GoogleDocsImportError(f"指定的 Pandoc 不存在或不安全：{candidate}")
        return candidate
    executable = managed_pandoc_path()
    if _managed_pandoc_is_valid(executable):
        return executable
    key = _normalized_platform()
    release = PANDOC_RELEASES.get(key)
    if release is None:
        raise GoogleDocsImportError(f"暂不支持自动准备 Pandoc：{key[0]}/{key[1]}")
    root = pandoc_root()
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="wandao-pandoc-", dir=root) as temporary:
        temporary_root = Path(temporary)
        archive_path = temporary_root / release["archive"]
        binary_path = temporary_root / executable.name
        digest_path = temporary_root / "pandoc.sha256"
        url = f"https://github.com/jgm/pandoc/releases/download/{PANDOC_VERSION}/{release['archive']}"
        print(f"首次使用：正在下载并校验 Pandoc {PANDOC_VERSION}...", flush=True)
        _download_file(url, archive_path, release["sha256"])
        _copy_archive_binary(archive_path, release["kind"], binary_path)
        if binary_path.stat().st_size == 0:
            raise GoogleDocsImportError("下载包中的 Pandoc 为空")
        if os.name != "nt":
            binary_path.chmod(
                binary_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            )
        digest_path.write_text(_file_sha256(binary_path) + "\n", encoding="ascii")
        os.replace(binary_path, executable)
        os.replace(digest_path, managed_pandoc_digest_path())
    if not _managed_pandoc_is_valid(executable):
        raise GoogleDocsImportError("Pandoc 安装后的完整性或版本校验失败，已拒绝运行")
    return executable


def pandoc_version(executable: Path) -> str:
    result = subprocess.run(
        [str(executable), "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
        shell=False,
    )
    if result.returncode != 0:
        raise GoogleDocsImportError(
            f"Pandoc 无法运行：{result.stderr.strip() or result.returncode}"
        )
    first_line = result.stdout.splitlines()[0] if result.stdout.splitlines() else ""
    if not first_line.lower().startswith("pandoc "):
        raise GoogleDocsImportError("Pandoc 版本输出不符合预期")
    return first_line


def markdown_files(source_dir: Path, max_import: int = 0) -> list[Path]:
    unresolved_root = source_dir.expanduser()
    if unresolved_root.is_symlink():
        raise GoogleDocsImportError(f"Markdown 目录不存在或不安全：{unresolved_root}")
    root = unresolved_root.resolve()
    if not root.is_dir():
        raise GoogleDocsImportError(f"Markdown 目录不存在或不安全：{root}")
    files = [
        path
        for path in iter_regular_files_under_root(root, suffixes={".md"})
        if not any(
            part.casefold() in IGNORED_SOURCE_DIRECTORY_NAMES
            for part in path.relative_to(root).parts[:-1]
        )
    ]
    files.sort(key=lambda path: path.relative_to(root).as_posix().casefold())
    return files[:max_import] if max_import > 0 else files


def _read_markdown(path: Path) -> str:
    if path.stat().st_size > MAX_MARKDOWN_BYTES:
        raise GoogleDocsImportError(
            f"Markdown 超过 {MAX_MARKDOWN_BYTES // (1024 * 1024)} MiB 限制：{path.name}"
        )
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise GoogleDocsImportError(f"无法按 UTF-8 读取 Markdown：{path.name}") from exc


def normalize_markdown_math(markdown: str) -> str:
    """Accept common `$ formula $` notes without treating plain prices as math."""

    def normalize(match: re.Match[str]) -> str:
        original = match.group(1)
        stripped = original.strip()
        if original == stripped or not stripped:
            return match.group(0)
        looks_like_math = bool(
            re.search(r"[\\_^{}=<>]", stripped)
            or re.fullmatch(r"[A-Za-z](?:\s*[+*/-]\s*[A-Za-z0-9]+)*", stripped)
        )
        return f"${stripped}$" if looks_like_math else match.group(0)

    def normalize_text(value: str) -> str:
        return SINGLE_DOLLAR_SPAN_RE.sub(normalize, value)

    def backtick_runs(value: str, start: int = 0):
        for match in re.finditer(r"`+", value[start:]):
            absolute_start = start + match.start()
            backslashes = 0
            cursor = absolute_start - 1
            while cursor >= 0 and value[cursor] == "\\":
                backslashes += 1
                cursor -= 1
            if backslashes % 2 == 0:
                yield absolute_start, start + match.end()

    normalized_lines: list[str] = []
    fence_marker = ""
    fence_length = 0
    code_span_ticks = 0

    for line in markdown.splitlines(keepends=True):
        logical_line = line.rstrip("\r\n")
        if fence_marker:
            normalized_lines.append(line)
            if re.fullmatch(
                rf" {{0,3}}{re.escape(fence_marker)}{{{fence_length},}}[ \t]*",
                logical_line,
            ):
                fence_marker = ""
                fence_length = 0
            continue

        if not code_span_ticks:
            opener = re.match(r" {0,3}(`{3,}|~{3,})(.*)$", logical_line)
            if opener and not (
                opener.group(1).startswith("`") and "`" in opener.group(2)
            ):
                fence_marker = opener.group(1)[0]
                fence_length = len(opener.group(1))
                normalized_lines.append(line)
                continue
            if line.startswith(("    ", "\t")):
                normalized_lines.append(line)
                continue

        pieces: list[str] = []
        cursor = 0
        for run_start, run_end in backtick_runs(line):
            run_length = run_end - run_start
            if code_span_ticks:
                pieces.append(line[cursor:run_end])
                cursor = run_end
                if run_length == code_span_ticks:
                    code_span_ticks = 0
            else:
                pieces.append(normalize_text(line[cursor:run_start]))
                pieces.append(line[run_start:run_end])
                cursor = run_end
                code_span_ticks = run_length
        remainder = line[cursor:]
        pieces.append(remainder if code_span_ticks else normalize_text(remainder))
        normalized_lines.append("".join(pieces))

    return "".join(normalized_lines)


def _run_pandoc(command: list[str], *, cwd: Path, input_text: str | None = None) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PANDOC_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GoogleDocsImportError(f"Pandoc 转换失败：{exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip()[-2000:] or f"退出码 {result.returncode}"
        raise GoogleDocsImportError(f"Pandoc 转换失败：{detail}")
    return result.stdout


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _validate_image_bytes(suffix: str, content: bytes) -> None:
    normalized = suffix.lower()
    valid = (
        (normalized == ".png" and content.startswith(b"\x89PNG\r\n\x1a\n"))
        or (normalized in {".jpg", ".jpeg"} and content.startswith(b"\xff\xd8\xff"))
        or (normalized == ".gif" and content.startswith((b"GIF87a", b"GIF89a")))
        or (
            normalized == ".webp"
            and len(content) >= 12
            and content[:4] == b"RIFF"
            and content[8:12] == b"WEBP"
        )
        or (normalized == ".bmp" and content.startswith(b"BM"))
        or (
            normalized in {".tif", ".tiff"}
            and content.startswith((b"II*\x00", b"MM\x00*"))
        )
    )
    if not valid:
        raise GoogleDocsImportError(
            f"图片扩展名与文件内容不匹配：{normalized or '无扩展名'}"
        )


def _copy_image_target(
    target: str, source_file: Path, source_root: Path, stage: Path, index: int
) -> tuple[str, int]:
    data_match = DATA_IMAGE_RE.fullmatch(target)
    if data_match:
        try:
            content = base64.b64decode(data_match.group(2), validate=True)
        except binascii.Error as exc:
            raise GoogleDocsImportError("Markdown 内嵌图片 Base64 无效") from exc
        if len(content) > MAX_IMAGE_BYTES:
            raise GoogleDocsImportError("Markdown 内嵌图片超过单图大小限制")
        suffix = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/gif": ".gif",
            "image/webp": ".webp",
        }.get(data_match.group(1).lower())
        if suffix is None:
            raise GoogleDocsImportError(f"不支持的内嵌图片类型：{data_match.group(1)}")
        _validate_image_bytes(suffix, content)
        destination = (
            stage
            / f"image-{index:04d}-{hashlib.sha256(content).hexdigest()[:12]}{suffix}"
        )
        destination.write_bytes(content)
        return destination.name, len(content)

    source, rejection = inspect_local_reference(source_root, source_file, target)
    if source is None:
        if rejection == "non_local_url":
            raise GoogleDocsImportError(
                "为避免隐式联网，导入仅接受本地图片和 data:image 图片"
            )
        if rejection == "missing_or_not_regular_file":
            raise GoogleDocsImportError(f"找不到本地图片：{target}")
        raise GoogleDocsImportError("图片路径不是所选 Markdown 目录内的安全相对路径")
    if source.suffix.lower() not in LOCAL_IMAGE_SUFFIXES:
        raise GoogleDocsImportError(
            f"不支持的本地图片格式：{source.suffix or '无扩展名'}"
        )
    size = source.stat().st_size
    if size > MAX_IMAGE_BYTES:
        raise GoogleDocsImportError("本地图片超过单图大小限制")
    content = source.read_bytes()
    if len(content) > MAX_IMAGE_BYTES:
        raise GoogleDocsImportError("本地图片超过单图大小限制")
    _validate_image_bytes(source.suffix, content)
    destination = (
        stage
        / f"image-{index:04d}-{hashlib.sha256(content).hexdigest()[:12]}{source.suffix.lower()}"
    )
    if not destination.exists():
        destination.write_bytes(content)
    return destination.name, len(content)


def stage_ast_images(
    ast: dict[str, Any], source_file: Path, source_root: Path, stage: Path
) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    total_bytes = 0
    image_index = 0

    def visit(value: Any) -> None:
        nonlocal image_index, total_bytes
        if isinstance(value, dict):
            if (
                value.get("t") == "Image"
                and isinstance(value.get("c"), list)
                and len(value["c"]) == 3
            ):
                destination = value["c"][2]
                if (
                    isinstance(destination, list)
                    and destination
                    and isinstance(destination[0], str)
                ):
                    image_index += 1
                    original = destination[0]
                    try:
                        local, size = _copy_image_target(
                            original, source_file, source_root, stage, image_index
                        )
                        if total_bytes + size > MAX_DOCUMENT_IMAGE_BYTES:
                            raise GoogleDocsImportError(
                                "单篇文档的图片总量超过安全限制"
                            )
                        total_bytes += size
                        destination[0] = local
                    except (GoogleDocsImportError, OSError) as exc:
                        failures.append({"resource": original[:500], "error": str(exc)})
                        destination[0] = "wandao-missing-image"
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(ast)
    return failures


def markdown_to_docx(
    executable: Path, source_file: Path, source_root: Path, destination: Path
) -> list[dict[str, str]]:
    markdown = normalize_markdown_math(
        INVALID_XML_CHARACTER_RE.sub(" ", _read_markdown(source_file))
    )
    with tempfile.TemporaryDirectory(prefix="wandao-google-docs-") as temporary:
        stage = Path(temporary).resolve()
        try:
            ast_text = _run_pandoc(
                [str(executable), "--from=gfm+tex_math_dollars-raw_html", "--to=json"],
                cwd=stage,
                input_text=markdown,
            )
        except GoogleDocsImportError as exc:
            if "Error parsing YAML metadata" not in str(exc):
                raise
            ast_text = _run_pandoc(
                [
                    str(executable),
                    "--from=gfm+tex_math_dollars-raw_html-yaml_metadata_block",
                    "--to=json",
                ],
                cwd=stage,
                input_text=markdown,
            )
        try:
            ast = json.loads(ast_text)
        except json.JSONDecodeError as exc:
            raise GoogleDocsImportError("Pandoc 返回了无效的中间文档") from exc
        resource_failures = stage_ast_images(ast, source_file, source_root, stage)
        ast_path = stage / "document.json"
        ast_path.write_text(json.dumps(ast, ensure_ascii=False), encoding="utf-8")
        staged_docx = stage / "document.docx"
        _run_pandoc(
            [
                str(executable),
                str(ast_path),
                "--from=json",
                "--to=docx",
                "--syntax-highlighting=default",
                f"--output={staged_docx}",
            ],
            cwd=stage,
        )
        if not staged_docx.is_file() or staged_docx.stat().st_size == 0:
            raise GoogleDocsImportError("Pandoc 没有生成 DOCX")
        if staged_docx.stat().st_size > MAX_DOCX_BYTES:
            raise GoogleDocsImportError("生成的 DOCX 超过上传大小限制")
        shutil.copyfile(staged_docx, destination)
    return resource_failures


def stable_docx_hash(docx_path: Path) -> str:
    """Hash DOCX content while ignoring Pandoc's generation timestamps."""

    digest = hashlib.sha256()
    try:
        with zipfile.ZipFile(docx_path) as archive:
            names = sorted(archive.namelist())
            if not names or len(names) != len(set(names)):
                raise GoogleDocsImportError("生成的 DOCX 文件结构无效")
            for name in names:
                content = archive.read(name)
                if name == "docProps/core.xml":
                    content = DOCX_CORE_TIMESTAMP_RE.sub(rb"\1\2", content)
                encoded_name = name.encode("utf-8")
                digest.update(len(encoded_name).to_bytes(4, "big"))
                digest.update(encoded_name)
                digest.update(len(content).to_bytes(8, "big"))
                digest.update(content)
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise GoogleDocsImportError("无法校验 Pandoc 生成的 DOCX") from exc
    return digest.hexdigest()


def _drive_http_error_details(exc: urllib.error.HTTPError) -> tuple[str, str]:
    reason = ""
    message = exc.reason if isinstance(exc.reason, str) else "未知错误"
    try:
        payload = json.loads(exc.read(64 * 1024).decode("utf-8"))
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            if isinstance(error.get("message"), str):
                message = error["message"]
            items = error.get("errors")
            if isinstance(items, list):
                reason = next(
                    (
                        str(item["reason"])
                        for item in items
                        if isinstance(item, dict)
                        and isinstance(item.get("reason"), str)
                    ),
                    "",
                )
        elif isinstance(error, str):
            message = error
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    return reason, message


def _drive_json_request(
    request: urllib.request.Request, *, timeout: int = 120
) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        reason, message = _drive_http_error_details(exc)
        raise GoogleDriveRequestError(
            exc.code,
            f"Google Drive API 请求失败（HTTP {exc.code}）：{message}",
            reason=reason,
        ) from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GoogleDocsImportError(f"Google Drive API 请求失败：{exc}") from exc


def find_existing_document(
    access_token: str,
    content_hash: str,
    source_path_hash: str,
) -> dict[str, Any] | None:
    query = (
        f"appProperties has {{ key='wandaoContentSha256' and value='{content_hash}' }} "
        f"and appProperties has {{ key='wandaoSourcePathSha256' and value='{source_path_hash}' }} "
        "and trashed=false"
    )
    url = (
        DRIVE_FILES_URL
        + "?"
        + urllib.parse.urlencode(
            {
                "q": query,
                "spaces": "drive",
                "pageSize": "1",
                "fields": "files(id,name,mimeType,webViewLink)",
            }
        )
    )
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
    )
    files = _drive_json_request(request).get("files", [])
    return files[0] if isinstance(files, list) and files else None


def _upload_metadata(title: str, content_hash: str, source_path_hash: str) -> bytes:
    return json.dumps(
        {
            "name": title,
            "mimeType": GOOGLE_DOC_MIME,
            "appProperties": {
                "wandaoContentSha256": content_hash,
                "wandaoSourcePathSha256": source_path_hash,
                "wandaoProvider": PROVIDER_ID,
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _start_resumable_upload(request: urllib.request.Request) -> str:
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            location = str(response.headers.get("Location") or "")
    except urllib.error.HTTPError as exc:
        reason, message = _drive_http_error_details(exc)
        raise GoogleDriveRequestError(
            exc.code,
            f"Google Drive API 请求失败（HTTP {exc.code}）：{message}",
            reason=reason,
        ) from exc
    except OSError as exc:
        raise GoogleDocsImportError(f"Google Drive API 请求失败：{exc}") from exc
    parsed = urllib.parse.urlsplit(location)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.googleapis.com"
        or not parsed.path.startswith("/upload/drive/v3/files")
    ):
        raise GoogleDocsImportError("Google Drive 返回了不安全的续传地址")
    return location


def upload_docx(
    access_token: str,
    docx_path: Path,
    title: str,
    content_hash: str,
    source_path_hash: str,
) -> dict[str, Any]:
    content = docx_path.read_bytes()
    if len(content) > MAX_DOCX_BYTES:
        raise GoogleDocsImportError("生成的 DOCX 超过 Google 文档 50 MB 转换限制")
    metadata = _upload_metadata(title, content_hash, source_path_hash)
    fields = "id,name,mimeType,webViewLink"
    if len(content) <= MULTIPART_UPLOAD_LIMIT:
        boundary = f"wandao_{secrets.token_hex(20)}"
        body = b"".join(
            [
                f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode(
                    "ascii"
                ),
                metadata,
                f"\r\n--{boundary}\r\nContent-Type: {DOCX_MIME}\r\n\r\n".encode(
                    "ascii"
                ),
                content,
                f"\r\n--{boundary}--\r\n".encode("ascii"),
            ]
        )
        url = (
            "https://www.googleapis.com/upload/drive/v3/files?"
            + urllib.parse.urlencode({"uploadType": "multipart", "fields": fields})
        )
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "Content-Type": f"multipart/related; boundary={boundary}",
            },
        )
    else:
        start_url = (
            "https://www.googleapis.com/upload/drive/v3/files?"
            + urllib.parse.urlencode({"uploadType": "resumable", "fields": fields})
        )
        start_request = urllib.request.Request(
            start_url,
            data=metadata,
            method="POST",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": DOCX_MIME,
                "X-Upload-Content-Length": str(len(content)),
            },
        )
        session_url = _start_resumable_upload(start_request)
        request = urllib.request.Request(
            session_url,
            data=content,
            method="PUT",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "Content-Type": DOCX_MIME,
                "Content-Length": str(len(content)),
            },
        )
    created = _drive_json_request(request)
    if created.get("mimeType") != GOOGLE_DOC_MIME or not created.get("id"):
        raise GoogleDocsImportError("Google Drive 没有返回有效的原生 Google 文档")
    return created


def source_title(path: Path) -> str:
    markdown = _read_markdown(path)
    match = re.search(r"(?m)^#\s+(.+?)\s*$", markdown)
    title = match.group(1).strip() if match else path.stem
    title = re.sub(r"[`*_\[\]]", "", title).strip()
    return title[:200] or path.stem[:200] or "Untitled"


def source_identity_hash(path: Path) -> str:
    identity = path.resolve().as_posix()
    if os.name == "nt":
        identity = identity.casefold()
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _access_token(source: str | Callable[[bool], str], *, force: bool = False) -> str:
    return source if isinstance(source, str) else source(force)


def _find_or_upload_document(
    access_token: str | Callable[[bool], str],
    docx: Path,
    title: str,
    content_hash: str,
    source_path_hash: str,
    *,
    args: argparse.Namespace | None = None,
) -> tuple[dict[str, Any], bool]:
    force_refresh = False
    for attempt in range(DRIVE_RETRY_ATTEMPTS):
        check_stopped(args)
        token = _access_token(access_token, force=force_refresh)
        try:
            existing = find_existing_document(token, content_hash, source_path_hash)
            if existing:
                return existing, True
            return (
                upload_docx(token, docx, title, content_hash, source_path_hash),
                False,
            )
        except GoogleDriveRequestError as exc:
            can_refresh = exc.status == 401 and not isinstance(access_token, str)
            quota_limited = exc.status == 403 and exc.reason in DRIVE_RETRY_REASONS
            can_retry = (
                can_refresh or quota_limited or exc.status in DRIVE_RETRY_STATUSES
            )
            if not can_retry or attempt + 1 >= DRIVE_RETRY_ATTEMPTS:
                raise
            force_refresh = can_refresh
            delay = 0 if can_refresh else min(2**attempt, 4)
            emit(
                f"Google Drive 暂时不可用（HTTP {exc.status}），正在重试 {attempt + 2}/{DRIVE_RETRY_ATTEMPTS}。",
                event="task.retry",
                level="warn",
                retry={
                    "attempt": attempt + 2,
                    "maxAttempts": DRIVE_RETRY_ATTEMPTS,
                    "status": exc.status,
                },
            )
            if delay:
                time.sleep(delay)
    raise GoogleDocsImportError("Google Drive 请求重试次数已用尽")


def import_files(
    access_token: str | Callable[[bool], str],
    executable: Path,
    files: list[Path],
    source_root: Path,
    *,
    progress_every: int = 1,
    args: argparse.Namespace | None = None,
) -> dict[str, Any]:
    imported: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    resource_failures: list[dict[str, str]] = []
    skipped = 0
    processed = 0
    stopped = False
    emit(
        f"开始导入 Google Docs：共 {len(files)} 篇。",
        event="task.started",
        totals={"documents": len(files)},
        sourceDir=str(source_root),
    )
    for index, source in enumerate(files, 1):
        if stop_requested(args):
            stopped = True
            emit(
                "收到停止请求，正在结束 Google Docs 导入。",
                event="task.stopping",
                level="warn",
            )
            break
        relative_path = source.relative_to(source_root).as_posix()
        try:
            check_stopped(args)
            emit(
                f"开始导入文档：{relative_path}",
                event="document.import.started",
                doc={"path": relative_path, "index": index},
            )
            with tempfile.TemporaryDirectory(
                prefix="wandao-google-upload-"
            ) as temporary:
                docx = Path(temporary) / "document.docx"
                image_failures = markdown_to_docx(executable, source, source_root, docx)
                resource_failures.extend(
                    {"relativePath": relative_path, **failure}
                    for failure in image_failures
                )
                content_hash = stable_docx_hash(docx)
                created, was_skipped = _find_or_upload_document(
                    access_token,
                    docx,
                    source_title(source),
                    content_hash,
                    source_identity_hash(source),
                    args=args,
                )
                if was_skipped:
                    skipped += 1
                imported.append(
                    {
                        "relativePath": relative_path,
                        "id": str(created.get("id") or ""),
                        "title": str(created.get("name") or source_title(source)),
                        "url": str(
                            created.get("webViewLink")
                            or f"https://docs.google.com/document/d/{created.get('id', '')}/edit"
                        ),
                        "skippedDuplicate": was_skipped,
                    }
                )
                emit(
                    f"Google Docs 文档{'已跳过重复项' if was_skipped else '导入完成'}：{created.get('name') or source.name}",
                    event="document.import.completed",
                    level="warn" if image_failures else "success",
                    doc={
                        "path": relative_path,
                        "id": str(created.get("id") or ""),
                        "title": str(created.get("name") or source.name),
                        "index": index,
                    },
                    stats={
                        "skippedDuplicate": was_skipped,
                        "resourceFailureCount": len(image_failures),
                    },
                )
        except ExportStopped:
            stopped = True
            emit(
                "收到停止请求，当前文档未完成，正在结束。",
                event="task.stopping",
                level="warn",
            )
            break
        except (GoogleDocsImportError, OSError) as exc:
            failures.append({"relativePath": relative_path, "error": str(exc)})
            emit(
                f"Google Docs 文档导入失败：{source.name}：{exc}",
                event="document.import.failed",
                level="error",
                doc={"path": relative_path, "index": index},
                error={"type": type(exc).__name__, "message": str(exc)},
            )
        processed = index
        if progress_every > 0 and (index % progress_every == 0 or index == len(files)):
            emit(
                f"progress {index}/{len(files)} imported={len(imported) - skipped} skipped={skipped} failures={len(failures)}",
                event="task.progress",
                progress={"current": index, "total": len(files)},
                stats={
                    "importedDocs": len(imported) - skipped,
                    "skippedCount": skipped,
                    "failureCount": len(failures),
                },
            )
    report = finalize_report(
        {
            "provider": PROVIDER_ID,
            "mode": "import",
            "totalDocs": len(files),
            "processedCount": processed,
            "importedDocs": len(imported) - skipped,
            "importedCount": len(imported) - skipped,
            "skippedCount": skipped,
            "successCount": len(imported),
            "failureCount": len(failures),
            "failures": failures,
            "resourceFailureCount": len(resource_failures),
            "resourceFailures": resource_failures,
            "imported": imported,
            "pandocVersion": pandoc_version(executable),
            "stopped": stopped,
        },
        provider=PROVIDER_ID,
        mode="import",
        output=source_root,
    )
    if stopped:
        summary = "Google Docs Markdown 导入已停止"
    elif failures or resource_failures:
        summary = f"Google Docs Markdown 导入完成，但有 {len(failures)} 个文档失败、{len(resource_failures)} 个图片失败"
    else:
        summary = "Google Docs Markdown 导入完成"
    emit(
        summary,
        event="task.stopped" if stopped else "task.completed",
        level="warn" if stopped or failures or resource_failures else "success",
        stats={
            "importedDocs": len(imported) - skipped,
            "skippedCount": skipped,
            "failureCount": len(failures),
            "resourceFailureCount": len(resource_failures),
        },
    )
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import Markdown into Google Docs through Pandoc DOCX conversion"
    )
    parser.add_argument(
        "--oauth-client", help="Google desktop OAuth client JSON（仅 --login）"
    )
    parser.add_argument(
        "--login", action="store_true", help="完成导入所需的 OAuth 授权"
    )
    parser.add_argument(
        "--prepare-pandoc", action="store_true", help="下载并校验插件托管的 Pandoc"
    )
    parser.add_argument("--scan-source", action="store_true", help="扫描本地 Markdown")
    parser.add_argument(
        "--import-one", action="store_true", help="导入第一篇或 source-file"
    )
    parser.add_argument("--import-all", action="store_true", help="批量导入 Markdown")
    parser.add_argument("--source-dir", type=Path, help="Markdown 根目录")
    parser.add_argument("--source-file", type=Path, help="单篇 Markdown 文件")
    parser.add_argument("--pandoc", type=Path, help="开发用途：指定 Pandoc 可执行文件")
    parser.add_argument(
        "--max-import", type=int, default=0, help="最多导入篇数，0 为全部"
    )
    parser.add_argument("--progress-every", type=int, default=1, help="每 N 篇输出进度")
    parser.add_argument("--yes", action="store_true", help="确认写入 Google Drive")
    return parser.parse_args(argv)


def _resolve_sources(
    args: argparse.Namespace, *, single: bool
) -> tuple[Path, list[Path]]:
    if args.source_file:
        unresolved_source = args.source_file.expanduser()
        if unresolved_source.is_symlink():
            raise GoogleDocsImportError("单篇导入文件必须是安全的 .md 文件")
        source = unresolved_source.resolve()
        if not source.is_file() or source.suffix.lower() != ".md":
            raise GoogleDocsImportError("单篇导入文件必须是安全的 .md 文件")
        if args.source_dir and args.source_dir.expanduser().is_symlink():
            raise GoogleDocsImportError("Markdown 目录不能是符号链接")
        root = (
            args.source_dir.expanduser().resolve() if args.source_dir else source.parent
        )
        if not _inside(root, source):
            raise GoogleDocsImportError("单篇 Markdown 必须位于所选 Markdown 目录内")
        return root, [source]
    if not args.source_dir:
        raise GoogleDocsImportError("请选择本地 Markdown 目录")
    root = args.source_dir.expanduser().resolve()
    files = markdown_files(root, 1 if single else max(0, args.max_import))
    return root, files


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    resolved_files: list[Path] = []
    try:
        if args.login:
            if not args.oauth_client:
                raise GoogleDocsImportError("登录需要选择 Google OAuth 桌面客户端 JSON")
            result = oauth_login(args.oauth_client)
            result["provider"] = PROVIDER_ID
            emit_json(result)
            return 0
        if args.prepare_pandoc:
            executable = ensure_pandoc(args.pandoc)
            emit_json(
                {
                    "provider": PROVIDER_ID,
                    "ready": True,
                    "path": str(executable),
                    "version": pandoc_version(executable),
                }
            )
            return 0
        if args.scan_source:
            root, files = _resolve_sources(args, single=False)
            emit_json(
                {
                    "provider": PROVIDER_ID,
                    "mode": "plan",
                    "sourceDir": str(root),
                    "totalDocs": len(files),
                    "files": [str(path.relative_to(root)) for path in files],
                }
            )
            return 0
        if not (args.import_one or args.import_all):
            raise GoogleDocsImportError(
                "请指定 --login、--prepare-pandoc、--scan-source、--import-one 或 --import-all"
            )
        if not args.yes:
            raise GoogleDocsImportError(
                "这是 Google Drive 写入操作，请添加 --yes 确认导入"
            )
        root, files = _resolve_sources(args, single=args.import_one)
        resolved_files = files
        if not files:
            raise GoogleDocsImportError("没有找到可导入的 Markdown 文件")
        access_token = CachedAccessToken(
            lambda: refresh_access_token({DRIVE_FILE_SCOPE})
        )
        access_token()  # Fail fast before the first-use Pandoc download.
        executable = ensure_pandoc(args.pandoc)
        result = import_files(
            access_token,
            executable,
            files,
            root,
            progress_every=max(0, args.progress_every),
            args=args,
        )
        emit_json(result)
        if result.get("stopped"):
            return 130
        return 1 if result["failureCount"] else 0
    except Exception as exc:
        message = str(exc)
        if args.import_one or args.import_all:
            failed_source = Path(args.source_file or args.source_dir or "")
            result = finalize_report(
                {
                    "provider": PROVIDER_ID,
                    "mode": "import",
                    "totalDocs": len(resolved_files),
                    "processedCount": 0,
                    "successCount": 0,
                    "failureCount": max(1, len(resolved_files)),
                    "failures": [
                        {"relativePath": failed_source.name, "error": message}
                    ],
                    "resourceFailures": [],
                },
                provider=PROVIDER_ID,
                mode="import",
                output=args.source_dir or args.source_file or "",
            )
            emit_json(result)
        else:
            emit_json({"provider": PROVIDER_ID, "error": message})
        print(message, file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
