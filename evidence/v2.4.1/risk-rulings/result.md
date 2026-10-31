# v2.4.1 风险裁决落实对照（裁决 1–5）

基线：`29f3c4e`（fix(web): tighten search results grid on wide viewports）
分支：`v2.5-dev`；一裁决一提交，共两笔：

- `293ae2e` fix(auth): revoke remember-me tokens regardless of user agent
- `453c135` fix(proxy): always trust loopback proxies with explicit whitelist

## 裁决 1 · 登出吊销「宽进严出」

| 项 | 实现 |
| --- | --- |
| `session_valid` | 仍严格：`r2` 令牌必须由当前请求 UA 重算出同一 `ua8` 指纹才算有效 |
| `logout` | 只验格式 + 过期 + HMAC 签名，**不校验 UA**；签名合法即写入 `sha256(token)` 到 `revoked_sessions` |
| 实现拆分 | 新增 `_remember_signed(token)`（仅签名，返回 `(expires_at, ua8)`）；`_remember_parts(token, ua)` 复用前者再叠加 UA 校验；`_revoke_remember(token)` 只调 `_remember_signed`；`logout(token, user_agent="")` 保留形参但不使用 |
| 签名非法 | 静默忽略、不抛错、不写吊销表（`/auth/logout` 仍 200） |
| 幂等 | 重复登出同一令牌不新增条目、不重写 auth.json |
| 补测 | `tests/test_server_auth.py::test_T60_followup_end_to_end_logout_binding`：登录（UA_ALICE）→ 从 UA_BOB 登出 → 用 UA_ALICE 访问 `/rpc` = 401 → 重启后仍 401；另伪造签名登出后吊销表不含该 id |
| 补测（管理器层） | `test_T60_followup_logout_revocation_and_rebuild`：UA 不匹配的登出也写入吊销，且重启后失效 |

## 裁决 2 · 可信代理并集

| 项 | 实现 |
| --- | --- |
| 可信集合 | `TGDL_TRUSTED_PROXIES` 配置后 = 配置值 ∪ `{127.0.0.1/32, ::1/128}`；未配置时仍为回环默认集合 |
| 语义 | 回环永远可信，显式白名单只追加、不取代；单条非法条目按不可信处理且不回退（保持 fail-closed） |
| 补测 | `tests/test_server_proxy.py::test_t61fu_rate_limit_key_loopback_still_trusted_with_whitelist`：配置 `192.168.2.50` 后，来自 `127.0.0.1` 且带 `X-Forwarded-For: 1.2.3.4` 的登录失败，限速键落在 `1.2.3.4`（即 XFF 被采信） |
| 既有用例调整 | `test_t61fu_trusted_proxy_host_whitelist_unions_loopback`、`test_t61fu_request_is_https_matrix` 的期望值改为并集语义 |

## 裁决 3 · Cookie Secure 的 OR 语义（接受，只补文档）

- 代码未改：`Cookie Secure = TGDL_COOKIE_SECURE or request_is_https(...)`。
- `docs/nas-reverse-proxy.md`「信任模型」小节追加：「因此**启用可信反代后走 http 直连将拿不到 Cookie**（浏览器会丢弃带 `Secure` 的 `Set-Cookie`），必须走 https 访问；要恢复纯 http 直连请把 `TGDL_TRUST_PROXY` 置 0。」
- 文档契约测试（`test_t61fu_reverse_proxy_doc_contract_tokens` 等）仍全绿。

## 裁决 4 · amd64 面：**未覆盖：本机无 Docker 运行时**

- 本机（Windows ARM64）实测：无 `docker` 可执行文件、无 `qemu-system-x86_64`、WSL 未安装任何发行版。因此本轮**未在 amd64 容器形态上做任何实测**。
- 以上结论按此口径写入本证据与 v2.4.1 release notes，不做任何推测性描述、不伪造日志。
- ARM64 真机面见 `evidence/v2.4.1/T62/`（群晖 DS218 / aarch64）。

## 裁决 5 · 废弃工作树归档与删除

归档文件：`evidence/v2.4.1/T61/abandoned-worktree.txt`（含 `log --oneline -3`、`status --short`、未提交 `diff --stat`、与 `v2.5-dev` 的 `diff --stat`）。

- 被废弃对象：工作树 `tgvd-v25-t61`（分支 `v2.5-dev-t61`，HEAD `ce91391`）内的旧版 T61 未提交改动（`README.md` / `server_app.py` / `server_auth.py` + 未跟踪 `docs/nas-reverse-proxy.md`、`tests/test_server_proxy.py`）。
- 处置：`git worktree remove --force` → `git branch -D v2.5-dev-t61`（was `ce91391`）；其内容已被 `0f9cf08` / `453c135` 的正确实现覆盖。
- 结果：`git worktree list` 中已无 `tgvd-v25-t61`，`git branch --list "v2.5-dev*"` 中已无 `v2.5-dev-t61`。

## 门规

```
$ python -m pytest tests -q
322 passed, 4 warnings in 9.93s

$ node --test "tests/js/*.test.mjs"
pass 66 / fail 0 / todo 1

$ python -c "import server_app; print(len(server_app.RPC_METHODS))"
56
```

日志全文：`pytest.log`、`node.log`（同目录）。

## 红线自查

- 未改 RPC 白名单（56 不变）、未改任务 JSON 顶层 7 键 / meta 13 键、未动 `_PHASE_NEXT` 与 `advanced_match`、WS 帧协议未动。
- 未新增依赖、未加功能；裁决 3 仅改文档一句。
- 未执行 push / tag / Release。
- 隐私：证据中无真实令牌、密码、手机号、频道名、文件名。
