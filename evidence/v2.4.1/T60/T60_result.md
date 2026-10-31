# T60 Web 管理员会话持久化 · 收口报告

- **分支/基线**：`v2.5-dev-t60` @ `f1923a2`（开工条件复核：工作树净 + HEAD 一致，PASS）
- **实现范围**：`remember=true` 签发 30 天长效 `r1.` HMAC 令牌（持久化 + 可吊销）；未勾选保持现状（内存会话，`TGDL_SESSION_HOURS` 默认 168h）
- **基线核对**：开工先跑门规——pytest 238 passed / node 66 passed + 1 todo + 0 failed / RPC 56，与任务书基线一致
- **总体结论**：**PASS**

---

## 1. git 原文

### 1.1 `git status --short`（收口时，`.temp/` 临时脚本已清理）

```
 M server_app.py
 M server_auth.py
 M tests/test_server_auth.py
 M web/css/setup.css
 M web/js/setup.js
 M web/setup.html
?? evidence/
```

### 1.2 `git diff --numstat`

```
9	3	server_app.py
194	18	server_auth.py
298	0	tests/test_server_auth.py
18	0	web/css/setup.css
4	1	web/js/setup.js
4	0	web/setup.html
```

改动恰为文件域内 6 个产品文件；`evidence/` 为本任务证据目录（仅 `.md` / `.log`）。未 commit / 未 add / 未 push / 未 tag。

---

## 2. 验收点 ↔ 测试映射与断言计数

测试全部位于 `tests/test_server_auth.py`（原有 5 个用例零削弱，新增 12 个测试函数，其中 1 个参数化 ×4 → pytest 计 15 项；断言计数为 AST 级 `assert` 语句数）。

| # | 任务书验收点（§5 / §3） | 测试名 | 断言数 |
| --- | --- | --- | --- |
| 1 | remember 登录 → 重建 AuthManager（模拟重启）→ `session_valid` 为 True | `test_remember_login_survives_restart` | 8 |
| 2 | 不勾选 remember（含两参旧调用形态）登录 → 重启 → False | `test_plain_login_does_not_survive_restart` | 8 |
| 3 | remember 登录 → logout → 重启 → False（吊销持久化；令牌不落盘明文） | `test_remember_logout_revocation_survives_restart` | 5 |
| 4 | 篡改令牌（改签名 / 改 expires_at / 另一把 session_key）→ False；畸形令牌不抛异常（15 个畸形样例 + 自然过期） | `test_tampered_and_malformed_remember_tokens_fail` | 7 |
| 5a | 应用层：`{"remember": true}` → Set-Cookie `max-age=2592000`、`httponly`、`samesite=strict` | `test_remember_cookie_end_to_end` | 10 |
| 5b | 同 data_dir 新建 app/auth（模拟重启）→ 携 Cookie `/rpc`(`login_state`) 200；29 天仍 200；31 天 401 | `test_remember_cookie_end_to_end`（同函数） | （同上） |
| 6 | 不带 remember 登录的 Cookie 重启后 `/rpc` 401（且 Set-Cookie `max-age=604800`、无 2592000） | `test_plain_cookie_rejected_after_restart_end_to_end` | 6 |
| 7 | 旧 auth.json（仅 4 键）→ 初始化成功、补齐两新键、原字段值不变、旧密码仍可登录 | `test_legacy_auth_json_is_backfilled` | 9 |
| 8 | `REMEMBER_DAYS == 30`、`remember_seconds == 30*86400`、`session_seconds == 168*3600` | `test_remember_days_constant_and_manager_defaults` | 3 |
| 9 | 未设置密码时初始化不创建 auth.json | `test_no_auth_file_is_created_when_unconfigured` | 2 |
| 10 | `setup()` 成功即写入 `session_key`(64 hex) 与 `revoked_sessions=[]` | `test_setup_writes_remember_fields` | 4 |
| 11 | `cookie_kwargs()` 无参 = 现状五键；`max_age`/`secure` 覆盖生效 | `test_cookie_kwargs_signature_is_backward_compatible` | 5 |
| 12 | remember 仅接受 JSON 布尔 `true`（`"true"`/`1`/`"yes"`/`null` 均按普通登录 `max-age=604800`） | `test_remember_rejects_non_boolean_values`（×4 参数，每参 5 断言） | 5×4 |

新增断言合计：**88**（AST 语句口径）。配套独立复跑脚本（`restart-check.log`）14 项检查与上述 1/2/3/4/7/9/10 点交叉验证，全 PASS。

---

## 3. 门规命令原文片段

均在本 worktree 根执行；完整输出见同目录 `pytest.log`、`node.log`、`restart-check.log`。

```
$ python -m pytest tests -q
253 passed, 2 warnings in 13.90s          # 基线 238 + 新增 15 = 253，全绿

$ python -m pytest tests/test_server_auth.py -q
20 passed, 2 warnings in 2.23s            # 单独复跑（原 5 + 新 15）

$ node --test "tests/js/*.test.mjs"
ℹ tests 67  ℹ pass 66  ℹ fail 0  ℹ todo 1   # 基线 66+1+0，无回退

$ python -c "import server_app; print(len(server_app.RPC_METHODS))"
56                                          # RPC 恒 56，零新增
```

三个日志文件尾部均按门规固定写 `T60 结论：PASS（N 项）`：pytest.log（253 项）、node.log（66 项）、restart-check.log（14 项）。

---

## 4. 接口 / 数据形状备忘

**auth.json 新键（向后兼容，读-改-写补齐，绝不丢字段；旧版本读取时忽略多余键）：**

| 键 | 形状 | 说明 |
| --- | --- | --- |
| `session_key` | 64 位 hex 字符串（`secrets.token_bytes(32).hex()`） | r1 令牌 HMAC 签名密钥；绝不写日志/异常文本/证据 |
| `revoked_sessions` | `[{"id": "<sha256(token) hex>", "expires_at": <int epoch>}]` | 吊销列表，只存 sha256，令牌不落盘明文 |

写入均为 tmp + `os.replace` 原子写，POSIX 下 `chmod 0o600`；补齐仅发生在 auth.json 已存在时（未设置密码不建文件）；`setup()` 首写即含两新键。

**长效令牌格式（仅 remember=true）：**

- `r1.<expires_at>.<hmac_hex>`，`expires_at = now + 30*86400`
- `hmac_hex = HMAC-SHA256(key=bytes.fromhex(session_key), msg=f"r1.{expires_at}".encode()).hexdigest()`
- 校验：内存会话优先 → `r1.` 识别 → 验签 → `expires_at > now` → sha256(token) 不在吊销列表（顺带清理过期吊销项）；任何失败返回 False 不抛异常
- `logout(token)`：内存 pop 保持现状；对验签通过的 `r1.` 令牌追加 `{"id", "expires_at"}` 进 `revoked_sessions` 并原子持久化 → 重启后旧令牌仍失效

**签名变更（均向后兼容，无参/旧参调用零变化）：**

- `AuthManager.login(self, password, client_key, remember=False)` —— 新 keyword 参数
- `AuthManager.cookie_kwargs(self, max_age=None, secure=None)` —— 新 keyword 参数；无参返回与旧版逐键相同
- `_set_session_cookie(response, auth, session_token, *, max_age=None)` —— `server_app.py` 统一入口
- `AuthManager.remember_seconds`（30 天）新属性；模块常量 `REMEMBER_DAYS = 30`、`REMEMBER_PREFIX = "r1."`

**HTTP 契约：**

- `POST /auth/login`：body 新可选键 `remember`，仅 JSON 布尔 `true` 生效（`is True` 判定）；成功时 Set-Cookie `max-age` 为 2592000（remember）/ 604800（默认 168h）
- `POST /auth/setup`：保持现状（`max_age=None` → `session_seconds`）
- `POST /auth/logout`：`delete_cookie(COOKIE_NAME, path="/")` 保留；服务端吊销由 `auth.logout` 内部完成
- `HttpOnly` / `SameSite=Strict` 不变；`Secure` 仍取 `auth.cookie_secure`（T61 联动项未提前实现）

**前端：**

- `web/setup.html`：`#login-form` 密码框下新增 `<label class="setup-check-row"><input id="login-remember" type="checkbox"><span>记住此设备（30 天）</span></label>`，默认不勾选；不勾选时流程与现状完全一致
- `web/js/setup.js`：登录提交体改为 `{ password: ..., remember: $("login-remember").checked }`，其余流程零改动
- `web/css/setup.css`：仅追加 `.setup-check-row` 一组最小样式（flex 行 + 16px checkbox + `accent-color: var(--color-primary)`，字号 `--text-sm`），复用既有令牌，零裸动画

**协议红线复核：** RPC 56 不变；未新增方法；未动 `RPC_METHODS` / `_PHASE_NEXT` / 任务 JSON 键 / `advanced_match` / compose / README / 其他测试文件；零新依赖、零外部 CDN、零联网、零真实账号数据。

---

## 5. 偏差清单

与任务书逐条比对，以下为实现层的刻意解读/加固，如实登记：

1. **验签比较器**：任务书 §3.2 字面为 "`hmac.compare_digest` 校验签名"，实现采用项目既有 `tokens_equal(signature, expected)`（server_auth.py 内部即 UTF-8 编码后的 `hmac.compare_digest`，见 `tokens_equal` docstring）。原因：cookie 值可含非 ASCII 字符，`hmac.compare_digest` 直接比较非 ASCII `str` 会抛 `TypeError`，将认证失败变成 500。语义等价（常量时间），且已用畸形样例 `"r1.123." + "é"*64` 覆证不抛异常。测试 `test_tampered_and_malformed_remember_tokens_fail` 对该行为负责。
2. **吊销写入的"格式正确"界定**：任务书 §3.2 logout 条目写"若令牌是格式正确的 `r1.` 令牌"，实现要求"格式正确 **且验签通过**"才写吊销项。原因：`/auth/logout` 本身不要求已授权（保持现状），若任意伪造 `r1.` 串都能进吊销列表，攻击者可无限灌大 auth.json。验签不过的令牌本就无效，无需吊销。
3. **坏 `session_key` 的处理**：任务书 §3.1 说"缺少这些键时……补齐"。实现将"存在但非法"（非 64 位 hex）的 `session_key` 同样重新生成。原因：空/坏 key 等于公开密钥，攻击者可据此自签有效 `r1.` 令牌（fail-open）；重新生成是 fail-closed 加固，且该键本属本任务新造，不涉及"绝不丢字段"的既有字段。
4. **`remember=True` 而 `session_key` 不可用**（理论不可达：初始化补齐保证）：`login` 退回普通内存会话，而非签发空 key 令牌或报错，保证登录可用性优先。
5. **`_revoke_remember` 对"已在吊销列表"的令牌**：直接 return 不重写文件（避免同令牌重复登出追加重复项），与任务书"追加"语义的幂等化处理。
6. **`session_valid` 对不可哈希类型**（如 list 令牌）仍与现状一致抛 `TypeError`：现状行为未扩大也未修复，任务书"畸形令牌不得抛异常"按字符串/None/int 口径覆盖（15 样例全过），未越界修复现状语义。

其余各点与任务书逐字一致；无未登记偏差。

---

## 6. 证据文件清单

| 文件 | 内容 |
| --- | --- |
| `evidence/v2.4.1/T60/T60_result.md` | 本报告 |
| `evidence/v2.4.1/T60/pytest.log` | `$ python -m pytest tests -q`（253 passed）+ `$ python -m pytest tests/test_server_auth.py -q`（20 passed）完整输出，尾部结论行 |
| `evidence/v2.4.1/T60/node.log` | `$ node --test "tests/js/*.test.mjs"`（66 pass / 0 fail / 1 todo）完整输出，尾部结论行 |
| `evidence/v2.4.1/T60/restart-check.log` | 独立于 pytest 的重启专项验证（临时目录 + 虚构密码；令牌打码、无 session_key/密码明文），14/14 PASS，尾部结论行 |

隐私扫描：三个日志经正则扫描，0 处 64 位 hex 串（无 session_key/HMAC 材料泄露）、0 处密码明文；产品代码无任何 `session_key` 相关 log/print 调用。

---

T60_DONE
