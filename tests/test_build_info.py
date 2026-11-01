# -*- coding: utf-8 -*-
"""构建元信息：登录页页脚与 /healthz 共用的版本 / 构建号 / 项目地址。"""
import server_config


def test_defaults_when_env_missing(monkeypatch):
    monkeypatch.delenv("TGDL_VERSION", raising=False)
    monkeypatch.delenv("TGDL_BUILD", raising=False)
    info = server_config.build_info()
    assert info == {
        "version": "dev",
        "build": "",
        "project": server_config.PROJECT_URL,
    }


def test_env_values_trimmed_and_build_shortened(monkeypatch):
    monkeypatch.setenv("TGDL_VERSION", "  v2.5.0-beta.1  ")
    monkeypatch.setenv("TGDL_BUILD", "9f3c1a7d4e5f60718293a4b5c6d7e8f901234567")
    info = server_config.build_info()
    assert info["version"] == "v2.5.0-beta.1"
    assert info["build"] == "9f3c1a7"


def test_project_url_points_at_public_repo():
    assert server_config.PROJECT_URL == "https://github.com/xmiqz/tg-video-downloader"


def test_blank_env_falls_back_to_dev(monkeypatch):
    monkeypatch.setenv("TGDL_VERSION", "   ")
    monkeypatch.setenv("TGDL_BUILD", "   ")
    info = server_config.build_info()
    assert info["version"] == "dev"
    assert info["build"] == ""
