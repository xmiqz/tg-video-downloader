# T67 图标与品牌位（FR-54 资产） — 结果

日期：2026-11-01 · 分支 `v2.5-dev` · 基线 `4a47c5d`（T65）

## 1. 交付内容

| 文件 | 变更 | 行数 |
| --- | --- | --- |
| `web/favicon.svg` | 重写：由生产几何派生（viewBox `-60 -60 120 120`，64px） | +31 / −10 |
| `web/assets/xian-icon.svg` | 新增：同一母版 256px 派生 | +35 |
| `build/tvg.ico` | 新增：16/24/32/48/64/128/256 七档，PNG 载荷、32bpp | 二进制 39896 B |
| `build.py` | GUI 增 `--icon` + `assets/` 随包；TUI 增 `--icon` | +4 |
| `wv2_native.py` | 窗口类 `hIcon`/`hIconSm` 从同一 ICO 载入，缺失静默降级 | +47 / −2 |
| `.gitignore` | `build/` 仍忽略产物，仅放行 `build/tvg.ico` | +4 |
| `tests/test_icon_assets.py` | 新增 6 用例：目录/载荷/几何/接线/冻结路径 | +88 |
| `tests/test_server_app.py` | 新增 1 用例：favicon 由静态目录下发 | +12 |

## 2. 实现口径（对齐任务书 §2 T67）

- **不存在第二份几何**：favicon 与母版均由生产 `web/js/motion/xian.js` 的
  `XIAN_GEOMETRY.canon` + `roundedPath()` 一次性派生（生成器在临时目录，未入库）；
  两个 SVG 都带 `data-geometry="nose:56,-18 wing:-44,-20 crown:-10,-48 keel:-24,44"`
  与 `data-radius="15.7"`，与运行时字符完全一致（设计稿的 `radius=7.4` 未采用，
  避免与 T65 的 15.7 分叉）。
- **16/24px 只留剪影**：平色两块（底 `#4F7CFF`、下翼 `#1B4FC4`、上翼 `#F5FAFC`），
  去掉光弦、节点、玻璃渐变与亮边；32px 起使用完整派生（tile 渐变、上下翼渐变、
  前缘亮边、三节点、三段光弦）。
- **栅格化**：Edge `--headless=new --default-background-color=00000000
  --force-device-scale-factor=1` 逐档按目标像素渲染（窗口大于目标后左上角裁剪），
  避免"先画 256 再缩"造成的细线丢失；ICO 用手写目录 + PNG 载荷封装，七档全 32bpp。
- **exe 图标**：GUI 与 TUI 两个 Windows 构建都用同一枚 `build/tvg.ico`；
  GUI 额外把 ICO 以 `--add-data` 放进 `assets/`，供冻结态窗口类载入。
- **窗口图标**：`wv2_native._app_icon_path()` 冻结态取 `_MEIPASS/assets/tvg.ico`，
  源码态取 `<repo>/build/tvg.ico`；`LoadImageW(IMAGE_ICON, LR_LOADFROMFILE)` 按
  `SM_CXICON`/`SM_CXICONSM` 取大小，任一异常返回 `None`，窗口照常创建。
- **品牌位复用**：壳品牌位 / 登录徽章 / 完成 Toast 已在 T65 走 `mountXian()`
  同源派生链，本任务不改动它们。
- **页面 favicon**：`index.html` 与 `setup.html` 均指向 `/web/favicon.svg`，
  由 `StaticFiles(directory=web_dir)` 下发，新增用例断言 200 + `image/svg+xml`。

## 3. 目检

| 内容 | 文件 |
| --- | --- |
| ICO 七档 1:1（浅/深底）+ 小尺寸 4 倍放大 + 任务栏近似底色 | `shot_ico_sheet.png` |
| 页面 favicon（模拟标签页 16px + 16/24/32/48/64/128 尺寸行，浅色） | `shot_favicon_light.png` |
| 同上（深色） | `shot_favicon_dark.png` |
| 渲染夹具 | `harness_favicon.html` |
| 只读探针（目录/载荷/几何/载入/接线/引用/RPC） | `probe.txt` |

结论：七档全部解码为声明尺寸的正方形 PNG，透明边一致（tile 占 86.7%，四周留白）；
16px 仍能分辨"白上翼 + 深蓝下翼"两块折面，24px 折面清晰，32px 起光弦与节点可辨；
深色底与任务栏近似底色（`#202020`）上白色前缘成立，无暗边、无糊团。

**残余项（如实记录）**：Windows 资源管理器/任务栏的真实目检需要打包产物，
本批只验证到"ICO 目录正确 + `LoadImageW` 实际载入成功（大小图标句柄均非空）+
PyInstaller `--icon` 接线"。打包后的 exe 图标与窗口标题栏图标目检留在 T68 发版验收。

## 4. 门规结果

```
python -m pytest tests -q            → 341 passed（新增 7）
node --test "tests/js/*.test.mjs"    → 130 tests / 129 pass / 1 todo / 0 fail
python probe_t67.py                  → RPC_METHODS = 56
```

红线自查：RPC 恰 56；任务 JSON 顶层 7 键未动；bundle 版本 1 未动；WS 帧 ≤40 未动；
`_PHASE_NEXT` 未改；不做存量任务回捞；证据与日志不含正文/频道名/文件名/token
（探针输出中的仓库绝对路径替换为 `<repo>`）。
