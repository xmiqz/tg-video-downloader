"""验证码投递渠道必须以 Telegram 返回的 auth.SentCode 为准。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from telethon.tl import types

import gui
import tg_video_dl as core


class FakeEngine:
    def submit(self, coro):
        return asyncio.run(coro)


class FakeClient:
    def __init__(self, sent_code):
        self.sent_code = sent_code

    async def send_code_request(self, phone):
        return self.sent_code


def make_api(monkeypatch, sent_code):
    monkeypatch.setattr(gui.core, "save_config", lambda cfg: None)
    api = gui.Api.__new__(gui.Api)
    api.engine = FakeEngine()
    api.client = FakeClient(sent_code)
    api.cfg = {}
    api._code_sent = False
    api._code_delivery = {}
    api.guest = False
    api._ensure_client = lambda: ""
    return api


def sent_code(code_type, next_type=None, timeout=None):
    return SimpleNamespace(type=code_type, next_type=next_type,
                           timeout=timeout)


def test_app_delivery_is_reported(monkeypatch):
    api = make_api(monkeypatch, sent_code(types.auth.SentCodeTypeApp(5)))
    result = api.send_code("8613800000000")
    assert result["delivery"] == "app"
    assert "Telegram App" in result["notice"]
    assert "warning" not in result
    state = api.login_state()
    assert state["has_session"] is True
    assert state["delivery"] == "app"
    assert "Telegram App" in state["notice"]


def test_sms_delivery_is_not_reported_as_app(monkeypatch):
    api = make_api(monkeypatch, sent_code(
        types.auth.SentCodeTypeSms(5), types.auth.CodeTypeCall(), 120))
    result = api.send_code("8613800000000")
    assert result["delivery"] == "sms"
    assert "短信" in result["notice"]
    assert "120 秒" in result["notice"]
    assert result["delivery_info"]["next_delivery"] == "call"


def test_email_setup_required_has_no_code(monkeypatch):
    result = core.sent_code_hint(core.sent_code_delivery(sent_code(
        types.auth.SentCodeTypeSetUpEmailRequired())))
    assert "先设置登录邮箱" in result
    assert "验证码未发送" in result


@pytest.mark.parametrize("code_type,expected", [
    (types.auth.SentCodeTypeSmsWord(""), "sms"),
    (types.auth.SentCodeTypeSmsPhrase(""), "sms"),
    (types.auth.SentCodeTypeEmailCode("a***b", 5), "email"),
    (types.auth.SentCodeTypeFragmentSms("https://fragment.com", 5), "fragment"),
])
def test_delivery_type_aliases(code_type, expected):
    assert core.sent_code_delivery(sent_code(code_type))["delivery"] == expected
