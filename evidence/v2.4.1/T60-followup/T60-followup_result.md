# T60-followup 结果报告：remember token 绑定设备指纹 + TTL 7 天

## 0. 基线与开工条件

- worktree：`c:\Users\xqz\Documents\trae_projects\dayly\_arm_inspect\repo\tgvd-v25-t60b`（分支 `v2.5-dev-t60b`）
- 基线 commit：`ce91391`（含 T60/T63）
- 开工核查：`git status --short` 为空、`git rev-parse --short HEAD` = `ce91391` → 满足
- 接续说明：上一会话仅完成开工核查与只读阅读（server_auth.py / server_app.py 定位）后中断，现场无任何改动（diff 为空、无 evidence 目录）；本次从实现阶段全新开始，未重做已完成的只读核查部分。

## 1. 改动清单（git status / diff --numstat）

```
git status --short
 M server_app.py
 M server_auth.py
 M tests/test_server_auth.py
?? evidence/v2.4.1/T60-followup/        ← 本报告与日志

git diff --numstat
30      16      server_app.py
102     35      server_auth.py
369     31      tests/test_server_auth.py
```

恰 3 个产品/测试文件改动，零夹带；未 commit/add/push/merge/rebase/tag。

## 2. 实现清单对照（任务书 §3 逐项）

| # | 要求 | 落点 |
|---|------|------|
| 1 | `REMEMBER_DAYS = 7`；`remember_seconds = 7*86400` | server_auth.py:34-36（常量名保留）、`__init__` 中 `REMEMBER_DAYS * 86400` 不变即 604800 |
| 2 | 令牌格式 `r2.<expires_at>.<ua8>.<hmac>`；`ua8 = sha256(UA utf-8)[:8]`，空 UA 用字面量 `-` 参与计算；签名原文 `r2.<expires_at>.<ua8>`，key=`session_key`；`login(remember=True, user_agent="")` 签发；`session_valid`/`_remember_parts` 用当前 UA 重算比较；旧 `r1.*` 一律无效；畸形/超长/非 ASCII 不抛异常 | `_ua_fingerprint()`（server_auth.py:39-58）、`_new_remember_session(user_agent)`、`_remember_parts(token, user_agent="")`（4 段解析 + UA 比较 + HMAC 验签，None 化一切失败）；`REMEMBER_PREFIX="r2."` 使 r1 前缀令牌直接短路无效 |
| 3 | 撤销表逻辑不变：logout 只对验签通过且 UA 匹配的 r2 令牌写 `sha256(完整 token)`；读取清理过期；auth.json 不新增顶层必填字段、原子写 + 0600 | `_revoke_remember(token, user_agent)`（先 `_remember_parts` 验签+UA 才落盘）、`_revocation_active` 保持原样；`_write_record`/`_persist_record` 未动 |
| 4 | server_app 全部校验拿到 UA：新增 `_session_valid(request, auth)` helper；替换 6 处调用（`_is_rpc_authorized`、`_asset_authorized`、`/auth/status`、`/`、`/login`、`/events` websocket 用 `ws.headers`）；`/auth/setup`、`/auth/login` 传请求 UA；`/auth/login` remember 分支 max_age 用 `auth.remember_seconds`（7 天），未勾选仍 `session_seconds` | server_app.py:231-239 helper；六处调用点 + `/auth/logout` 也传 UA（撤销需 UA 匹配） |
| 5 | 不新增 RPC、不改 Cookie 属性、不加依赖 | `cookie_kwargs` 未动（HttpOnly/SameSite=Strict/Path=/）；RPC_METHODS = 56（见 §4 命令 4）；零新 import |

补充：setup 生成的普通内存会话不绑定 UA（`_new_session()` 不变，user_agent 仅对称传参，行为保持现状）。

## 3. 测试证据（tests/test_server_auth.py）

### 3.1 更新的 T60 既有用例（6 处）

| 用例 | 更新点 |
|------|--------|
| `test_remember_days_constant_and_manager_defaults` | `REMEMBER_DAYS == 7`、`remember_seconds == 7*86400` |
| `test_remember_login_survives_restart` | `r2.` 前缀、`token.count(".")==3`、TTL 边界 6 天有效 / 7 天+1s 无效 |
| `test_plain_login_does_not_survive_restart` | plain token 断言改为 `not startswith("r2.")` |
| `test_tampered_and_malformed_remember_tokens_fail` | 四段拆解；篡改签名/exp/ua8；跨实例伪造（`r2.<exp>.<ua8>` 原文重签仍无效）；畸形矩阵新增 list 输入与 `r2.` 形态（含 `r2...`、3 段、5 段、非 ASCII ua8、400 位 exp 等） |
| `test_remember_cookie_end_to_end` | `max-age=604800`、令牌 `r2.`、第 6 天仍 200、第 7 天+1s 401 |
| `test_plain_cookie_rejected_after_restart_end_to_end` | 断言改为 `not startswith("r2.")`；`max-age=604800`（与会话 TTL 一致） |

（`test_remember_rejects_non_boolean_values`、`test_remember_logout_revocation_survives_restart`、`test_cookie_kwargs_signature_is_backward_compatible` 无需改动即继续通过。）

### 3.2 新增 T60_followup 用例（8 项，全部 PASS）

1. `test_T60_followup_remember_token_binds_user_agent` — 带 UA 签发：`ua8 == sha256(UA)[:8]`；同 UA True、换 UA/空 UA/缺省 False；空 UA 与 None 均按 sha256("-") 铸造并对称验证；非 str 畸形 UA 永不匹配；非 ASCII UA（"浏览器/1.0 (测试代理)"）确定性哈希且仍绑定；r2 同 UA 同秒确定性同 token（特性登记）
2. `test_T60_followup_remember_ttl_is_exactly_seven_days` — TTL 恰 `7*86400`；到期前 1s True、恰好到期 False、过期 1s False（注入 clock 推进）
3. `test_T60_followup_forged_and_tampered_tokens_fail` — 伪造 exp（不重签）无效；篡改 ua8（不重签）无效；用本实例真实 key 自洽重签"他人 UA"令牌：原 UA 可用、跨 UA 无效；r1 旧格式（正确签名）一律 False
4. `test_T60_followup_malformed_inputs_never_raise` — token：None/""/int/float/list/tuple/dict/尾部脏字符/NUL/4000 位 exp/非 ASCII 段/带空格段全 False 无异常；`_remember_parts` 直测 list/None → None；UA：None/123/list/dict/""/超长非 ASCII 全 False 无异常
5. `test_T60_followup_logout_revocation_and_rebuild` — 登出仅撤销匹配 UA 的令牌；mismatched UA 登出不落撤销条目（json 内 id 集合不变）；重建 AuthManager（同 auth.json）：已登出仍 False、未登出仍 True
6. `test_T60_followup_plain_session_is_not_ua_bound` — `remember=false` 走内存会话：任意 UA 均有效；重启后失效
7. `test_T60_followup_end_to_end_ua_mismatch_returns_401` — `/auth/login`(remember, UA-A) → Set-Cookie `max-age=604800`；新 app 实例：同 UA `/rpc` 200、换 UA 401、空 UA 401；`/auth/status` authenticated 随 UA 绑定翻转
8. `test_T60_followup_end_to_end_logout_binding` — 他人 UA 登出不破坏令牌（仍 200）；本人 UA 登出后 401；重启后撤销仍生效 401

### 3.3 命令输出片段（四条必跑，全文见 pytest.log / node.log）

```
$ C:\Users\xqz\AppData\Local\Programs\Python\Python312-arm64\python.exe -m pytest tests -q
  279 passed, 4 warnings in 8.83s          ← 基线 271 全绿 + 新增 8 项
$ C:\Users\xqz\AppData\Local\Programs\Python\Python312-arm64\python.exe -m pytest tests/test_server_auth.py -q
  28 passed, 4 warnings in 1.00s
$ node --test "tests/js/*.test.mjs"
  pass 66 / fail 0 / todo 1                ← 与任务书口径 66+1 todo 一致
$ C:\Users\xqz\AppData\Local\Programs\Python\Python312-arm64\python.exe -c "import server_app; print(len(server_app.RPC_METHODS))"
  56
```

## 4. 偏差清单

1. **畸形 UA 语义强化**：任务书仅要求"空 UA 用 `-` 参与计算"且"畸形输入不抛异常"；实现将非 str 的畸形 UA（list/int/dict 等）定义为**永不匹配任何令牌**（`_ua_fingerprint` 返回 None，校验侧短路），比"并入空 UA 语义"更严格。空 UA（""/None/缺失 header）两侧对称使用 sha256("-")[:8]。已用测试固定（用例 1/4）。
2. **r2 令牌确定性**：无 nonce，同 UA + 同 expires_at 铸出相同令牌（r1 时代即如此）。会话彼此不可区分、撤销表以 sha256(完整 token) 键控亦自洽；测试用注入 clock 推进 120s 区分同 UA 两枚令牌。已在代码 docstring 与用例 1 注释登记。
3. **`/auth/logout` 同步传 UA**：任务书 §3.4 的 6 处替换未列 logout，但 §3.3"logout 只对验签通过且 UA 匹配的 r2 令牌写入"要求撤销链路拿 UA，故 handler 一并传入。属任务书内在要求的实现，非范围外新增。
4. **remember_seconds 与 session_seconds 数值巧合相等**：168h = 7d = 604800。既有用例 `test_remember_rejects_non_boolean_values` 的 `max-age=2592000 not in / max-age=604800 in` 断言原样继续通过，未改语义。
5. **端到端 UA 一致性依赖**：未显式带 UA 的既有端到端用例（httpx 默认 UA）在签发/校验两侧处于同一进程同版本，对 r2 绑定天然一致；显式一致/不一致 UA 的路径由新用例 7/8 覆盖。
6. **实现缺陷修正过程**（开发中发现并修复，最终态无缺陷）：`_remember_token_valid`/`_revoke_remember` 旧两元组解包未同步三元组（首轮 10 FAIL，修复后全绿）；空 UA 一度误返回字面 `"-"` 而非 sha256("-")（对比断言揪出，修正为"字面量参与哈希"）。
7. **接续现场**：上一会话异常中断发生于只读阶段，无半成品残留；本次开工重新核验（HEAD=ce91391、树净），从零实施，无重做已验证内容。

## 5. 红线自查

- RPC 白名单恒 56（命令 4 输出）；未新增方法、未改任务 JSON 顶层 7 键/meta 13 键、未动 `_PHASE_NEXT` 与 `advanced_match`、WS 帧 ≤40 未动。
- 零新依赖（两个文件 imports 无增删）；Cookie 属性（HttpOnly/SameSite=Strict/Path=/）与 Cookie 形态完全未动。
- 只修不加：未引入新功能；改动严格限定 server_auth.py、server_app.py、tests/test_server_auth.py、evidence/v2.4.1/T60-followup/。
- 隐私：未读下载目录/消息正文/频道名；证据仅含虚构测试值（虚构 UA、虚构密码占位、无真实 token/手机号/频道名/文件名）。
- 未执行 commit/add/push/merge/rebase/tag；提交留给调用方复核。

## 6. 交付物

- 本文件：`evidence/v2.4.1/T60-followup/T60-followup_result.md`
- `evidence/v2.4.1/T60-followup/pytest.log`（四条命令中三条 Python 命令全文 + 尾部结论行）
- `evidence/v2.4.1/T60-followup/node.log`（node --test 全文 + 尾部结论行）

T60_FOLLOWUP_DONE
