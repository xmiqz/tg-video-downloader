# NAS 反向代理部署手册（HTTPS / 域名访问）

> 适用：v2.4.1 起 `compose.yaml` 部署形态。服务名/容器名
> `tg-video-downloader`，容器端口 `8787`，探活端点 `/healthz`。
> 本文给出 Caddy 与 nginx 两套最小可用片段，字段名、端口、服务名与仓库
> 逐字一致，可直接照抄后替换域名与证书路径。
>
> 范围声明：本服务**不内置证书签发**（TLS 由反代负责）、**不做 OAuth、
> 不做多用户**（单管理员密码）。本文只解决"可信反代后面的转发头与
> Cookie Secure"这一件事。

## 信任模型（改配置前先读）

应用默认**不信任任何转发头**，三个开关层层收紧：

| 配置 | 行为 |
| --- | --- |
| `TGDL_TRUST_PROXY=0`（默认） | 完全不看 `X-Forwarded-*`：限速键与 Cookie 判定一律用直连地址 |
| `TGDL_TRUST_PROXY=1`，未设 `TGDL_TRUSTED_PROXIES` | 仅信任直连端为回环 `127.0.0.1` 与 `::1`；其余来源的转发头一律忽略 |
| `TGDL_TRUST_PROXY=1` + 显式 `TGDL_TRUSTED_PROXIES` | **仅**信任白名单内的直连地址，白名单**取代**默认回环集合 |

要点：

- **公网来源的转发头一律忽略。** 直连端不在白名单内时，客户端伪造的
  `X-Forwarded-For` 既不会参与登录限速键，也不会影响 Cookie 判定。
- **显式白名单取代默认**：设置了 `TGDL_TRUSTED_PROXIES` 后，默认回环
  集合不再生效。若反向代理与应用同机（直连端是 `127.0.0.1`），必须把
  `127.0.0.1` 一并写进白名单。
- **白名单只写反代地址**：不要写掩码长度为 `/0` 的全网段（IPv4 或
  IPv6 的全部地址网段）或任何超宽网段（例如 `/8` 级别的大段）——那等于
  对全网放开伪造转发头的能力。
- **Cookie Secure 的判定**：`TGDL_COOKIE_SECURE=1` 显式开启；或
  `TGDL_TRUST_PROXY=1` + 白名单 + 反代发送 `X-Forwarded-Proto: https`
  时自动开启。两者是 OR 关系，显式设置不会被覆盖。

### 容器侧配置

在自己的部署目录里给 `compose.yaml` 的 `environment` 段**追加**两行
（无需改动仓库文件）：

```yaml
    environment:
      TGDL_TRUST_PROXY: "1"
      TGDL_TRUSTED_PROXIES: "172.18.0.1"   # ← 换成你的反代地址，见下一节
```

### `TGDL_TRUSTED_PROXIES` 填什么

`TGDL_TRUSTED_PROXIES` 是逗号分隔的 IP 或 CIDR。容器里看到的
"直连端"是反向代理到达容器时使用的源地址：

| 反代位置 | 容器看到的直连端 | 示例值 |
| --- | --- | --- |
| 宿主机（反代监听 443，转发到 `127.0.0.1:8787` 映射端口） | Docker 网桥网关 | `172.18.0.1`（用 `docker network inspect` 查默认网关） |
| 同一 compose 网络里的反代容器 | 反代容器的 IP | `172.18.0.5` 或整段 `172.18.0.0/16`（只含 compose 网段） |
| `network_mode: host` 同机形态 | `127.0.0.1` | `127.0.0.1` |

## Caddy 最小片段

```caddyfile
# /etc/caddy/Caddyfile
tgvd.example.com {
    # 内网自签用 tls internal；有公网可解析域名时删掉本行，
    # Caddy 会自动 ACME 签发并续期。
    tls internal

    encode gzip

    # 上传体积：应用侧壁纸上传上限 8 MB，放一倍余量。
    request_body {
        max_size 16MB
    }

    reverse_proxy 127.0.0.1:8787 {
        # 覆盖（而不是追加）X-Forwarded-For：只保留 Caddy 看到的
        # 直连客户端，丢弃客户端自带的伪造值。
        header_up X-Forwarded-For {remote_host}
        header_up X-Forwarded-Proto {scheme}
    }
}
```

WebSocket 透传：`/events` 端点由 `reverse_proxy` 自动处理
`Upgrade`/`Connection` 头，无需额外配置。

## nginx 最小片段

```nginx
# /etc/nginx/conf.d/tgvd.conf

# WebSocket 升级映射（/events 需要）
map $http_upgrade $connection_upgrade {
    default upgrade;
    ""      close;
}

# 登录失败限速：与 server_auth.py 常量一致——
# 连续失败 MAX_FAILURES=5 次、FAILURE_WINDOW_SECS=600 秒窗口内累计、
# 触发后最长封锁 MAX_BLOCK_SECS=900 秒。反代层 rate≈6r/m，
# 只是挡批量撞库的第一层；真正的封禁由应用自己做。
limit_req_zone $binary_remote_addr zone=tgvd_auth:10m rate=6r/m;

server {
    listen 443 ssl;
    server_name tgvd.example.com;

    # 证书自行准备（本服务不内置证书签发）
    ssl_certificate     /etc/ssl/tgvd/fullchain.pem;
    ssl_certificate_key /etc/ssl/tgvd/privkey.pem;

    # 上传体积：应用侧壁纸上传上限 8 MB，放一倍余量
    client_max_body_size 16m;

    location = /auth/login {
        limit_req zone=tgvd_auth burst=5 nodelay;
        proxy_pass http://127.0.0.1:8787;
        proxy_http_version 1.1;
        # 覆盖（而不是追加）：只保留 nginx 看到的直连客户端
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location / {
        proxy_pass http://127.0.0.1:8787;
        proxy_http_version 1.1;
        # WebSocket 透传（/events）
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 3600s;
        # 覆盖（而不是追加）：只保留 nginx 看到的直连客户端
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

> 同一 compose 网络形态下，把 `proxy_pass` 换成
> `http://tg-video-downloader:8787`（服务名），并把
> `TGDL_TRUSTED_PROXIES` 填成反代容器所在的网段。

## 部署验证（/healthz 探活）

```bash
# 经反代（期望 {"ok": true, "service": "tg-video-downloader"}）
curl -fsS https://tgvd.example.com/healthz

# NAS 本机直连容器（期望同上；compose healthcheck 用的就是它）
curl -fsS http://127.0.0.1:8787/healthz
```

再验证转发头信任是否生效：浏览器经 `https://` 登录后查看
`Set-Cookie`，应包含 `Secure`（前提是 `TGDL_TRUST_PROXY=1` 且白名单
填对了反代地址）；若登录响应的 `Set-Cookie` 没有 `Secure`，通常是
`TGDL_TRUSTED_PROXIES` 与容器实际看到的直连端不一致。

## 常见错误

| 症状 | 原因 | 处理 |
| --- | --- | --- |
| `Set-Cookie` 缺 `Secure` | `TGDL_TRUSTED_PROXIES` 填的不是容器看到的直连端（常见为填成外网反代 IP 而非网桥网关） | `docker network inspect` 查网关后重填 |
| 所有客户端共享一个限速键 | 同上：直连端不可信时应用忽略 `X-Forwarded-For`，用直连地址当限速键 | 修正白名单后重启容器 |
| 反代后仍提示 HTTP 警告 | `TGDL_COOKIE_SECURE` 未设且转发判定未生效 | 设 `TGDL_COOKIE_SECURE=1`，或按上一行修白名单 |

## 不做什么

- **不内置证书**：TLS 终止在反代，证书由运维准备（ACME 或自签）。
- **不做 OAuth / 多用户**：单一管理员密码（见首次启动向导），本文
  配置不引入第二套身份体系。
- **不做静态凭据加密**：见
  [nas-backup-restore.md](./nas-backup-restore.md) 的数据敏感级说明。
