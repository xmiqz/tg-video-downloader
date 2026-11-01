# T61-followup 结果报告：仅信任白名单反代的转发头（TGDL_TRUSTED_PROXIES）

## 0. 基线与开工条件

- worktree：`c:\Users\xqz\Documents\trae_projects\dayly\_arm_inspect\repo\tgvd-v25-t61b`（分支 `v2.5-dev-t61b`）
- 基线 commit：`e3302b4`（fix(auth): bind remember-me tokens to device fingerprint and shorten ttl，即 T60-followup 收口提交）
- 开工核查：`git status --short` 为空、`git rev-parse --short HEAD` = `e3302b4` → 满足

## 1. 改动清单（git status / diff --numstat）

```
git status --short
 M README.md
 M docs/nas-backup-restore.md
 M server_app.py
 M server_auth.py
 M server_config.py
 M tests/_v1_support.py
 M tests/test_v1_auth.py
 M tests/test_v1_config.py
?? docs/nas-reverse-proxy.md
?? evidence/v2.4.1/T61-followup/        ← 本报告与日志
?? tests/test_server_proxy.py

git diff --numstat
 4       0       README.md
 5       0       docs/nas-backup-restore.md
25       6       server_app.py
85       2       server_auth.py
40       0       server_config.py
 2       0       tests/_v1_support.py
 3       1       tests/test_v1_auth.py
 8       3       tests/test_v1_config.py
```

恰 8 改 + 3 新建（含证据目录），全部落在任务书 §2 文件域内，零夹带；
未 commit/add/push/merge/rebase/tag。

## 2. 实现清单对照（任务书 §3 逐项）

| # | 要求 | 落点 |
|---|------|------|
| 1 | `TGDL_TRUSTED_PROXIES` 环境变量（逗号分隔 IP/CIDR）、文件键 `trusted_proxies`、`RuntimeConfig.trusted_proxies: tuple[str, ...] = ()`、`normalized()` 用 `ipaddress.ip_network(..., strict=False)` 校验规范化、非法值 `ValueError`（错误信息不回显输入） | server_config.py：`_as_trusted_proxies()`、`RuntimeConfig` 新字段、`normalized()` 保留并规范化、`load_runtime_config` 读 env（优先）与 file_cfg；`str(ip_network(text, strict=False))` 规范化为 `<ip>/32`（IPv6 `/128`）形式 |
| 2 | 语义三分支：`TGDL_TRUST_PROXY=0` 不看转发头；`=1` 未设白名单仅信任 `127.0.0.1/32`+`::1/128`；显式白名单**取代**默认（不自动附加回环） | `DEFAULT_TRUSTED_PROXIES = ("127.0.0.1/32", "::1/128")`（server_auth.py）+ `is_trusted_proxy_host` 的"未配置→默认 / 已配置→仅白名单"分支；文档 §信任模型明示"反代同机需把 127.0.0.1 一并列入" |
| 3 | `is_trusted_proxy_host(host, trusted_proxies=())`：host 必须是 IP；非 IP / 空 / None → False | server_auth.py：`_proxy_networks`（热路径非法条目静默丢弃不抛错）+ `is_trusted_proxy_host`；"localhost"（主机名）与 `127.255.255.254`（默认集合外的回环地址）均 False |
| 4 | `client_key(request, trust_proxy=False, trusted_proxies=())`：仅当开关开且直连端可信才取 XFF 第一段，返回形状不变；第三参数可选、既有单/双参数调用不抛错 | server_auth.py：签名扩展（第二位 `trust_proxy` 位置不变），`if trust_proxy and is_trusted_proxy_host(host, trusted_proxies)` 门控；返回仍为 `"<ip>|<path>"` |
| 5 | `request_is_https(request, trust_proxy=False, trusted_proxies=())`：直连 https → True；否则开关开 + 直连端可信 + XFP 第一段 https（去空白、大小写不敏感）→ True | server_auth.py：先看 `request.url.scheme`，再看门控后的 `x-forwarded-proto` 第一段 `strip().lower()` |
| 6 | `/auth/login` 限速键与 `_set_session_cookie` 的 Secure 判定传 `config.trust_proxy` 与 `config.trusted_proxies`；`Cookie Secure = config.cookie_secure or request_is_https(...)`（OR，不覆盖 `TGDL_COOKIE_SECURE=1`） | server_app.py：`/auth/login` 调 `client_key(request, config.trust_proxy, config.trusted_proxies)`；`_set_session_cookie` 增 keyword-only `secure=None`（不传保持旧行为），`create_app` 内新增 `_effective_cookie_secure(request)` helper，`/auth/setup`、`/auth/login` 两处传入 |
| 7 | 启动警告保持 T61 原要求（非 loopback 且无 TLS 只警告一次、不阻断），文案补充反代 HTTPS 两种做法 | server_app.py `run_blocking`：触发条件未动（`config.auth_required and not config.cookie_secure`），文案补"若由反代提供 HTTPS，也可设置 TGDL_TRUST_PROXY=1 与 TGDL_TRUSTED_PROXIES=<反代地址>，由服务按 X-Forwarded-Proto 自动加 Secure" |
| 8 | 新建 `docs/nas-reverse-proxy.md`：Caddy/nginx 最小片段（TLS、XFP、XFF、上传体积、失败限速、/healthz、WebSocket 透传），字段名/端口/服务名逐字一致，写明"覆盖而非追加"，禁止全网段示例，默认仅回环、白名单取代默认、公网转发头一律忽略，限速数字与应用一致，不做内置证书/OAuth/多用户 | docs/nas-reverse-proxy.md（新建，9 小节：信任模型 / 容器侧配置 / 白名单填法 / Caddy / nginx / 部署验证 / 常见错误 / 不做什么） |
| 9 | `docs/nas-backup-restore.md` 追加一句数据敏感级说明 | 文末新增"## 数据敏感级"小节，原句照录：`/data` 目录等价于账号密码…（`tg_config.json` / `tg_session.session` 为明文 + `0600`） |
| 10 | README NAS/Docker 章节加 `docs/nas-reverse-proxy.md` 链接 | README.md 备份手册链接之后追加一段两行，含有效 Markdown 链接 |
| 11 | 不新增 RPC、不加依赖、不联网、不加外部 CDN | RPC_METHODS = 56（见 §3 命令 4）；唯一新 import 是标准库 `ipaddress`（server_auth.py，server_config.py 原有）；requirements-* 未动 |

## 3. 测试证据（先红后绿）

### 3.1 红态（实现落地前，全部亲见）

| 对象 | 红的表现 |
|------|----------|
| `tests/test_server_proxy.py` | collection ImportError：`cannot import name 'is_trusted_proxy_host' from 'server_auth'` |
| `tests/test_v1_auth.py::test_tr8_rate_limit_is_per_client`（适配后） | TypeError（`RuntimeConfig` 无 `trusted_proxies` 字段） |
| `tests/test_v1_config.py::test_tr12_trust_proxy_affects_client_key`（适配后） | AssertionError（10.0.0.5 默认不可信，不再无条件采信 XFF） |
| 实现落地后复检 | 揪出 1 个真实缺陷并修复（见 §4 偏差 1）后全绿 |

### 3.2 新增用例（tests/test_server_proxy.py，21 个函数，参数化展开 42 项，全 PASS）

| 用例 | 覆盖任务书条款 |
|------|----------------|
| `test_t61fu_env_parsing_comma_ip_cidr_whitespace` | §4.6 逗号/空格/IP/CIDR/空段 |
| `test_t61fu_env_empty_and_default_values` | §4.6 未设变量 + 全空白值 → () |
| `test_t61fu_invalid_value_raises_without_echoing_input` | §4.6 非法值 `ValueError` 且错误信息不回显输入 |
| `test_t61fu_file_key_and_env_precedence` | §4.6 文件键 `trusted_proxies` + env 覆盖 file |
| `test_t61fu_runtime_config_default_and_normalized_dedupe` | §3.1 默认 () + normalized 规范化/去重/幂等 |
| `test_t61fu_normalized_rejects_bad_entries` | §3.1 非法网段掩码 / 非字符串条目 / 坏 IP → `ValueError` |
| `test_t61fu_trusted_proxy_host_default_matrix`（13 项参数化） | §4.5 默认只信 127.0.0.1/::1；10.0.0.1、192.168.2.50、203.0.113.5、非 IP、空、None、`0.0.0.0`、`::` 均 False；`127.255.255.254` 亦 False |
| `test_t61fu_trusted_proxy_host_whitelist_replaces_default` | §4.5 白名单命中 True；取代默认回环（127.0.0.1 在白名单外时 False）；`/32`、`/128`、IPv6 CIDR 可用；全非法白名单 fail-closed |
| `test_t61fu_client_and_https_backward_compatible_call_shapes` | §3.2 单参数/双参数/三参数调用不抛错；缺 client 的请求 `unknown` 直连端永不可信 |
| `test_t61fu_trust_proxy_off_ignores_forwarded_headers` | §4.6 开关关时白名单不生效（XFF/XFP 均忽略），开关开且可信才生效 |
| `test_t61fu_rate_limit_key_uses_xff_from_default_loopback` | §4.1 回环直连 + trust_proxy=1 → 限速键 `1.2.3.4` |
| `test_t61fu_rate_limit_key_untrusted_lan_source_uses_direct_host` | §4.2 直连 192.168.2.50（未列白名单）→ 键仍 `192.168.2.50` |
| `test_t61fu_rate_limit_key_whitelisted_source_uses_xff` | §4.3 `trusted_proxies=("192.168.2.50",)` 后 → 键 `1.2.3.4` |
| `test_t61fu_rate_limit_isolation_end_to_end` | §4.1 行为面：伪造 XFF 封禁不影响其他客户端 |
| `test_t61fu_cookie_secure_matrix` | §4.4 四分支：开关关+XFP=https → 无 Secure；开关开+回环+XFP=https → 有；`TGDL_COOKIE_SECURE=1`+XFP=http → 仍有（OR）；开关开+直连不可信 → 无 |
| `test_t61fu_setup_cookie_honours_secure_semantics` | §3.3 `/auth/setup` 走同一 Secure 判定 |
| `test_t61fu_request_is_https_matrix`（10 项参数化） | §3.2 直连 https 一票通过；XFP 第一段/去空白/大小写/逗号列表；开关关白名单无效；白名单取代默认 |
| `test_t61fu_request_is_https_whitelisted_source` | 白名单 CIDR 命中后 XFP 生效 |
| `test_t61fu_docs_exist_and_readme_links_them` | §4.7 两份 docs 存在 + README 有效链接 |
| `test_t61fu_reverse_proxy_doc_contract_tokens` | §4.7 Caddy/nginx/变量名/`/healthz`/`8787`/`tg-video-downloader`/"覆盖"/限速数字 5·600·900；不含 `0.0.0.0/0` |
| `test_t61fu_backup_doc_marks_data_as_sensitive` | §3.5 追加句含"敏感级"、`tg_config.json`、`tg_session`、`0600` |

### 3.3 既有测试适配（任务书 §4.8，仅最小改动）

| 文件 | 改动 |
|------|------|
| `tests/_v1_support.py` | `make_remote_app` 增 `trusted_proxies=()` 并传入 `RuntimeConfig`（+2/-0） |
| `tests/test_v1_auth.py` | `test_tr8_rate_limit_is_per_client`：`TestClient(app, client=("127.0.0.1", 50000))` 让直连端为回环（默认可信），XFF 断言原样保留（+3/-1） |
| `tests/test_v1_config.py` | `test_tr12_trust_proxy_affects_client_key`：默认信任下 `10.0.0.5` 不使用 XFF，显式 `trusted_proxies=("10.0.0.5",)` 后才用；`clean_env` 增加 `TGDL_TRUSTED_PROXIES` 隔离（+8/-3） |

`tests/test_server_auth.py`、`tests/test_compose_contract.py` 等其余用例**未改动**，全量回归全绿（`cookie_kwargs` 契约、compose 契约、T60 remember 系列均原样通过）。

### 3.4 命令输出片段（四条必跑，全文见 pytest.log / node.log）

```
$ C:\Users\xqz\AppData\Local\Programs\Python\Python312-arm64\python.exe -m pytest tests -q
  321 passed, 4 warnings in 10.31s     ← 基线 279 全绿 + 新增 42
$ C:\Users\xqz\AppData\Local\Programs\Python\Python312-arm64\python.exe -m pytest tests/test_server_proxy.py -q
  42 passed, 2 warnings in 0.86s
$ node --test "tests/js/*.test.mjs"
  pass 66 / fail 0 / todo 1            ← 与任务书口径 66+1 todo 一致
$ C:\Users\xqz\AppData\Local\Programs\Python\Python312-arm64\python.exe -c "import server_app; print(len(server_app.RPC_METHODS))"
  56
```

## 4. 偏差清单

1. **运行时非法白名单条目的 fail-closed 语义（开发中发现并修复的真缺陷）**：任务书只规定配置加载时非法值抛 `ValueError`，未规定 `is_trusted_proxy_host` 直接收到非法条目（绕过配置层）的行为。首版实现用 `or` 兜底，把"白名单全非法"误判为"未配置"而**静默回退默认回环**，被 `test_t61fu_trusted_proxy_host_whitelist_replaces_default` 的一条断言（`is_trusted_proxy_host("127.0.0.1", ("garbage",)) is False`）当场揪出。修正为显式区分：空输入→默认回环集合；非空输入→仅用其可解析部分，解析后为空则全部不可信（绝不回退）。热路径对单条非法输入仍不抛错（不让写错的配置把登录变 500），该条目按不可信处理。
2. **规范化形式**：`RuntimeConfig.trusted_proxies` 按任务书存 `tuple[str, ...]`，单 IP 规范化为 `<ip>/32`（IPv6 `<ip>/128`）；`server_auth` 侧同时兼容裸 IP 与带掩码形式（`ip_network(strict=False)`）。
3. **compose.yaml 未动**：`TGDL_TRUSTED_PROXIES` 不在 compose 传递链中（compose 契约测试锁定现有变量集，任务书也禁改 compose.yaml），改为在 `docs/nas-reverse-proxy.md`"容器侧配置"一节指导用户在自己的部署文件 `environment` 段追加两行。
4. **文档全网段警示的措辞**：任务书 §3.4 要求文档写明"不要填 `0.0.0.0/0` 或任何全网段"，而 §4.7 又要求文档不含字面 `0.0.0.0/0`。两者以"掩码长度为 `/0` 的全网段（IPv4 或 IPv6 的全部地址网段）或任何超宽网段（例如 `/8` 级别的大段）"的表述同时满足；测试以 `"0.0.0.0/0" not in text` + 信任模型小节双重锁定。
5. **新增标准库 import**：`server_auth.py` 增加 `import ipaddress`（server_config.py 原有）；零第三方新依赖，`requirements-server.txt` / `requirements-dev.txt` 未动。
6. **启动警告触发条件未动**：按任务书"保持 T61 原要求（非 loopback 且无 TLS 只警告一次、不阻断）"，仅扩展文案给出 `TGDL_COOKIE_SECURE=1` 与 `TGDL_TRUST_PROXY=1`+`TGDL_TRUSTED_PROXIES` 两条路径；`trust_proxy=1` 但最终 Cookie 未加 Secure 的场景仍会打印该警告（警告不阻断，提示语义由文案覆盖）。
7. **`request_like` 冗余 import 保留**：`test_tr12_trust_proxy_affects_client_key` 原有一处未使用的 `from _v1_support import request_like`，按"最小改动"原则未顺手删除。

## 5. 红线自查

- **协议**：RPC 白名单恒 56（命令 4 输出）；未新增方法；未改任务 JSON 顶层 7 键/meta 13 键；未动 `_PHASE_NEXT` 与 `advanced_match`；WS 帧协议未动（`/events` 鉴权流程零改动）。
- **只修不加**：未引入新功能；未做静态凭据加密、内置证书、OAuth、多用户；改动严格限定任务书 §2 文件域。
- **依赖**：零新第三方依赖（`ipaddress` 为标准库）；未联网；未加外部 CDN；`web/`、`compose.yaml`、`tests/test_server_auth.py`、`tests/test_compose_contract.py` 未改动。
- **隐私**：未读下载目录/消息正文/频道名；测试与文档全部使用保留/文档专用地址段（127.0.0.1、192.168.2.x、10.0.0.x、203.0.113.x、198.51.100.x、2001:db8::/32）；证据无真实密码/token/手机号/频道名/文件名/证书私钥。
- **提交纪律**：未执行 commit/add/push/merge/rebase/tag；提交由调用方复核后进行。

## 6. 交付物

- 本文件：`evidence/v2.4.1/T61-followup/T61-followup_result.md`
- `evidence/v2.4.1/T61-followup/pytest.log`（全量 321 + 专项 42 + RPC 56 三条命令全文，UTF-8，尾部结论行）
- `evidence/v2.4.1/T61-followup/node.log`（node --test 全文，UTF-8，尾部结论行）

T61_FOLLOWUP_DONE
