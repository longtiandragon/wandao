#!/usr/bin/env python3
"""Google OAuth helpers used by the Google Docs Markdown importer."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable

from wandao_core.browser import default_data_dir, find_chrome
from wandao_core.credentials import write_private_json


PROVIDER_ID = "google-docs-import"
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
DRIVE_SCOPES = DRIVE_FILE_SCOPE
REQUIRED_DRIVE_SCOPES = frozenset({DRIVE_FILE_SCOPE})
DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
ACCESS_TOKEN_REFRESH_SECONDS = 50 * 60
TOKEN_FILE = "google_docs_oauth.json"
GOOGLE_AUTH_URIS = {
    "https://accounts.google.com/o/oauth2/auth",
    "https://accounts.google.com/o/oauth2/v2/auth",
}
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"


class GoogleDocsError(RuntimeError):
    """User-facing Google Docs import error."""


class CachedAccessToken:
    """Refresh an OAuth access token before a long batch can outlive it."""

    def __init__(self, refresh: Callable[[], str]) -> None:
        self._refresh = refresh
        self._token = ""
        self._refreshed_at = 0.0

    def __call__(self, force: bool = False) -> str:
        if (
            force
            or not self._token
            or time.monotonic() - self._refreshed_at >= ACCESS_TOKEN_REFRESH_SECONDS
        ):
            self._token = self._refresh()
            self._refreshed_at = time.monotonic()
        return self._token


class OAuthCallbackServer(HTTPServer):
    timeout = 1


def credentials_path() -> Path:
    if os.environ.get("WANDAO_PLUGIN_DATA_DIR") or os.environ.get("WANDAO_DATA_DIR"):
        return default_data_dir() / TOKEN_FILE
    app_data = os.environ.get("APPDATA")
    if app_data:
        return (
            Path(app_data).expanduser().resolve()
            / "wandao"
            / "plugin-data"
            / "google_docs"
            / TOKEN_FILE
        )
    return (
        Path.home().resolve() / ".wandao" / "plugin-data" / "google_docs" / TOKEN_FILE
    )


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def load_oauth_client(path: str | Path) -> dict[str, str]:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise GoogleDocsError(f"OAuth 客户端 JSON 不存在：{source}")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GoogleDocsError("无法读取 OAuth 客户端 JSON") from exc
    client = payload.get("installed") if isinstance(payload, dict) else None
    if not isinstance(client, dict):
        raise GoogleDocsError("OAuth 客户端必须是 Google Cloud 创建的“桌面应用”类型")
    required = ("client_id", "client_secret", "auth_uri", "token_uri")
    if any(not isinstance(client.get(key), str) or not client[key] for key in required):
        raise GoogleDocsError("OAuth 客户端 JSON 缺少必要字段")
    if (
        client["auth_uri"] not in GOOGLE_AUTH_URIS
        or client["token_uri"] != GOOGLE_TOKEN_URI
    ):
        raise GoogleDocsError("OAuth 客户端 JSON 不是 Google 官方端点")
    return {key: client[key] for key in required}


def _google_error_message(exc: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(exc.read(64 * 1024).decode("utf-8"))
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
        if isinstance(error, str):
            description = (
                payload.get("error_description") if isinstance(payload, dict) else None
            )
            return str(description or error)
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    return str(exc.reason or "未知错误")


def _post_form(url: str, data: dict[str, str], *, timeout: int = 30) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(data).encode("ascii"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise GoogleDocsError(
            f"Google OAuth 请求失败（HTTP {exc.code}）：{_google_error_message(exc)}"
        ) from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GoogleDocsError(f"Google OAuth 请求失败：{exc}") from exc


def granted_scope_text(tokens: dict[str, Any]) -> str:
    """Return the scopes Google actually granted, failing on partial consent."""

    raw_scope = tokens.get("scope")
    if raw_scope is None:
        return DRIVE_SCOPES
    if not isinstance(raw_scope, str):
        raise GoogleDocsError("Google 返回了无效的 OAuth 权限范围，请重新授权")
    granted = set(raw_scope.split())
    if not REQUIRED_DRIVE_SCOPES.issubset(granted):
        raise GoogleDocsError("Google 未授予导入所需的权限，请重新授权并允许全部请求")
    return " ".join(sorted(granted))


def open_authorization_url(auth_url: str) -> str:
    """Open OAuth in Wandao's configured Chromium browser when available."""

    browser_path = find_chrome()
    if browser_path:
        try:
            process = subprocess.Popen(
                [browser_path, auth_url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                return_code = process.wait(timeout=0.75)
            except subprocess.TimeoutExpired:
                return "chromium"
            if return_code == 0:
                return "chromium"
        except OSError:
            pass
    try:
        opened = webbrowser.open(auth_url, new=1, autoraise=True)
    except (OSError, webbrowser.Error):
        opened = False
    if opened:
        return "system"
    raise GoogleDocsError("无法打开 Chrome 或系统浏览器进行 Google 授权")


def oauth_login(client_path: str | Path, *, timeout: int = 300) -> dict[str, Any]:
    client = load_oauth_client(client_path)
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    result: dict[str, str] = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({key: values[0] for key, values in query.items() if values})
            valid = bool(result.get("code")) and secrets.compare_digest(
                result.get("state", ""), state
            )
            text = (
                "Google 授权完成，可以关闭此页面并返回 Wandao。"
                if valid
                else "Google 授权未完成，请返回 Wandao 查看错误。"
            )
            body = f"<meta charset='utf-8'><h2>{text}</h2>".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    try:
        server = OAuthCallbackServer(("127.0.0.1", 0), CallbackHandler)
    except OSError as exc:
        raise GoogleDocsError("无法启动本地 OAuth 回调端口") from exc
    redirect_uri = f"http://127.0.0.1:{server.server_port}/oauth2/callback"
    auth_url = (
        client["auth_uri"]
        + "?"
        + urllib.parse.urlencode(
            {
                "client_id": client["client_id"],
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": DRIVE_SCOPES,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "access_type": "offline",
                "prompt": "consent",
            }
        )
    )
    try:
        open_authorization_url(auth_url)
        deadline = time.monotonic() + max(30, timeout)
        while (
            time.monotonic() < deadline
            and "code" not in result
            and "error" not in result
        ):
            server.handle_request()
    finally:
        server.server_close()
    if "error" in result:
        raise GoogleDocsError(f"Google 拒绝授权：{result['error']}")
    if "code" not in result:
        raise GoogleDocsError("等待 Google 授权超时")
    if not secrets.compare_digest(result.get("state", ""), state):
        raise GoogleDocsError("OAuth state 校验失败，请重新授权")

    tokens = _post_form(
        client["token_uri"],
        {
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "code": result["code"],
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
    )
    refresh_token = str(tokens.get("refresh_token") or "")
    if not refresh_token:
        raise GoogleDocsError("Google 未返回 refresh_token，请撤销旧授权后重新登录")
    granted_scopes = granted_scope_text(tokens)
    write_private_json(
        credentials_path(),
        {
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "token_uri": client["token_uri"],
            "refresh_token": refresh_token,
            "scope": granted_scopes,
        },
    )
    return {"provider": PROVIDER_ID, "loggedIn": True, "scope": granted_scopes}


def load_saved_credentials(required_scopes: set[str] | None = None) -> dict[str, str]:
    path = credentials_path()
    if not path.is_file():
        raise GoogleDocsError("尚未授权，请先点击“授权 Google Docs”")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GoogleDocsError("已保存的 Google 凭证无法读取，请重新授权") from exc
    credential_keys = ("client_id", "client_secret", "token_uri", "refresh_token")
    if not isinstance(payload, dict) or any(
        not isinstance(payload.get(key), str) or not payload[key]
        for key in credential_keys
    ):
        raise GoogleDocsError("已保存的 Google 凭证不完整，请重新授权")
    if payload["token_uri"] != GOOGLE_TOKEN_URI:
        raise GoogleDocsError("已保存凭证的 OAuth 端点不是 Google 官方地址，请重新授权")
    saved_scopes = set(str(payload.get("scope") or "").split())
    expected_scopes = required_scopes or {DRIVE_FILE_SCOPE}
    if not expected_scopes.issubset(saved_scopes):
        raise GoogleDocsError("已保存凭证的权限范围不匹配，请重新授权")
    return {key: payload[key] for key in (*credential_keys, "scope")}


def refresh_access_token(required_scopes: set[str] | None = None) -> str:
    saved = load_saved_credentials(required_scopes)
    tokens = _post_form(
        saved["token_uri"],
        {
            "client_id": saved["client_id"],
            "client_secret": saved["client_secret"],
            "refresh_token": saved["refresh_token"],
            "grant_type": "refresh_token",
        },
    )
    access_token = str(tokens.get("access_token") or "")
    if not access_token:
        raise GoogleDocsError("Google 没有返回 access_token，请重新授权")
    expected_scopes = required_scopes or {DRIVE_FILE_SCOPE}
    returned_scope = tokens.get("scope")
    if returned_scope is not None and (
        not isinstance(returned_scope, str)
        or not expected_scopes.issubset(set(returned_scope.split()))
    ):
        raise GoogleDocsError("Google 刷新后的权限范围不匹配，请重新授权")
    return access_token
