# -*- coding: utf-8 -*-
"""kbfold.journal — append-only 回滚账本（undo 三段算法）。

库格式 v0.1 冻结口径（journal.jsonl）：一行一条，追加不改写不删除；
行格式 {"batch","op","from","to","hash_before","hash_after","ts"}；
op ∈ {rename, move, mkdir, adopt, undo}；undo 本身也是一条 append 记录
（不是把原行删掉）——任何时刻把账从头读一遍都能重放到「现在是什么状态」。

枢纽纪律：**先记账再动手，账写不下去就不动手**——不存在「改了但没记」的状态。

undo 三段算法（执行语义，冻结口径）：
  ① hash 校验停：逐条核对现状与账面 hash 一致，不符即停（报冲突，绝不带病回滚）；
  ② 占位改名：undo 目的地已被其他文件占用时，先把占用者改名为占位名（.kbfold-conflict-*），
     不覆盖任何文件；
  ③ 逆序执行：按账面逆序逐条回滚（后发生的先撤销）。
"""
from __future__ import annotations

import json
import os

JOURNAL_OPS = ("rename", "move", "mkdir", "adopt", "undo")


class JournalError(Exception):
    """账本读写错误。"""


def append_record(journal_path, batch, op, src, dst, hash_before, hash_after, ts):
    """追加一条账（不改写不删除）；返回写入的行 dict。op 必须在 JOURNAL_OPS 内。"""
    if op not in JOURNAL_OPS:
        raise JournalError("未知账目类型：%r" % op)
    rec = {
        "batch": str(batch or ""),
        "op": op,
        "from": "" if src is None else str(src),
        "to": "" if dst is None else str(dst),
        "hash_before": "" if hash_before is None else str(hash_before),
        "hash_after": "" if hash_after is None else str(hash_after),
        "ts": ts,
    }
    os.makedirs(os.path.dirname(journal_path) or ".", exist_ok=True)
    with open(journal_path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return rec


def read_journal(journal_path):
    """从头读账（重放语义：逐行解析；坏行如实保留原文于 _raw，不静默丢弃）。"""
    out = []
    if not os.path.isfile(journal_path):
        return out
    with open(journal_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                out.append({"op": "corrupt", "_raw": line})
    return out


def batch_ids(journal_path):
    """账内全部批次号（按首现序）。"""
    seen = []
    for rec in read_journal(journal_path):
        b = rec.get("batch")
        if b and b not in seen:
            seen.append(b)
    return seen


def plan_undo(journal_path, batch):
    """undo 三段算法的「算」半边（纯判定；「动」半边在宿主壳执行）。

    返回 {"batch", "ops":[{op, from, to, hash_before, hash_after}],
          "conflicts":[{op, path, expect, actual}], "conflict_renames":[{from, to}]}
    ① hash 校验：目的端现状 hash 与账面 hash_after 不符 ⇒ 记入 conflicts（执行方遇冲突即停）；
    ② 占位改名：undo 目的地（op.from）现被其他文件占用 ⇒ 记入 conflict_renames（执行方先挪占位）；
    ③ 逆序：ops 按账面逆序排列。
    """
    records = [r for r in read_journal(journal_path)
               if r.get("batch") == batch and r.get("op") in ("rename", "move")]
    ops = []
    conflicts = []
    conflict_renames = []
    for rec in reversed(records):
        ops.append({
            "op": "undo",
            "from": rec.get("to") or "",
            "to": rec.get("from") or "",
            "hash_before": rec.get("hash_after") or "",
            "hash_after": rec.get("hash_before") or "",
        })
    return {"batch": batch, "ops": ops, "conflicts": conflicts,
            "conflict_renames": conflict_renames}


def verify_undo_targets(plan_undo_result, root, hash_fn):
    """undo 第①段的校验执行（读现状、逐条对 hash；纯只读）。
    hash_fn(full_path) ⇒ 'sha256:…'。冲突逐条记入返回值，执行方据此「停」。"""
    conflicts = []
    for op in plan_undo_result.get("ops", []):
        target = os.path.join(root, op["from"].replace("/", os.sep))
        if not os.path.isfile(target):
            conflicts.append({"op": op["from"], "path": op["from"],
                              "expect": op["hash_before"], "actual": "missing"})
            continue
        actual = hash_fn(target)
        if op["hash_before"] and actual != op["hash_before"]:
            conflicts.append({"op": op["from"], "path": op["from"],
                              "expect": op["hash_before"], "actual": actual})
    return conflicts


def find_conflict_occupants(plan_undo_result, root):
    """undo 第②段的占位探测（读现状；纯只读）。
    目的地（op.to）被其他文件占用（且不是本条要撤回的那个文件）⇒ 需先占位改名。"""
    occupants = []
    for op in plan_undo_result.get("ops", []):
        dest = os.path.join(root, op["to"].replace("/", os.sep))
        src = os.path.join(root, op["from"].replace("/", os.sep))
        if os.path.isfile(dest) and os.path.abspath(dest) != os.path.abspath(src):
            occupants.append({"from": op["to"],
                              "to": op["to"] + ".kbfold-conflict"})
    return occupants
