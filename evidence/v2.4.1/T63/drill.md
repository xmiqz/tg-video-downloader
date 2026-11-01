# T63 容器升级 / 回滚演练记录

- 日期：2026-11-01
- worktree：`c:\Users\xqz\Documents\trae_projects\dayly\_arm_inspect\repo\tgvd-v25-t63`（分支 `v2.5-dev-t63`，基线 `f1923a2`）
- 演练对象：`compose.yaml` 形态的 NAS 常驻部署，镜像 `ghcr.io/xmiqz/tg-video-downloader`，服务名/容器名 `tg-video-downloader`，数据卷 `/data`（默认命名卷 `tg-video-downloader-data`）

## 0. 环境限制（阻塞项，如实声明）

**本机（Windows ARM64）没有安装 Docker，真机三段式演练（旧镜像 → 新镜像 → 回滚旧镜像）未执行。**
本记录不包含任何伪造的容器执行输出；容器行为面仅给出可在 NAS 上逐步照抄执行的
命令清单（第 3 节）与预期矩阵（第 2 节），待有 Docker 的节点（飞牛 fnOS
192.168.2.110 / 群晖 DSM 192.168.2.111 / 本机 Docker Desktop amd64）按清单补跑。
本机已完成并可复现的部分：compose 契约核对、契约测试（18 项）及其造红验证（14
个破坏用例全部命中）、pytest / node 门规（第 4 节，真实输出）。

## 1. 演练前提核对（本机静态核对，已完成）

| 契约项 | compose.yaml 现状 | 结论 |
| --- | --- | --- |
| `restart: unless-stopped` | line 5 | 在位 |
| healthcheck 打 `http://127.0.0.1:8787/healthz` | line 39-48 | 在位 |
| 默认命名卷 `tgvd-data:/data`、卷名 `tg-video-downloader-data` | line 30, 50-52 | 在位 |
| `read_only: true` + 唯一可写 tmpfs `/tmp` | line 31-33 | 在位 |
| `cap_drop: ALL`、`no-new-privileges:true`、`user` 默认 `10001:10001` | line 34-37, 8 | 在位 |
| 镜像固定版本 tag（`${TGVD_IMAGE:-ghcr.io/xmiqz/tg-video-downloader:v2.4.0}`），全文无 `:latest` | line 3 | 在位 |
| `TGDL_*` 环境变量名与 `server_config.py` 逐字一致（六变量全部命中） | line 11-19 | 在位 |

**结论：compose 契约无缺口，compose.yaml 零改动**；本任务交付为契约测试 +
备份/升级/回滚文档 + 本演练记录。上述核对由 `tests/test_compose_contract.py`
（18 项断言）固化，任何人改坏任一项都会红。

## 2. 升级 / 回滚预期矩阵

数据面（任务库、下载文件）从不被升级/回滚触碰：容器重建只换镜像，`/data`
命名卷原样保留。登录状态受 v2.4.1「记住此设备」（T60）影响，分三种情况：

| 阶段 | 操作 | 任务库（`/data/tasks/*.json`） | 下载文件（`/data/downloads/`） | `auth.json` | 登录状态预期 |
| --- | --- | --- | --- | --- | --- |
| ① 旧镜像 v2.4.0 | —（基线） | 基线 | 基线 | 旧键 | 已登录会话在 |
| ② 新镜像 v2.4.1 | 备份 → pull → up -d | **完整保留** | **完整保留** | 追加 `session_key` / `revoked_sessions` | 勾选「记住此设备」→ **免重登**（长效会话被识别）；未勾选 → **需重登一次**（内存会话本就不跨重启，与升级无关） |
| ③ 回滚 v2.4.0 | 改回旧 tag → up -d | **完整保留** | **完整保留** | 新键仍在，旧版读取时忽略未知键 | 勾选「记住此设备」签发的长效会话**不被旧版识别 → 需重登一次**（原密码有效）；未勾选 → 需重登（本就不跨重启，无额外损失） |

矩阵依据：
- 数据不迁移：v2.4.1 仅向 `auth.json` 追加 `session_key` / `revoked_sessions`，
  任务 JSON 顶层 7 键（`id/created/channel/channel_title/params/folder/items`，
  `tg_video_dl.py:3866`）与搜索 meta 13 键不变，`/data/downloads` 位置不变。
- 旧版忽略未知键：`server_auth.py` 读取 `auth.json` 时只校验必需键子集
  （`version/salt/iterations/digest`，`server_auth.py:75-77`），不拒绝未知键。
- 升级/回滚后 `curl /healthz` 应返回 `{"ok": true, "service": "tg-video-downloader"}`（`server_app.py:337-342`）。

## 3. NAS 真机演练命令清单（待补跑，未执行）

以下命令与 `docs/nas-backup-restore.md` 逐字一致，可在 NAS 部署目录
（存放 `compose.yaml` 的目录）按顺序照抄执行。**每一步执行后把实际输出
补记到本节末尾，不要删除本节标题与说明。**

### 阶段 0：升级前备份（保证可回退）

```bash
docker compose ps
docker compose stop tg-video-downloader
docker run --rm \
  -v tg-video-downloader-data:/data:ro \
  -v "$(pwd)":/backup \
  alpine:3.20 tar czf /backup/tgvd-backup-$(date +%Y%m%d).tar.gz -C /data .
docker compose start tg-video-downloader
curl -fsS http://127.0.0.1:8787/healthz
```

### 阶段 1：升级到新镜像

```bash
echo 'TGVD_IMAGE=ghcr.io/xmiqz/tg-video-downloader:v2.4.1' > .env
docker compose pull
docker compose up -d
curl -fsS http://127.0.0.1:8787/healthz
```

浏览器打开 `http://NAS地址:8787`：核对任务库清单完整、下载文件可访问；
分别用「勾选记住此设备」与「不勾选」两种方式登录一次并重启容器
（`docker compose restart`），核对第 2 节矩阵 ② 行的登录预期。

### 阶段 2：回滚旧镜像

```bash
echo 'TGVD_IMAGE=ghcr.io/xmiqz/tg-video-downloader:v2.4.0' > .env
docker compose pull
docker compose up -d
curl -fsS http://127.0.0.1:8787/healthz
```

浏览器核对第 2 节矩阵 ③ 行：任务库完整、下载文件可访问、被要求重新登录
属预期（原密码有效）。随后可补记容器安全面证据：

```bash
docker inspect tg-video-downloader --format \
  '{{.HostConfig.ReadOnlyRootfs}} {{.HostConfig.CapDrop}} {{.Config.User}} {{.State.Health.Status}}'
```

预期输出：`true [ALL] 10001:10001 healthy`（PUID/PGID 未改时）。

### 验证点核对表（真机补跑时逐项打勾）

- [ ] 阶段 1 后：任务库清单完整保留
- [ ] 阶段 1 后：下载文件可访问
- [ ] 阶段 1 后：勾选「记住此设备」的浏览器免重登（重启容器后仍 200）
- [ ] 阶段 1 后：未勾选的浏览器需重登一次（预期行为）
- [ ] 阶段 2 后：任务库清单完整保留
- [ ] 阶段 2 后：下载文件可访问
- [ ] 阶段 2 后：勾选「记住此设备」的浏览器需重登一次（预期行为，原密码有效）
- [ ] 阶段 2 后：`docker inspect` 输出 `ReadOnlyRootfs=true / CapDrop=[ALL] / healthy`

## 4. 本机已执行部分的真实输出片段

契约测试单独复跑（门规之一）：

```
$ python -m pytest tests/test_compose_contract.py -q
18 passed, 1 warning in 0.04s
```

全量 pytest（门规之一，基线 238 + 新增 18）：

```
$ python -m pytest tests -q
256 passed, 2 warnings in 9.74s
```

RPC 数（门规之一）：

```
$ python -c "import server_app; print(len(server_app.RPC_METHODS))"
56
```

前端门规（66 passed + 1 todo + 0 failed，与基线一致）：

```
$ node --test "tests/js/*.test.mjs"
ℹ tests 67
ℹ pass 66
ℹ fail 0
ℹ todo 1
```

契约测试造红验证（测试有效性，14 个破坏用例全部命中预期 FAIL；
行尾耗时尾巴略）：

```
$ python .temp/t63_redcheck.py   （一次性脚本，已删除）
[baseline] 18 passed
[PASS] restart 改 always -> test_restart_unless_stopped | 1 failed, 17 passed
[PASS] healthcheck URL 改掉 healthz 路径 -> test_healthcheck_probes_loopback_healthz | 1 failed, 17 passed
[PASS] read_only 改 false -> test_readonly_rootfs | 1 failed, 17 passed
[PASS] no-new-privileges 改掉 -> test_no_new_privileges | 1 failed, 17 passed
[PASS] cap_drop 整块删掉 -> test_cap_drop_all | 1 failed, 17 passed
[PASS] user 默认改 0 -> test_unprivileged_user_default_10001 | 1 failed, 17 passed
[PASS] 镜像默认 tag 改 :latest -> test_image_pinned_to_versioned_tag | 1 failed, 17 passed
[PASS] 环境变量拼错 TGDL_DATA_DIRR -> test_tgdl_env_names_match_server_config | 1 failed, 17 passed
[PASS] 服务加第二个挂载 /srv -> test_service_mounts_only_named_volume_at_data | 1 failed, 17 passed
[PASS] tmpfs 改到 /var/tmp -> test_only_writable_tmpfs_is_tmp | 1 failed, 17 passed
[PASS] 顶层卷名改掉 -> test_named_volume_declared_once_with_fixed_name | 1 failed, 17 passed
[PASS] 文档删「回滚」小节标题 -> test_backup_doc_required_sections | 1 failed, 17 passed
[PASS] 文档删「兼容」小节标题 -> test_backup_doc_required_sections | 1 failed, 17 passed
[PASS] README 链接目标改错 -> test_readme_links_backup_doc | 1 failed, 17 passed
[restored] 18 passed
[sha256] 全部与初始一致
[verdict] 造红验证全部命中：契约测试具备杀伤力
```

## 5. 结论

- compose 契约核对与测试固化：**完成**（18 项断言全绿，14 个破坏用例造红全命中）。
- 备份 / 升级 / 回滚 / 兼容文档：**完成**（`docs/nas-backup-restore.md`）。
- 真机三段式演练：**未执行（本机无 Docker，阻塞项）**，命令清单已备好，
  待 NAS 节点补跑后在本文件补记输出与勾选核对表。

T63 结论：PASS（35 项）
