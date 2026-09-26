"""偏移敏感性扫描：解析临界点 + 稳定区间分段 + 代表值重算。

对选定来源的 ``clock_offset`` 在 ``[lo, hi]`` 内做"假设扫描"，其余假设参数
（含页面未保存的 overrides）保持不变。

为什么可以解析求临界点
----------------------
事件估计时刻对 offset 是斜率 -1 的线性函数（``t_est = t_local - offset - …``），
因此每条约束边 a → b 的间隔区间端点

    gap_lo(x) = lo_b(x) - hi_a(x)
    gap_hi(x) = hi_b(x) - lo_a(x)

都是扫描变量 x（该来源 offset）的一次函数，边状态（ok / boundary / impossible）
只在 gap_lo 或 gap_hi 穿越 0 的**临界点**处改变。把所有临界点排序后，扫描范围
被切成若干"边状态稳定"的连续区间，每段只需取一个代表值（中点）调用
``engine.compute`` 全量推理，即可与当前结果对比新增 / 消失的冲突。

offset 抵消
-----------
若同一条边两端都属于被扫描来源，两端区间随 offset 同步平移，gap 与 x 无关
（x 在差值中抵消），不产生临界点 —— 典型例子是同来源的 source_order 时序边。
"""
from __future__ import annotations

import math
from typing import Any

from . import engine, scenarios

MAX_INTERVALS = 60  # 每段一次全量重算，限制段数防止过宽范围拖垮服务
MAX_OFFSET_ABS = 31_536_000 * 10  # 上下界绝对值上限（±10 年，防御性）


class SensitivityError(ValueError):
    """可直接映射为 HTTP 4xx 的扫描请求错误。"""


# ------------------------------------------------------------ 结果摘要

def _brief_conflict(c: dict) -> dict:
    return {
        "signature": c["signature"],
        "id": c["id"],
        "kind": c["kind"],
        "severity": c.get("severity"),
        "events": c["events"],
        "explanation": c["explanation"],
    }


def _brief_edge(e: dict) -> dict:
    return {
        "edge_id": e["id"],
        "from": e["from"],
        "to": e["to"],
        "kind": e["kind"],
        "note": e["note"],
        "gap_lo": e["gap_lo"],
        "gap_hi": e["gap_hi"],
        "gap_lo_text": e["gap_lo_text"],
        "gap_hi_text": e["gap_hi_text"],
    }


def _impossible_index(result: dict) -> dict[tuple, dict]:
    """{(from, to, kind): 摘要} —— 当前推理结果中所有不可能边。"""
    out: dict[tuple, dict] = {}
    for e in result["edges"]:
        if e["status"] == "impossible":
            out[(e["from"], e["to"], e["kind"])] = _brief_edge(e)
    return out


def _conflict_index(result: dict) -> dict[str, dict]:
    """{signature: 摘要} —— 与方案对比相同的稳定签名匹配口径。"""
    return {c["signature"]: _brief_conflict(c) for c in result["conflicts"]}


def _diff_index(base: dict, other: dict, sort_key) -> tuple[list, list]:
    added = sorted((other[k] for k in other if k not in base), key=sort_key)
    removed = sorted((base[k] for k in base if k not in other), key=sort_key)
    return added, removed


def _edge_sort_key(brief: dict) -> tuple:
    return (brief["from"], brief["to"], brief["kind"])


def _conflict_sort_key(brief: dict) -> str:
    return brief["signature"]


def _stats(res: dict) -> dict:
    s = res["stats"]
    return {
        "impossible_edges": s["impossible_edges"],
        "boundary_edges": s["boundary_edges"],
        "conflict_count": s["conflict_count"],
        "ambiguity_group_count": s["ambiguity_group_count"],
    }


# ------------------------------------------------------------ 主入口

def scan_offset_sensitivity(source_id: Any, lo: Any, hi: Any,
                            overrides: dict | None = None) -> dict:
    """扫描 ``source_id`` 的 clock_offset ∈ [lo, hi]，返回稳定区间分段结果。

    返回结构：
    - baseline：当前生效假设（含未保存 overrides）下的 offset / 不可能边 / 冲突签名；
    - critical_points：全部解析临界点（含范围外，便于提示）；
    - intervals：边状态稳定的连续区间，每段含代表值重算结果与相对当前的新增/消失；
    - message：范围内无临界点等需要明确提示审计员的情形（此时 intervals 为空）。
    """
    source_id = str(source_id or "").strip()
    if not source_id:
        raise SensitivityError("必须指定要扫描的来源 source_id")
    try:
        lo_f, hi_f = float(lo), float(hi)
    except (TypeError, ValueError) as exc:
        raise SensitivityError(f"扫描上下界必须是数值: lo={lo!r}, hi={hi!r}") from exc
    if not (math.isfinite(lo_f) and math.isfinite(hi_f)):
        raise SensitivityError("扫描上下界必须是有限数值（不接受 NaN / 无穷）")
    if not lo_f < hi_f:
        raise SensitivityError(
            f"扫描上下界无效：需要 下界 < 上界（收到 [{lo_f}, {hi_f}]）")
    if abs(lo_f) > MAX_OFFSET_ABS or abs(hi_f) > MAX_OFFSET_ABS:
        raise SensitivityError(
            f"扫描上下界超出合理范围（±{MAX_OFFSET_ABS:.0f} 秒），请缩小到可信的时钟偏差区间")

    stored_sources, raw_events, raw_edges = engine.load_model()
    if source_id not in stored_sources:
        raise SensitivityError(f"来源不存在: {source_id}")

    # 页面当前（可能未保存）的假设即扫描基线；已删除来源的陈旧 override 忽略
    clean_ov = scenarios.clean_assumptions(overrides or {}, strict=False)
    baseline = engine.compute(clean_ov)

    cur_offset = next((float(s["effective"]["clock_offset"])
                       for s in baseline["sources"] if s["id"] == source_id), 0.0)
    nodes_by_id = {e["id"]: e for e in baseline["events"]}
    src_event_ids = {e["id"] for e in raw_events if e["source_id"] == source_id}

    # ------------------------------------------------ 解析临界点
    # 事件区间随扫描来源 offset 平移：lo(x) = lo(cur) + cur - x（斜率 -1）。
    # b∈s, a∉s：gap_lo(x)=0 → x* = cur + lo_b - hi_a；gap_hi(x)=0 → x* = cur + hi_b - lo_a
    # a∈s, b∉s：gap_lo(x)=0 → x* = cur + hi_a - lo_b；gap_hi(x)=0 → x* = cur + lo_a - hi_b
    criticals: list[dict] = []
    scanned = invariant = 0
    for r in raw_edges:
        a_in = r["from_event"] in src_event_ids
        b_in = r["to_event"] in src_event_ids
        if a_in and b_in:
            invariant += 1  # 两端同属该来源：offset 抵消，gap 恒定，不制造临界点
            continue
        if not a_in and not b_in:
            continue
        a = nodes_by_id.get(r["from_event"])
        b = nodes_by_id.get(r["to_event"])
        if a is None or b is None:
            continue  # 悬空端点（导入时已告警），引擎同样跳过
        scanned += 1
        if b_in:
            roots = (("gap_lo", cur_offset + b["lo"] - a["hi"]),
                     ("gap_hi", cur_offset + b["hi"] - a["lo"]))
        else:
            roots = (("gap_lo", cur_offset + a["hi"] - b["lo"]),
                     ("gap_hi", cur_offset + a["lo"] - b["hi"]))
        for which, x in roots:
            criticals.append({
                "offset": round(x, 6),
                "edge_id": r.get("id"),
                "from": r["from_event"],
                "to": r["to_event"],
                "kind": r.get("kind", "causal"),
                "which": which,  # 在该 offset 处穿越 0 的间隔端点
            })
    criticals.sort(key=lambda c: (c["offset"], c["from"], c["to"], c["which"]))

    base_impossible = _impossible_index(baseline)
    base_conflicts = _conflict_index(baseline)

    resp: dict[str, Any] = {
        "scan_type": "offset-sensitivity",
        "source": {"id": source_id,
                   "name": stored_sources[source_id].get("name", source_id)},
        "range": {"lo": lo_f, "hi": hi_f},
        "baseline": {
            "offset": round(cur_offset, 6),
            "offset_in_range": lo_f <= cur_offset <= hi_f,
            "stats": _stats(baseline),
            "impossible_edges": sorted(base_impossible.values(), key=_edge_sort_key),
            "conflicts": sorted(base_conflicts.values(), key=_conflict_sort_key),
        },
        "edges": {"total": len(raw_edges), "scanned": scanned,
                  "invariant": invariant},
        "critical_points": criticals,
        "intervals": [],
        "message": None,
    }

    # ------------------------------------------------ 明确提示的退化情形
    if scanned == 0:
        if invariant:
            resp["message"] = (
                f"该来源参与的 {invariant} 条约束边两端都属于该来源，"
                "offset 整体抵消，边状态与 offset 无关，无临界点可扫描。")
        else:
            resp["message"] = ("该来源的事件未参与任何约束边，"
                               "offset 变化不影响任何边状态，无临界点可扫描。")
        return resp

    in_range = sorted({c["offset"] for c in criticals
                       if lo_f < c["offset"] < hi_f})
    if not in_range:
        nearest = min((c["offset"] for c in criticals),
                      key=lambda x: min(abs(x - lo_f), abs(x - hi_f)), default=None)
        hint = (f"；最近的临界点位于 offset={nearest} s，可调整扫描范围后重试"
                if nearest is not None else "")
        resp["message"] = (
            f"扫描范围 [{lo_f}, {hi_f}] 内没有临界点：该来源 offset 在此区间内变化时，"
            f"全部 {scanned} 条相关约束边的状态保持稳定{hint}。")
        return resp

    if len(in_range) + 1 > MAX_INTERVALS:
        raise SensitivityError(
            f"范围内临界点过多（{len(in_range)} 个，将产生 {len(in_range) + 1} 段，"
            f"上限 {MAX_INTERVALS} 段），请缩小扫描范围。")

    # ------------------------------------------------ 分段代表值重算
    bounds = [lo_f] + in_range + [hi_f]
    for seg_lo, seg_hi in zip(bounds, bounds[1:]):
        rep = round((seg_lo + seg_hi) / 2, 6)
        ov = {sid: dict(params) for sid, params in clean_ov.items()}
        ov.setdefault(source_id, {})["clock_offset"] = rep
        res = engine.compute(ov)
        imp = _impossible_index(res)
        con = _conflict_index(res)
        imp_added, imp_removed = _diff_index(base_impossible, imp, _edge_sort_key)
        con_added, con_removed = _diff_index(base_conflicts, con, _conflict_sort_key)
        resp["intervals"].append({
            "lo": round(seg_lo, 6),
            "hi": round(seg_hi, 6),
            "representative": rep,
            "contains_current": seg_lo <= cur_offset <= seg_hi,
            "stats": _stats(res),
            "impossible_edges": sorted(imp.values(), key=_edge_sort_key),
            "impossible_added": imp_added,
            "impossible_removed": imp_removed,
            "conflict_signatures": sorted(con),
            "conflicts_added": con_added,
            "conflicts_removed": con_removed,
            "conflicts_persisted": len(con) - len(con_added),
        })
    return resp
