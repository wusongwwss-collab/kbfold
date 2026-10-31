# -*- coding: utf-8 -*-
"""CLI 壳端到端测试：plan --demo 断网预览 → plan 落盘 → apply(rename-only) → undo 闭环。

夹具一律 tmp_path（测试数据隔离）；examples/demo/fixtures 只读实测演示承诺。
"""

import json
import os
import shutil
import subprocess
import sys

import pytest

PUB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE_PY = os.path.join(PUB_ROOT, "core", "py")
if CORE_PY not in sys.path:
    sys.path.insert(0, CORE_PY)

CLI = os.path.join(PUB_ROOT, "hosts", "cli", "kbfold_cli.py")
DEMO_ROOT = os.path.join(PUB_ROOT, "examples", "demo", "fixtures")
DEMO_RULES = os.path.join(PUB_ROOT, "examples", "demo", "kbfold.yml")


def run_cli(*argv, cwd=None):
    proc = subprocess.run([sys.executable, CLI] + list(argv), cwd=cwd or str(PUB_ROOT),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    return proc.returncode, proc.stdout.decode("utf-8"), proc.stderr.decode("utf-8")


# ---------------------------------------------------------------------------
# plan --demo：断网可跑出方案预览（公开承诺第一枪）
# ---------------------------------------------------------------------------

def test_cli_plan_demo_offline_preview():
    code, out, err = run_cli("plan", DEMO_ROOT, "--demo")
    assert code == 0, err
    assert "整备方案预览" in out
    assert "rename-only" in out
    assert "未写任何文件" in out
    # --demo 不落盘
    assert not os.path.isfile(os.path.join(DEMO_ROOT, ".kb", "plan.json"))


def test_cli_plan_demo_json_ir():
    code, out, err = run_cli("plan", DEMO_ROOT, "--demo", "--json")
    assert code == 0, err
    p = json.loads(out)
    assert p["version"] == 1
    assert p["stats"]["renames"] >= 1 and p["stats"]["inbox"] >= 1
    hit = [r for r in p["renames"] if "采购合同" in r["from"]]
    assert hit and hit[0]["kb"] == "XH-2024" and hit[0]["confidence"] == 0.95


def test_cli_plan_with_rules_file(tmp_path):
    dest = tmp_path / "root"
    shutil.copytree(DEMO_ROOT, str(dest))
    code, out, err = run_cli("plan", str(dest), "--rules", DEMO_RULES)
    assert code == 0, err
    assert os.path.isfile(str(dest / ".kb" / "plan.json"))
    assert os.path.isfile(str(dest / ".kb" / "rules.lock.yaml"))


# ---------------------------------------------------------------------------
# plan → apply(rename-only) → undo 闭环（tmp 夹具内，不动 examples 原件）
# ---------------------------------------------------------------------------

def test_cli_plan_apply_undo_loop(tmp_path):
    root = tmp_path / "proj"
    shutil.copytree(DEMO_ROOT, str(root))
    code, out, err = run_cli("plan", str(root), "--rules", DEMO_RULES)
    assert code == 0, err
    plan_path = root / ".kb" / "plan.json"
    p = json.loads(plan_path.read_text(encoding="utf-8"))
    batch = p["batch"]

    code, out, err = run_cli("apply", str(root), "--rules", DEMO_RULES)
    assert code == 0, err
    # 平移真的发生：源不在、投影在；.xhproject 标记文件不动
    assert not os.path.isfile(str(root / "星海项目" / "合同" / "2024-采购合同-v2.md"))
    dest_file = root / "XH-2024" / "02-合同" / "2024-采购合同-v2.md"
    assert os.path.isfile(str(dest_file))
    # 过程稿被 @exclude：原地不动
    assert os.path.isfile(str(root / "星海项目" / "过程稿" / "合同-草稿-旧版.md"))
    # 账本先记后动：journal 有 rename 行
    journal_text = (root / ".kb" / "journal.jsonl").read_text(encoding="utf-8")
    assert '"op": "rename"' in journal_text or '"op":"rename"' in journal_text

    # undo 闭环
    code, out, err = run_cli("undo", str(root), "--batch", batch)
    assert code == 0, err
    assert os.path.isfile(str(root / "星海项目" / "合同" / "2024-采购合同-v2.md"))
    assert not os.path.isfile(str(dest_file))
    # undo 也是 append 记录
    journal_text2 = (root / ".kb" / "journal.jsonl").read_text(encoding="utf-8")
    assert journal_text2.count("\n") > journal_text.count("\n")


def test_cli_undo_hash_conflict_stops(tmp_path):
    root = tmp_path / "proj"
    shutil.copytree(DEMO_ROOT, str(root))
    run_cli("plan", str(root), "--rules", DEMO_RULES)
    p = json.loads((root / ".kb" / "plan.json").read_text(encoding="utf-8"))
    code, _out, _err = run_cli("apply", str(root), "--rules", DEMO_RULES)
    assert code == 0
    # 篡改投影文件内容 ⇒ hash 校验停（绝不带病回滚）
    dest_file = root / "XH-2024" / "02-合同" / "2024-采购合同-v2.md"
    dest_file.write_text("# 被人改过的内容\n", encoding="utf-8")
    code, out, err = run_cli("undo", str(root), "--batch", p["batch"])
    assert code == 1
    assert "hash-conflict" in err or "中止" in err


# ---------------------------------------------------------------------------
# audit / validate / explain / search / index
# ---------------------------------------------------------------------------

def test_cli_audit_self_attestation():
    code, out, err = run_cli("audit")
    assert code == 0, err
    assert "零外连自证" in out
    assert "禁网模块命中：0" in out
    assert "删除动作命中：0" in out
    assert "import" in out  # 逐文件 import 清单


def test_cli_validate_rules():
    code, out, err = run_cli("validate", "--rules", DEMO_RULES)
    assert code == 0, err
    assert "通过" in out
    # 黑名单：to: data 启动即报错
    bad = os.path.join(PUB_ROOT, "examples", "demo", "_bad_rules.tmp.yml")
    with open(bad, "w", encoding="utf-8") as f:
        f.write('version: 1\nroot: .\nbasis: g\nkbs:\n  - name: A\n    id: A1\n'
                '    paths:\n      - match: "**/x/**"\n        to: "data"\n')
    try:
        code2, out2, err2 = run_cli("validate", "--rules", str(bad))
        assert code2 == 1
        assert "GENERIC_NAMES" in err2
    finally:
        os.remove(str(bad))


def test_cli_index_search_explain(tmp_path):
    root = tmp_path / "proj"
    shutil.copytree(DEMO_ROOT, str(root))
    code, out, err = run_cli("index", str(root), "--rules", DEMO_RULES)
    assert code == 0, err
    assert (root / ".kb" / "index.sqlite").is_file()

    code, out, err = run_cli("search", str(root), "采购合同")
    assert code == 0, err
    assert "[1]" in out and "采购合同" in out

    # apply 后 explain 能回答「为什么在这」
    run_cli("apply", str(root), "--rules", DEMO_RULES)
    code, out, err = run_cli("explain", str(root), "XH-2024/02-合同/2024-采购合同-v2.md")
    assert code == 0, err
    rec = json.loads(out)
    assert rec["found"] is True
    assert "L0" in rec["reason"] and rec["kb"] == "XH-2024"

# ---------------------------------------------------------------------------
# apply 只做 rename-only 档：全档=默认；@exclude 与 inbox 件不动
# ---------------------------------------------------------------------------

def test_cli_apply_leaves_excluded_and_inbox_in_place(tmp_path):
    root = tmp_path / "proj"
    shutil.copytree(DEMO_ROOT, str(root))
    run_cli("plan", str(root), "--rules", DEMO_RULES)
    code, out, err = run_cli("apply", str(root), "--rules", DEMO_RULES)
    assert code == 0, err
    assert os.path.isfile(str(root / "星海项目" / "过程稿" / "合同-草稿-旧版.md"))
    assert os.path.isfile(str(root / "散落资料" / "随手记.txt"))
