# -*- coding: utf-8 -*-
"""kbfold.readorder — 阅读顺序（纯函数、零依赖、零 IO）。

移植自冻结底本 ``kb-readorder.js``（JS→Py 移植件）。只吃「行框」，出「输入下标的阅读序列」；
接口 order_lines(boxes, opts) 不变；不依赖任何模型/解析器。

主算法＝按列左边界聚类（tolK 三档 0.4/0.6/1.0，缺省 0.6；冻结参数，禁调参改名）：
  同一栏的行左边界严格对齐，与缝宽无关；间距类参数一律字体相对量，禁用页宽百分比。
主路径（每层递归）：
  ① 尺度 unit = 行框高度中位数；聚类容差 tol = max(3, tolK × unit)；
  ② 对 x0 排序后一维聚类；簇内行数 ≥2 才为候选栏；
  ③ 守卫（缺一不可，防误切）：a) 候选栏行数 ≥2；b) 相邻栏 x 区间重叠 < 中位栏宽 30%；
     c) 候选栏 ≥2 才分栏；d) 分栏后总覆盖宽度 ≥ 页宽 60%；
     e) 相邻栏之间须有正空白缝（扣 2px OCR 抖动）——缝宽法只作交叉校验。
  ④ 跨栏元素（标题/表格行）= 框宽 ≥1.6×行宽中位 且 与 ≥2 个候选栏区间实质重叠 ⇒ 抽出，按 y 分带；
  ⑤ 小于 2 行的簇并入最近候选栏，不单独成栏。
  单栏（或伪分栏）⇒ 水平单缝切段 → (round(y/8), x) 兜底排序。
增强（E1-E4，同底本）：页眉/页脚排除、低置信信号、竖排/旋转放弃分栏、表格行防护。
"""
from __future__ import annotations

import math

from ._compat import js_number, js_round

# 字体相对量（主判据，冻结参数）
COL_TOL_K = 0.6          # 聚类容差 = 0.6 × 行高中位（≈0.6 字宽）
COL_MIN_LINES = 2        # 候选栏最少行数
COL_OVERLAP_K = 0.3      # 相邻栏重叠上限 = 30% 中位栏宽
COL_COVERAGE_MIN = 0.6   # 分栏总覆盖宽度 ≥ 页宽 60%
COL_GAP_TOL_PX = 2       # 交叉校验：扣掉 2px OCR 抖动后仍须为正空白缝
COL_SPAN_WIDTH_K = 1.6   # 跨栏元素：框宽 ≥1.6×行宽中位

# 增强（E1-E4）
HF_FRAC = 0.08            # 页眉/页脚候选区：页面上/下各 8%
HF_NEIGHBOR_UNITS = 3     # 「没有同列邻居」：上下 3×行高内不得有 x 重叠的邻居
HF_X_OVERLAP = 0.5        # 邻居判定：x 重叠 ≥ 较窄框宽 50% 视为同列
LOW_GAP_UNITS = 0.5       # 低置信：簇间缝扣抖动后 < 0.5×行高
LOW_OVERLAP_LO = 0.2      # 低置信：相邻簇重叠率 ∈ [20%, 30%)
LOW_MIN_CLUSTER = 3       # 低置信：最小簇行数 <3
VERTICAL_ASPECT = 0.8     # 竖排：行框中位宽 < 中位高 × 0.8
MAX_TILT_DEG = 5          # 旋转：行框倾角中位 > 5°
TABLE_ROW_TOL_K = 0.1     # 表格行判定：y 中心差 ≤ 0.1×行高算同一行
TABLE_ROW_RATIO = 0.6     # ≥60% 的行带跨 ≥2 栏 ⇒ 判表格

# 旧口径（缝宽法）——仅导出兼容/诊断，主路径不再消费
VGAP_FRAC = 0.03
VGAP_LINE_FACTOR = 0
GAP_BLANK_MIN = 0.6
HGAP_FRAC = 0.012        # 页高 1.2%（水平切段；与栏数无关）
HGAP_MIN_PX = 8          # 水平缝绝对下限（px）
ROW_GROUP_PX = 8         # 兜底排序的「同一视觉行」容差
OVERLAP_TOL_PX = 2       # OCR 抖动容差（诊断面用）


def _num(v, d):
    # 缺键（None ≙ 底本 undefined ⇒ NaN）⇒ 兜底；合法值照常取用
    if v is None:
        return d
    n = js_number(v)
    return n if math.isfinite(n) else d


def _norm_boxes(boxes):
    """行框归一：缺省/非法值兜底为 0，并保证 x1≥x0、y1≥y0（脏输入不炸）；保留可选倾角（E3）。"""
    out = []
    for b in (boxes if isinstance(boxes, list) else []):
        b = b or {}
        x0 = _num(b.get("x0"), 0)
        y0 = _num(b.get("y0"), 0)
        angle_v = b["angle"] if "angle" in b else b.get("tilt")
        out.append({
            "x0": x0,
            "y0": y0,
            "x1": max(x0, _num(b.get("x1"), x0)),
            "y1": max(y0, _num(b.get("y1"), y0)),
            "angle": abs(_num(angle_v, 0)),
        })
    return out


def _median(nums):
    a = sorted(v for v in (nums or [])
               if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v))
    if not a:
        return 0
    return a[math.floor(len(a) / 2)]


def _median_line_height(lst, idxs):
    hs = []
    for i in idxs:
        h = lst[i]["y1"] - lst[i]["y0"]
        if h > 0:
            hs.append(h)
    return _median(hs)


def _is_vertical_or_rotated(lst, idxs):
    """E3：竖排（中位宽 < 中位高×0.8）或旋转（倾角中位 >5°）⇒ 放弃分栏，直接单栏。"""
    if not idxs:
        return False
    ws = [lst[i]["x1"] - lst[i]["x0"] for i in idxs]
    hs = [lst[i]["y1"] - lst[i]["y0"] for i in idxs]
    med_w = _median(ws)
    med_h = _median(hs)
    if med_h > 0 and med_w < med_h * VERTICAL_ASPECT:
        return True
    tilt = _median([_num(lst[i]["angle"], 0) for i in idxs])
    return tilt > MAX_TILT_DEG


def _header_footer_candidates(lst, idxs, page_h, unit):
    """E1：页眉/页脚候选 = 落在上/下 8% 区域、且上下 3×行高内没有同列邻居。
    只做「候选」，是否真的排除由调用方在确认剩余仍能分栏后决定（保守回退）。"""
    out = []
    if not (page_h > 0) or not (unit > 0) or len(idxs) < 4:
        return out
    top_edge = page_h * HF_FRAC
    bot_edge = page_h * (1 - HF_FRAC)
    near = unit * HF_NEIGHBOR_UNITS
    for i in idxs:
        b = lst[i]
        in_top = b["y1"] <= top_edge
        in_bot = b["y0"] >= bot_edge
        if not in_top and not in_bot:
            continue
        has_neighbor = False
        for j in idxs:
            if j == i:
                continue
            o = lst[j]
            overlap = min(b["x1"], o["x1"]) - max(b["x0"], o["x0"])
            min_w = min(b["x1"] - b["x0"], o["x1"] - o["x0"]) or 1
            if overlap < min_w * HF_X_OVERLAP:
                continue  # 不同列
            v_gap = max(0.0, max(b["y0"], o["y0"]) - min(b["y1"], o["y1"]))
            if v_gap <= near and v_gap > 0:
                has_neighbor = True
                break
        if not has_neighbor:
            out.append(i)
    return out


def _looks_like_table_grid(lst, cols, unit):
    """E4：表格行伪装成栏 —— ① ≥3 个候选栏；② ≥60% 的「同一视觉行」含 ≥2 栏的框；
    ③ 过半栏的框宽参差（medW < maxW×0.75）。③ 是关键区分（实测必需）。"""
    if not cols or len(cols) < 3:
        return False
    tol = max(2, unit * TABLE_ROW_TOL_K)
    rows = {}
    for ci, c in enumerate(cols):
        for i in c["idxs"]:
            cy = (lst[i]["y0"] + lst[i]["y1"]) / 2
            key = js_round(cy / tol)
            rows.setdefault(key, set())
            rows[key].add(ci)
    total = len(rows)
    multi = sum(1 for s in rows.values() if len(s) >= 2)
    if total < 2 or (multi / total) < TABLE_ROW_RATIO:
        return False
    ragged = 0
    for c in cols:
        widths = [lst[i]["x1"] - lst[i]["x0"] for i in c["idxs"]]
        max_w = max(widths)
        med_w = _median(widths)
        if max_w > 0 and med_w < max_w * 0.75:
            ragged += 1
    return ragged >= math.ceil(len(cols) / 2)


def _widest_gap(intervals):
    """一维投影：intervals=[[a,b],…] ⇒ 最宽内部空白缝（水平切段用；区域两端外侧空白不计）。"""
    if not intervals:
        return None
    iv = sorted(intervals, key=lambda p: p[0])
    max_end = iv[0][1]
    best = None
    for i in range(1, len(iv)):
        a = iv[i][0]
        b = iv[i][1]
        if a > max_end:
            size = a - max_end
            if best is None or size > best["size"]:
                best = {"start": max_end, "end": a, "size": size}
        if b > max_end:
            max_end = b
    return best


def _cluster_by_left(lst, idxs, tol):
    """按 x0 排序后一维聚类：相邻 x0 差 ≤ tol 归同簇（主算法）。"""
    if not idxs:
        return []
    sorted_idx = sorted(idxs, key=lambda i: (lst[i]["x0"], i))
    clusters = []
    cur = [sorted_idx[0]]
    for t in range(1, len(sorted_idx)):
        i = sorted_idx[t]
        prev = cur[-1]
        if lst[i]["x0"] - lst[prev]["x0"] <= tol:
            cur.append(i)
        else:
            clusters.append(cur)
            cur = [i]
    clusters.append(cur)
    return clusters


def _col_stats(lst, idxs):
    min_x0 = math.inf
    max_x1 = -math.inf
    total = 0.0
    widths = []
    for i in idxs:
        b = lst[i]
        min_x0 = min(min_x0, b["x0"])
        max_x1 = max(max_x1, b["x1"])
        total += b["x0"]
        widths.append(b["x1"] - b["x0"])
    return {"idxs": idxs, "minX0": min_x0, "maxX1": max_x1,
            "meanX0": total / len(idxs), "medW": _median(widths)}


def _column_plan(lst, idxs, page_w, opts=None):
    """列聚类方案（主算法 + 守卫；增强竖排/表格防护与置信度）。不排序，只出方案。"""
    o = opts or {}
    empty = {"mode": "single", "columnMode": "single", "columns": [], "spanning": [],
             "unit": 0, "tol": 0, "reason": "", "confidence": "high", "confidenceReasons": []}
    if not idxs or len(idxs) < 2:
        out = dict(empty)
        out["reason"] = "tooFew"
        return out
    unit = _median_line_height(lst, idxs) or 1
    # E3：竖排/旋转直接放弃分栏（不聚簇）
    if o.get("verticalCheck") is not False and _is_vertical_or_rotated(lst, idxs):
        out = dict(empty)
        out["unit"] = unit
        out["reason"] = "vertical_or_rotated"
        return out
    tol_k = max(0.0, _num(o.get("tolK"), COL_TOL_K))
    tol = max(3, unit * tol_k)

    clusters = _cluster_by_left(lst, idxs, tol)
    big = [c for c in clusters if len(c) >= COL_MIN_LINES]
    if len(big) < 2:
        out = dict(empty)
        out.update({"unit": unit, "tol": tol, "reason": "clusters<2"})
        return out

    stats = sorted((_col_stats(lst, c) for c in big), key=lambda s: s["meanX0"])
    med_w = _median([s["medW"] for s in stats]) or 1
    med_line_w = _median([lst[i]["x1"] - lst[i]["x0"] for i in idxs]) or 1
    span_ov = max(2, med_w * COL_OVERLAP_K)

    # 跨栏元素：框宽 ≥1.6×行宽中位，且与 ≥2 个候选栏区间实质重叠（防把普通行/宽行当标题）
    spanning = []
    for i in idxs:
        b = lst[i]
        if (b["x1"] - b["x0"]) < med_line_w * COL_SPAN_WIDTH_K:
            continue
        n = 0
        for s in stats:
            ov = min(b["x1"], s["maxX1"]) - max(b["x0"], s["minX0"])
            if ov >= span_ov:
                n += 1
        if n >= 2:
            spanning.append(i)
    spanning_set = set(spanning)
    rest = [i for i in idxs if i not in spanning_set]
    if len(rest) < 4:
        out = dict(empty)
        out.update({"unit": unit, "tol": tol, "spanning": spanning, "reason": "tooFewAfterSpan"})
        return out

    # 去掉跨栏元素后重新聚类；<2 行的小簇并入最近候选栏（不单独成栏）
    clusters2 = _cluster_by_left(lst, rest, tol)
    big2 = [c for c in clusters2 if len(c) >= COL_MIN_LINES]
    if len(big2) < 2:
        out = dict(empty)
        out.update({"unit": unit, "tol": tol, "spanning": spanning, "reason": "clusters<2(afterSpan)"})
        return out
    smalls = [c for c in clusters2 if len(c) < COL_MIN_LINES]
    merged = [{"idxs": list(c)} for c in big2]
    for s in smalls:
        mean_s = sum(lst[i]["x0"] for i in s) / len(s)
        best = 0
        bd = math.inf
        for ci, c in enumerate(merged):
            cm = sum(lst[i]["x0"] for i in c["idxs"]) / len(c["idxs"])
            d = abs(cm - mean_s)
            if d < bd:
                bd = d
                best = ci
        merged[best]["idxs"] = merged[best]["idxs"] + s
    cols = sorted((_col_stats(lst, c["idxs"]) for c in merged), key=lambda s: s["meanX0"])
    med_w2 = _median([c["medW"] for c in cols]) or 1

    # E4：表格行伪装成栏（同一视觉行横跨 ≥2 栏占多数）⇒ 退回单栏，让兜底按行优先排
    if _looks_like_table_grid(lst, cols, unit):
        out = dict(empty)
        out.update({"unit": unit, "tol": tol, "spanning": spanning, "reason": "table_grid"})
        return out

    # b) 相邻栏 x 区间重叠 < 30% 中位栏宽
    for k in range(len(cols) - 1):
        ov = min(cols[k]["maxX1"], cols[k + 1]["maxX1"]) - max(cols[k]["minX0"], cols[k + 1]["minX0"])
        if ov >= med_w2 * COL_OVERLAP_K:
            out = dict(empty)
            out.update({"unit": unit, "tol": tol, "spanning": spanning, "reason": "overlap"})
            return out
    # d) 总覆盖宽度 ≥ 页宽 60%（防把页边孤立元素当栏）
    min_x = min(c["minX0"] for c in cols)
    max_x = max(c["maxX1"] for c in cols)
    if not (page_w > 0) or (max_x - min_x) < page_w * COL_COVERAGE_MIN:
        out = dict(empty)
        out.update({"unit": unit, "tol": tol, "spanning": spanning, "reason": "coverage"})
        return out
    # e) 交叉校验（缝宽法降级为守卫）：相邻栏之间须有正空白缝（扣 OCR 抖动）
    for k in range(len(cols) - 1):
        if (cols[k + 1]["minX0"] - cols[k]["maxX1"]) <= COL_GAP_TOL_PX:
            out = dict(empty)
            out.update({"unit": unit, "tol": tol, "spanning": spanning, "reason": "noGap"})
            return out

    columns = []
    for c in cols:
        columns.append(sorted(c["idxs"], key=lambda i: (lst[i]["y0"], lst[i]["x0"], i)))
    span_sorted = sorted(spanning, key=lambda p: ((lst[p]["y0"] + lst[p]["y1"]) / 2, lst[p]["x0"], p))

    # E2：低置信信号（不改排序，只给埋点）。满足任一 ⇒ low
    confidence_reasons = []
    for k in range(len(cols) - 1):
        gap = (cols[k + 1]["minX0"] - cols[k]["maxX1"]) - COL_GAP_TOL_PX
        if gap < unit * LOW_GAP_UNITS:
            confidence_reasons.append("gap<0.5lh")
        ov = min(cols[k]["maxX1"], cols[k + 1]["maxX1"]) - max(cols[k]["minX0"], cols[k + 1]["minX0"])
        ratio = ov / med_w2
        if LOW_OVERLAP_LO <= ratio < COL_OVERLAP_K:
            confidence_reasons.append("overlap20-30")
    for c in cols:
        if len(c["idxs"]) < LOW_MIN_CLUSTER:
            confidence_reasons.append("minCluster<3")
            break
    return {
        "mode": "columns", "columnMode": "columns", "columns": columns, "spanning": span_sorted,
        "unit": unit, "tol": tol, "reason": "",
        "confidence": "low" if confidence_reasons else "high",
        "confidenceReasons": confidence_reasons,
    }


def _compute_order(boxes, opts=None):
    """求行阅读顺序（主算法：列左边界聚类为主，缝宽法降级为交叉校验）。"""
    lst = _norm_boxes(boxes)
    n = len(lst)
    o = opts or {}
    if n == 0:
        return {"order": [], "meta": {"orderConfidence": "high", "columnMode": "single",
                                      "reason": "empty", "excluded": [], "confidenceReasons": []}}
    page_w = _num(o.get("pageW"), 0) if _num(o.get("pageW"), 0) > 0 else max(1, max(b["x1"] for b in lst))
    real_page_h = _num(o.get("pageH"), 0) > 0
    page_h = _num(o.get("pageH"), 0) if real_page_h else max(1, max(b["y1"] for b in lst))
    row_min = max(float(HGAP_MIN_PX), max(0.0, _num(o.get("rowGapRatio"), HGAP_FRAC)) * page_h)
    all_idx = list(range(n))
    unit = _median_line_height(lst, all_idx) or 1

    # E3：竖排/旋转 ⇒ 放弃分栏（可观测）；verticalCheck:False 仅供对照诊断
    vertical = False if o.get("verticalCheck") is False else _is_vertical_or_rotated(lst, all_idx)
    column_mode = "single" if (o.get("columnMode") == "single" or vertical) else "auto"
    if vertical:
        reason = "vertical_or_rotated"
    elif o.get("columnMode") == "single":
        reason = "column_mode_single"
    else:
        reason = ""

    # E1：页眉/页脚只从分栏判定输入里排除（输出仍保留，头在前、脚在后）；剩余不足以分栏 ⇒ 不排除
    root_idxs = all_idx
    excluded = []
    if column_mode == "auto" and real_page_h and o.get("headerFooter") is not False:
        hf = _header_footer_candidates(lst, all_idx, page_h, unit)
        if hf:
            hf_set = set(hf)
            rest = [i for i in all_idx if i not in hf_set]
            if len(rest) >= 4:
                probe = _column_plan(lst, rest, page_w, o)
                if probe["mode"] == "columns" and len(probe["columns"]) >= 2:
                    root_idxs = rest
                    excluded = hf
    root_plan = _column_plan(lst, root_idxs, page_w, o)

    out = []

    def visit(idxs, region_y0=None, region_y1=None):
        if len(idxs) <= 1:
            if len(idxs) == 1:
                out.append(idxs[0])
            return
        y0 = min(lst[i]["y0"] for i in idxs) if region_y0 is None else region_y0
        y1 = max(lst[i]["y1"] for i in idxs) if region_y1 is None else region_y1

        # ① 列聚类主路径
        if column_mode != "single":
            plan = _column_plan(lst, idxs, page_w, o)
            if plan["mode"] == "columns":
                if not plan["spanning"]:
                    for col in plan["columns"]:
                        visit(col, y0, y1)
                    return
                # 跨栏元素是「整行分隔带」：带内先排 → 跨栏元素单发 → 下一带
                span_set = set(plan["spanning"])
                plain = [i for i in idxs if i not in span_set]
                taken = set()
                for s in plan["spanning"]:
                    sc = (lst[s]["y0"] + lst[s]["y1"]) / 2
                    band = [i for i in plain if i not in taken and ((lst[i]["y0"] + lst[i]["y1"]) / 2) < sc]
                    for i in band:
                        taken.add(i)
                    if band:
                        visit(band, y0, min(y1, lst[s]["y0"]))
                    out.append(s)
                tail = [i for i in plain if i not in taken]
                if tail:
                    visit(tail, max(y0, lst[plan["spanning"][-1]]["y1"]), y1)
                return

        # ② 水平投影切上下（单栏回退路径；保留既有「最宽缝」口径）
        hg = _widest_gap([[lst[i]["y0"], lst[i]["y1"]] for i in idxs])
        if hg and hg["size"] >= row_min:
            mid = (hg["start"] + hg["end"]) / 2
            top = []
            bottom = []
            for i in idxs:
                (top if lst[i]["y1"] <= mid else bottom).append(i)
            if top and bottom:
                visit(top, y0, hg["start"])
                visit(bottom, hg["end"], y1)
                return

        # ③ 兜底：按 (round(y/8), x) 排序（同一视觉行 8px 归并）
        sorted_idx = sorted(idxs, key=lambda i: (js_round(lst[i]["y0"] / ROW_GROUP_PX),
                                                 lst[i]["x0"], i))
        for i in sorted_idx:
            out.append(i)

    # 页眉 → 正文 → 页脚（排除项仍在输出里，不丢块）
    headers = sorted([i for i in excluded if lst[i]["y1"] <= page_h * HF_FRAC],
                     key=lambda i: (lst[i]["y0"], lst[i]["x0"], i))
    footers = sorted([i for i in excluded if lst[i]["y0"] >= page_h * (1 - HF_FRAC)],
                     key=lambda i: (lst[i]["y0"], lst[i]["x0"], i))
    for i in headers:
        out.append(i)
    visit(root_idxs)
    for i in footers:
        out.append(i)

    # E2：置信度（单栏恒 high；多栏看根方案）
    order_confidence = "high"
    confidence_reasons = []
    if column_mode != "single" and root_plan["mode"] == "columns":
        order_confidence = root_plan.get("confidence") or "high"
        confidence_reasons = root_plan.get("confidenceReasons") or []
    if not reason and root_plan["mode"] == "single":
        reason = root_plan.get("reason") or ""
    return {
        "order": out,
        "meta": {
            "orderConfidence": order_confidence,
            "columnMode": "columns" if (column_mode != "single" and root_plan["mode"] == "columns") else "single",
            "reason": reason,
            "excluded": list(excluded),
            "confidenceReasons": confidence_reasons,
            "plan": root_plan,
        },
    }


def order_lines(boxes, opts=None):
    """求行阅读顺序。boxes：[{x0,y0,x1,y1,angle?}]（页内像素坐标）；
    opts：{pageW?, pageH?, rowGapRatio?, tolK?, columnMode?}（columnMode:'single' 强制不分栏）。
    返回输入下标的阅读序列（列表）。"""
    return _compute_order(boxes, opts)["order"]


def analyze_order(boxes, opts=None):
    """与 order_lines 同算一遍，返回可枚举的结构化结果（供埋点/诊断；排序结果完全一致）。"""
    r = _compute_order(boxes, opts)
    meta = r["meta"] or {}
    return {
        "order": r["order"],
        "orderConfidence": meta.get("orderConfidence") or "high",
        "columnMode": meta.get("columnMode") or "single",
        "reason": meta.get("reason") or "",
        "excluded": meta.get("excluded") or [],
        "confidenceReasons": meta.get("confidenceReasons") or [],
        "unit": (meta.get("plan") or {}).get("unit", 0) if meta.get("plan") else 0,
        "tol": (meta.get("plan") or {}).get("tol", 0) if meta.get("plan") else 0,
    }


# ⚠️ 诊断件：缝宽法（主路径不消费；保留供诊断核对「单栏 0 条缝」与历史对照）
def _vertical_seams(lst, idxs, col_min, region_y0, region_y1):
    h = max(1, region_y1 - region_y0)
    edges = []
    for i in idxs:
        edges.append(lst[i]["x0"])
        edges.append(lst[i]["x1"])
    edges.sort()
    uniq = [v for k, v in enumerate(edges) if k == 0 or v != edges[k - 1]]
    blank_runs = []
    for k in range(len(uniq) - 1):
        a = uniq[k]
        b = uniq[k + 1]
        if b - a < 1:
            continue
        cov = 0.0
        for i in idxs:
            bx = lst[i]
            ov = min(bx["x1"], b) - max(bx["x0"], a)
            if ov >= max(OVERLAP_TOL_PX, (b - a) * 0.5):
                yy0 = max(bx["y0"], region_y0)
                yy1 = min(bx["y1"], region_y1)
                if yy1 > yy0:
                    cov += (yy1 - yy0)
        blank = max(0.0, h - min(cov, h))
        if blank / h >= GAP_BLANK_MIN:
            last = blank_runs[-1] if blank_runs else None
            if last and abs(last["b"] - a) < 1:
                last["b"] = b
                last["width"] = last["b"] - last["a"]
            else:
                blank_runs.append({"a": a, "b": b, "width": b - a})
    return [s for s in blank_runs if s["width"] >= col_min]
