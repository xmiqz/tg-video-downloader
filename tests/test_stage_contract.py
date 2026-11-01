"""T66 舞台契约：STAGE_EDGES 必须与内核 _PHASE_NEXT 逐边一致。

舞台只消费事件，不自己猜进度；因此它暴露的阶段机不能比内核多一条边，
也不能少一条。另核对路径 8px 预采样的贴合误差 <0.5px，以及每帧不读
getPointAtLength（性能条款）。
"""
from __future__ import annotations

import math
import re
from pathlib import Path

import tg_video_dl as core

ROOT = Path(__file__).resolve().parents[1]
STAGE_JS = ROOT / "web" / "js" / "motion" / "stage.js"
STAGE_CSS = ROOT / "web" / "css" / "views" / "stage.css"

EDGE_RE = re.compile(r'\{\s*from:\s*"([a-z_]+)",\s*to:\s*"([a-z_]+)"\s*\}')
NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def stage_source() -> str:
    return STAGE_JS.read_text(encoding="utf-8")


def stage_phases() -> list[str]:
    text = stage_source()
    block = text.split("STAGE_PHASES = Object.freeze([", 1)[1].split("])", 1)[0]
    return re.findall(r'"([a-z_]+)"', block)


def stage_edges() -> set[tuple[str, str]]:
    return set(EDGE_RE.findall(stage_source()))


def kernel_edges() -> set[tuple[str, str]]:
    return {(src, dst) for src, nexts in core._PHASE_NEXT.items() for dst in nexts}


def test_phase_names_match_kernel():
    assert stage_phases() == list(core._PHASE_NEXT), "十态名称与内核逐字一致"


def test_stage_edges_are_exactly_kernel_edges():
    edges = stage_edges()
    want = kernel_edges()
    assert edges == want, "多出 " + repr(sorted(edges - want)) + "，缺少 " + repr(sorted(want - edges))


def test_terminal_states_have_no_outbound_flow():
    edges = stage_edges()
    for terminal in ("done", "aborted", "error"):
        assert not {e for e in edges if e[0] == terminal}, terminal + " 不得出边"


def test_no_self_loops_and_no_duplicate_edges():
    text = stage_source()
    found = EDGE_RE.findall(text)
    assert len(found) == len(set(found)), "STAGE_EDGES 不得重复"
    assert all(a != b for a, b in found), "不得自环"


# ---------------------------------------------------------- 路径贴合


def _segments(d: str) -> list[tuple[tuple[float, float], tuple[float, float], tuple[float, float]]]:
    nums = [float(x) for x in NUM_RE.findall(d)]
    assert len(nums) >= 8, "路径参数不足：" + d
    prev = (nums[0], nums[1])
    out = []
    for i in range(2, len(nums) - 5, 6):
        c = ((nums[i], nums[i + 1]), (nums[i + 2], nums[i + 3]), (nums[i + 4], nums[i + 5]))
        out.append((prev, c[0], c[1], c[2]))  # type: ignore[arg-type]
        prev = c[2]
    return out


def _cubic(a, b, c, d, t):
    u = 1 - t
    return (
        u * u * u * a[0] + 3 * u * u * t * b[0] + 3 * u * t * t * c[0] + t * t * t * d[0],
        u * u * u * a[1] + 3 * u * u * t * b[1] + 3 * u * t * t * c[1] + t * t * t * d[1],
    )


def _dense(d: str, per_seg: int = 400) -> list[tuple[float, float]]:
    pts = []
    for seg in _segments(d):
        # seg = (start, c1, c2, end)
        start, c1, c2, end = seg
        for j in range(per_seg + 1):
            pts.append(_cubic(start, c1, c2, end, j / per_seg))
    return pts


def _uniform_8px(d: str, step: float = 8.0) -> list[tuple[float, float]]:
    """与 stage.js sampleCubicPath(d, 8) 同算法：细弦长 + 等距重采样。"""
    poly = []
    for start, c1, c2, end in _segments(d):
        hull = (math.dist(start, c1) + math.dist(c1, c2) + math.dist(c2, end))
        count = max(4, math.ceil(hull / 0.5))
        poly.append(start)
        for j in range(1, count + 1):
            poly.append(_cubic(start, c1, c2, end, j / count))
    cum = [0.0]
    for i in range(1, len(poly)):
        cum.append(cum[-1] + math.dist(poly[i - 1], poly[i]))
    total = cum[-1]
    n = max(2, math.ceil(total / step))
    out = []
    for i in range(n + 1):
        target = i / n * total
        lo, hi = 0, len(cum) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if cum[mid] <= target:
                lo = mid
            else:
                hi = mid
        span = cum[hi] - cum[lo]
        f = (target - cum[lo]) / span if span else 0.0
        out.append((
            poly[lo][0] + (poly[hi][0] - poly[lo][0]) * f,
            poly[lo][1] + (poly[hi][1] - poly[lo][1]) * f,
        ))
    return out


def test_path_presampling_tolerance_below_half_pixel():
    wires = re.findall(r'"M[^"]*"', stage_source())
    assert len(wires) >= 9, "舞台至少 9 条数据线（桌面/紧凑/移动）"
    for raw in wires:
        d = raw[1:-1]
        dense = _dense(d)
        pts = _uniform_8px(d)
        worst = 0.0
        for x, y in pts:
            worst = max(worst, min(math.dist((x, y), q) for q in dense))
        assert worst < 0.5, "路径贴合误差应 <0.5px，实际 %.3f：%s" % (worst, d)
        assert math.dist(pts[0], dense[0]) < 1e-6, "起点必须落在曲线上"
        assert math.dist(pts[-1], dense[-1]) < 1e-6, "终点必须落在曲线上"


def test_stage_reads_no_live_path_and_presamples_at_8px():
    text = re.sub(r"//[^\n]*", "", stage_source())
    assert "getPointAtLength" not in text, "每帧不得读实时路径"
    assert re.search(r"sampleCubicPath\(d,\s*8\)", text), "启动预采样必须 8px/点"


def test_stage_host_is_offscreen_optimised():
    css = STAGE_CSS.read_text(encoding="utf-8")
    assert "content-visibility: auto" in css, "屏幕外舞台必须 content-visibility:auto"
