# -*- coding: utf-8 -*-
"""check-pack-parity 自测（T13 #9，2026-11-08）。

最小自测四例：
  1. 真实仓当前基线应 PASS（零行业包，exit 0）——只读 git 管理的静态内容，不依赖可变状态；
  2. packs/ 出现疑似行业包目录 → 非零退出并列差异（行业包上线日误入开源仓即红）；
  3. README 闭源面披露行被删 → 非零退出（README 清单差异面）；
  4. 内置包混入新模板件 → 非零退出（文件快照面）。

负例一律用 tmp_path 副本（YQ-1119 隔离纪律），不动真实仓。
"""

import os
import shutil
import subprocess
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT = os.path.join(_REPO, "scripts", "check-pack-parity.py")


def _run(repo):
    p = subprocess.run([sys.executable, _SCRIPT, "--repo", str(repo)],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _copy_repo_skeleton(dst):
    """tmp_path 造最小同构副本：packs/ + README（只拷 parity 相关件）。"""
    shutil.copytree(os.path.join(_REPO, "packs"), os.path.join(dst, "packs"))
    shutil.copy2(os.path.join(_REPO, "README.md"), os.path.join(dst, "README.md"))
    return dst


def test_baseline_repo_passes():
    code, out = _run(_REPO)
    assert code == 0, out
    assert "PASS" in out and "零行业包" in out


def test_industry_pack_leak_fails(tmp_path):
    repo = _copy_repo_skeleton(tmp_path / "r1")
    os.makedirs(os.path.join(repo, "packs", "law-finance", "templates"))
    with open(os.path.join(repo, "packs", "law-finance", "templates", "t.yaml"),
              "w", encoding="utf-8") as f:
        f.write("# 行业模板\n")
    code, out = _run(repo)
    assert code == 1
    assert "packs/law-finance" in out and "packs 新增件" in out


def test_readme_closed_face_removed_fails(tmp_path):
    repo = _copy_repo_skeleton(tmp_path / "r2")
    readme = os.path.join(repo, "README.md")
    with open(readme, encoding="utf-8") as f:
        lines = [ln for ln in f.read().splitlines()
                 if not ("开源面" in ln and "闭源面" in ln)]
    with open(readme, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    code, out = _run(repo)
    assert code == 1
    assert "README 清单差异" in out


def test_builtin_pack_extra_file_fails(tmp_path):
    repo = _copy_repo_skeleton(tmp_path / "r3")
    with open(os.path.join(repo, "packs", "kbfold", "templates", "secret-extra.yaml"),
              "w", encoding="utf-8") as f:
        f.write("# 未登记新模板\n")
    code, out = _run(repo)
    assert code == 1
    assert "kbfold/templates/secret-extra.yaml" in out
