# -*- coding: utf-8 -*-
"""TG 视频下载器 —— 现代图形界面（pywebview）。

架构说明：核心下载/扫描/任务引擎完全复用 tg_video_dl.py，本文件只做一层
HTTP/JS 桥接，把引擎的异步能力暴露给 web/ 下的界面。引擎的 CLI 入口与
本 GUI 互不影响。
"""
import asyncio
import base64
import ctypes
import io
import json
import os
import pathlib
import queue
import subprocess
import sys
import threading
import time

import webview

import tg_video_dl as core
from telethon import TelegramClient
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PasswordHashInvalidError,
)


def resource_dir():
    """web 资源目录：打包后从解包目录取，开发时取脚本所在 web/"""
    if getattr(sys, "frozen", False):
        base = pathlib.Path(getattr(sys, "_MEIPASS", ""))
        p = base / "web"
        if p.exists():
            return str(p)
    return str(pathlib.Path(__file__).resolve().parent / "web")


class EngineLoop:
    """在独立线程跑一个 asyncio 事件循环，供桥接方法提交协程。"""

    def __init__(self):
        self.loop = None
        self.ready = threading.Event()

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()
        self.ready.wait()

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.ready.set()
        self.loop.run_forever()

    def submit(self, coro):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result()


class Api:
    def __init__(self, engine):
        self.engine = engine
        self.client = None
        self.cfg = {}
        self._preview = {}      # mid -> record（含活动消息对象）
        # 实时取光相关（仅 Windows 液态玻璃模式使用）
        self.capture = None
        self._form = None
        self._drv = None
        self._pump_timer = None
        self._handler = None

    # ---------- 外观模式 ----------
    def get_appearance(self):
        """外观：dark=静态暗（默认）/ light=静态亮 / liquid=实时透明液态玻璃。"""
        return {"ok": True, "mode": self.cfg.get("appearance", "dark")}

    def set_appearance(self, mode):
        if mode not in ("dark", "light", "liquid"):
            return {"ok": False, "error": "未知外观模式"}
        self.cfg["appearance"] = mode
        core.save_config(self.cfg)
        if sys.platform == "win32":
            try:
                self._apply_mode(mode)
            except Exception as e:
                print("[GUI] 切换外观失败：", e)
        return {"ok": True}

    def _apply_mode(self, mode):
        """把取光任务的启停 marshal 到 UI 线程。"""
        form, drv = self._form, self._drv
        if form is None or drv is None:
            return

        def _do():
            drv.set_mode(mode)

        from System import Action
        form.Invoke(Action(_do))

    # ---------- client 生命周期辅助 ----------
    def _new_client(self):
        return TelegramClient(
            core.SESSION_PATH, self.cfg["api_id"], self.cfg["api_hash"],
            flood_sleep_threshold=120,
        )

    def _discard_client(self, client):
        """断开并丢弃一个（可能半连接的）client，任何失败都吞掉。"""
        if client is None:
            return

        async def _do():
            try:
                await client.disconnect()
            except Exception:
                pass

        try:
            self.engine.submit(_do())
        except Exception:
            pass

    def _remove_session_files(self):
        for p in (core.SESSION_PATH + ".session",
                  core.SESSION_PATH + ".session-journal"):
            try:
                pathlib.Path(p).unlink()
            except Exception:
                pass

    def _ensure_client(self):
        """client 缺失时（connect() 曾失败）新建并连接。返回 None 或错误串。"""
        if self.client is not None:
            return None
        client = self._new_client()

        async def _do():
            await client.connect()

        try:
            self.engine.submit(_do())
        except Exception as e:
            self._discard_client(client)
            return f"{type(e).__name__}: {e}"
        self.client = client
        return None

    # ---------- 退出登录 ----------
    def logout(self):
        """退出登录：通知 TG 销毁授权并删除本地会话，回到未登录状态。"""
        if core.any_active():
            return {"ok": False, "busy": True,
                    "error": "有下载任务正在进行，请先中止下载后再退出登录。"}
        client = self.client

        async def _do():
            if client is None:
                return
            try:
                await client.log_out()
            except Exception:
                # 网络异常等导致服务端注销失败，也要本地断开，
                # 否则旧连接仍占着事件循环
                try:
                    await client.disconnect()
                except Exception:
                    pass

        try:
            if client is not None:
                self.engine.submit(_do())
        except Exception:
            pass
        self.client = None
        self._preview = {}
        self._remove_session_files()
        # 清掉已保存手机号，重新登录时从输入手机号开始
        self.cfg.pop("phone", None)
        try:
            core.save_config(self.cfg)
        except Exception:
            pass
        return {"ok": True}

    # ---------- 登录 ----------
    def login_state(self):
        if self.client is not None:
            return {"logged_in": True}
        # 已存在会话则尝试静默连接
        return {"logged_in": False,
                "has_session": pathlib.Path(core.SESSION_PATH + ".session").exists()}

    def connect(self):
        if self.client is not None:
            return {"ok": True}
        core.ensure_dirs_and_migrate()
        self.cfg = core.setup_credentials()
        client = self._new_client()

        async def _do():
            # 连接 + 鉴权探测整体限时：网络不通 / 同一 session 被另一实例
            # 占用导致服务端反复重置时，快速失败而不是在启动时长时间挂起。
            async def _connect_and_check():
                await client.connect()
                if await client.is_user_authorized():
                    return "ok"
                phone = self.cfg.get("phone") or ""
                if not phone:
                    return "need_phone"
                await client.send_code_request(phone)
                return "need_code"

            return await asyncio.wait_for(_connect_and_check(), timeout=20.0)

        try:
            status = self.engine.submit(_do())
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            if type(e).__name__ == "SendCodeUnavailableError":
                # 首次 SendCodeRequest 已成功（验证码已通过 App 内消息送达，
                # phone_code_hash 已在 client 内存），失败的只是升级重发。
                # 保留 client，直接进入输入验证码步骤
                self.client = client
                return {"ok": True, "stage": "need_code",
                        "warning": "验证码已通过 Telegram App 消息发送，请直接查看并输入最新验证码；无需重复点击发送。"}
            # 半成品 client 不能保留，否则后续 send_code 会拿到死连接/None
            self._discard_client(client)
            if type(e).__name__ == "AuthKeyDuplicatedError":
                # 会话密钥已被服务端作废（同一 session 被两处 IP 同时使用），
                # 删除会话文件，稍后由 send_code 走全新连接
                self._remove_session_files()
                msg += "\n本地登录信息已失效，请直接重新发送验证码登录。"
            if type(e).__name__ in ("TimeoutError", "asyncio.TimeoutError"):
                msg = ("连接 Telegram 服务器超时（20 秒）。\n"
                       "请检查网络或代理后重试；若桌面版正在运行，"
                       "请先关闭它再启动。")
            if "locked" in msg or "is locked" in msg:
                return {"ok": False, "busy": True,
                        "error": "会话正被其它程序占用（桌面版还在运行？），请先关闭后重试。"}
            return {"ok": False, "error": msg}
        self.client = client
        if status == "ok":
            return {"ok": True, "stage": "ready"}
        return {"ok": True, "stage": status}

    def send_code(self, phone):
        # connect() 失败后 self.client 可能为 None，先补建连接
        err = self._ensure_client()
        if err:
            return {"ok": False, "error": err}

        async def _do():
            await self.client.send_code_request(phone)

        try:
            self.engine.submit(_do())
        except Exception as e:
            ename = type(e).__name__
            if ename == "AuthKeyDuplicatedError":
                self._discard_client(self.client)
                self.client = None
                self._remove_session_files()
                return {"ok": False, "error": f"{ename}: {e}"}
            if ename == "SendCodeUnavailableError":
                # 首次发送已成功，验证码已到 App；失败的只是短信升级重发
                self.cfg["phone"] = phone
                core.save_config(self.cfg)
                return {"ok": True, "stage": "need_code",
                        "warning": "验证码已通过 Telegram App 消息发送，请直接查看并输入最新验证码；无需重复点击发送。"}
            return {"ok": False, "error": f"{ename}: {e}"}
        self.cfg["phone"] = phone
        core.save_config(self.cfg)
        return {"ok": True}

    def verify_code(self, code):
        async def _do():
            try:
                await self.client.sign_in(self.cfg.get("phone"), code)
                return "ready"
            except SessionPasswordNeededError:
                return "need_password"
            except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
                return f"__err__{type(e).__name__}: {e}"
        r = self.engine.submit(_do())
        if r.startswith("__err__"):
            return {"ok": False, "error": r[len("__err__"):]}
        return {"ok": True, "stage": r}

    def verify_password(self, password):
        async def _do():
            try:
                await self.client.sign_in(password=password)
                return "ready"
            except PasswordHashInvalidError as e:
                return f"__err__{e}"
        r = self.engine.submit(_do())
        if r.startswith("__err__"):
            return {"ok": False, "error": r[len("__err__"):]}
        return {"ok": True, "stage": r}

    def me(self):
        async def _do():
            u = await self.client.get_me()
            return {"name": (u.first_name or ""), "username": u.username or "",
                    "phone": u.phone or ""}
        try:
            return {"ok": True, **self.engine.submit(_do())}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------- 频道预览（高级搜索） ----------
    def preview(self, params):
        """params: 前端组装的高级搜索参数对象"""
        try:
            channel_in = params.get("channel", "")
            channel_name = core.parse_channel(channel_in)
        except Exception as e:
            return {"ok": False, "error": str(e)}

        async def _do():
            entity = await self.client.get_entity(channel_name)
            title = getattr(entity, "title", None) or channel_name
            p = dict(params)
            p.pop("channel", None)
            records, meta = await core.search_videos(self.client, entity, p)
            # 用 (来源, mid) 做键，支持评论区
            self._preview = {
                (r.get("src", core.SRC_CHANNEL), r["msg"].id): r
                for r in records
            }
            items = [{
                "mid": r["msg"].id,
                "src": r.get("src", core.SRC_CHANNEL),
                "name": r["name"],
                "size": r["size"],
                "date": (f"{r['msg'].date:%Y-%m-%d}" if r["msg"].date else ""),
                "text": r["text"],
            } for r in records]
            return {
                "ok": True, "channel": channel_name, "title": title,
                "meta": meta, "items": items,
            }

        try:
            return self.engine.submit(_do())
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # ---------- 建任务并下载 ----------
    def start_download(self, channel, title, keys, parallel, folder=""):
        todo = []
        for k in keys:
            src = k.get("src", core.SRC_CHANNEL)
            rec = self._preview.get((src, int(k["mid"])))
            if rec is not None:
                todo.append(rec)
        if not todo:
            return {"ok": False, "error": "没有选择视频"}
        if not folder:
            root = core.get_download_root(self.cfg)
        else:
            root = pathlib.Path(folder)
            root.mkdir(parents=True, exist_ok=True)
        safe = "".join(c for c in (title or channel) if c not in '\\/:*?"<>|').strip()
        folder_dest = root / (safe or "channel")
        folder_dest.mkdir(parents=True, exist_ok=True)

        task = {
            "id": core.new_task_id(),
            "created": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
            "channel": channel, "channel_title": title,
            "params": {"parallel": max(1, min(int(parallel), 8))},
            "folder": str(folder_dest),
            "items": [core.make_task_item(r) for r in todo],
        }
        core.save_task(task)

        def _run_bg():
            async def _do():
                await core.execute_downloads(
                    self.client, task, todo,
                    task["params"]["parallel"], gui={},
                )
            fut = asyncio.run_coroutine_threadsafe(_do(), self.engine.loop)
            try:
                fut.result()
            except Exception as ex:
                print("[GUI] 下载异常：", ex)

        threading.Thread(target=_run_bg, daemon=True).start()
        return {"ok": True, "task_id": task["id"]}

    # ---------- 实时状态 ----------
    def active_states(self):
        return core.active_states()

    def is_active(self):
        return core.any_active()

    def get_item_text(self, task_id, src, mid):
        """按需取单条消息原文（轮询快照为省流量不再携带 text）。"""
        mid = int(mid)
        # 活动会话：直接从监控条目取，含最新文本
        sess = core.ACTIVE_SESSIONS.get(task_id)
        if sess is not None:
            e = sess.mon.order.get((src, mid))
            if e is not None:
                return {"ok": True, "text": e.get("text", "")}
        t = self._load_one(task_id)
        if t is None:
            return {"ok": False, "error": "任务不存在"}
        for ti in t["items"]:
            if ti["msg"] == mid and ti.get("src", core.SRC_CHANNEL) == src:
                return {"ok": True, "text": ti.get("text", "")}
        return {"ok": False, "error": "未找到该条目"}

    def abort_task(self, task_id):
        sess = core.ACTIVE_SESSIONS.get(task_id)
        if sess is None:
            return {"ok": False, "error": "该任务当前没有下载会话"}
        sess.request_abort()
        return {"ok": True}

    def item_action(self, task_id, action, keys):
        """单项/批量操作。action: pause/resume/skip/remove。
        任务下载中：走会话控制（暂停释放槽位、恢复优先调度）；
        非下载中：remove 直接删条目，skip/resume 直接改持久状态。"""
        ks = [(k.get("src", core.SRC_CHANNEL), int(k["mid"]))
              for k in keys]
        sess = core.ACTIVE_SESSIONS.get(task_id)
        if sess is not None:
            sess.request(action, ks)
            return {"ok": True}
        t = self._load_one(task_id)
        if t is None:
            return {"ok": False, "error": "任务不存在"}
        if action == "remove":
            return {"ok": True, "removed": core.remove_items(t, ks)}
        changed = 0
        target = "skipped" if action == "skip" else "pending"
        for k in ks:
            if core.set_item_status(t, k, target):
                changed += 1
        return {"ok": True, "changed": changed}

    # ---------- 任务历史 ----------
    def list_tasks(self):
        out = []
        for t in core.load_tasks():
            c = core.task_counts(t)
            out.append({
                "id": t["id"], "channel_title": t.get("channel_title", ""),
                "channel": t.get("channel", ""), "created": t.get("created", ""),
                "finished": c["finished"], "total": c["total"],
                "pending": c["pending"], "failed": c["failed"],
                "skipped": c["skipped"],
            })
        return out

    def get_task(self, task_id):
        for t in core.load_tasks():
            if t["id"] == task_id:
                return {"ok": True, "task": t,
                        "counts": core.task_counts(t)}
        return {"ok": False, "error": "任务不存在"}

    def delete_task(self, task_id):
        for t in core.load_tasks():
            if t["id"] == task_id:
                core.delete_task(t)
                return {"ok": True}
        return {"ok": False, "error": "任务不存在"}

    def resume_task(self, task_id, only_failed=False):
        for t in core.load_tasks():
            if t["id"] != task_id:
                continue
            if task_id in core.ACTIVE_SESSIONS:
                return {"ok": False, "error": "该任务已在下载中，请勿重复开始"}
            statuses = ("failed",) if only_failed else ("pending", "failed")
            n = sum(1 for ti in t["items"] if ti["status"] in statuses)
            if not n:
                return {"ok": False, "error": "没有需要下载的视频"}
            parallel = int(t.get("params", {}).get("parallel") or 1)

            def _run_bg():
                async def _do():
                    records = await core.fetch_records(self.client, t, statuses)
                    if records:
                        await core.execute_downloads(
                            self.client, t, records, parallel, gui={}
                        )
                fut = asyncio.run_coroutine_threadsafe(_do(), self.engine.loop)
                try:
                    fut.result()
                except Exception as ex:
                    print("[GUI] 续传异常：", ex)

            threading.Thread(target=_run_bg, daemon=True).start()
            return {"ok": True}
        return {"ok": False, "error": "任务不存在"}

    # ---------- 任务编辑 ----------
    def _load_one(self, task_id):
        for t in core.load_tasks():
            if t["id"] == task_id:
                return t
        return None

    def edit_task(self, task_id, fields):
        """批量修改任务属性：title/parallel/folder（+move_files）"""
        t = self._load_one(task_id)
        if t is None:
            return {"ok": False, "error": "任务不存在"}
        if fields.get("title"):
            core.rename_task(t, fields["title"])
        if fields.get("parallel"):
            core.set_parallel(t, int(fields["parallel"]))
        moved_msgs = []
        if fields.get("folder"):
            _, moved_msgs = core.change_folder(
                t, fields["folder"], bool(fields.get("move_files"))
            )
        return {"ok": True, "warnings": moved_msgs}

    def set_item(self, task_id, src, mid, status):
        t = self._load_one(task_id)
        if t is None:
            return {"ok": False, "error": "任务不存在"}
        ok = core.set_item_status(t, (src, int(mid)), status)
        return {"ok": ok}

    def remove_items(self, task_id, keys):
        t = self._load_one(task_id)
        if t is None:
            return {"ok": False, "error": "任务不存在"}
        ks = [(k.get("src", core.SRC_CHANNEL), int(k["mid"])) for k in keys]
        sess = core.ACTIVE_SESSIONS.get(task_id)
        if sess is not None:
            # 下载中：取消正在下的条目并从清单删除，槽位随之释放
            sess.request("remove", ks)
            return {"ok": True, "removed": len(ks)}
        n = core.remove_items(t, ks)
        return {"ok": True, "removed": n}

    def scan_append(self, task_id, params):
        """按高级搜索参数扫描并把新视频追加到已有任务"""
        t = self._load_one(task_id)
        if t is None:
            return {"ok": False, "error": "任务不存在"}

        async def _do():
            entity = await self.client.get_entity(t["channel"])
            records, meta = await core.search_videos(self.client, entity, params)
            added = core.append_records(t, records)
            return added, meta

        try:
            added, meta = self.engine.submit(_do())
            return {"ok": True, "added": added, "meta": meta}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # ---------- 独立编辑窗口 ----------
    def open_editor(self, task_id):
        """弹出第二个专业编辑窗口（任务/条目管理）。"""
        t = self._load_one(task_id)
        if t is None:
            return {"ok": False, "error": "任务不存在"}
        url = (str(pathlib.Path(resource_dir()) / "editor.html")
               + f"?id={task_id}")
        s = _dpi_scale()
        bg = "#e9ecf5" if self.cfg.get("appearance") == "light" else "#07080f"
        webview.create_window(
            "任务编辑 · " + t.get("channel_title", ""),
            url=url, js_api=self,
            width=int(1080 * s), height=int(740 * s),
            min_size=(int(880 * s), int(580 * s)),
            background_color=bg,
        )
        return {"ok": True}

    def editor_data(self, task_id, enrich=True):
        """任务与全部条目（含 TG 消息原文、时长、日期）。
        enrich=True 时后台补全缺失 text/duration；回捞时传 False 仅取本地。"""
        t = self._load_one(task_id)
        if t is None:
            return {"ok": False, "error": "任务不存在"}
        # 从 Telegram 补全旧条目缺失的消息文本/时长：改为后台执行，绝不阻塞
        # 首屏返回——否则条目多时 get_entity + 分批 get_messages 会让编辑窗口
        # 长时间停在 loading。后台独立重新加载任务副本，避免与本次返回竞争；
        # 补全结果由 enrich 内部 save_task 持久化，前端再静默拉一次即可。
        if enrich and self.client is not None:
            def _bg_enrich():
                try:
                    tt = self._load_one(task_id)
                    if tt is None:
                        return

                    async def _en():
                        await core.enrich_task_items(self.client, tt)

                    self.engine.submit(_en())
                except Exception as e:
                    print("[GUI] 条目详情补全失败：", e)

            threading.Thread(target=_bg_enrich, daemon=True).start()
        return {
            "ok": True,
            "task": {
                "id": t["id"],
                "channel_title": t.get("channel_title", ""),
                "channel": t.get("channel", ""),
                "folder": t.get("folder", ""),
                "parallel": t.get("params", {}).get("parallel", 1),
                "created": t.get("created", ""),
            },
            "items": t["items"],
            "counts": core.task_counts(t),
        }

    def editor_save(self, task_id, fields):
        return self.edit_task(task_id, fields)

    def editor_set_status(self, task_id, keys, status):
        t = self._load_one(task_id)
        if t is None:
            return {"ok": False, "error": "任务不存在"}
        ks = [(k.get("src", core.SRC_CHANNEL), int(k["mid"])) for k in keys]
        n = core.set_items_status(t, ks, status)
        return {"ok": True, "n": n}

    def editor_remove(self, task_id, keys):
        return self.remove_items(task_id, keys)

    def item_file_info(self, task_id, src, mid):
        """条目对应本地文件信息：是否存在/路径/大小"""
        t = self._load_one(task_id)
        if t is None:
            return {"ok": False}
        ti = core.find_item(t, (src, int(mid)))
        if ti is None:
            return {"ok": False}
        p = pathlib.Path(t["folder"]) / ti["name"]
        partial = pathlib.Path(str(p) + ".dl")
        return {"ok": True, "exists": p.exists(),
                "path": str(p), "st_size": (p.stat().st_size if p.exists() else 0),
                "partial": partial.exists()}

    def items_file_info(self, task_id):
        """批量返回任务下所有条目的文件存在状态：{"src|mid": {exists,partial}}"""
        t = self._load_one(task_id)
        if t is None:
            return {"ok": False}
        folder = pathlib.Path(t["folder"])
        out = {}
        for ti in t["items"]:
            p = folder / ti["name"]
            key = f"{ti.get('src', core.SRC_CHANNEL)}|{ti['msg']}"
            out[key] = {"exists": p.exists(), "partial": (pathlib.Path(str(p)+'.dl').exists())}
        return {"ok": True, "items": out}

    def open_item_file(self, task_id, src, mid):
        r = self.item_file_info(task_id, src, mid)
        if r.get("exists"):
            return self._open_path(r["path"])
        return {"ok": False, "error": "文件不存在"}

    def _open_path(self, p):
        try:
            if sys.platform.startswith("win"):
                os.startfile(p)  # noqa
            elif sys.platform == "darwin":
                subprocess.Popen(["open", p])
            else:
                subprocess.Popen(["xdg-open", p])
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------- 其它 ----------
    def open_folder(self, path):
        if not path:
            path = str(core.get_download_root(self.cfg))
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # noqa
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def choose_folder(self):
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            p = filedialog.askdirectory()
            root.destroy()
            return {"ok": True, "path": p}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_settings(self):
        return {
            "download_root": str(
                self.cfg.get("download_root") or core.DEFAULT_DOWNLOAD_ROOT
            ),
        }

    def set_download_root(self, path):
        self.cfg["download_root"] = path
        core.save_config(self.cfg)
        core.get_download_root(self.cfg)
        return {"ok": True}

    # ---------- 实时环境取光 ----------
    def backdrop_frame(self):
        cap = getattr(self, "capture", None)
        if cap is None:
            return {"ok": False}
        try:
            changed, url, ver = cap.render_jpeg()
            return {"ok": True, "changed": changed, "url": url, "v": ver}
        except Exception as e:
            print("[GUI] backdrop_frame 失败：", e)
            return {"ok": False}


if sys.platform == "win32":
    from ctypes import wintypes

    class _MagImageHeader(ctypes.Structure):
        # width/height/format/stride 的偏移（0/4/8/24）是回调唯一读取的
        # 字段，必须与运行时一致；尾部字段不使用。
        _fields_ = [
            ("width", wintypes.UINT),
            ("height", wintypes.UINT),
            ("format", ctypes.c_byte * 16),
            ("stride", wintypes.UINT),
            ("offset_x", wintypes.LONG),
            ("offset_y", wintypes.LONG),
        ]

    _MAGCB = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, ctypes.c_void_p, _MagImageHeader,
        ctypes.c_void_p, _MagImageHeader, wintypes.RECT, wintypes.RECT,
        wintypes.HRGN,
    )

    class _MagTransform(ctypes.Structure):
        _fields_ = [("v", ctypes.c_float * 3 * 3)]

    # ---- 64 位安全的原型声明 ----
    # 不声明时 ctypes 默认把句柄当 32 位 c_int：CreateWindowExW 返回的 HWND
    # 一旦超过 0x7FFFFFFF 就被截断，后续 Mag* 调用拿到坏句柄而 access
    # violation（取光组件偶发初始化失败、液态玻璃“时有时无”的根因）。
    _u32 = ctypes.windll.user32
    _magf = ctypes.windll.magnification
    _EnumProcT = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    _u32.GetWindowRect.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _u32.GetWindowRect.restype = wintypes.BOOL
    _u32.EnumChildWindows.argtypes = [
        wintypes.HWND, _EnumProcT, wintypes.LPARAM]
    _u32.EnumChildWindows.restype = wintypes.BOOL
    _u32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _u32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _u32.EnumThreadWindows.argtypes = [
        wintypes.DWORD, _EnumProcT, wintypes.LPARAM]
    _u32.EnumThreadWindows.restype = wintypes.BOOL
    _u32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    _u32.CreateWindowExW.restype = wintypes.HWND
    _u32.SetLayeredWindowAttributes.argtypes = [
        wintypes.HWND, wintypes.COLORREF, wintypes.BYTE, wintypes.DWORD]
    _u32.SetLayeredWindowAttributes.restype = wintypes.BOOL
    _u32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    _u32.SetWindowPos.restype = wintypes.BOOL
    _u32.InvalidateRect.argtypes = [
        wintypes.HWND, wintypes.LPVOID, wintypes.BOOL]
    _u32.InvalidateRect.restype = wintypes.BOOL
    _u32.UpdateWindow.argtypes = [wintypes.HWND]
    _u32.UpdateWindow.restype = wintypes.BOOL
    _u32.IsIconic.argtypes = [wintypes.HWND]
    _u32.IsIconic.restype = wintypes.BOOL

    _magf.MagInitialize.restype = wintypes.BOOL
    _magf.MagSetWindowFilterList.argtypes = [
        wintypes.HWND, wintypes.DWORD, ctypes.c_int,
        ctypes.POINTER(wintypes.HWND)]
    _magf.MagSetWindowFilterList.restype = wintypes.BOOL
    _magf.MagSetImageScalingCallback.argtypes = [wintypes.HWND, _MAGCB]
    _magf.MagSetImageScalingCallback.restype = wintypes.BOOL
    _magf.MagSetWindowSource.argtypes = [wintypes.HWND, wintypes.RECT]
    _magf.MagSetWindowSource.restype = wintypes.BOOL
    _magf.MagSetWindowTransform.argtypes = [
        wintypes.HWND, ctypes.POINTER(_MagTransform)]
    _magf.MagSetWindowTransform.restype = wintypes.BOOL


class AmbientCapture:
    """实时环境取光。

    创建一个不可见、鼠标穿透的全屏放大镜窗口，借助 Magnification API
    的缩放回调持续拿到真实屏幕像素；主窗口被排除在采样之外，于是
    主窗口区域采到的正是它"背后"的内容（壁纸 / 其他窗口）。
    """

    def __init__(self):
        self._cb = None
        self.mag_hwnd = None
        self.main_hwnd = None
        self.sw = self.sh = 0
        self._lock = threading.Lock()
        self._frame = None           # (w, h, buffer BGRA)
        self._buf = None             # 复用的全屏缓冲，避免每帧分配大块
        self.version = 0
        self._cache_key = None
        self._prev_bytes = None       # 上一次 JPEG 字节，用于内容去重

    def _main_rect(self):
        r = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(
            self.main_hwnd, ctypes.byref(r))
        return (r.left, r.top, r.right - r.left, r.bottom - r.top)

    def start(self, main_hwnd):
        user32 = ctypes.windll.user32
        mag = ctypes.windll.magnification
        sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        self.sw, self.sh = sw, sh
        self.main_hwnd = int(main_hwnd)

        if not mag.MagInitialize():
            raise OSError("MagInitialize 失败")

        # 收集要排除的窗口：主窗 + 全部子窗
        excluded = [self.main_hwnd]
        enum_proc = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )(lambda h, _l: (excluded.append(int(h)), True)[1])
        user32.EnumChildWindows(self.main_hwnd, enum_proc, 0)

        # 同一 UI 线程的所有窗口（WebView2/Chromium 的隐藏窗口在该线程）
        tid = user32.GetWindowThreadProcessId(self.main_hwnd, None)
        user32.EnumThreadWindows(int(tid), enum_proc, 0)

        # 放大镜始终为全屏：几何完全不依赖主窗当前矩形（主窗在线程恢复
        # DPI 的瞬间可能处于虚拟化中间态），这是取光稳定不崩的关键。
        # 主窗背后的像素在 render_jpeg 里按主窗矩形实时裁剪。
        # 分层（alpha=0 不可见）+ 鼠标穿透 + 工具窗（不进任务栏）。
        # 归主窗所有（owned），确保不在任务栏/Alt+Tab 出现，并随主窗销毁。
        self.mag_hwnd = user32.CreateWindowExW(
            0x00080000 |            # WS_EX_LAYERED
            0x00000020 |            # WS_EX_TRANSPARENT（鼠标穿透）
            0x00000080 |            # WS_EX_TOOLWINDOW（不显示任务栏按钮）
            0x08000000,             # WS_EX_NOACTIVATE（不抢焦点）
            "Magnifier", None,
            0x80000000 | 0x10000000,      # WS_POPUP | WS_VISIBLE
            0, 0, sw, sh,
            wintypes.HWND(self.main_hwnd), None, None, None,
        )
        if not self.mag_hwnd:
            raise OSError("放大镜窗口创建失败")
        user32.SetLayeredWindowAttributes(self.mag_hwnd, 0, 0, 0x2)
        excluded.append(int(self.mag_hwnd))

        # 去重
        seen, uniq = set(), []
        for hh in excluded:
            if hh not in seen:
                seen.add(hh)
                uniq.append(hh)

        arr = (wintypes.HWND * len(uniq))(*[wintypes.HWND(hh) for hh in uniq])
        # MW_FILTERMODE_EXCLUDE = 0
        mag.MagSetWindowFilterList(self.mag_hwnd, 0, len(uniq), arr)

        self._cb = _MAGCB(self._on_frame)
        mag.MagSetImageScalingCallback(self.mag_hwnd, self._cb)
        mag.MagSetWindowSource(self.mag_hwnd, wintypes.RECT(0, 0, sw, sh))
        mt = _MagTransform()
        for i in range(3):
            mt.v[i][i] = 1.0
        mag.MagSetWindowTransform(self.mag_hwnd, ctypes.byref(mt))

        # 放大镜宿主窗口在创建/首次显示阶段会自行带上 WS_EX_TOPMOST，并把
        # 属主（主窗）一起抬进置顶层——表现为其它应用窗口无法盖到主窗上。
        # 该窗口 alpha=0 不可见、只用于采样，置顶毫无必要：统一显式取消。
        # 已实测 pump 的 MagSetWindowSource 不会再把置顶加回。
        _SWP = 0x0001 | 0x0002      # SWP_NOSIZE | SWP_NOMOVE
        _NOTOPMOST = wintypes.HWND(-2)
        user32.SetWindowPos(self.mag_hwnd, _NOTOPMOST, 0, 0, 0, 0, _SWP)
        user32.SetWindowPos(wintypes.HWND(self.main_hwnd),
                            _NOTOPMOST, 0, 0, 0, 0, _SWP)

    def _on_frame(self, hwnd, src, hdr, dst, dhdr, unc, clip, dirty):
        try:
            if src and hdr.stride and hdr.width and hdr.height:
                w, h, st = int(hdr.width), int(hdr.height), int(hdr.stride)
                row, need = w * 4, w * 4 * h
                with self._lock:
                    if self._buf is None or len(self._buf) < need:
                        self._buf = ctypes.create_string_buffer(need)
                    buf = self._buf
                    if st == row:
                        # 行紧凑：一次整块拷贝，不再逐行 Python 循环
                        ctypes.memmove(buf, src, need)
                    else:
                        addr = ctypes.addressof(buf)
                        for yy in range(h):
                            ctypes.memmove(addr + yy * row, src + yy * st, row)
                    self._frame = (w, h, buf)
                    self.version += 1
        except Exception:
            pass
        return True

    def pump(self):
        """在 UI 线程主动驱动全屏放大镜重采样（UpdateWindow 同步触发回调）。"""
        user32 = ctypes.windll.user32
        mag = ctypes.windll.magnification
        mag.MagSetWindowSource(self.mag_hwnd,
                               wintypes.RECT(0, 0, self.sw, self.sh))
        user32.InvalidateRect(self.mag_hwnd, None, False)
        user32.UpdateWindow(self.mag_hwnd)

    def render_jpeg(self, out_w=None, quality=72):
        """按主窗当前屏幕位置，从最新全屏帧裁剪背后区域，降采样并烘焙
        模糊，编码为 JPEG data URL。
        返回 (是否有变化, url或None, 版本号)。可从任意线程调用。"""
        rect = self._main_rect()
        with self._lock:
            if self._frame is None:
                return False, None, 0
            fw, fh, buf = self._frame
            ver = self.version
            if out_w is None:
                # 输出宽按主窗物理宽自适应：背景是预模糊环境光，按物理
                # 像素 2:1 降采样即可，彻底消除旧版固定 560 宽在 4K 屏被
                # 拉伸 6 倍造成的缩略图/马赛克感；上下限兼容低 DPI 窗口。
                rw = rect[2]
                out_w = max(640, min(1600, round(rw * 0.5)))
            key = (ver, rect, out_w, quality)
            if key == self._cache_key:
                return False, None, ver
            x, y, w, h = rect
            x0, y0 = max(0, x), max(0, y)
            x1, y1 = min(fw, x + w), min(fh, y + h)
            if x1 <= x0 or y1 <= y0:
                return False, None, ver
            from PIL import Image
            full = Image.frombuffer("RGBA", (fw, fh), buf, "raw", "BGRA", 0, 1)
            # crop 在锁内完成，得到独立图像，避免 buf 被 pump 复用
            crop = full.crop((x0, y0, x1, y1))
            cw = out_w
            ch = max(2, round(cw * crop.size[1] / crop.size[0]))
            small = crop.resize((cw, ch), Image.BILINEAR).convert("RGB")

        # 锁外：盒式霜化的有效遮挡宽度以“物理像素 48px”为目标（第一版
        # 560宽/k7 在 4K 上恰好≈48 物理像素），按当前输出宽与主窗物理宽
        # 反推 k——即霜化中间图保持约 80px 的绝对尺寸，与第一版一致；
        # 而最终输出仍为高分辨率+q72，不会重现当年的 JPEG 块/马赛克。
        # 再叠一道小高斯抹平盒式振铃。注：仅 liquid 调用，静态主题不受影响。
        phys_w = rect[2] or cw
        k = max(6, min(cw // 70, round(48 * cw / phys_w)))
        baked = small.resize(
            (max(1, cw // k), max(1, ch // k)), Image.BILINEAR
        ).resize((cw, ch), Image.BILINEAR)
        try:
            from PIL import ImageFilter
            frost_r = max(1, round(k * 0.2))   # 抹平盒式网格，随 k 等比
            baked = baked.filter(ImageFilter.GaussianBlur(frost_r))
        except Exception:
            pass
        # 轻微提色：环境彩光在玻璃边缘的折射更生动
        try:
            from PIL import ImageEnhance
            baked = ImageEnhance.Color(baked).enhance(1.15)
        except Exception:
            pass
        bio = io.BytesIO()
        baked.save(bio, format="JPEG", quality=quality)
        data = bio.getvalue()
        if data == self._prev_bytes:
            # 裁剪内容真正未变，不重复推送（静止画面零 JS 通信）
            self._cache_key = key
            return False, None, ver
        self._prev_bytes = data
        url = ("data:image/jpeg;base64,"
               + base64.b64encode(data).decode("ascii"))
        self._cache_key = key
        return True, url, ver


def _dpi_scale():
    """系统/主屏 DPI 缩放比例（不受调用线程 DPI context 影响的取法）。"""
    u = ctypes.windll.user32
    try:
        d = u.GetDpiForSystem()
        if d and d >= 96:
            return d / 96.0
    except Exception:
        pass
    try:
        shcore = ctypes.windll.shcore
        mon = u.MonitorFromWindow(wintypes.HWND(0), 1)  # MONITOR_DEFAULTTOPRIMARY
        dpx = wintypes.UINT()
        dpy = wintypes.UINT()
        shcore.GetDpiForMonitor(mon, 0, ctypes.byref(dpx), ctypes.byref(dpy))
        if dpx.value:
            return dpx.value / 96.0
    except Exception:
        pass
    return 1.0


class _BackdropDriver:
    """绑定方法形式的定时器回调（pythonnet netfx 对 bound method 转委托最稳）。"""

    def __init__(self, api, window, hwnd, user32):
        self.api = api
        self.cap = None        # 放大镜懒创建：仅液态玻璃模式才存在
        self.timer = None
        self.window = window
        self.hwnd = hwnd
        self.user32 = user32
        self.last_ver = 0
        self.n = 0

    def set_mode(self, mode):
        """UI 线程调用：进入液态玻璃才创建放大镜并启动泵，其余模式停止。"""
        if mode == "liquid":
            if self.cap is None:
                cap = AmbientCapture()
                cap.start(self.hwnd)
                self.cap = cap
                self.api.capture = cap
            if self.timer is not None:
                self.timer.Start()
        elif self.timer is not None:
            self.timer.Stop()

    def on_pump(self, sender, event):
        # 必须在 UI 线程：同步驱动放大镜更新最新帧
        try:
            # 主窗最小化时不采样（保留最后一帧背景，零开销）
            if self.cap is not None and not self.user32.IsIconic(self.hwnd):
                self.cap.pump()
        except Exception as ex:
            print("[GUI] pump 失败：", ex)

    def push_loop(self):
        # 后台线程：编码并通过 evaluate_js 推送（该同步调用禁止在 UI 线程执行）
        last = 0
        while True:
            time.sleep(0.2)              # 5fps 推送；环境光变化缓慢，足够流畅，
                                        # 静止画面字节去重零通信
            try:
                if self.cap is None or self.cap.version == last:
                    continue
                changed, url, ver = self.cap.render_jpeg()
                if changed and url:
                    self.window.evaluate_js(
                        "window._setBackdrop && window._setBackdrop("
                        + json.dumps(url) + ")"
                    )
                last = ver
            except Exception as ex:
                print("[GUI] 背景推送失败：", ex)
                time.sleep(0.5)


def main():
    if sys.platform == "win32":
        # PerMonitorV2 DPI 感知，保证物理坐标与放大镜裁剪准确
        try:
            if not ctypes.windll.user32.SetProcessDpiAwarenessContext(-4):
                raise OSError
        except Exception:
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()

    engine = EngineLoop()
    engine.start()
    api = Api(engine)
    api.cfg = core.load_config()          # 提前读取，外观在登录前即生效
    _mode = api.cfg.get("appearance", "dark")
    _bg = "#e9ecf5" if _mode == "light" else "#07080f"
    scale = _dpi_scale() if sys.platform == "win32" else 1.0
    window = webview.create_window(
        "TG 视频下载器",
        url=str(pathlib.Path(resource_dir()) / "index.html"),
        js_api=api,
        width=int(1180 * scale),
        height=int(780 * scale),
        min_size=(int(960 * scale), int(640 * scale)),
        transparent=False,                      # 玻璃完全由网页内 backdrop 层渲染
        background_color=_bg,
        frameless=False,
    )

    def on_shown():
        if sys.platform != "win32":
            window.evaluate_js("document.body.classList.add('fake-light')")
            return

        form = window.native
        user32 = ctypes.windll.user32

        def setup_ui():
            # 本函数在真正的 UI 线程执行（Timer 必须建在有消息循环的线程）
            hwnd = form.Handle.ToInt64()

            # 恢复 UI 线程 PerMonitorV2 context：CLR/WinForms 启动后可能把
            # UI 线程切到 GDI 缩放的 unaware context，导致放大镜只有逻辑分辨率
            user32.SetThreadDpiAwarenessContext.restype = wintypes.HANDLE
            user32.SetThreadDpiAwarenessContext.argtypes = [wintypes.HANDLE]
            _newctx = user32.SetThreadDpiAwarenessContext(wintypes.HANDLE(-4))
            print("[GUI] thread ctx ->", _newctx,
                  "DPI=", user32.GetDpiForSystem())

            try:
                import clr
                clr.AddReference("System.Windows.Forms")
                from System.Windows.Forms import Timer
                from System import EventHandler
                drv = _BackdropDriver(api, window, hwnd, user32)
                handler = EventHandler(drv.on_pump)
                timer = Timer()
                timer.Interval = 200       # 5fps 环境取光：背景帧每帧要解码+
                                           # 重光栅全屏层，5fps 较 10fps GPU 减半
                timer.Tick += handler
                drv.timer = timer
                threading.Thread(target=drv.push_loop,
                                 daemon=True).start()
                api._pump_timer = timer       # 保活
                api._drv = drv
                api._handler = handler
                api._form = form
                # 按已保存外观决定是否创建放大镜并启动泵
                drv.set_mode(api.cfg.get("appearance", "dark"))
                # 窗口在 unaware 线程创建（物理尺寸被虚拟化缩小3倍），线程
                # 恢复 V2 后需把物理尺寸补回，WebView2 才能得到 1180 CSS 视口
                s = user32.GetDpiForSystem() / 96.0
                if s > 1.0:
                    SWP_NOMOVE = 0x0002
                    SWP_NOZORDER = 0x0004
                    user32.SetWindowPos(
                        hwnd, 0, 0, 0,
                        int(1180 * s), int(780 * s),
                        SWP_NOMOVE | SWP_NOZORDER)
                user32.SetWindowTextW(hwnd, "TG 视频下载器")   # 恢复标题
            except Exception as e:
                print("[GUI] 取光组件初始化失败：", e)
                try:
                    window.evaluate_js(
                        "document.body.classList.add('fake-light')")
                except Exception:
                    pass

        # on_shown 在非 UI 线程触发，需把初始化 marshal 到 UI 线程同步执行
        from System import Action
        try:
            form.Invoke(Action(setup_ui))
        except Exception as e:
            print("[GUI] Invoke 到 UI 线程失败：", e)
            try:
                window.evaluate_js("document.body.classList.add('fake-light')")
            except Exception:
                pass

    window.events.shown += on_shown
    webview.start(debug=False)


if __name__ == "__main__":
    main()
