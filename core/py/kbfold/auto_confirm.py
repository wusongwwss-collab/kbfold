# -*- coding: utf-8 -*-
"""kbfold.auto_confirm — 「识别完成 ⇒ 是否自动建库」的决策纯函数（零 IO）。

移植自冻结底本 ``kb-auto-confirm.js``（JS→Py 移植件）。语义逐字保留：

  · 高置信批（全部候选 confidence ≥ 阈值，且存在至少一个有落点的候选）⇒ 自动；
  · 拿不准的批（批内任一候选 confidence < 阈值）⇒ 整批留给用户——保守整批制：
    批内只要有一件拿不准就不自动，防半自动半人工的割裂态；
  · 全载体批（全部候选无落点）⇒ 不自动（禁自动替用户放弃）；
  · 阈值：默认 0.8；0/off/非法 ⇒ 关闭自动建库。

判据刻意保守（宁可不自动，不可错自动）：confidence 缺省/非法一律按 0（进「拿不准」）；
status ≠ 'candidate' 的候选不参与判定；无候选 ⇒ 不自动。
"""
from __future__ import annotations

from math import isfinite

from ._compat import js_number

#: 默认阈值（冻结值）。
DEFAULT_THRESHOLD = 0.8


def parse_threshold(raw):
    """解析阈值设置值。返回 0=关闭；>0=生效阈值（缺省 0.8）。"""
    s = str("" if raw is None else raw).strip().lower()
    if not s or s in ("off", "none"):
        return 0.0  # 空串=关（显式 opt-out 形态之一）
    v = js_number(s)
    if not isfinite(v) or v <= 0:
        return 0.0  # 0/负/非法 ⇒ 关
    return min(1.0, v)  # 上限 1（confidence 域）


def decide_auto_confirm(cands, threshold):
    """自动建库决策。cands：[{confidence, mapping, status}]；threshold >0 生效、≤0 关闭。
    返回 {auto, reason, lowCount, carrierOnly, eligible}，
    reason ∈ 'disabled'|'no-candidates'|'low-confidence'|'all-carrier'|'ok'。"""
    out = {"auto": False, "reason": "disabled", "lowCount": 0, "carrierOnly": False, "eligible": 0}
    if not (isinstance(threshold, (int, float)) and not isinstance(threshold, bool)
            and isfinite(float(threshold)) and threshold > 0):
        return out
    live = [c for c in (cands or [])
            if str((c or {}).get("status") or "candidate") == "candidate"]
    if not live:
        out["reason"] = "no-candidates"
        return out
    with_mapping = 0
    for c in live:
        conf = js_number((c or {}).get("confidence"))
        if not isfinite(conf) or conf < threshold:  # 缺省/非法按 0 ⇒ 拿不准
            out["lowCount"] += 1
        if isinstance(c, dict) and isinstance(c.get("mapping"), dict) and len(c["mapping"]) > 0:
            with_mapping += 1
    out["eligible"] = len(live) - out["lowCount"]
    if out["lowCount"] > 0:
        out["reason"] = "low-confidence"
        return out  # 保守整批制
    if with_mapping == 0:
        out["carrierOnly"] = True
        out["reason"] = "all-carrier"
        return out
    out["auto"] = True
    out["reason"] = "ok"
    return out
