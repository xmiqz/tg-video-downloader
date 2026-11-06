# AGENTS.md · TG 视频下载器

本文件适用于整个仓库。Codex 及支持 AGENTS 规范的工具在修改、测试、发布前必须遵守。

## 1. 分支与合并

- `main` 是唯一正式主线，必须始终保持可发布状态。
- 所有开发通过短期分支 + Pull Request 合入 `main`，默认 Squash Merge。
- 分支命名：`feat/<topic>`、`fix/<topic>`、`hotfix/<topic>`、`docs/<topic>`。
- 禁止直接向 `main` 推送代码，禁止强推 `main`，禁止删除 `main`。
- 不再长期维护 `dev/*` 平行主线；发布冻结期如需独立分支，使用 `release/<version>`，发布后删除。
- 临时分支合并后清理；仍有未合并价值的改动不得直接删除。

## 2. 修改前

1. 先检查仓库真实状态：`git status`、当前分支、与 `origin/main` 的 ahead/behind。
2. 确认没有覆盖他人未提交改动；不要复用来源不明的 dirty worktree。
3. 涉及发布时，先核对远端分支、Tag、Release、CI 与 GHCR 的真实状态，不依赖历史聊天结论。

## 3. 测试与验证

修改后必须运行对应测试，不得伪造或推断结果：

```bash
python -m pytest -q
node --test "tests/js/*.test.mjs"
python build.py dist        # 涉及桌面端构建时
docker compose config       # 涉及 Compose 时
```

- 后端测试覆盖服务、认证、RPC、Compose 契约与版本号。
- 前端测试覆盖视图逻辑、动效令牌、主题与截图矩阵契约。
- 涉及 Docker 时必须验证 `linux/amd64` 与 `linux/arm64` 构建；无法原生运行 arm64 时用 QEMU，并明确标注这不是原生硬件验证。
- 无法执行的测试必须写明原因，不能标记为通过。

## 4. 发布纪律

- Tag 是版本事实来源；Tag 创建后不可移动、不可覆盖。需要修复时发布新的 patch Tag。
- 正式 Tag 必须指向已通过 CI 的 `main` 提交。
- 桌面端、Docker 镜像、Release 资产与 Release Notes 必须对应同一个 Git SHA。
- 构建号统一使用 7 位提交短 SHA。
- 发布前核对：Tag 指向、Release 的 prerelease 状态、资产 SHA-256、GHCR manifest 的 amd64/arm64。
- 未经最终确认，不得创建正式 Tag、发布 Release 或推送正式 Docker 镜像。
- 不得把 Token、密码、`.session`、`auth.json`、`.setup-token` 或内网地址写入仓库与截图。

## 5. 数据与兼容性

- 任务 JSON、数据目录、导出包、Compose 卷名与端口属于兼容性契约，改动前必须评估迁移方案。
- 不得设计需要删除用户数据卷才能升级的方案。
- Docker 固定以 `10001:10001` 运行；宿主机 bind mount 必须先 `chown 10001:10001`。
- 镜像内已写入 `TGDL_VERSION` / `TGDL_BUILD`，Compose 不得用空值覆盖。

## 6. 代码边界

- 不做与任务无关的大规模重构，不顺手改无关业务代码。
- 修复优先解决根因，不用表面补丁掩盖问题。
- 保持既有代码风格；新增规则优先写入本文件或 `docs/`，不要散落在注释里。
- 提交前检查 `git diff`，排除行尾噪声、构建产物、pytest 临时目录与个人数据。

## 7. 关键目录

- `web/`：零构建前端（ES Module + CSS），入口 `index.html` / `taskedit.html` / `setup.html`。
- `server_app.py` / `server_auth.py` / `server_config.py`：浏览器与 NAS 服务。
- `gui.py` / `wv2_*`：Windows WebView2 桌面壳。
- `.github/workflows/ci.yml`：PR 与 main 的检查。
- `.github/workflows/build.yml`：Tag 触发的多平台桌面构建与 Release。
- `.github/workflows/container.yml`：Docker 验证、GHCR 发布与发布后校验。
