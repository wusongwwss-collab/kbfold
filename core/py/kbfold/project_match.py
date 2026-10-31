# -*- coding: utf-8 -*-
"""kbfold.project_match — 知识库按名称解析（唯一判据，零 IO，可直测）。

移植自冻结底本 ``kb-project-match.js``（JS→Py 移植件）。判定口径逐字保留：

匹配顺序（三条，逐级放宽；命中即停）：
  ① 精确：去空白 + 小写后全等
  ② 包含：库名包含目标，或目标包含库名（必须唯一，多条命中视为歧义）
  ③ 前缀：目标作为库名前缀（必须唯一）
任一级出现多条 ⇒ 视为歧义，按未命中处理（调用方退回当前库并显式标注），禁猜。
"""
from __future__ import annotations

import re

_WS_RE = re.compile(r"\s+")


def _norm(s):
    return _WS_RE.sub("", str("" if s is None else s)).lower()


def match_project_by_name(rows, name):
    """库名 → 编号三级匹配。rows：[{id, name}]；返回 {hit, reason}。
    reason ∈ 'exact'|'contains'|'prefix'|'ambiguous'|'empty'|'not-found'。"""
    lst = [r for r in (rows if isinstance(rows, list) else [])
           if isinstance(r, dict) and r.get("id")]
    want = _norm(name)
    if not want:
        return {"hit": None, "reason": "empty"}

    for r in lst:
        if _norm(r.get("name")) == want:
            return {"hit": r, "reason": "exact"}

    contains = []
    for r in lst:
        n = _norm(r.get("name"))
        if want in n or n in want:
            contains.append(r)
    if len(contains) == 1:
        return {"hit": contains[0], "reason": "contains"}
    if len(contains) > 1:
        return {"hit": None, "reason": "ambiguous"}

    prefix = [r for r in lst if _norm(r.get("name")).find(want) == 0]
    if len(prefix) == 1:
        return {"hit": prefix[0], "reason": "prefix"}
    if len(prefix) > 1:
        return {"hit": None, "reason": "ambiguous"}

    return {"hit": None, "reason": "not-found"}
