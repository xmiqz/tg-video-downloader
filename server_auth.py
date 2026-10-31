# -*- coding: utf-8 -*-
"""Single-user authentication for the v2.4 browser service.

This layer protects the web application itself. Telegram's phone/code/2FA
authentication remains in gui.Api and is intentionally a separate concern.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
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

# T60: persistent "remember this device" sessions. T60-followup binds
# the token to the issuing browser (User-Agent fingerprint) and shortens
# the TTL from 30 days to 7 days; the prefix bump to "r2." forces every
# pre-release "r1.*" token to re-login.
REMEMBER_DAYS = 7
REMEMBER_PREFIX = "r2."
REMEMBER_KEY_BYTES = 32


def _ua_fingerprint(user_agent):
    """First 8 hex chars of sha256(user agent); an empty UA hashes '-'.

    Every r2 remember token embeds this fingerprint so a leaked cookie
    cannot be replayed from a different browser. Rules:

    * ``""`` / ``None`` (a missing header) hash the literal "-" for both
      minting and validation - they stay symmetric.
    * a non-empty ``str`` hashes its raw utf-8 bytes.
    * anything else (numbers, containers, ...) is malformed input and
      returns ``None``, which matches NOTHING. Callers treat ``None`` as
      "cannot validate", keeping every entry point exception-free.

    Deterministic caveat: two logins from the same UA in the same second
    mint the identical token - harmless, since they are indistinguishable
    sessions anyway (the revocation list keys on that same value).
    """
    if user_agent is None:
        raw = "-"
    elif isinstance(user_agent, str):
        raw = user_agent or "-"
    else:
        return None
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]


def _valid_session_key(value):
    """A remember-signing key must be a 64-char hex string of 32 bytes."""
    if not isinstance(value, str) or len(value) != REMEMBER_KEY_BYTES * 2:
        return False
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return True


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
        self.remember_seconds = REMEMBER_DAYS * 86400
        self.cookie_secure = bool(cookie_secure)
        self.iterations = int(iterations)
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self._sessions = {}
        self._failures = {}
        self._record = self._load_record()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self._record is not None:
            self._ensure_remember_fields()
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

    def _persist_record(self, record):
        """Atomically persist the full auth record, preserving every key."""
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

    def _ensure_remember_fields(self):
        """Backfill remember-me keys into an existing auth.json.

        Only runs when auth.json already exists. Missing or malformed
        session_key/revoked_sessions values are (re)created in place with a
        read-modify-write that keeps every pre-existing field untouched.
        """
        record = self._record
        if record is None:
            return
        updated = None
        if not _valid_session_key(record.get("session_key")):
            updated = dict(record)
            updated["session_key"] = secrets.token_bytes(
                REMEMBER_KEY_BYTES).hex()
        if not isinstance(record.get("revoked_sessions"), list):
            updated = updated or dict(record)
            updated["revoked_sessions"] = []
        if updated is not None:
            self._persist_record(updated)

    def _write_record(self, password):
        salt = secrets.token_bytes(16)
        digest = self._derive(password, salt, self.iterations)
        self._persist_record({
            "version": AUTH_VERSION,
            "salt": salt.hex(),
            "iterations": self.iterations,
            "digest": digest.hex(),
            "created_at": int(self._clock()),
            "session_key": secrets.token_bytes(REMEMBER_KEY_BYTES).hex(),
            "revoked_sessions": [],
        })
        try:
            self.token_file.unlink()
        except FileNotFoundError:
            pass

    def setup(self, bootstrap_token, password, user_agent=""):
        with self._lock:
            if self.configured:
                return LoginResult(False, "管理员密码已经设置")
            expected = self.bootstrap_token()
            supplied = str(bootstrap_token or "")
            if not tokens_equal(supplied, expected):
                return LoginResult(False, "初始化令牌无效")
            error = self._password_error(password)
            if error:
                return LoginResult(False, error)
            self._write_record(str(password))
            # The setup session stays an ordinary in-memory session and is
            # NOT UA-bound; user_agent is accepted only for call-site
            # symmetry with login.
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

    def login(self, password, client_key, remember=False, user_agent=""):
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
            # A remembered login needs a persisted signing key; without one
            # (never expected after backfill) fall back to a plain session
            # instead of minting tokens signed with an empty key.
            if remember and self._session_key_bytes():
                return self._new_remember_session(user_agent)
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
        return tokens_equal(actual, expected)

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

    def _session_key_bytes(self):
        """The persisted remember-signing key, or b"" when unavailable."""
        record = self._record
        if not isinstance(record, dict):
            return b""
        value = record.get("session_key")
        if not _valid_session_key(value):
            return b""
        return bytes.fromhex(value)

    def _new_remember_session(self, user_agent=""):
        """Mint a stateless ``r2.<expires_at>.<ua8>.<hmac>`` long-lived token.

        ``ua8`` is the sha256 fingerprint of the issuing browser's
        User-Agent, embedded in both the token payload and the HMAC
        (signed over ``r2.<expires_at>.<ua8>``), so the cookie only
        validates from the browser it was minted for. The token is never
        stored server-side; validity is recomputed from the persisted
        ``session_key`` plus the revocation list, so it survives service
        restarts.
        """
        expires_at = int(self._clock() + self.remember_seconds)
        # A malformed UA at mint time falls back to the empty-UA "-"
        # fingerprint, so minting itself can never fail.
        ua8 = _ua_fingerprint(user_agent) or "-"
        signature = hmac.new(
            self._session_key_bytes(),
            f"{REMEMBER_PREFIX}{expires_at}.{ua8}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        token = f"{REMEMBER_PREFIX}{expires_at}.{ua8}.{signature}"
        return LoginResult(True, session_token=token, expires_at=expires_at)

    def _remember_parts(self, token, user_agent=""):
        """Return (expires_at, ua8, signature) for a valid r2 token, else None.

        The embedded ``ua8`` must match the fingerprint recomputed from the
        CURRENT request UA, and the HMAC-SHA256 signature over
        ``r2.<expires_at>.<ua8>`` must verify. Legacy ``r1.*`` tokens,
        malformed / overlong / non-ASCII input, tampered values and UA
        mismatches all resolve to None; this helper never raises.
        """
        if not isinstance(token, str) or not token.startswith(REMEMBER_PREFIX):
            return None
        parts = token.split(".")
        if len(parts) != 4 or parts[0] != "r2":
            return None
        _, expires_raw, ua8, signature = parts
        try:
            expires_at = int(expires_raw)
        except ValueError:
            return None
        key = self._session_key_bytes()
        if not key:
            return None
        # Malformed UA values (non-str) can never match a token.
        expected_ua8 = _ua_fingerprint(user_agent)
        if expected_ua8 is None:
            return None
        if not tokens_equal(ua8, expected_ua8):
            return None
        expected = hmac.new(
            key, f"{REMEMBER_PREFIX}{expires_at}.{ua8}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not tokens_equal(signature, expected):
            return None
        return expires_at, ua8, signature

    def _revocation_active(self, token_id, now):
        """True when token_id is revoked; prune stale entries in passing."""
        record = self._record
        entries = record.get("revoked_sessions")
        if not isinstance(entries, list) or not entries:
            return False
        keep = []
        revoked = False
        changed = False
        for entry in entries:
            if (isinstance(entry, dict)
                    and isinstance(entry.get("id"), str)
                    and isinstance(entry.get("expires_at"), int)):
                if entry["expires_at"] <= now:
                    changed = True
                    continue
                keep.append({"id": entry["id"],
                             "expires_at": entry["expires_at"]})
                if entry["id"] == token_id:
                    revoked = True
            else:
                changed = True
        if changed:
            updated = dict(record)
            updated["revoked_sessions"] = keep
            self._persist_record(updated)
        return revoked

    def _revoke_remember(self, token, user_agent=""):
        """Persist the revocation of a well-formed r2 token (id only).

        Only tokens whose signature AND UA fingerprint verify against the
        current request get an entry, so a hijacker replaying a stolen
        cookie from a different browser cannot scrub the victim's logout.
        """
        parsed = self._remember_parts(token, user_agent)
        if parsed is None:
            return
        expires_at = parsed[0]
        now = int(self._clock())
        token_id = hashlib.sha256(token.encode("utf-8")).hexdigest()
        record = self._record
        entries = []
        raw = record.get("revoked_sessions")
        for entry in raw if isinstance(raw, list) else []:
            if not (isinstance(entry, dict)
                    and isinstance(entry.get("id"), str)
                    and isinstance(entry.get("expires_at"), int)):
                continue
            if entry["expires_at"] <= now:
                continue
            if entry["id"] == token_id:
                return  # already revoked; do not rewrite the file
            entries.append({"id": entry["id"],
                             "expires_at": entry["expires_at"]})
        entries.append({"id": token_id, "expires_at": expires_at})
        updated = dict(record)
        updated["revoked_sessions"] = entries
        self._persist_record(updated)

    def _remember_token_valid(self, token, user_agent=""):
        parsed = self._remember_parts(token, user_agent)
        if parsed is None:
            return False
        expires_at = parsed[0]
        now = int(self._clock())
        if expires_at <= now:
            return False
        token_id = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return not self._revocation_active(token_id, now)

    def _prune_sessions(self):
        now = self._clock()
        for token, expiry in list(self._sessions.items()):
            if expiry <= now:
                self._sessions.pop(token, None)

    def session_valid(self, token, user_agent=""):
        """Accept an in-memory session or a UA-bound r2 remember token.

        In-memory tokens (setup / remember=false logins) are exact-match
        keys and are not UA-bound. r2 remember tokens additionally require
        the current request UA to recompute to the same fingerprint.
        Any malformed input (None, numbers, containers, non-ASCII) resolves
        to False without raising.
        """
        if not isinstance(token, str) or not token:
            return False
        with self._lock:
            expires = self._sessions.get(token, 0)
            if expires > self._clock():
                return True
            self._sessions.pop(token, None)
            return self._remember_token_valid(token, user_agent)

    def logout(self, token, user_agent=""):
        """Drop the in-memory session and revoke the r2 token (if any).

        Malformed tokens are ignored; they can be neither an in-memory
        key nor a signable remember token.
        """
        with self._lock:
            if not isinstance(token, str):
                return
            self._sessions.pop(token, None)
            self._revoke_remember(token, user_agent)

    def cookie_kwargs(self, max_age=None, secure=None):
        """Cookie attributes; keyword-only overrides stay opt-in.

        Called without arguments this returns exactly the pre-T60 shape so
        every existing call site keeps its behaviour.
        """
        return {
            "key": COOKIE_NAME,
            "httponly": True,
            "samesite": "strict",
            "secure": self.cookie_secure if secure is None else bool(secure),
            "max_age": self.session_seconds if max_age is None else int(max_age),
            "path": "/",
        }


def client_key(request, trust_proxy=False, trusted_proxies=()):
    """Per-client rate-limit key for the login endpoint.

    T61-followup: the ``X-Forwarded-For`` header only contributes to the
    key when the operator opted in (``TGDL_TRUST_PROXY=1``) AND the
    *direct* peer is on the trusted proxy whitelist (loopback by
    default), so a public client can never spoof its own rate-limit
    identity. The returned ``"<ip>|<path>"`` shape is unchanged.
    """
    host = request.client.host if request.client else "unknown"
    if trust_proxy and is_trusted_proxy_host(host, trusted_proxies):
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            host = forwarded.split(",", 1)[0].strip() or host
    return f"{host}|{request.url.path}"


def request_is_https(request, trust_proxy=False, trusted_proxies=()):
    """True when the request is known to be HTTPS (Cookie Secure input).

    A direct HTTPS connection always wins. Otherwise ``X-Forwarded-Proto``
    is consulted only when the operator opted in AND the direct peer is
    a trusted proxy; the first comma-separated value must equal ``https``
    after stripping whitespace, case-insensitively. Everything else -
    a missing/empty header, a non-trusted peer, a plain HTTP request -
    resolves to False without raising.
    """
    url = getattr(request, "url", None)
    if str(getattr(url, "scheme", "") or "").lower() == "https":
        return True
    if not trust_proxy:
        return False
    host = request.client.host if request.client else ""
    if not is_trusted_proxy_host(host, trusted_proxies):
        return False
    forwarded = request.headers.get("x-forwarded-proto", "")
    first = forwarded.split(",", 1)[0].strip().lower() if forwarded else ""
    return first == "https"


# T61-followup: peers whose X-Forwarded-* headers are honoured.
# Empty whitelist = "not configured" and falls back to this loopback set;
# an explicit TGDL_TRUSTED_PROXIES *replaces* it (add 127.0.0.1 explicitly
# when the reverse proxy runs on the same machine).
DEFAULT_TRUSTED_PROXIES = ("127.0.0.1/32", "::1/128")


def _proxy_networks(trusted_proxies):
    """Parse whitelist entries into ip_network objects; bad ones are dropped.

    This runs on the request hot path, so a mistyped whitelist entry must
    degrade to "that peer is untrusted" instead of raising a 500. The
    hard validation lives in server_config._as_trusted_proxies, which
    rejects bad values at startup with a ValueError.
    """
    out = []
    for item in trusted_proxies or ():
        if isinstance(item, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
            out.append(item)
            continue
        try:
            out.append(ipaddress.ip_network(str(item).strip(), strict=False))
        except ValueError:
            continue
    return tuple(out)


def is_trusted_proxy_host(host, trusted_proxies=()):
    """True iff ``host`` is an IP address inside the trusted proxy set.

    Without a configured whitelist the default loopback set applies
    (127.0.0.1/32 and ::1/128 only - no hostnames, no "localhost",
    no other loopback addresses). With a whitelist, the whitelist fully
    replaces the default - including the degenerate case where every
    entry fails to parse, which must NOT silently fall back to loopback
    (that would trust a peer the operator explicitly scoped away).
    Non-IP input, empty values and ``None`` all resolve to False.
    """
    if not trusted_proxies:
        networks = _proxy_networks(DEFAULT_TRUSTED_PROXIES)
    else:
        networks = _proxy_networks(trusted_proxies)
    if not networks:
        return False
    try:
        addr = ipaddress.ip_address(str(host or "").strip())
    except ValueError:
        return False
    return any(addr in net for net in networks)


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


def tokens_equal(actual, expected):
    """Constant-time secret comparison that tolerates any text.

    ``secrets.compare_digest`` / ``hmac.compare_digest`` raise ``TypeError``
    when either argument is a non-ASCII ``str`` (a client can always put
    non-ASCII bytes in a bearer header or a JSON field), which would turn a
    plain authentication failure into a 500 or a crashed WebSocket handler.
    Encoding both sides to UTF-8 first keeps the comparison constant-time and
    makes an empty/None side simply fail to match.
    """
    if not actual or not expected:
        return False
    if not isinstance(actual, (str, bytes)):
        return False
    if not isinstance(expected, (str, bytes)):
        return False
    left = actual.encode("utf-8") if isinstance(actual, str) else actual
    right = expected.encode("utf-8") if isinstance(expected, str) else expected
    return hmac.compare_digest(left, right)


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
