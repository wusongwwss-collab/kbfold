# -*- coding: utf-8 -*-
"""WorkBuddy 送审包机闸（YQ-1153·技能单包形态）——源件格式 + 构建产物完整性 + 幂等。

判据：
  1. wb-skill/SKILL.md YAML 头：必填字段齐全；冒号后恰一空格；值不以引号包裹
  2. 本包为技能单包（连接器因 node+python 双运行时后置）：刻意不含 wb-connector
  3. build.mjs（需 node）：产物清单完整且连跑两次逐字节一致；缺 node 时跳过第 3 组
"""
import hashlib
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WB = os.path.join(ROOT, "hosts", "workbuddy")
DIST = os.path.join(ROOT, "dist")

FIELDS = ["name", "display_name", "display_name_en", "description",
          "description_zh", "description_en", "category", "version", "author"]
NODE = shutil.which("node")


def _head():
    with open(os.path.join(WB, "wb-skill", "SKILL.md"), encoding="utf-8") as f:
        return f.read().split("---")[1]


@pytest.mark.parametrize("field", FIELDS)
def test_skill_head_field(field):
    assert re.search(rf"^{field}: .+", _head(), re.M), field


def test_skill_head_spacing_and_no_quotes():
    lines = [l for l in _head().split("\n") if re.match(r"^\S", l)]
    assert all(re.match(r"^[\w_]+: ", l) for l in lines)
    assert not any(re.match(r'^[\w_]+: ["\']', l) for l in lines)


def test_skill_naming_and_author():
    h = _head()
    assert re.search(r"display_name: 知识库整备台", h)
    assert re.search(r"author: 弈枢AI Easur Work", h)


def test_skill_only_no_connector():
    assert not os.path.isdir(os.path.join(WB, "wb-connector")), "本批拍板=技能单包形态，连接器后置"


MUST_EXIST = [
    ("wb-skill", "SKILL.md"), ("wb-skill", "references", "README.md"),
    ("wb-skill", "references", "FORMAT.md"),
    ("wb-skill", "templates", "rules.example.yaml"),
    ("wb-skill", "templates", "file-kind.yaml"), ("wb-skill", "templates", "project-stage.yaml"),
    ("wb-skill", "LICENSE"),
]


def _hash_tree():
    out = []
    for base, _dirs, files in os.walk(DIST):
        for fn in files:
            p = os.path.join(base, fn)
            rel = os.path.relpath(p, DIST).replace("\\", "/")
            out.append(rel + ":" + hashlib.sha256(open(p, "rb").read()).hexdigest()[:16])
    return "|".join(sorted(out))


@pytest.mark.skipif(NODE is None, reason="node 不可用")
def test_build_complete_and_idempotent():
    subprocess.run([NODE, os.path.join(WB, "build.mjs")], cwd=ROOT, check=True, capture_output=True)
    for seg in MUST_EXIST:
        assert os.path.isfile(os.path.join(DIST, *seg)), seg
    h1 = _hash_tree()
    subprocess.run([NODE, os.path.join(WB, "build.mjs")], cwd=ROOT, check=True, capture_output=True)
    assert h1 == _hash_tree()
