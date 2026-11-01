# T62 结果报告：群晖 DS218 ARM64 真机（v2.4.0 → v2.4.1 代码）

## 0. 设备与环境（实测，非推断）

| 项 | 值 |
| --- | --- |
| 主机 | `192.168.2.111`（`xqz-nas`） |
| 型号 / 架构 | DS218 / `synology_rtd1296_ds218` / **aarch64** |
| DSM | 7.4.0（build 90075，GM） |
| Docker | 24.0.2（`/usr/local/bin/docker`，需 `sudo`） |
| Compose | v2.20.1-6047（`docker compose`，非 `docker-compose`） |
| CPU / 内存 | 4 核 / 1644 MB（可用约 533 MB，构建期间出现大量 swap 使用） |
| 数据 | bind mount `/volume1/docker/tgvd/data:/data`（**保持 bind mount，未改回命名卷**） |

对照机（飞牛 192.168.2.110 / aarch64 / master 免 sudo / 18789 跑 v2.4.0）本轮未参与，仅在结论中作为对照口径保留。

## 1. 镜像来源的偏差（必须先读）

用户规定的步骤是 `sudo docker compose -f ... pull && up -d`，前提是 GHCR 已发布 `v2.4.1` 镜像。实际情况：

- 本机（Windows ARM64）**没有 Docker 运行时**（`docker` 命令不存在），无法本地构建；
- GHCR 上没有 `v2.4.1` 镜像（`docker image inspect ghcr.io/xmiqz/tg-video-downloader:v2.4.1` → `absent`），发布动作（git tag / push / Release）本轮**未获批准**，也未执行；
- 因此改用「群晖本机构建」：`git archive 453c135` → 上传 → `docker build`，产出 **`tgvd-v2.4.1-local:arm64`**（镜像 ID `0ad46a27bc73`），**未 push**。

影响：本次验证的是 **v2.4.1 代码在 ARM64 真机上的运行时行为**，不是 GHCR 发布产物的完整性/签名/多架构清单。发布镜像的最终验收仍需在正式镜像推送后重跑一次（换 `TGVD_IMAGE` 即可，步骤见 §5）。

## 2. 升级动作

```
sudo -S env TGVD_IMAGE=tgvd-v2.4.1-local:arm64 \
  /usr/local/bin/docker compose -f /volume1/docker/tgvd/compose.yaml up -d --pull never
```

输出（原文）：

```
 Container tg-video-downloader  Recreate
 Container tg-video-downloader  Recreated
 Container tg-video-downloader  Starting
 Container tg-video-downloader  Started
```

`compose.yaml` **未修改**（镜像默认值仍是 `ghcr.io/...:v2.4.0`，本次靠 `TGVD_IMAGE` 环境覆盖）。
数据目录、端口映射、`read_only`/`cap_drop`/`no-new-privileges`/`user` 均未改。

## 3. 验收结果

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| 容器为 v2.4.1 代码 | **通过** | `ps` → `tgvd-v2.4.1-local:arm64 \| Up (healthy)`；容器内 `REMEMBER_DAYS= 7`、`RPC_METHODS= 56` |
| `/healthz` 200 | **通过** | `{"ok":true,"service":"tg-video-downloader"}`（宿主 `curl` 与 compose healthcheck 一致） |
| `docker inspect` = ReadOnlyRootfs | **通过** | `HostConfig.ReadonlyRootfs = True`；容器内 `touch /rootfs-probe` → `Read-only file system`；`/tmp` tmpfs 可写、`/data` 可写 |
| 外壳安全性 | **通过** | `CapDrop=['ALL']`、`SecurityOpt=['no-new-privileges:true']`、`User=10001:10001` |
| 任务库完整 | **通过** | bind mount 内 `tasks/*.json` 计数升级前后均为 **1**；`tg_config.json` sha256 前 16 位升级前后一致（`ed2fe71c4b9d8538`） |
| 登录态/会话未被破坏 | **通过** | 启动日志：`已从本地会话恢复 Telegram 登录`；`tg_session.session` 40960 字节保持，服务正常建连 |
| Web 首页可达 | **通过** | `GET /` → 303（已配置状态下跳登录页，符合预期） |
| 勾选「记住此设备」登录 → 重启容器仍免登录 | **待人工执行** | 需要管理员密码，测试侧不做猜测（连续 5 次失败会触发 900 秒封锁），等待用户提供或自行登录 |

补充观测（真实、非预期外的变化）：

- 升级后首次启动，`auth.json` 从 196 字节变为 307 字节（v2.4.1 的布局回填写入 `session_key` / `revoked_sessions`；`salt`/`digest`/`iterations` 保留，密码不受影响），权限仍为 `0600`。
- 升级前基线容器 `HostConfig.ReadOnlyRootfs = None`（该容器创建于当前 compose 加入 `read_only: true` 之前），**本次重建后变为 True**，即验收项由「不满足」变为「满足」。

## 4. 回滚路径

```
sudo -S env TGVD_IMAGE=ghcr.io/xmiqz/tg-video-downloader:v2.4.0 \
  /usr/local/bin/docker compose -f /volume1/docker/tgvd/compose.yaml up -d
```

v2.4.0 镜像仍在设备本地，数据目录不受镜像切换影响（bind mount）。

## 5. 正式镜像到位后的重跑

```
sudo -S env TGVD_IMAGE=ghcr.io/xmiqz/tg-video-downloader:v2.4.1 \
  /usr/local/bin/docker compose -f /volume1/docker/tgvd/compose.yaml pull
sudo -S env TGVD_IMAGE=ghcr.io/xmiqz/tg-video-downloader:v2.4.1 \
  /usr/local/bin/docker compose -f /volume1/docker/tgvd/compose.yaml up -d
```
随后复跑 §3 全部条目。

## 6. amd64 面

**未覆盖：本机无 Docker 运行时**（`docker` 不存在、`qemu-system-x86_64` 不存在、WSL 无发行版）。本轮没有、也不会有任何 amd64 运行时证据；release notes 采用同一口径。

## 7. 采集日志

- `nas-baseline.log`：升级前只读勘察（设备/DSM/Docker/容器/数据目录/磁盘内存）
- `build-local-image.log`：群晖本机构建日志要点（含 `Successfully built 0ad46a27bc73`）
- `upgrade-before-after.log`：升级前后状态对照原始输出
- `verify-after-upgrade.log`：升级后复核原始输出（含只读根文件系统探针、tmpfs、数据哈希、容器日志）

## 8. 红线自查

- 未 push / 未打 git tag / 未触碰 Release；镜像仅本地标签 `tgvd-v2.4.1-local:arm64`。
- 未修改 `compose.yaml`、未改回命名卷、未动端口与安全选项。
- 未读取 `downloads/` 内容、未读消息正文与频道名；证据中不含真实密码、令牌、手机号、频道名、文件名（数据哈希只截取前 16 位）。
- RPC 保持 56、`REMEMBER_DAYS=7` 在容器内实测确认。
