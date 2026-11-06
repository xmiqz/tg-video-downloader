# NAS 备份 / 升级 / 回滚手册（Docker compose 部署）

> 适用：v2.4 起 `compose.yaml` 部署形态（任何装有 Docker 的 NAS 或服务器）。服务名/容器名 `tg-video-downloader`，端口 `8787`，全部
> 用户数据都在容器内 `/data`（默认命名卷 `tg-video-downloader-data`）。
> 本文所有命令可直接照抄执行。

## 数据都在 /data 里

升级、回滚、备份、恢复都只围绕一个目录：容器内 `/data`。它包含：

| 内容 | 位置 | 说明 |
| --- | --- | --- |
| 管理员登录凭据 | `/data/auth.json` | 管理员密码哈希（v2.5.0 起另含长效会话签名材料，见文末兼容边界） |
| Telegram 会话 | `/data/tg_session*` | 主号 Telethon session（含 `.session` 等附属文件）；小号 session 同目录 |
| 任务清单 | `/data/tasks/*.json` | 每个任务一个 JSON 文件（断点续传；损坏的旧 `.bak` 轮转文件也在该目录） |
| 下载文件 | `/data/downloads/` | 默认下载根目录；移动到别处的文件不在卷内，需自行备份 |
| 程序配置 | `/data/tg_config.json` | 程序设置 |
| 服务配置（可选） | `/data/server.json` | 无头服务运行时配置覆盖 |
| 首跑令牌 | `/data/.setup-token` | 仅未设置管理员密码时存在，设置完成后自动删除 |

**删掉 `/data`（或删除卷 `tg-video-downloader-data`）等于删掉全部数据。**
任何升级、回滚、改挂载方式之前，先做一次备份。

## 备份 /data

建议先停容器再备份，保证任务清单与下载文件在磁盘上的状态一致
（容器运行时下载线程会持续写文件）。

**命名卷（compose 默认形态）**：在部署目录（存放 `compose.yaml` 的目录）执行：

```bash
# 1. 停容器（数据不再变动）
docker compose stop tg-video-downloader

# 2. 把命名卷打成 tar 包，落在当前目录
docker run --rm \
  -v tg-video-downloader-data:/data:ro \
  -v "$(pwd)":/backup \
  alpine:3.20 tar czf /backup/tgvd-backup-$(date +%Y%m%d).tar.gz -C /data .

# 3. 重新启动
docker compose start tg-video-downloader

# 4. 健康检查确认服务回来了
curl -fsS http://127.0.0.1:8787/healthz
```

**绑定挂载（自建目录形态）**：宿主机目录示例沿用 README 的
`/vol1/1000/docker/tg-video-downloader-data`，按实际路径替换：

```bash
docker compose stop tg-video-downloader
tar czf tgvd-backup-$(date +%Y%m%d).tar.gz \
  -C /vol1/1000/docker/tg-video-downloader-data .
docker compose start tg-video-downloader
```

### 恢复 /data

**命名卷**：把备份包放回部署目录后执行（注意把 `YYYYMMDD` 换成实际文件名）。
`tar` 解包出来的属主是 root，最后一步 `chown 10001:10001` 是必须的，
否则容器以 uid 10001 + `cap_drop: ALL` 起不来：

```bash
docker compose down
docker run --rm \
  -v tg-video-downloader-data:/data \
  -v "$(pwd)":/backup \
  alpine:3.20 sh -c \
  "find /data -mindepth 1 -delete && tar xzf /backup/tgvd-backup-YYYYMMDD.tar.gz -C /data && chown -R 10001:10001 /data"
docker compose up -d
curl -fsS http://127.0.0.1:8787/healthz
```

**绑定挂载**：

```bash
docker compose down
mkdir -p /vol1/1000/docker/tg-video-downloader-data
tar xzf tgvd-backup-YYYYMMDD.tar.gz \
  -C /vol1/1000/docker/tg-video-downloader-data
chown -R 10001:10001 /vol1/1000/docker/tg-video-downloader-data
docker compose up -d
```

> 绑定挂载目录必须属主 `10001:10001`；新建的目录默认是 `root:root 0755`，
> 容器内 uid 10001 无法写入，会在启动时报 `Permission denied`。

## 升级步骤

`compose.yaml` 默认镜像 tag 固定为已发布版本（`v2.5.0`），升级就是
换 tag 后拉镜像重建容器，数据卷原样保留。

1. **先备份**：按上一节操作。

2. **指定新版本 tag**，两种方式任选：

   方式 A —— 在部署目录写 `.env`（不动 `compose.yaml`）：

   ```bash
   echo 'TGVD_IMAGE=ghcr.io/xmiqz/tg-video-downloader:v2.5.0' > .env
   ```

   方式 B —— 直接编辑 `compose.yaml` 第 3 行，把默认 tag 改成新版本：

   ```yaml
   image: ${TGVD_IMAGE:-ghcr.io/xmiqz/tg-video-downloader:v2.5.0}
   ```

3. **拉取新镜像并重建容器**：

   ```bash
   docker compose pull
   docker compose up -d
   ```

4. **健康检查**（NAS 本机执行；返回 `{"ok": true, "service": "tg-video-downloader"}` 即正常）：

   ```bash
   curl -fsS http://127.0.0.1:8787/healthz
   ```

5. **浏览器登录验证**：打开 `http://NAS地址:8787`，用原管理员密码登录，
   进入任务库确认任务清单仍在，再打开「下载中」概览确认页面正常。

升级只替换容器镜像，不触碰 `/data`；`.env` / `compose.yaml` 属于部署文件，
建议随备份包一起保存一份。

## 回滚步骤

新版本出现问题时，把镜像 tag 改回上一个稳定版本即可，数据不需要回退。

1. **先备份当前数据**（回滚前也备份，防手滑）。
2. **改回旧镜像 tag**：用 `.env` 指定，或编辑 `compose.yaml` 改回旧 tag：

   ```bash
   echo 'TGVD_IMAGE=ghcr.io/xmiqz/tg-video-downloader:v2.4.0' > .env
   ```

3. **重建容器**（本地没有旧镜像时 `pull` 会自动拉回）：

   ```bash
   docker compose pull
   docker compose up -d
   ```

4. **验证任务库与登录状态**：

   ```bash
   curl -fsS http://127.0.0.1:8787/healthz
   ```

   浏览器打开 `http://NAS地址:8787`：任务清单应完整保留；登录状态按下一节
   兼容边界的预期核对（若被要求重新登录，属正常现象，用原密码登录即可）。

## 数据兼容边界

v2.5.0 是收口补丁，**没有**任何数据结构迁移：

- `auth.json`：v2.5.0 仅在此文件**追加** `session_key` 与 `revoked_sessions`
  两个键，用于「记住此设备」长效会话的签名与吊销。旧版本读取该文件时只校验
  必需键、忽略未知键，因此 v2.5.0 写出的 `auth.json` 在 v2.4.0 下照常可用。
- 任务清单：任务 JSON 顶层 7 键不变；搜索结果 meta 13 键不变。
- 下载目录：`/data/downloads` 位置与结构不变，升级/回滚不移动任何文件。

**回滚到 v2.4.0 后的登录状态**：

- 在 v2.5.0 下勾选「记住此设备」签发的长效会话 Cookie **不被 v2.4.0 识别**，
  回滚后浏览器会要求重新登录一次；管理员密码本身不变，直接用原密码登录即可。
- 未勾选「记住此设备」的会话本来就是内存会话，不跨服务重启；回滚后同样需要
  重新登录，与升级前的行为一致，没有额外损失。

一句话总结：**升级不丢数据、不需要重新设置；回滚不丢数据，最多重新登录一次。**

## 数据敏感级

`/data` 目录等价于账号密码，请按同等敏感级保管；本版不做静态凭据加密
（`tg_config.json` / `tg_session.session` 为明文 + `0600`）。
