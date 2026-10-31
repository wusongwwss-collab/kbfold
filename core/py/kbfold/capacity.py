# -*- coding: utf-8 -*-
"""kbfold.capacity — 配额/容量护栏的可见面（纯函数：同一输入同一输出）。

移植自冻结底本 ``kb-capacity.js``（JS→Py 移植件）；四闸阈值口径取自同族冻结底本
``vector-index.js`` 的 BUDGET 常量（此处按冻结值登记，唯一事实源仍在引擎侧读数）。

四闸（冻结契约，禁调参改名）：单库 10 万块 / 单库矩阵 512MB / 全进程 1GB / 单查询 500ms；
达阈＝≥（100%），预警线＝80%（仅提示，不算闸）。
读数缺一块就如实降级（available=False / seen=False），不编数字、不用 0 冒充正常。
本件只读：不拦截、不降级、不改检索结果。
"""
from __future__ import annotations

from math import isfinite

from ._compat import js_number, js_round

#: 四闸冻结阈值（与冻结底本 vector-index.js BUDGET 同值登记）。
BUDGET = {
    "chunksPerLib": 100000,
    "bytesPerLib": 512 * 1024 * 1024,      # 0.5GB
    "bytesTotal": 1024 * 1024 * 1024,      # 1GB（本进程所有库矩阵合计）
    "queryMs": 500,
}
#: 引擎决策信号（闸值触发才立项；不自动迁移）。
BUDGET_ACTION = "consider-sqlite-vec"

WARN_RATIO = 0.8          # 80% 黄（界面预警线；不是闸）
DIM = 1024                # 与向量索引的 dim 一致；矩阵字节 = 行数 × DIM × 4B
LEVEL_ORDER = {"ok": 0, "warn": 1, "danger": 2}


def _jstr(v):
    """JS String(number) 等价：整值不带小数尾（10 → '10'，10.5 → '10.5'）。"""
    n = js_number(v)
    if not isfinite(n):
        return "NaN"
    if n == int(n) and abs(n) < 1e15:
        return str(int(n))
    return repr(n)


def _num(v):
    n = js_number(v)
    return n if (isfinite(n) and n > 0) else 0


def _ratio(value, limit):
    v = _num(value)
    ln = _num(limit)
    if not ln > 0:
        return 0  # 阈值为 0/缺失 ⇒ 不判（防 Infinity/假报）
    return v / ln


def _level(ratio, has_data):
    if has_data is False:
        return "unknown"
    if ratio >= 1:
        return "danger"
    if ratio >= WARN_RATIO:
        return "warn"
    return "ok"


def _worst(levels):
    worst = "ok"
    for l in levels:
        if l == "unknown":
            continue
        if LEVEL_ORDER[l] > LEVEL_ORDER[worst]:
            worst = l
    return worst


def _pct(ratio):
    r = _ratio(ratio, 1)  # 复用取数守卫
    return js_round(min(r, 1) * 1000) / 10


def fmt_chunks(n):
    """块数：万级走「万」，万以下走千分位（例：1.2 万 / 10 万；860 / 10 万）。"""
    v = _num(n)
    if v >= 10000:
        w = v / 10000
        return (_jstr(js_round(w)) if w >= 100 else _jstr(js_round(w * 10) / 10)) + " 万"
    return _jstr(js_round(v))


def fmt_bytes(bytes_):
    """字节：≥1GB 走 GB，≥1MB 走 MB，>0 走 KB（不把 0.1MB 说成 0MB）；0 如实 0 MB。"""
    b = _num(bytes_)
    if b >= 1024 * 1024 * 1024:
        g = b / 1024 / 1024 / 1024
        return _jstr(js_round(g * 100) / 100) + " GB"
    mb = b / 1024 / 1024
    if mb >= 1:
        return _jstr(js_round(mb)) + " MB"
    if b > 0:
        return _jstr(max(1, js_round(b / 1024))) + " KB"
    return "0 MB"


def build_capacity(input_=None):
    """生成容量面板模型（UI 只渲染，不再做任何阈值判断）。

    input：
      - live          引擎容量快照（{thresholds, totalBytes, entries}）
      - lib           {chunks, chunksVec, filesVec, itemsVec, dim}：当前库的 SQL 计数（取不到传 None）
      - activePrefix  当前库的向量缓存目录（用于从全进程存活表里认出「本库」；空串＝认不出）
      - thresholds    可选覆盖（只给夹具用；生产不传，取 BUDGET 唯一事实源）
    """
    inp = input_ or {}
    live = inp.get("live") or None
    lib_in = inp.get("lib") or None
    prefix = str(inp.get("activePrefix") or "")
    th = dict(BUDGET)
    if isinstance(live, dict) and isinstance(live.get("thresholds"), dict):
        th.update(live["thresholds"])
    if isinstance(inp.get("thresholds"), dict):
        th.update(inp["thresholds"])
    dim = _num((lib_in or {}).get("dim") if isinstance(lib_in, dict) else None) or DIM

    # ── 1. 本库：SQL 计数（库内真实块数）＋ 存活表里本库的已加载矩阵 ──
    chunks = _num((lib_in or {}).get("chunks") if isinstance(lib_in, dict) else None)
    chunks_vec = _num((lib_in or {}).get("chunksVec") if isinstance(lib_in, dict) else None)
    files_vec = _num((lib_in or {}).get("filesVec") if isinstance(lib_in, dict) else None)
    items_vec = _num((lib_in or {}).get("itemsVec") if isinstance(lib_in, dict) else None)
    live_entries = live.get("entries") if isinstance(live, dict) and isinstance(live.get("entries"), list) else []
    # 本库 = 缓存目录同前缀的那几条；空前缀且条目也无 cacheDir 时视为「同一个库」
    matched = [e for e in live_entries if e and str((e.get("cacheDir") or "")) == prefix]
    live_by_name = {}
    for e in matched:
        live_by_name[str(e.get("name") or "")] = e

    idx_defs = [
        {"name": "kb_chunks", "label": "块", "n": chunks_vec},
        {"name": "file_embeddings", "label": "文件", "n": files_vec},
        {"name": "item_vectors", "label": "条目", "n": items_vec},
    ]
    indexes = []
    for d in idx_defs:
        live_e = live_by_name.get(d["name"]) or None
        # 装载过 ⇒ 用运行态真实字节；没装载过 ⇒ 按库内向量行数估算（矩阵 = n × dim × 4B）
        bytes_ = _num(live_e.get("matrixBytes")) if live_e else d["n"] * dim * 4
        query_ms = None
        if live_e and live_e.get("queryMs") is not None:
            query_ms = js_number(live_e.get("queryMs"))
        indexes.append({
            "name": d["name"],
            "label": d["label"],
            "n": _num(live_e.get("n")) if live_e else d["n"],
            "nInDb": d["n"],
            "bytes": bytes_,
            "loaded": bool(live_e),
            "queryMs": query_ms,
        })
    matrix_bytes = 0 if not indexes else max(x["bytes"] for x in indexes)  # 闸②口径＝单个索引矩阵（取最大）
    matrix_bytes_total = sum(x["bytes"] for x in indexes)  # 本库三份矩阵合计（诊断/提示用）
    lib_query_ms = 0
    for x in indexes:
        if x["queryMs"] is not None:
            lib_query_ms = max(lib_query_ms, js_number(x["queryMs"]) or 0)
    lib_query_seen = any(x["queryMs"] is not None for x in indexes)

    # ── 2. 全进程：存活矩阵合计（未加载的库不计——它们不占常驻内存）──
    total_bytes = _num(live.get("totalBytes") if isinstance(live, dict) else None)
    proc_entries = sorted(live_entries, key=lambda e: -_num(e.get("matrixBytes")) if e else 0)[:8]
    proc_entries = [
        {
            "name": str((e or {}).get("name") or ""),
            "libId": str((e or {}).get("libId") or ""),
            "n": _num((e or {}).get("n")),
            "matrixBytes": _num((e or {}).get("matrixBytes")),
            "isActiveLib": bool(prefix) and str((e or {}).get("cacheDir") or "") == prefix,
        }
        for e in proc_entries
    ]
    proc_libs = len({str((e or {}).get("cacheDir") or (e or {}).get("libId") or "") for e in live_entries})

    # ── 3. 三环（口径与四闸逐条对齐）──
    r_chunks = _ratio(chunks_vec, th["chunksPerLib"])
    r_matrix = _ratio(matrix_bytes, th["bytesPerLib"])
    r_total = _ratio(total_bytes, th["bytesTotal"])
    has_lib = bool(lib_in)
    gauges = [
        {
            "key": "chunksPerLib",
            "label": "本库向量块",
            "value": chunks_vec,
            "limit": _num(th["chunksPerLib"]),
            "ratio": r_chunks,
            "pct": _pct(r_chunks),
            "level": _level(r_chunks, has_lib),
            "display": fmt_chunks(chunks_vec) + " / " + fmt_chunks(th["chunksPerLib"]) + "块",
            "tip": "本库已建向量的块数 / 建议上限（10 万块）。块是向量检索的最小单位——按块计，不按文件数。"
                   + "库内现有块 " + _jstr(chunks) + " 个，其中已向量化 " + _jstr(chunks_vec) + " 个。"
                   + "到上限后检索会变慢、常驻内存上升，但仍可继续用；建议把特别大的库拆成几个小库。",
        },
        {
            "key": "bytesPerLib",
            "label": "本库矩阵",
            "value": matrix_bytes,
            "limit": _num(th["bytesPerLib"]),
            "ratio": r_matrix,
            "pct": _pct(r_matrix),
            "level": _level(r_matrix, has_lib),
            "display": fmt_bytes(matrix_bytes) + " / " + fmt_bytes(th["bytesPerLib"]),
            "tip": "本库向量索引常驻内存（取三份里最大的一份）/ 单库上限 512MB。"
                   + "三份：块 " + fmt_bytes(indexes[0]["bytes"]) + " · 文件 " + fmt_bytes(indexes[1]["bytes"])
                   + " · 条目 " + fmt_bytes(indexes[2]["bytes"])
                   + "（合计 " + fmt_bytes(matrix_bytes_total) + "）。"
                   + ("" if any(x["loaded"] for x in indexes)
                      else "当前尚未加载，读数按库内向量行数估算，首次检索装载后逐字节一致。"),
        },
        {
            "key": "bytesTotal",
            "label": "全进程矩阵",
            "value": total_bytes,
            "limit": _num(th["bytesTotal"]),
            "ratio": r_total,
            "pct": _pct(r_total),
            "level": _level(r_total, True),
            "display": fmt_bytes(total_bytes) + " / " + fmt_bytes(th["bytesTotal"]),
            "tip": "本机所有**已加载**向量矩阵合计 / 全进程上限 1GB。"
                   + "只统计已装载到内存的矩阵（未打开的库不占内存、也不计入）；实例回收后自动退出统计。"
                   + "同时打开的库越多这个数越大——建议关掉暂时不用的库。"
                   + (("当前常驻 " + _jstr(proc_libs) + " 个库的矩阵。") if proc_libs else ""),
        },
    ]

    # ── 4. 闸④：检索耗时（没查询过 ⇒ seen=False，不判定，不假报）──
    query_seen = bool(lib_query_seen)
    r_query = _ratio(lib_query_ms, th["queryMs"]) if query_seen else 0
    query = {
        "seen": query_seen,
        "ms": int(js_round(lib_query_ms)) if query_seen else None,
        "limit": _num(th["queryMs"]),
        "ratio": r_query,
        "pct": _pct(r_query),
        "level": _level(r_query, True) if query_seen else "unknown",
        "display": ((_jstr(js_round(lib_query_ms)) + "ms") if query_seen else "—") + " / " + _jstr(_num(th["queryMs"])) + "ms",
        "tip": "最近一次向量检索耗时 / 上限 500ms（只统计已发生查询的索引；没查过显示 —，不做假判定）。"
               + "超过后用户体感就是「问一问变慢」，建议拆分大库或缩小检索范围。",
    }

    # ── 5. 结论、越界清单与建议（人话；引擎决策信号只留给诊断面）──
    available = bool(has_lib or total_bytes > 0)
    level = _worst([g["level"] for g in gauges] + [query["level"]]) if available else "unknown"
    breaches = [g["key"] for g in gauges if g["level"] == "danger"]
    if query["level"] == "danger":
        breaches.append("queryMs")
    warns = [g["key"] for g in gauges if g["level"] == "warn"]
    if query["level"] == "warn":
        warns.append("queryMs")

    suggestions = []
    if "chunksPerLib" in breaches:
        suggestions.append("本库向量块已到建议上限（10 万块）：检索会变慢、内存占用明显上升。建议把大库拆成几个小库，或归档不再常用的旧库。")
    if "bytesPerLib" in breaches:
        suggestions.append("本库向量索引已占满 512MB 常驻内存：建议拆分知识库，或归档不常用的旧库。")
    if "bytesTotal" in breaches:
        suggestions.append("全进程向量内存合计已到 1GB：同时打开的库太多。关掉暂时不用的库，或拆分大库。")
    if "queryMs" in breaches:
        suggestions.append("单次检索已超过 500ms：库已偏大，建议拆分大库或缩小检索范围。")
    if not suggestions and (warns or level == "warn"):
        max_pct = max([g["pct"] for g in gauges] + [query["pct"]])
        suggestions.append("容量已用到 " + _jstr(max_pct) + "%（≥80% 预警线）：建议提前规划——把特别大的知识库拆成几个小库，或归档不再常用的旧库。")

    if level == "danger":
        summary = "容量已到上限"
    elif level == "warn":
        summary = "容量接近上限（≥80%）"
    elif level == "ok":
        summary = "容量充裕"
    else:
        summary = "容量数据暂不可用"
    summary_tip = (
        "四闸（单库 10 万块 / 单库 512MB / 全进程 1GB / 单查询 500ms）均未到限。"
        "到 80% 会变黄、到 100% 变红并给出建议；提示只是提醒，不拦截任何检索。"
        if level == "ok" else summary
    )

    return {
        "ok": True,
        "available": available,
        "level": level,
        "summary": summary,
        "summaryTip": summary_tip,
        "thresholds": {
            "chunksPerLib": _num(th["chunksPerLib"]),
            "bytesPerLib": _num(th["bytesPerLib"]),
            "bytesTotal": _num(th["bytesTotal"]),
            "queryMs": _num(th["queryMs"]),
        },
        "lib": {
            "active": len(matched) > 0,
            "chunks": chunks,
            "chunksVec": chunks_vec,
            "matrixBytes": matrix_bytes,
            "matrixBytesTotal": matrix_bytes_total,
            "indexes": indexes,
            "queryMs": int(js_round(lib_query_ms)) if query_seen else None,
        },
        "process": {
            "totalBytes": total_bytes,
            "libs": proc_libs,
            "entries": proc_entries,
        },
        "gauges": gauges,
        "query": query,
        "breaches": breaches,
        "warns": warns,
        "action": BUDGET_ACTION if breaches else None,  # 引擎决策信号（报告口径：闸值触发才立项；不自动迁移）
        "suggestions": suggestions,
        "dim": dim,
    }
