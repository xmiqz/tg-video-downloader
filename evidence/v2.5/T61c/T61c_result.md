# T61c 任务条目保住引用文本 — 证据

## 改动

| 文件 | 改动 |
| --- | --- |
| `tg_video_dl.py` | `make_task_item` 增 `ref_text`（`_clip_ref_text` 压缩空白 + 截 120 加省略号）；任务顶层键仍 7 个 |
| `gui.py` | `editor_data` 拟名补第 4 参 `ti.get("ref_text")`，算法不动 |
| `taskport.py` | `export_task` / `parse_bundle` 携带 `ref_text`（截 120）；`BUNDLE_VERSION` 保持 1 |
| `web/js/taskedit.js` | 正文为空且 `ref_text` 非空时单元格显示「引用：…」；详情另起一行「引用文本」 |
| `web/js/views/editor.js` | 同上（浏览器壳编辑页） |
| `tests/test_taskport.py` | 导出键集断言 8 键 → 9 键，注明 `ref_text` 属文本元数据 |
| `tests/test_task_ref_text.py` | 新增 8 条用例 |

## 键名与长度（仅键名，不含正文）

`probe.txt`：

- item 键 9 个：`date / duration / msg / name / ref_text / size / src / status / text`
- 任务顶层键 7 个：`channel / channel_title / created / folder / id / items / params`
- `ref_text` 落盘长度 121（120 + 省略号），导出截 120
- 导出 item 键同上 9 个；`BUNDLE_VERSION = 1`

## 门规

- `python -m pytest tests -q`：334 passed（基线 326 + 新增 8）
- `node --test "tests/js/*.test.mjs"`：71 pass + 1 todo + 0 fail
- `RPC_METHODS`：56，未增删
