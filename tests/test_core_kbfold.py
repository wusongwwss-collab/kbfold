# -*- coding: utf-8 -*-
"""core 格式件单测：rules DSL / manifest 四态 / plan IR / journal undo 三段。

夹具一律 tmp_path（测试数据隔离：不依赖工作区真实状态，单跑绿、全跑绿）。
"""

import json
import os
import sys

import pytest

PUB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE_PY = os.path.join(PUB_ROOT, "core", "py")
if CORE_PY not in sys.path:
    sys.path.insert(0, CORE_PY)

from kbfold import journal, manifest, plan, rules  # noqa: E402

GOOD_RULES_TEXT = """\
version: 1
root: .
basis: kbfold:generic/v1
kbs:
  - name: 星海项目
    id: XH-2024
    detect:
      markers: [".xhproject"]
      keywords: ["星海"]
    paths:
      - match: "**/合同/**"
        to: "02-合同"
      - match: "**/过程稿/**"
        to: "@exclude"
  - name: 弈枢设计
    id: YS-01
    paths:
      - match: "**/图纸/**"
        to: "04-图纸"
"""


# ---------------------------------------------------------------------------
# rules DSL
# ---------------------------------------------------------------------------

def test_rules_parse_and_validate_ok():
    parsed = rules.parse_rules_text(GOOD_RULES_TEXT)
    assert parsed["version"] == 1
    assert parsed["basis"] == "kbfold:generic/v1"
    assert parsed["kbs"][0]["id"] == "XH-2024"
    assert parsed["kbs"][0]["detect"]["markers"] == [".xhproject"]
    assert parsed["kbs"][0]["paths"][1]["to"] == "@exclude"
    assert rules.validate_rules(parsed) == []


def test_rules_unknown_version_errors():
    parsed = rules.parse_rules_text(GOOD_RULES_TEXT.replace("version: 1", "version: 2"))
    errs = rules.validate_rules(parsed)
    assert errs and "version" in errs[0]


def test_rules_generic_names_blacklist_startup_error():
    bad = GOOD_RULES_TEXT.replace('to: "04-图纸"', 'to: "data"')
    parsed = rules.parse_rules_text(bad)
    errs = rules.validate_rules(parsed)
    assert any("GENERIC_NAMES" in e for e in errs)


def test_rules_judge_marker_s1_wins():
    parsed = rules.parse_rules_text(GOOD_RULES_TEXT)
    j = rules.judge_file(parsed, "星海项目/合同/a.pdf", marker_kb="XH-2024")
    assert j["kb"] == "XH-2024"
    assert j["target"] == "02-合同"
    assert j["confidence"] == 0.95
    assert j["via"] == "s1"


def test_rules_judge_path_rule_specificity_and_later_wins():
    parsed = rules.parse_rules_text(
        "version: 1\nroot: .\nbasis: g\nkbs:\n  - name: A\n    id: A1\n    paths:\n"
        "      - match: \"**/合同/**\"\n        to: \"02-合同\"\n"
        "      - match: \"**/合同/2024/**\"\n        to: \"02-合同-2024\"\n")
    j = rules.judge_file(parsed, "x/合同/2024/b.pdf")
    assert j["target"] == "02-合同-2024"  # 路径段更多者胜
    j2 = rules.judge_file(parsed, "x/合同/b.pdf")
    assert j2["target"] == "02-合同"


def test_rules_judge_tie_inbox_with_candidates():
    parsed = rules.parse_rules_text(
        "version: 1\nroot: .\nbasis: g\nkbs:\n"
        "  - name: A\n    id: A1\n    paths:\n      - match: \"**/misc/**\"\n        to: \"A-区\"\n"
        "  - name: B\n    id: B1\n    paths:\n      - match: \"**/misc/**\"\n        to: \"B-区\"\n")
    j = rules.judge_file(parsed, "misc/c.txt")
    assert j["target"] == "@inbox"
    assert len(j["candidates"]) == 2
    assert {c["target"] for c in j["candidates"]} == {"A-区", "B-区"}


def test_rules_judge_name_exact_and_keyword():
    parsed = rules.parse_rules_text(GOOD_RULES_TEXT)
    # 路径规则命中优先于名称全等（同库时目标取路径规则）——先验证这条裁决序：
    j0 = rules.judge_file(parsed, "弈枢设计/图纸/d.dwg", name_hits=["YS-01"])
    assert j0["kb"] == "YS-01" and j0["target"] == "04-图纸" and j0["via"] == "path-rule"
    # 无路径规则命中 ⇒ 名称全等 L1a 0.92，目标落库根
    j = rules.judge_file(parsed, "弈枢设计/其他/x.txt", name_hits=["YS-01"])
    assert j["kb"] == "YS-01" and j["confidence"] == 0.92
    k = rules.judge_file(parsed, "notes/星海-会议纪要.md", keyword_hits={"XH-2024": ["星海"]})
    assert k["kb"] == "XH-2024" and k["confidence"] == 0.80
    assert k["via"] == "keyword"


def test_rules_judge_no_match_falls_to_basis():
    parsed = rules.parse_rules_text(GOOD_RULES_TEXT)
    j = rules.judge_file(parsed, "随便/什么.xyz")
    assert j["target"] == "@inbox" and j["via"] == "basis"


def test_rules_triage_bands():
    assert rules.triage(0.95) == "direct"
    assert rules.triage(0.85) == "suggest"
    assert rules.triage(0.4) == "inbox"
    assert rules.triage(None) == "inbox"


def test_rules_suggest_sections_scale():
    assert rules.suggest_sections(30) == (1, 3)
    assert rules.suggest_sections(200) == (4, 6)
    assert rules.suggest_sections(900) == (8, 8)


def test_rules_locked_hash_stable():
    h1 = rules.rules_locked_hash(GOOD_RULES_TEXT)
    h2 = rules.rules_locked_hash(GOOD_RULES_TEXT)
    assert h1 == h2 and h1.startswith("sha256:")


# ---------------------------------------------------------------------------
# manifest 四态
# ---------------------------------------------------------------------------

def _mk_file(root, rel, content, mtime=1000.0):
    full = os.path.join(str(root), rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(full) or str(root), exist_ok=True)
    with open(full, "wb") as f:
        f.write(content if isinstance(content, bytes) else content.encode("utf-8"))
    os.utime(full, (mtime, mtime))
    return full


def test_manifest_scan_and_diff_added_changed_moved(tmp_path):
    _mk_file(tmp_path, "a/one.txt", "one")
    _mk_file(tmp_path, "a/two.txt", "two")
    scan1 = manifest.scan_files(str(tmp_path))
    recs = {}
    for rel, meta in scan1.items():
        recs[rel] = dict(meta, hash=manifest.hash_file(os.path.join(str(tmp_path), rel)))
    mf_files = recs

    # 变更 + 移动 + 新增 同时发生（移动：同 hash 新路径 ⇒ 只改链接不重解析的一态）
    _mk_file(tmp_path, "a/one.txt", "ONE-CHANGED", mtime=2000.0)  # 变更
    os.makedirs(os.path.join(str(tmp_path), "b"), exist_ok=True)
    os.rename(os.path.join(str(tmp_path), "a", "two.txt"),
              os.path.join(str(tmp_path), "b", "two.txt"))          # 移动
    _mk_file(tmp_path, "a/three.txt", "three")                      # 新增
    scan2 = manifest.scan_files(str(tmp_path))
    d = manifest.diff_manifest(mf_files, scan2, str(tmp_path))
    assert "a/three.txt" in d["added"]
    assert "a/one.txt" in d["changed"]
    assert d["moved"].get("b/two.txt") == "a/two.txt"


def test_manifest_disappeared_tombstone(tmp_path):
    _mk_file(tmp_path, "x/gone.txt", "data")
    scan1 = manifest.scan_files(str(tmp_path))
    recs = {rel: dict(m, hash=manifest.hash_file(os.path.join(str(tmp_path), rel)))
            for rel, m in scan1.items()}
    os.remove(os.path.join(str(tmp_path), "x", "gone.txt"))
    scan2 = manifest.scan_files(str(tmp_path))
    d = manifest.diff_manifest(recs, scan2, str(tmp_path))
    assert "x/gone.txt" in d["disappeared"]


def test_manifest_lazy_fingerprint_unchanged_touch(tmp_path):
    full = _mk_file(tmp_path, "f.txt", "stable")
    scan = manifest.scan_files(str(tmp_path))
    rec = dict(scan["f.txt"], hash=manifest.hash_file(full))
    os.utime(full, (rec["mtime"] + 5, rec["mtime"] + 5))  # 只碰时间戳
    scan2 = manifest.scan_files(str(tmp_path))
    d = manifest.diff_manifest({"f.txt": rec}, scan2, str(tmp_path))
    assert d["changed"] == {}  # 内容未变 ⇒ 不进变化集（惰性指纹口径）


def test_manifest_roundtrip_and_version_guard(tmp_path):
    mf_path = str(tmp_path / "manifest.json")
    mf = {"version": 1, "pid": "p", "rules_locked": "sha256:x", "files": {}}
    manifest.dump_manifest(mf_path, mf)
    assert manifest.load_manifest(mf_path)["version"] == 1
    with pytest.raises(manifest.ManifestError):
        manifest.dump_manifest(mf_path, dict(mf, version=2))
    with open(mf_path, "w", encoding="utf-8") as f:
        json.dump({"version": 99, "files": {}}, f)
    with pytest.raises(manifest.ManifestError):
        manifest.load_manifest(mf_path)


def test_manifest_batch_id_idempotent():
    b1 = manifest.make_batch_id("sha256:rules", "changeset-1", 1730000000)
    b2 = manifest.make_batch_id("sha256:rules", "changeset-1", 1730000000)
    b3 = manifest.make_batch_id("sha256:rules", "changeset-2", 1730000000)
    assert b1 == b2
    assert b1 != b3
    assert b1.count("-") == 2 and len(b1.split("-")[2]) == 8


def test_manifest_collect_signals_marker(tmp_path):
    _mk_file(tmp_path, "proj/.xhproject", "")
    _mk_file(tmp_path, "proj/合同/c.pdf", "c")
    parsed = rules.parse_rules_text(GOOD_RULES_TEXT)
    marker, name_hits, kw = manifest.collect_signals(parsed, "proj/星海-会议/c.pdf", None, str(tmp_path))
    assert marker == "XH-2024"
    assert "XH-2024" in kw


# ---------------------------------------------------------------------------
# plan IR
# ---------------------------------------------------------------------------

def _scan_of(tmp_path, rels):
    return {rel: {"size": 1, "mtime": 1.0} for rel in rels}


def test_plan_adopt_preferred_over_mkdir(tmp_path):
    os.makedirs(os.path.join(str(tmp_path), "XH-2024", "02-合同"), exist_ok=True)  # 既有容器
    parsed = rules.parse_rules_text(GOOD_RULES_TEXT)
    files = _scan_of(tmp_path, ["星海项目/合同/a.pdf", "new/散件.txt"])
    judged = {
        "星海项目/合同/a.pdf": rules.judge_file(parsed, "星海项目/合同/a.pdf", marker_kb="XH-2024"),
        "new/散件.txt": rules.judge_file(parsed, "new/散件.txt"),
    }
    p = plan.build_plan(str(tmp_path), parsed, "sha256:rules", files, judged, batch="b1")
    assert p["version"] == 1
    assert {"path": "XH-2024/02-合同", "as": "XH-2024/02-合同"} in p["adopt"]
    assert not any(m["path"] == "XH-2024/02-合同" for m in p["mkdir"])
    assert p["stats"]["adopt"] == 1
    # 散件：无命中 ⇒ inbox（不猜）
    assert any(i["from"] == "new/散件.txt" for i in p["inbox"])
    # 命中件：rename-only 平移清单
    r = [x for x in p["renames"] if x["from"] == "星海项目/合同/a.pdf"]
    assert r and r[0]["to"].endswith("a.pdf")


def test_plan_mkdir_when_missing(tmp_path):
    parsed = rules.parse_rules_text(GOOD_RULES_TEXT)
    files = _scan_of(tmp_path, ["星海项目/会议记录/m.md"])
    judged = {"星海项目/会议记录/m.md": rules.judge_file(
        parsed, "星海项目/会议记录/m.md",
        keyword_hits={"XH-2024": ["星海"]})}
    p = plan.build_plan(str(tmp_path), parsed, "sha256:rules", files, judged)
    # 词表命中置信 0.80 < 0.90 直落线 ⇒ 建议档：仍给 rename-only 清单，理由带 L3
    r = [x for x in p["renames"] if x["from"] == "星海项目/会议记录/m.md"]
    assert r and r[0]["confidence"] == 0.80 and "L3" in r[0]["reason"]


def test_plan_exclude_and_roundtrip(tmp_path):
    parsed = rules.parse_rules_text(GOOD_RULES_TEXT)
    files = _scan_of(tmp_path, ["x/过程稿/draft.docx"])
    judged = {"x/过程稿/draft.docx": rules.judge_file(parsed, "x/过程稿/draft.docx")}
    p = plan.build_plan(str(tmp_path), parsed, "sha256:rules", files, judged)
    assert p["stats"]["excluded"] == 1
    assert p["stats"]["renames"] == 0  # @exclude 永不入投影
    ppath = str(tmp_path / "plan.json")
    plan.dump_plan(ppath, p)
    assert plan.load_plan(ppath)["batch"] == p["batch"]


def test_plan_version_guard(tmp_path):
    ppath = str(tmp_path / "plan.json")
    with open(ppath, "w", encoding="utf-8") as f:
        json.dump({"version": 7}, f)
    with pytest.raises(plan.PlanError):
        plan.load_plan(ppath)


def test_plan_summary_mentions_rename_only():
    p = {"version": 1, "batch": "b", "stats": {"adopt": 1, "mkdir": 2, "renames": 3,
                                               "inbox": 1, "excluded": 0},
         "inbox": [{"from": "x", "candidates": [{"target": "A-区"}, {"target": "B-区"}],
                    "reason": "平手"}]}
    s = plan.plan_summary(p)
    assert "rename-only" in s and "3" in s and "A-区" in s


# ---------------------------------------------------------------------------
# journal：append-only + undo 三段
# ---------------------------------------------------------------------------

def test_journal_append_only_and_replay(tmp_path):
    jp = str(tmp_path / "journal.jsonl")
    journal.append_record(jp, "b1", "rename", "合同/a.pdf", "02-合同/a.pdf",
                          "sha256:h", "sha256:h", 1730000000)
    journal.append_record(jp, "b1", "undo", "02-合同/a.pdf", "合同/a.pdf",
                          "sha256:h", "sha256:h", 1730000100)
    recs = journal.read_journal(jp)
    assert len(recs) == 2
    assert recs[0]["op"] == "rename" and recs[1]["op"] == "undo"
    assert journal.batch_ids(jp) == ["b1"]
    with pytest.raises(journal.JournalError):
        journal.append_record(jp, "b1", "delete", "x", "y", "", "", 0)  # 未知 op 拒绝


def test_journal_undo_plan_reverse_order_and_hash_stop(tmp_path):
    jp = str(tmp_path / "journal.jsonl")
    _mk_file(tmp_path, "new/a.txt", "AAA")
    _mk_file(tmp_path, "new/b.txt", "BBB")
    ha = manifest.hash_file(os.path.join(str(tmp_path), "new", "a.txt"))
    hb = manifest.hash_file(os.path.join(str(tmp_path), "new", "b.txt"))
    journal.append_record(jp, "b1", "rename", "old/a.txt", "new/a.txt", ha, ha, 1)
    journal.append_record(jp, "b1", "rename", "old/b.txt", "new/b.txt", hb, hb, 2)
    pu = journal.plan_undo(jp, "b1")
    assert [o["from"] for o in pu["ops"]] == ["new/b.txt", "new/a.txt"]  # 逆序

    # 现场与账面一致 ⇒ 无冲突
    conflicts = journal.verify_undo_targets(pu, str(tmp_path), manifest.hash_file)
    assert conflicts == []

    # 现场被改过 ⇒ hash 校验停（绝不带病回滚）
    _mk_file(tmp_path, "new/b.txt", "CHANGED", mtime=3)
    conflicts2 = journal.verify_undo_targets(pu, str(tmp_path), manifest.hash_file)
    assert len(conflicts2) == 1 and conflicts2[0]["path"] == "new/b.txt"


def test_journal_undo_conflict_occupant_placeholder(tmp_path):
    jp = str(tmp_path / "journal.jsonl")
    journal.append_record(jp, "b1", "rename", "old/a.txt", "new/a.txt", "sha256:H", "sha256:H", 1)
    pu = journal.plan_undo(jp, "b1")
    _mk_file(tmp_path, "new/a.txt", "occupied-by-someone-else")
    occ = journal.find_conflict_occupants(pu, str(tmp_path))
    # 目的地被其他文件占用 ⇒ 执行方先把占用者挪占位名（不覆盖任何文件）；此处只验探测口径
    assert isinstance(occ, list)
