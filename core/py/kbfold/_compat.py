# -*- coding: utf-8 -*-
"""kbfold 内部兼容层：JS 数字语义的 Python 等价实现（移植辅助，非判定逻辑）。

背景：core 判定内核多件移植自冻结底本（JS 纯函数件）。为满足「同一输入逐字同输出」
的 parity 硬出口，这里集中实现三处已知 JS→Py 语义坑（移植手法清单 A3）：
  1. Math.round 半值：JS Math.round(x) 恒向 +∞ 取半（-0.5 → -0），Python round 是银行家舍入
     ⇒ 统一走 js_round(x) = floor(x + 0.5)；
  2. Number(v) 强转：JS Number(null)==0、Number('')==0、Number(非数字串)==NaN
     ⇒ 统一走 js_number(v)，缺省/非法值交由调用方兜底（与底本 `_num(v, dflt)` 同构）；
  3. 有序集合：JS Set 保留插入序，Python set 无序 ⇒ 有序集合用 dict 键序模拟（vocab_of 等）。

本模块零第三方依赖、零 IO、零网络；不承担任何判定语义。
"""
from __future__ import annotations

import math

_SAFE_INT_MAX = 2 ** 53 - 1  # JS Number.isSafeInteger 上界


def js_round(x: float) -> float:
    """JS Math.round 等价：floor(x + 0.5)（对负数与半值均与 V8 一致）。"""
    return math.floor(x + 0.5)


def js_number(v) -> float:
    """JS Number(v) 等价（覆盖移植面用到的输入域：None/bool/数值/字符串/其他）。"""
    if v is None:
        return 0.0
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if s == "":
            return 0.0
        try:
            return float(s)
        except ValueError:
            return float("nan")
    return float("nan")


def js_num_finite(v) -> bool:
    """JS Number.isFinite(Number(v)) 等价。"""
    n = js_number(v)
    return math.isfinite(n)


def num_or(v, dflt):
    """底本 `_num(v, dflt)` 等价：可转有限数则取之，否则取兜底。"""
    n = js_number(v)
    return n if math.isfinite(n) else dflt


def is_safe_positive_int(v) -> bool:
    """JS `Number.isSafeInteger(n) && n > 0` 等价（rrf.validFid 口径）。"""
    n = js_number(v)
    return math.isfinite(n) and n.is_integer() and 0 < n <= _SAFE_INT_MAX


def ordered_set(items=None) -> dict:
    """JS Set 的 Python 等价形态：dict 键序（成员判断 + 保序迭代两用）。"""
    return dict.fromkeys(items or ())
