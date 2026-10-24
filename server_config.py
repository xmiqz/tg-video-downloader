# -*- coding: utf-8 -*-
"""v2.4 headless runtime configuration.

Precedence is deliberately boring: command line > TGDL_* environment > JSON
config file > defaults. Authentication secrets are not accepted here; they
live in the data volume so one accidental `docker inspect` cannot expose them.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import pathlib
from dataclasses import dataclass, field

import tg_video_dl as core


TRUE_VALUES = {"1", "true", "yes", "on"}
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in TRUE_VALUES


def _as_roots(value, base):
    if value is None:
        return ()
    if isinstance(value, str):
        raw = value.split(os.pathsep)
    elif isinstance(value, (list, tuple)):
        raw = value
    else:
        raise ValueError("allowed_roots 必须是路径列表或路径分隔符字符串")
    out = []
    for item in raw:
        text = str(item).strip()
        if not text:
            continue
        path = pathlib.Path(text).expanduser()
        if not path.is_absolute():
            path = base / path
        out.append(path.resolve())
    return tuple(dict.fromkeys(out))


def is_loopback_host(host):
    value = str(host or "").strip().lower()
    if value in ("localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True)
class RuntimeConfig:
    data_dir: pathlib.Path
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    allowed_roots: tuple[pathlib.Path, ...] = field(default_factory=tuple)
    cookie_secure: bool = False
    trust_proxy: bool = False
    desktop_mode: bool = False
    session_hours: int = 168
    bootstrap_token_file: pathlib.Path | None = None
    config_file: pathlib.Path | None = None

    @property
    def loopback(self):
        return is_loopback_host(self.host)

    @property
    def auth_required(self):
        return not self.loopback

    @property
    def base_url(self):
        host = self.host
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{self.port}"

    def normalized(self):
        data_dir = pathlib.Path(self.data_dir).expanduser().resolve()
        roots = self.allowed_roots or (data_dir,)
        if data_dir not in roots:
            roots = (data_dir,) + tuple(roots)
        token_file = self.bootstrap_token_file
        if token_file is None:
            token_file = data_dir / ".setup-token"
        token_file = token_file.expanduser().resolve()
        return RuntimeConfig(
            data_dir=data_dir,
            host=self.host,
            port=self.port,
            allowed_roots=tuple(dict.fromkeys(roots)),
            cookie_secure=self.cookie_secure,
            trust_proxy=self.trust_proxy,
            desktop_mode=self.desktop_mode,
            session_hours=self.session_hours,
            bootstrap_token_file=token_file,
            config_file=(self.config_file.expanduser().resolve()
                         if self.config_file else None),
        )


def _read_config_file(path):
    path = pathlib.Path(path).expanduser()
    if not path.exists():
        return {}, path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"配置文件无法读取：{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("配置文件顶层必须是 JSON 对象")
    return data, path.resolve()


def _default_data_dir():
    raw = os.environ.get("TGDL_DATA_DIR")
    if raw:
        return pathlib.Path(raw)
    return core.APP_DIR / core.DATA_DIR_NAME


def _config_path_from_env():
    raw = os.environ.get("TGDL_CONFIG")
    return pathlib.Path(raw).expanduser() if raw else None


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m server_app",
        description="TG 视频下载器 v2.4 无头 Web 服务",
    )
    parser.add_argument("--host", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, help="监听端口，默认 8787")
    parser.add_argument("--data-dir", help="数据卷目录")
    parser.add_argument("--config", help="JSON 配置文件")
    parser.add_argument(
        "--allowed-root",
        action="append",
        default=None,
        help="允许下载/移动到的根目录，可重复；默认仅数据目录",
    )
    parser.add_argument(
        "--trust-proxy",
        action="store_true",
        default=None,
        help="信任反向代理提供的 X-Forwarded-For",
    )
    parser.add_argument(
        "--cookie-secure",
        action="store_true",
        default=None,
        help="会话 Cookie 仅通过 HTTPS 发送",
    )
    return parser


def load_runtime_config(argv=None):
    args = build_parser().parse_args(argv)

    data_from_cli = bool(args.data_dir)
    data_from_env = bool(os.environ.get("TGDL_DATA_DIR"))
    data_dir = pathlib.Path(args.data_dir) if args.data_dir else _default_data_dir()
    if not data_dir.is_absolute():
        data_dir = pathlib.Path.cwd() / data_dir
    data_dir = data_dir.expanduser().resolve()

    config_path = pathlib.Path(args.config) if args.config else _config_path_from_env()
    if config_path is None:
        config_path = data_dir / "server.json"
    elif not config_path.is_absolute():
        config_path = pathlib.Path.cwd() / config_path
    file_cfg, config_path = _read_config_file(config_path)
    if not data_from_cli and not data_from_env and file_cfg.get("data_dir"):
        configured_data = pathlib.Path(str(file_cfg["data_dir"])).expanduser()
        if not configured_data.is_absolute():
            configured_data = config_path.parent / configured_data
        data_dir = configured_data.resolve()

    host = args.host
    if host is None:
        host = os.environ.get("TGDL_HOST", file_cfg.get("host", DEFAULT_HOST))

    port = args.port
    if port is None:
        port = int(os.environ.get("TGDL_PORT", file_cfg.get("port", DEFAULT_PORT)))
    if not 1 <= int(port) <= 65535:
        raise ValueError("端口必须在 1..65535 之间")

    roots_raw = args.allowed_root
    if roots_raw is None:
        roots_raw = os.environ.get("TGDL_ALLOWED_ROOTS")
    if roots_raw is None:
        roots_raw = file_cfg.get("allowed_roots")

    trust_proxy = args.trust_proxy
    if trust_proxy is None:
        trust_proxy = _as_bool(
            os.environ.get("TGDL_TRUST_PROXY"), file_cfg.get("trust_proxy", False))

    cookie_secure = args.cookie_secure
    if cookie_secure is None:
        cookie_secure = _as_bool(
            os.environ.get("TGDL_COOKIE_SECURE"),
            file_cfg.get("cookie_secure", False),
        )

    session_hours = int(
        os.environ.get("TGDL_SESSION_HOURS",
                       file_cfg.get("session_hours", 168))
    )
    if not 1 <= session_hours <= 24 * 365:
        raise ValueError("TGDL_SESSION_HOURS 必须在 1..8760 之间")

    token_raw = os.environ.get("TGDL_SETUP_TOKEN_FILE",
                               file_cfg.get("setup_token_file"))
    token_file = pathlib.Path(token_raw) if token_raw else None
    if token_file is not None and not token_file.is_absolute():
        token_file = data_dir / token_file

    return RuntimeConfig(
        data_dir=data_dir,
        host=str(host).strip() or DEFAULT_HOST,
        port=int(port),
        allowed_roots=_as_roots(roots_raw, data_dir),
        cookie_secure=bool(cookie_secure),
        trust_proxy=bool(trust_proxy),
        session_hours=session_hours,
        bootstrap_token_file=token_file,
        config_file=config_path,
    ).normalized()
