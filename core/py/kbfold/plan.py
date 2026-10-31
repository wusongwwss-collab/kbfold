# -*- coding: utf-8 -*-
"""kbfold.plan — 整备方案 IR（plan.json 读写器 + 方案生成）。

库格式 v0.1 冻结口径：plan.json 可 diff 可贴 issue；adopt 优先于 mkdir
（目录里已有的同类容器认作 adopt 纳入体系，不改名不移动，只在真正缺口处 mkdir——
公开规范「adopt 优先于 mkdir」定稿口径）。所以一次 plan 的产物是分列清单：
adopt 了什么、要新建什么、要平移（改名）什么、哪些进 _inbox 待归。

plan IR 结构（version=1；不认识的 version 报错退出）：
  {version, root, batch, rules_locked, adopt:[{path, as}], mkdir:[{path}],
   renames:[{from, to, kb, confidence, reason}], inbox:[{from, candidates, reason}],
   excluded:[{from, reason}], stats:{...}}
"""
from __future__ import annotations

import json
import os
import posixpath

from . import rules as rules_mod

PLAN_VERSION = 1


class PlanError(Exception):
    """plan IR 格式错误（含不认识版本：报错退出）。"""


def load_plan(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or data.get("version") != PLAN_VERSION:
        raise PlanError("不认识的 plan 版本（当前只支持 version: %d，不做猜测性兼容）" % PLAN_VERSION)
    return data


def dump_plan(path, plan):
    if plan.get("version") != PLAN_VERSION:
        raise PlanError("plan.version 必须为 %d" % PLAN_VERSION)
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _projection_path(kb_id, target):
    """投影路径：库编号前缀 + 目标目录（库根下的位置）。"""
    kb_id = str(kb_id or "").strip()
    target = str(target or "").strip().strip("/\\")
    if not target or target.startswith("@"):
        return ""
    if not kb_id:
        return target
    return kb_id + "/" + target


def build_plan(root, rules, rules_locked, files, judged, batch=""):
    """由判定结果生成分列清单（纯计算；不写盘）。

    root：扫描根；rules：解析后的规则 dict；rules_locked：规则快照指纹；
    files：{rel: {"size","mtime"}}（scan_files 产物）；judged：{rel: judge_file 产物}；
    batch：组合键幂等的批次号。
    """
    adopt = []
    mkdir = []
    renames = []
    inbox = []
    excluded = []
    seen_dirs = set()

    for rel in sorted(files.keys()):
        j = judged.get(rel) or {"kb": None, "target": "@inbox", "via": "basis",
                                "reason": "未判定", "confidence": None, "candidates": []}
        target = str(j.get("target") or "@inbox")
        if target == "@exclude":
            excluded.append({"from": rel, "reason": str(j.get("reason") or "")})
            continue
        if target == "@inbox":
            inbox.append({"from": rel, "candidates": j.get("candidates") or [],
                          "reason": str(j.get("reason") or "")})
            continue
        proj = _projection_path(j.get("kb"), target)
        if not proj:
            inbox.append({"from": rel, "candidates": j.get("candidates") or [],
                          "reason": "目标为空（不猜）"})
            continue
        current_dir = posixpath.dirname(rel)
        if current_dir == proj:
            continue  # 已在目标位置：不动
        dest = posixpath.join(root.replace("\\", "/").rstrip("/"), proj)
        # adopt 优先于 mkdir：目标目录已存在 ⇒ 认作既有容器纳入体系（不改名不移动）
        if proj not in seen_dirs:
            seen_dirs.add(proj)
            if os.path.isdir(os.path.join(root, proj.replace("/", os.sep))):
                adopt.append({"path": proj, "as": proj})
            else:
                mkdir.append({"path": proj})
        new_name = posixpath.join(proj, posixpath.basename(rel))
        renames.append({
            "from": rel,
            "to": new_name,
            "kb": j.get("kb"),
            "confidence": j.get("confidence"),
            "reason": str(j.get("reason") or ""),
        })

    return {
        "version": PLAN_VERSION,
        "root": root.replace("\\", "/"),
        "batch": batch,
        "rules_locked": rules_locked,
        "adopt": adopt,
        "mkdir": mkdir,
        "renames": renames,
        "inbox": inbox,
        "excluded": excluded,
        "stats": {
            "files": len(files),
            "adopt": len(adopt),
            "mkdir": len(mkdir),
            "renames": len(renames),
            "inbox": len(inbox),
            "excluded": len(excluded),
        },
    }


def plan_summary(plan):
    """人话摘要（报告观感 ≥ 执行观感：先看后动）。"""
    st = plan.get("stats") or {}
    lines = [
        "整备方案预览（%s）" % plan.get("batch", ""),
        "  纳入既有目录（adopt）：%d 处" % st.get("adopt", 0),
        "  需新建目录（mkdir）：%d 处" % st.get("mkdir", 0),
        "  拟改名平移：%d 件（rename-only 档；apply 前先记账）" % st.get("renames", 0),
        "  待归（不猜，附候选）：%d 件" % st.get("inbox", 0),
        "  永不入投影（@exclude，仍记指纹）：%d 件" % st.get("excluded", 0),
    ]
    for item in (plan.get("inbox") or [])[:5]:
        cand = "、".join(sorted({c.get("target", "") for c in item.get("candidates", []) if c.get("target")}))
        lines.append("    - %s：%s%s" % (item.get("from"), item.get("reason"),
                                         ("（候选：" + cand + "）") if cand else ""))
    return "\n".join(lines)
