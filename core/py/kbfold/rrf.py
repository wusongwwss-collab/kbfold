# -*- coding: utf-8 -*-
"""kbfold.rrf — 三路名次融合（Reciprocal Rank Fusion，只读名次不读分数，纯函数）。

移植自冻结底本 ``kb-rank-fusion.js``（JS→Py 移植件）。口径逐字保留：

  score(d) = Σ_i 1 / (k + rank_i(d))，k = 60（Cormack et al. 2009 原式）。
  三路分数量纲不同（语义余弦 / -bm25 / 0），直接相加无意义 ⇒ 只读名次。

契约细节（夹具逐条锁定）：
  - 去重：同一路内同 file_id 只认最高名次（首个出现者）；跨路按 file_id 合并（参与多路 ⇒ 多份贡献）。
  - 归一：raw / (活动通道数 × 1/(k+1))，∈ (0,1]；活动通道 = 有有效命中的路数（单路单命中 = 1）。
  - 稳定：score 降序 → rrf 降序 → file_id 升序（同分可复现，不依赖插入顺序）。
  - 元数据：取通道序首个出现该文件的命中记录，后续通道补空缺字段（不覆盖首现通道已有值）。
  - 纯函数：零 IO、不改入参、绝不抛（非法条目跳过，不影响其余）。

rerank 钩子：可选注入 {file_id: 分}（min-max 归一到 0..1，尺度无关）；传入时
最终分 = (1-w)·rrf + w·rerank，w 默认 0.7（冻结定值；w=0 属禁值）。
"""
from __future__ import annotations

import math
from math import isfinite

from ._compat import is_safe_positive_int, js_number, js_round

DEFAULT_K = 60
DEFAULT_RERANK_WEIGHT = 0.7
#: 后续通道补空缺的元数据字段（score 由融合层重算，不在此列）
META_FILL_KEYS = ("name", "project_id", "folder", "page", "parentText", "semScanned", "hits")


def clamp01(n):
    v = js_number(n)
    if not isfinite(v):
        return 0.0
    return max(0.0, min(1.0, v))


def round6(n):
    return js_round(js_number(n) * 1000000) / 1000000


def valid_fid(h):
    if not isinstance(h, dict):
        return False
    return is_safe_positive_int(h.get("file_id"))


def normalize_channels(channels):
    """通道入参归一：数组形态（带 name）或对象形态（键即通道名）。"""
    if isinstance(channels, list):
        out = []
        for i, ch in enumerate(channels):
            name = str((ch or {}).get("name") or ("ch%d" % (i + 1))) if isinstance(ch, dict) else "ch%d" % (i + 1)
            hits = ch.get("hits") if isinstance(ch, dict) and isinstance(ch.get("hits"), list) else []
            out.append({"name": name, "hits": hits})
        return out
    if isinstance(channels, dict):
        return [{"name": name, "hits": hits if isinstance(hits, list) else []}
                for name, hits in channels.items()]
    return []


def normalize_rerank_scores(input_):
    """rerank 钩子入参归一（dict / 数组）⇒ {file_id: 0..1}（本次集合 min-max 归一）。"""
    raw_map = {}

    def put(fid, score):
        if is_safe_positive_int(fid):
            s = js_number(score)
            if isfinite(s):
                raw_map[int(js_number(fid))] = s

    if isinstance(input_, dict):
        for key, val in input_.items():
            put(key, val)
    elif isinstance(input_, list):
        for it in input_:
            if isinstance(it, dict):
                put(it.get("file_id"), it.get("score"))
    out = {}
    if not raw_map:
        return out
    mn = min(raw_map.values())
    mx = max(raw_map.values())
    for fid, v in raw_map.items():
        out[fid] = 1 if mx == mn else (v - mn) / (mx - mn)
    return out


def _fill_meta(meta, hit):
    """后续通道补空缺字段（不覆盖首现通道的已有值）。"""
    for key in META_FILL_KEYS:
        cur = meta.get(key)
        nxt = hit.get(key)
        if (cur is None or cur == "") and nxt is not None and nxt != "":
            meta[key] = nxt


def fuse(channels, rerank_scores=None, opts=None):
    """RRF 名次融合（唯一入口；纯函数）。返回融合列表（rank 从 1 起）。"""
    o = opts or {}
    # k = max(1, floor(Number(opts.k) || 60))（与底本逐字同口径）
    k = max(1, math.floor(js_number(o.get("k")) or DEFAULT_K))
    w = clamp01(o.get("rerankWeight") if o.get("rerankWeight") is not None else DEFAULT_RERANK_WEIGHT)
    lst = normalize_channels(channels)
    # 活动通道数（有有效命中的路数；全空 ⇒ 归一分母取 0，输出空列表）
    active = 0
    for ch in lst:
        seen = set()
        for h in ch["hits"]:
            if valid_fid(h):
                seen.add(int(js_number(h["file_id"])))
        if seen:
            active += 1
    max_contrib = active * (1.0 / (k + 1)) if active > 0 else 0.0

    by_file = {}
    order = []  # 首次出现序（仅做稳定兜底，最终仍按分数排）
    for ch in lst:
        seen = set()
        rank = 0
        for h in ch["hits"]:
            if not valid_fid(h):
                continue
            fid = int(js_number(h["file_id"]))
            if fid in seen:
                continue  # 同路去重：只认最高名次（首个）
            seen.add(fid)
            rank += 1
            rec = by_file.get(fid)
            if rec is None:
                rec = {"meta": dict(h), "ranks": {}, "contrib": 0.0}
                by_file[fid] = rec
                order.append(fid)
            else:
                _fill_meta(rec["meta"], h)
            rec["ranks"][ch["name"]] = rank
            rec["contrib"] += 1.0 / (k + rank)

    rerank = normalize_rerank_scores(rerank_scores)
    out = []
    for fid in order:
        rec = by_file[fid]
        rrf = clamp01(rec["contrib"] / max_contrib) if max_contrib > 0 else 0.0
        score = rrf
        reranked = False
        if fid in rerank:
            score = (1 - w) * rrf + w * rerank[fid]
            reranked = True
        entry = dict(rec["meta"])
        entry.update({
            "file_id": fid,
            "score": round6(score),
            "rrf": round6(rrf),
            "rank": 0,
            "ranks": rec["ranks"],
            "channels": list(rec["ranks"].keys()),
        })
        if reranked:
            entry["rerank"] = True
        out.append(entry)
    out.sort(key=lambda x: (-x["score"], -x["rrf"], x["file_id"]))
    for i, entry in enumerate(out):
        entry["rank"] = i + 1
    return out
