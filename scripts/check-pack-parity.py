#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""check-pack-parity — packs/ 实际内容 vs README 闭源面清单 parity 机闸（T13 #9，2026-11-08）。

用途：行业包（闭源六行业模板包）上线或闭源面清单变更时**一跑即核**——
列 packs/ 新增件与 README 清单差异，有差异即非零退出，防止：
  1. 闭源行业包/行业模板件误入开源仓 packs/；
  2. packs/ 开源面新增包/文件而 README 开源面清单未同步；
  3. README 闭源面披露行（开源面=…闭源面=…）被删改。

当前基线（v0.2.x）：packs/ 仅内置开源 L0 包 `kbfold/`（README.md / rules.example.yaml /
templates/{file-kind,project-stage}.yaml），零行业包 ⇒ 本脚本应 PASS（exit 0）。

用法：python scripts/check-pack-parity.py [--repo <仓根>]
"""

import argparse
import os
import sys

# ---------------------------------------------------------------------------
# 基线清单（随开源面扩展**显式**更新本清单 = 有意变更；不更新即跑红）
# ---------------------------------------------------------------------------

# packs/ 允许的顶层子目录（开源内置包）。行业包属闭源面（README 闭源面行），不在此列。
OPEN_PACKS_ALLOWLIST = {"kbfold"}

# 内置包文件级快照（相对 packs/ 的 POSIX 路径；新增开源模板须同步此处与 README）。
PACK_FILES_SNAPSHOT = {
    "kbfold/README.md",
    "kbfold/rules.example.yaml",
    "kbfold/templates/file-kind.yaml",
    "kbfold/templates/project-stage.yaml",
}

# README 闭源面披露行必须包含的关键词（该行是开源/闭源边界的唯一披露正本）。
README_CLOSED_FACE_KEYS = ("开源面", "闭源面", "六行业模板包")

_EXCLUDE_DIRS = {"__pycache__", ".DS_Store"}


def _rel_posix(root, dirpath, name):
    return os.path.relpath(os.path.join(dirpath, name), root).replace("\\", "/")


def scan_pack_files(packs_dir):
    """枚举 packs/ 下全部文件（相对 packs/ 的 POSIX 路径集合）。"""
    found = set()
    for dirpath, dirnames, filenames in os.walk(packs_dir):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIRS]
        for fn in filenames:
            if fn in _EXCLUDE_DIRS:
                continue
            found.add(_rel_posix(packs_dir, dirpath, fn))
    return found


def top_dirs(packs_dir):
    return {d for d in os.listdir(packs_dir)
            if os.path.isdir(os.path.join(packs_dir, d)) and d not in _EXCLUDE_DIRS}


def readme_closed_face_ok(readme_path):
    try:
        with open(readme_path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return False
    return any(all(k in line for k in README_CLOSED_FACE_KEYS)
               for line in text.splitlines())


def check(repo):
    packs_dir = os.path.join(repo, "packs")
    readme = os.path.join(repo, "README.md")
    problems = []

    if not os.path.isdir(packs_dir):
        return ["packs/ 目录缺失：%s" % packs_dir], []

    # 核 1：packs/ 顶层子目录 vs 开源白名单（多余 = 疑似行业包/未登记新包）
    actual_tops = top_dirs(packs_dir)
    for extra in sorted(actual_tops - OPEN_PACKS_ALLOWLIST):
        problems.append("packs 新增件：packs/%s/ 不在开源包白名单 %s —— 疑似行业包（闭源面）误入开源仓，"
                        "或开源新包未登记白名单与 README" % (extra, sorted(OPEN_PACKS_ALLOWLIST)))
    for missing in sorted(OPEN_PACKS_ALLOWLIST - actual_tops):
        problems.append("packs 缺件：白名单包 packs/%s/ 实际不存在" % missing)

    # 核 2：内置包文件级快照 vs 实际
    actual_files = scan_pack_files(packs_dir)
    snapshot = {p for p in PACK_FILES_SNAPSHOT}
    for extra in sorted(actual_files - snapshot):
        problems.append("packs 新增件：packs/%s 不在文件快照——新增开源模板须同步本脚本快照与 README，"
                        "行业模板（闭源面）不得入仓" % extra)
    for missing in sorted(snapshot - actual_files):
        problems.append("packs 缺件：packs/%s 在快照中但实际不存在（README「两套通用模板」口径）" % missing)

    # 核 3：README 闭源面披露行存在且含关键词
    if not readme_closed_face_ok(readme):
        problems.append("README 清单差异：未找到同时含 %s 的闭源面披露行——"
                        "开源/闭源边界披露被删改" % "/".join(README_CLOSED_FACE_KEYS))

    notes = ["packs/ 顶层包：%s" % (sorted(actual_tops) or "（空）"),
             "packs/ 文件 %d 件；行业包数＝0（当前基线）" % len(actual_files)
             if not (actual_tops - OPEN_PACKS_ALLOWLIST) else
             "packs/ 文件 %d 件；疑似行业包 %d 个" % (len(actual_files),
                                                      len(actual_tops - OPEN_PACKS_ALLOWLIST))]
    return problems, notes


def main(argv=None):
    ap = argparse.ArgumentParser(description="packs/ vs README 闭源面清单 parity 机闸")
    ap.add_argument("--repo", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    help="仓根（默认＝脚本上上级）")
    args = ap.parse_args(argv)
    problems, notes = check(args.repo)
    for n in notes:
        print("  " + n)
    if problems:
        print("FAIL: packs 与 README 闭源面清单存在 %d 处差异：" % len(problems))
        for p in problems:
            print("  - " + p)
        return 1
    print("PASS: packs/ 实际内容与 README 闭源面清单一致（零行业包，基线态）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
