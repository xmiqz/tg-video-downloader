# Docker / NAS 部署手册

适用版本：v2.7.0 及后续兼容版本。服务名与容器名为 `tg-video-downloader`，Web 端口 `8787`，探活端点 `/healthz`。本文覆盖群晖 DSM、飞牛 fnOS 与通用 Docker 主机；只使用示例路径，不包含真实内网地址或凭据。

## 1. 通用要求

- CPU 架构：`linux/amd64` 或 `linux/arm64`，镜像同时提供两种 manifest。
- Docker 24+ 与 Compose v2（`docker compose` 子命令）。
- 固定以 `10001:10001` 运行，镜像内 `/data` 同属主。**不要用 PUID / PGID 覆盖**。
- 容器启用只读根文件系统、`cap_drop: ALL` 与 `no-new-privileges:true`，入口无法代为 chown；bind mount 必须先在宿主机授权。
- 默认命名卷 `tg-video-downloader-data` 继承镜像属主，无需手工 chown；NAS 推荐 bind mount 到应用目录。
- 内置 HTTP 无 TLS。仅本机或可信局域网直接访问；跨网络必须置于 HTTPS 反向代理之后。

## 2. 通用 Compose 与参数

在线 YAML（优先 jsDelivr，部分网络无法访问 `raw.githubusercontent.com`）：

```bash
docker compose -p tg-video-downloader -f https://cdn.jsdelivr.net/gh/xmiqz/tg-video-downloader@v2.7.0/compose.yaml up -d
```

仓库内 YAML：`./compose.yaml`。下面给出等价展开，便于检查环境变量：

```yaml
services:
  tg-video-downloader:
    image: ${TGVD_IMAGE:-ghcr.io/xmiqz/tg-video-downloader:v2.7.0}
    container_name: tg-video-downloader
    restart: unless-stopped
    user: "10001:10001"
    ports:
      - "${TGDL_PORT:-8787}:8787"
    environment:
      TGDL_HOST: 0.0.0.0
      TGDL_PORT: "8787"
      TGDL_DATA_DIR: /data
      TGDL_ALLOWED_ROOTS: /data
      TGDL_COOKIE_SECURE: "${TGDL_COOKIE_SECURE:-0}"
      TGDL_TRUST_PROXY: "${TGDL_TRUST_PROXY:-0}"
    volumes:
      - ${TGVD_DATA_DIR:-tgvd-data}:/data
    read_only: true
    tmpfs:
      - /tmp:size=64m,mode=1777
    cap_drop:
      - ALL
    security_opt:
      - no-new-privileges:true
```

### 参数表

| 变量 | 默认值 | 说明 |
| :-- | :-- | :-- |
| `TGVD_IMAGE` | `ghcr.io/xmiqz/tg-video-downloader:v2.7.0` | 镜像与标签；固定版本推荐 `v2.7.0`，自动更新用 `stable` |
| `TGVD_DATA_DIR` | 命名卷 `tgvd-data` | 宿主机绝对路径，映射到容器 `/data`；命名卷无需 chown，bind mount 必须 chown |
| `TGDL_PORT` | `8787` | 宿主机映射端口；容器内固定 `8787` |
| `TGDL_COOKIE_SECURE` | `0` | HTTPS 反向代理后设为 `1`；HTTP 直连保持 `0` |
| `TGDL_TRUST_PROXY` | `0` | 可信反向代理后设为 `1`，配合 `TGDL_TRUSTED_PROXIES` |
| `TGDL_TRUSTED_PROXIES` | 空 | 可信反代 IP / CIDR，逗号分隔；不要写 `/0` 或 `/8` 等超宽网段 |
| `TGDL_HOST` | 容器内 `0.0.0.0` | 容器监听地址；正式 Compose 已固定，不建议覆盖 |
| `TGDL_ALLOWED_ROOTS` | `/data` | 容器内允许读写的根目录；正式 Compose 已固定，不建议覆盖 |

宿主机目录与容器路径是两套概念：`TGVD_DATA_DIR=/volume1/docker/tg-video-downloader/data` 时，浏览器、备份脚本和登录命令里看到的仍是容器内 `/data`。所有 Telegram 会话、任务 JSON、下载文件都在 `/data` 下。配置优先级：命令行参数 > `TGDL_*` 环境变量 > `tg_config.json` / `server.json` > 默认值。

首次启动读令牌，随后设置管理员密码：

```bash
docker exec tg-video-downloader cat /data/.setup-token
curl -fsS http://127.0.0.1:8787/healthz
```

浏览器访问 `http://NAS地址:8787`。该密码只保护 Web 管理界面，不是 Telegram 两步验证密码。

## 3. 群晖 DSM / Container Manager

### 3.1 GUI 项目

1. 在 File Station 建目录 `docker/tg-video-downloader/data`（常见挂载为 `/volume1/docker/tg-video-downloader/data`）。
2. 用 SSH 登录 NAS，授权目录属主：

   ```bash
   sudo chown -R 10001:10001 /volume1/docker/tg-video-downloader/data
   ```

3. Container Manager → 项目 → 新增，项目名 `tg-video-downloader`，路径选 `docker/tg-video-downloader`，来源选“使用现有的 docker-compose.yml”或粘贴上面的 Compose。
4. 在项目的 `.env` 中写 `TGVD_DATA_DIR=/volume1/docker/tg-video-downloader/data`；如需改端口再加 `TGDL_PORT=8787`。
5. 启动项目，确认容器状态为运行中，端口映射 `8787`。
6. 首次启动读 `/data/.setup-token`，浏览器打开 `http://NAS地址:8787` 完成初始化。

GUI 改镜像标签：编辑项目 `.env` 的 `TGVD_IMAGE`，重新构建/启动项目。不要在 GUI 里直接改容器用户或去掉只读根。

### 3.2 SSH Compose

```bash
mkdir -p /volume1/docker/tg-video-downloader/data
sudo chown -R 10001:10001 /volume1/docker/tg-video-downloader/data
cd /volume1/docker/tg-video-downloader
curl -fsSLo compose.yaml https://cdn.jsdelivr.net/gh/xmiqz/tg-video-downloader@v2.7.0/compose.yaml
printf 'TGVD_DATA_DIR=%s\n' /volume1/docker/tg-video-downloader/data > .env
docker compose up -d
docker compose ps
docker exec tg-video-downloader cat /data/.setup-token
```

### 3.3 升级与回滚

```bash
# 升级（先备份 /data，见第 6 节）
cd /volume1/docker/tg-video-downloader
docker compose pull && docker compose up -d
curl -fsS http://127.0.0.1:8787/healthz

# 回滚到 v2.6.2（数据目录不变）
printf 'TGVD_DATA_DIR=%s\nTGVD_IMAGE=%s\n' \
  /volume1/docker/tg-video-downloader/data \
  ghcr.io/xmiqz/tg-video-downloader:v2.6.2 > .env
docker compose up -d
```

## 4. 飞牛 fnOS

### 4.1 Docker 应用

1. 在共享目录建 `docker/tg-video-downloader/data`，示例路径 `/vol1/1000/docker/tg-video-downloader/data`。
2. 通过 SSH 或终端授权：

   ```bash
   sudo chown -R 10001:10001 /vol1/1000/docker/tg-video-downloader/data
   ```

   飞牛部分共享目录权限继承较严；若容器仍报 `Permission denied`，可先放宽共享目录 ACL 为可读写（必要时临时 `chmod 777 <data目录>` 做排查），再执行上面的 `chown`。排查完成后应收紧权限，不要长期把会话文件暴露给其他用户。

3. Docker 应用 → 项目 / Compose → 新建，路径选 `/vol1/1000/docker/tg-video-downloader`，粘贴通用 Compose，端口 `8787`。
4. `.env` 写 `TGVD_DATA_DIR=/vol1/1000/docker/tg-video-downloader/data`。
5. 启动后读令牌、设置管理员密码，浏览器访问 `http://NAS地址:8787`。

### 4.2 SSH / 终端 Compose

```bash
mkdir -p /vol1/1000/docker/tg-video-downloader/data
sudo chown -R 10001:10001 /vol1/1000/docker/tg-video-downloader/data
cd /vol1/1000/docker/tg-video-downloader
curl -fsSLo compose.yaml https://cdn.jsdelivr.net/gh/xmiqz/tg-video-downloader@v2.7.0/compose.yaml
printf 'TGVD_DATA_DIR=%s\n' /vol1/1000/docker/tg-video-downloader/data > .env
docker compose up -d
docker compose ps
docker exec tg-video-downloader cat /data/.setup-token
```

### 4.3 升级与回滚

与群晖 SSH Compose 相同；飞牛注意镜像拉取走 arm64 manifest。若 GHCR 出现 TLS / 网络瞬断，先重试 `docker compose pull`，再确认 `docker manifest inspect ghcr.io/xmiqz/tg-video-downloader:v2.7.0` 包含 `linux/arm64`。只有镜像仓库不可达时才考虑从另一台设备转存，不推荐作为常态。

## 5. 自动更新

**GHCR 本身不是更新器。** 它只保存镜像；已运行的容器不会自动更换镜像标签。固定 `v2.7.0` 标签永远指向同一版本，不会自动变化。要实现自动更新，必须切换到移动标签 `stable`（或 `latest`）并使用更新器。

### 5.1 手动更新

```bash
docker compose pull && docker compose up -d
curl -fsS http://127.0.0.1:8787/healthz
```

### 5.2 Watchtower

```yaml
services:
  watchtower:
    image: containrrr/watchtower:latest
    container_name: watchtower
    restart: unless-stopped
    command: --schedule "0 0 4 * * *" --cleanup --label-enable
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    environment:
      WATCHTOWER_LABEL_ENABLE: "true"
```

在 `tg-video-downloader` 服务上增加标签：

```yaml
labels:
  com.centurylinklabs.watchtower.enable: "true"
```

并把 `TGVD_IMAGE` 切到移动标签：

```bash
printf 'TGVD_IMAGE=%s\n' ghcr.io/xmiqz/tg-video-downloader:stable > .env
docker compose up -d
```

安全警告：`/var/run/docker.sock` 等价于宿主机 Docker 管理权限，只应在可信 NAS 上使用，并限制镜像来源。更新容器前仍应先备份 `/data`。`stable` 由正式发布工作流维护。

### 5.3 Diun 只通知

Diun 能检测 `stable` / 固定标签的 digest 变化并发送通知，但**不会自动更新容器**。收到提醒后仍需手动执行 `docker compose pull && docker compose up -d`，或由 Watchtower 完成更新。

## 6. 备份、恢复与回滚

升级前先停容器再备份，保证任务 JSON 与下载文件一致。命名卷与 bind mount 的完整命令见 [NAS 备份 / 升级 / 回滚手册](./nas-backup-restore.md)。最小检查点：

- 容器内 `/data` 是唯一用户数据根；不要删除命名卷 `tg-video-downloader-data` 或 bind 目录。
- `.session`、`auth.json`、`.setup-token` 等同登录凭据，备份包必须离线保存。
- 回滚只换镜像 tag，不回退 `/data`；`v2.6.2` 镜像保留时可快速回滚。
- 更新后核对 `/healthz` 的版本号与构建 SHA 是否与 Tag 对应。

## 7. HTTPS 反向代理

内置 HTTP 无 TLS。Caddy / nginx 片段与 `TGDL_TRUST_PROXY` / `TGDL_COOKIE_SECURE` 设置见 [NAS 反向代理部署手册](./nas-reverse-proxy.md)。反代通过后必须设置 `TGDL_COOKIE_SECURE=1`，并只把真实反代地址写入 `TGDL_TRUSTED_PROXIES`。

## 8. 常见错误

| 症状 | 原因 | 处理 |
| :-- | :-- | :-- |
| `Permission denied` / 无法创建 `/data/downloads` | bind mount 属主不是 `10001:10001` | 宿主机执行 `chown -R 10001:10001 <data目录>`；飞牛必要时先修共享目录 ACL |
| 端口 `8787` 打不开 | 端口被占用、未映射或代理未放行 | `docker compose ps`，检查 `TGDL_PORT` 与 NAS 防火墙 |
| `manifest unknown` | 镜像 Tag 写错或仓库不可达 | 核对 `TGVD_IMAGE`，重试 `docker compose pull`，`docker manifest inspect` 看 amd64/arm64 |
| GHCR TLS / 网络瞬断 | 出口网络到 GHCR 不稳定 | 重试拉取；仅仓库不可达时从另一台设备转存镜像 |
| 升级后仍是旧版本 | 使用固定 `v2.7.0` 且未执行 pull/up，或 `.env` 覆盖了 `TGVD_IMAGE` | 执行 `docker compose pull && docker compose up -d`，检查 `.env` |
| `latest` 行为不确定 | 生产环境不建议跟随移动标签 | 固定版本用 `v2.7.0`；需要自动更新用 `stable` |
| Cookie 丢失 / 反代后需重新登录 | `TGDL_COOKIE_SECURE` 未开或可信代理白名单不对 | 设为 `1` 并按反代手册修正 `TGDL_TRUSTED_PROXIES` |
