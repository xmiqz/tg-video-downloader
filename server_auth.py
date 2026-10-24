# -*- coding: utf-8 -*-
"""Single-user authentication for the v2.4 browser service.

This layer protects the web application itself. Telegram's phone/code/2FA
authentication remains in gui.Api and is intentionally a separate concern.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import pathlib
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit


COOKIE_NAME = "tgdl_session"
AUTH_VERSION = 1
PBKDF2_ITERATIONS = 310_000
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 512
MAX_FAILURES = 5
FAILURE_WINDOW_SECS = 600
MAX_BLOCK_SECS = 900


@dataclass(frozen=True)
class LoginResult:
    ok: bool
    error: str = ""
    retry_after: int = 0
    session_token: str = ""
    expires_at: int = 0


class AuthManager:
    def __init__(self, data_dir, *, session_hours=168, cookie_secure=False,
                 bootstrap_token_file=None, iterations=PBKDF2_ITERATIONS,
                 clock=None):
        self.data_dir = pathlib.Path(data_dir).resolve()
        self.auth_path = self.data_dir / "auth.json"
        self.token_file = pathlib.Path(
            bootstrap_token_file or self.data_dir / ".setup-token"
        ).resolve()
        self.session_seconds = int(session_hours) * 3600
        self.cookie_secure = bool(cookie_secure)
        self.iterations = int(iterations)
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self._sessions = {}
        self._failures = {}
        self._record = self._load_record()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._sync_bootstrap_token()

    @property
    def configured(self):
        return self._record is not None

    def _load_record(self):
        if not self.auth_path.exists():
            return None
        try:
            data = json.loads(self.auth_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(
                f"认证文件损坏：{self.auth_path}，请停机后人工恢复或删除"
            ) from exc
        if not isinstance(data, dict):
            raise RuntimeError("认证文件格式错误")
        required = {"version", "salt", "iterations", "digest"}
        if not required.issubset(data):
            raise RuntimeError("认证文件缺少必要字段")
        try:
            bytes.fromhex(str(data["salt"]))
            bytes.fromhex(str(data["digest"]))
            int(data["iterations"])
        except ValueError as exc:
            raise RuntimeError("认证文件字段格式错误") from exc
        return data

    def _sync_bootstrap_token(self):
        with self._lock:
            if self.configured:
                try:
                    self.token_file.unlink()
                except FileNotFoundError:
                    pass
                return
            if self.token_file.exists():
                return
            self.token_file.parent.mkdir(parents=True, exist_ok=True)
            token = secrets.token_urlsafe(32)
            tmp = self.token_file.with_name(self.token_file.name + ".tmp")
            tmp.write_text(token + "\n", encoding="utf-8")
            if os.name != "nt":
                os.chmod(tmp, 0o600)
            os.replace(tmp, self.token_file)
            if os.name != "nt":
                os.chmod(self.token_file, 0o600)

    def bootstrap_token(self):
        with self._lock:
            if self.configured:
                return ""
            try:
                return self.token_file.read_text(encoding="utf-8").strip()
            except OSError:
                return ""

    def _derive(self, password, salt, iterations):
        return hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iterations)
        )

    def _write_record(self, password):
        salt = secrets.token_bytes(16)
        digest = self._derive(password, salt, self.iterations)
        record = {
            "version": AUTH_VERSION,
            "salt": salt.hex(),
            "iterations": self.iterations,
            "digest": digest.hex(),
            "created_at": int(self._clock()),
        }
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.auth_path.with_name(self.auth_path.name + ".tmp")
        tmp.write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, self.auth_path)
        if os.name != "nt":
            os.chmod(self.auth_path, 0o600)
        self._record = record
        try:
            self.token_file.unlink()
        except FileNotFoundError:
            pass

    def setup(self, bootstrap_token, password):
        with self._lock:
            if self.configured:
                return LoginResult(False, "管理员密码已经设置")
            expected = self.bootstrap_token()
            supplied = str(bootstrap_token or "")
            if not expected or not hmac.compare_digest(expected, supplied):
                return LoginResult(False, "初始化令牌无效")
            error = self._password_error(password)
            if error:
                return LoginResult(False, error)
            self._write_record(str(password))
            return self._new_session()

    @staticmethod
    def _password_error(password):
        if not isinstance(password, str):
            return "密码格式错误"
        if len(password) < MIN_PASSWORD_LENGTH:
            return f"密码至少需要 {MIN_PASSWORD_LENGTH} 位"
        if len(password) > MAX_PASSWORD_LENGTH:
            return "密码过长"
        return ""

    def _failure_state(self, client_key):
        now = self._clock()
        state = self._failures.get(client_key)
        if not state:
            return None
        attempts = [t for t in state["attempts"]
                    if now - t <= FAILURE_WINDOW_SECS]
        if not attempts and state["blocked_until"] <= now:
            self._failures.pop(client_key, None)
            return None
        state["attempts"] = attempts
        if state["blocked_until"] <= now:
            state["blocked_until"] = 0
        return state

    def login(self, password, client_key):
        with self._lock:
            state = self._failure_state(client_key)
            now = self._clock()
            if state and state["blocked_until"] > now:
                remain = max(1, int(state["blocked_until"] - now + 0.999))
                return LoginResult(
                    False, f"尝试次数过多，请 {remain} 秒后重试", remain
                )
            if not self.configured:
                return LoginResult(False, "尚未设置管理员密码")
            error = self._password_error(password)
            if error or not self._verify(password):
                self._record_failure(client_key)
                return LoginResult(False, "密码错误")
            self._failures.pop(client_key, None)
            return self._new_session()

    def _verify(self, password):
        record = self._record
        try:
            salt = bytes.fromhex(str(record["salt"]))
            expected = bytes.fromhex(str(record["digest"]))
            actual = self._derive(
                password, salt, int(record["iterations"])
            )
        except Exception:
            return False
        return hmac.compare_digest(actual, expected)

    def _record_failure(self, client_key):
        now = self._clock()
        state = self._failure_state(client_key)
        if state is None:
            state = {"attempts": [], "blocked_until": 0}
            self._failures[client_key] = state
        state["attempts"].append(now)
        count = len(state["attempts"])
        if count >= MAX_FAILURES:
            overflow = count - MAX_FAILURES
            block = min(60 * (2 ** overflow), MAX_BLOCK_SECS)
            state["blocked_until"] = now + block

    def _new_session(self):
        token = secrets.token_urlsafe(32)
        expires_at = int(self._clock() + self.session_seconds)
        self._sessions[token] = expires_at
        self._prune_sessions()
        return LoginResult(True, session_token=token, expires_at=expires_at)

    def _prune_sessions(self):
        now = self._clock()
        for token, expiry in list(self._sessions.items()):
            if expiry <= now:
                self._sessions.pop(token, None)

    def session_valid(self, token):
        if not token:
            return False
        with self._lock:
            expires = self._sessions.get(token, 0)
            if expires <= self._clock():
                self._sessions.pop(token, None)
                return False
            return True

    def logout(self, token):
        with self._lock:
            self._sessions.pop(token or "", None)

    def cookie_kwargs(self):
        return {
            "key": COOKIE_NAME,
            "httponly": True,
            "samesite": "strict",
            "secure": self.cookie_secure,
            "max_age": self.session_seconds,
            "path": "/",
        }


def client_key(request, trust_proxy=False):
    host = request.client.host if request.client else "unknown"
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            host = forwarded.split(",", 1)[0].strip() or host
    return f"{host}|{request.url.path}"


def same_origin(request):
    origin = request.headers.get("origin")
    if not origin:
        return True
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    host = request.headers.get("host", "")
    return bool(parsed.scheme in ("http", "https")
                and parsed.netloc
                and parsed.netloc.lower() == host.lower())


def websocket_same_origin(ws):
    origin = ws.headers.get("origin")
    if not origin:
        return True
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    host = ws.headers.get("host", "")
    return bool(parsed.scheme in ("http", "https")
                and parsed.netloc
                and parsed.netloc.lower() == host.lower())
