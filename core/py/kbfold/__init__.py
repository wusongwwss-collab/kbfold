# -*- coding: utf-8 -*-
"""KBFold · 知识库整备台 — 零依赖判定引擎（Python 标准库 only）。

一核多壳的「核」：判定梯 L0-L6、四层信号 S1-S4、规则 DSL v1、manifest/journal/plan
读写器、RRF k=60、父子分块 512/64/4、容量四闸、阅读顺序 tolK 三档。
三承诺：不删文件（代码里没有删除动作）、不联网（audit 自证）、不猜（置信不足一律进 _inbox/）。

本包全部判定参数为冻结契约（搬运正本逐字保留并注出处），禁调参改名后当成新设计。
"""
from __future__ import annotations

__version__ = "0.1.0"

from . import (assign, auto_confirm, capacity, chunk, classify, dir_classify,
               journal, manifest, plan, project_match, readorder, rrf, rules)

__all__ = [
    "assign", "auto_confirm", "capacity", "chunk", "classify", "dir_classify",
    "journal", "manifest", "plan", "project_match", "readorder", "rrf", "rules",
    "__version__",
]
