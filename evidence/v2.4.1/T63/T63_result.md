# T63 收口报告：容器升级/回滚演练与 compose 契约

- 任务：T63 容器升级/回滚演练与 compose 契约（v2.4.1 收口补丁，批次 A）
- worktree：`c:\Users\xqz\Documents\trae_projects\dayly\_arm_inspect\repo\tgvd-v25-t63`
- 分支：`v2.5-dev-t63`，基线 commit：`f1923a2`（开工核对：`git status --short` 为空、HEAD 为 `f1923a2`）
- 日期：2026-11-01
- 解释器：`C:\Users\xqz\AppData\Local\Programs\Python\Python312-arm64\python.exe`；git：`C:\Users\xqz\tools\PortableGit\bin\git.exe`

## 1. 交付物与 git 原文

### git status --short 原文

```
 M README.md
?? docs/
?? evidence/
?? tests/test_compose_contract.py
```

### git diff --numstat 原文

```
3	0	README.md
```

### 文件清单

| 文件 | 性质 | 说明 |
| --- | --- | --- |
| `compose.yaml` | **零改动** | 契约核对 8 项全部在位，无缺口，不重排（见 §3） |
| `README.md` | +3/-0 | NAS / Docker 章节插入一行备份手册链接，原句与上下文零改动 |
| `tests/test_compose_contract.py` | 新增 | 18 项契约断言，纯 pathlib+正则，零新依赖 |
| `docs/nas-backup-restore.md` | 新增 | 备份 / 升级 / 回滚 / 数据兼容边界手册，命令可照抄 |
| `evidence/v2.4.1/T63/{T63_result.md, drill.md, pytest.log, node.log}` | 新增 | 全部 `*.md` / `*.log`，无 `*.txt` |

## 2. 契约断言 ↔ 测试名对照（tests/test_compose_contract.py，18 项）

| # | 测试名 | 断言的契约 |
| --- | --- | --- |
| 1 | `test_no_tab_indentation` | compose.yaml 全文无 tab 缩进 |
| 2 | `test_top_level_keys_are_services_and_volumes` | 顶层 key 恰为 `services:` + `volumes:` |
| 3 | `test_single_service_named_tg_video_downloader` | 恰一个服务 `tg-video-downloader`；`container_name` 同名 |
| 4 | `test_restart_unless_stopped` | `restart: unless-stopped` |
| 5 | `test_healthcheck_probes_loopback_healthz` | healthcheck 探活 `http://127.0.0.1:8787/healthz` |
| 6 | `test_port_pinned_to_8787` | 端口映射默认 `"${TGDL_PORT:-8787}:8787"` |
| 7 | `test_named_volume_declared_once_with_fixed_name` | 顶层 `volumes:` 恰一个具名卷 `tgvd-data:`，卷名固定 `tg-video-downloader-data` |
| 8 | `test_service_mounts_only_named_volume_at_data` | 服务 volumes 恰一条 `tgvd-data:/data` |
| 9 | `test_readonly_rootfs` | `read_only: true` |
| 10 | `test_only_writable_tmpfs_is_tmp` | tmpfs 恰一条且目标为 `/tmp`（除 /data 外唯一可写点） |
| 11 | `test_cap_drop_all` | `cap_drop` 列表恰 `ALL` |
| 12 | `test_no_new_privileges` | `security_opt` 恰 `no-new-privileges:true` |
| 13 | `test_unprivileged_user_default_10001` | `user` 默认 `"${PUID:-10001}:${PGID:-10001}"` |
| 14 | `test_image_pinned_to_versioned_tag` | 默认 tag 固定为 `ghcr.io/xmiqz/tg-video-downloader:vX.Y.Z`，全文禁止 `:latest` |
| 15 | `test_tgdl_env_names_match_server_config` | compose 每个传入的 `TGDL_*` 变量名都被 `server_config.py` 的 `os.environ.get("...")` 逐字读取；六契约变量齐备 |
| 16 | `test_readme_links_backup_doc` | README 有指向 `./docs/nas-backup-restore.md` 的有效 Markdown 链接（钉链接目标形态，正文提及不算） |
| 17 | `test_backup_doc_required_sections` | 文档存在且 `##` 级小节含「备份 / 升级 / 回滚 / 兼容」四关键词（排除 h1 短路） |
| 18 | `test_backup_doc_repo_names_are_exact` | 文档含 `tg-video-downloader`、`tg-video-downloader-data`、`/data`、`8787`、`/healthz`，并点名 `session_key`/`revoked_sessions` 与「记住此设备」 |

测试实现：不引入 PyYAML 等新依赖，仅 `pathlib` + `re` + 模块内极简缩进解析
辅助（`_load_lines` / `_block` / `_service_lines` / `_service_scalar` / `_list_items`）；
全部断言只读仓库文件，无 Docker、无网络即可通过。

### 造红验证（测试有效性）

14 个破坏用例逐项改文件 → 跑测试 → 恢复 → sha256 核对，**14/14 全部命中预期 FAIL**，
恢复后 18 passed、三文件 sha256 与初始一致。覆盖：restart 改 always、healthcheck
改路径、read_only 改 false、no-new-privileges 改 false、cap_drop 删块、user 改 0、
tag 改 :latest、变量拼错、加第二挂载、tmpfs 改 /var/tmp、卷名改掉、文档删「回滚」/
「兼容」小节标题、README 链接目标改错。详见 `drill.md` §4。

## 3. 形状备忘：compose 契约核对结论

**compose.yaml 零改动。** 逐条核对（基线 `f1923a2` 的 `compose.yaml`）：

| 契约项 | 现状位置 | 结论 |
| --- | --- | --- |
| `restart: unless-stopped` | line 5 | 在位 |
| healthcheck 打 `http://127.0.0.1:8787/healthz` | line 39-48 | 在位 |
| 默认命名卷 `tgvd-data` → `/data`，卷名 `tg-video-downloader-data` | line 30, 50-52 | 在位 |
| `read_only: true`；除 `/data` 外仅既有 `/tmp` tmpfs 可写 | line 31-33 | 在位 |
| `cap_drop: ALL`、`no-new-privileges:true`、`user` 默认 `10001:10001` | line 34-37, 8 | 在位 |
| 镜像固定 tag `${TGVD_IMAGE:-ghcr.io/xmiqz/tg-video-downloader:v2.4.0}`，无 `:latest` | line 3 | 在位 |
| 环境变量名与 `server_config.py` 逐字一致（`TGDL_HOST/TGDL_PORT/TGDL_DATA_DIR/TGDL_ALLOWED_ROOTS/TGDL_COOKIE_SECURE/TGDL_TRUST_PROXY`） | compose line 11-19 ↔ `server_config.py:197/201/207/214/219/132` | 六个全中 |

依据任务书「仅当契约缺口确实存在才改；现状已满足的字段不要无意义重排」，
故不改；由第 2 节测试固化，防止后续任务改坏。

## 4. 三条门规命令原文片段（worktree 内实际执行）

全量 pytest（基线 238 + 新增 18 = 256，全绿）：

```
$ python -m pytest tests -q
256 passed, 2 warnings in 9.74s
```

契约测试单独复跑：

```
$ python -m pytest tests/test_compose_contract.py -q
18 passed, 1 warning in 0.04s
```

前端门规（与基线 66 passed + 1 todo + 0 failed 一致）：

```
$ node --test "tests/js/*.test.mjs"
ℹ tests 67
ℹ pass 66
ℹ fail 0
ℹ todo 1
```

RPC 数（红线：恒 56）：

```
$ python -c "import server_app; print(len(server_app.RPC_METHODS))"
56
```

日志原件：`pytest.log`（含上述前两条 + RPC 数）、`node.log`。

## 5. 演练记录

`drill.md`（本目录）：
- 「旧镜像 → 新镜像 → 回滚旧镜像」预期矩阵：任务库/下载文件三阶段全保留；
  登录状态 remember=true 升级后保留、回滚后需重登一次；remember=false
  本就不跨重启（升级/回滚后均需重登，无额外损失）。
- **本机无 Docker，真机演练未执行（阻塞项）**，已如实标注，未伪造任何
  容器执行输出；NAS 可照抄的三阶段命令清单与验证点核对表已备好，
  待有 Docker 节点补跑后回填。
- 契约测试与 pytest 实际输出片段已收录。

## 6. 偏差清单

1. **真机演练未执行（阻塞项）**：本机无 Docker（任务书第 3 节第 5 条允许，
   要求如实标注）。drill.md 按要求登记「环境无 Docker，真机演练未执行
   （阻塞项）」并列出 NAS 命令清单；未伪造任何输出。
2. **并行任务结构事实**：T60（会话持久化）在独立 worktree 实施，本
   worktree 的 `server_auth.py` 尚无 `session_key` / `revoked_sessions` 键。
   `docs/nas-backup-restore.md` 的兼容边界按任务书给定的 v2.4.1 目标口径
   撰写（描述发版态），契约测试不触碰 `server_auth.py`（该文件属 T60
   交付域），避免跨 worktree 文件依赖。
3. **过程记录（非交付缺陷）**：造红验证阶段揪出 2 处测试初稿缺陷并已在
   交付版本修复—— 文档小节断言被 h1 标题（含全部关键词）短路，
   已改为只认 `##` 级小节； README 链接断言过弱（正文提及文件名即过），
   已改为钉住 Markdown 链接目标形态 `](./docs/nas-backup-restore.md)`。
   两者均通过破坏用例红态亲见确认。
4. 其余无偏差：零新依赖、零 RPC 增删、零 commit/add/push/tag（提交由
   调用方复核后执行）、未触碰 `server_app.py`/`server_auth.py`/
   `server_config.py`/`web/`/`Dockerfile`/其他测试文件。

## 7. 隐私自检

`evidence/v2.4.1/T63/` 四文件扫描 `api_hash|api_id|+86|手机号|t.me|频道名`：
无 token、无手机号、无频道名、无真实下载文件名；命中的 `xqz` 仅为任务书
worktree 路径所含本机用户名（环境信息，非红线敏感项）。未读取
`TG视频下载器数据\downloads` 及任何下载内容。

T63 结论：PASS（35 项）

> 35 项构成：契约断言 18 项全绿 + 造红破坏用例 14 项全命中 + 全量 pytest
> 256 passed 1 项 + node 66 passed + 1 todo + 0 failed 1 项 + RPC_METHODS 56
> 恒定 1 项。真机三段式演练 1 项为阻塞登记（本机无 Docker，命令清单已备，
> 待 NAS 节点补跑回填 drill.md），不计入 PASS 项。

T63_DONE
