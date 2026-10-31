# -*- coding: utf-8 -*-
"""K3 反馈与安全面：KBFold 反馈 SDK（core/py/kbfold/feedback.py + CLI feedback 子命令）测试。

口径正本 = docs/FEEDBACK.md：三档 L0（关，默认）/L1（三态点选）/L2（计数+delta+结构
hash）/L3（脱敏失败样例）；首启问一次；本地落盘 .kbfold/feedback.jsonl；原文残留检测。

夹具一律 tmp_path（测试数据隔离，铁律 0.14：禁依赖真实工作区状态）；
本模块零网络调用（SDK 只产上行载荷不联网，与三承诺一致）。
"""

import json
import os
import subprocess
import sys

import pytest

PUB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE_PY = os.path.join(PUB_ROOT, "core", "py")
CLI = os.path.join(PUB_ROOT, "hosts", "cli", "kbfold_cli.py")
if CORE_PY not in sys.path:
    sys.path.insert(0, CORE_PY)

from kbfold import feedback as fb  # noqa: E402


@pytest.fixture()
def fbdir(tmp_path, monkeypatch):
    """反馈目录指到 tmp_path，并清掉环境变量干扰（单跑绿/全跑绿/乱序绿）。"""
    d = str(tmp_path / ".kbfold")
    monkeypatch.setenv("KBFOLD_FEEDBACK_DIR", d)
    monkeypatch.delenv("KBOLD_FEEDBACK_DIR", raising=False)
    return d


def _l1_payload():
    return {
        "event_type": "archive_suggestion_vote",
        "result": "否决",
        "signal_level": "S2",
        "confidence": "0.85-0.95",
    }


def _l2_payload():
    p = _l1_payload()
    p["template_hits"] = {"tpl-meeting-minutes": 12, "channel-keyword": 3, "_inbox_backlog": 2}
    p["rule_delta"] = {"rule_id": "R-001", "field": "threshold", "before": "0.7", "after": "0.8"}
    p["dir_names"] = ["星海项目/2024合同", "远洋项目/会议记录"]
    return p


# ---------------------------------------------------------------------------
# L0 默认关：record / prepare_upload 全丢弃
# ---------------------------------------------------------------------------

def test_l0_record_dropped(fbdir):
    sdk = fb.FeedbackSdk(level="L0", feedback_dir=fbdir)
    res = sdk.record("L1", _l1_payload())
    assert res == {"dropped": True, "reason": "feedback_off(L0)"}
    assert not os.path.isfile(os.path.join(fbdir, "feedback.jsonl"))


def test_l0_prepare_upload_rejected(fbdir):
    sdk = fb.FeedbackSdk(level="L0", feedback_dir=fbdir)
    res = sdk.prepare_upload("L3", {"samples": [{"reason": "x", "sample": "y"}]})
    assert res["ok"] is False and res["rejected"] is True
    assert res["reason"] == "feedback_off(L0)"


def test_default_level_is_l0(fbdir):
    assert fb.FeedbackSdk(feedback_dir=fbdir).level == "L0"


# ---------------------------------------------------------------------------
# 首启问一次（纯函数 + state 落盘）
# ---------------------------------------------------------------------------

def test_should_ask_consent_first_run_true_then_never(fbdir):
    assert fb.should_ask_consent(None) is True
    assert fb.should_ask_consent({}) is True
    assert fb.should_ask_consent({"consent_asked": False}) is True
    # 问过一次置位后永不再问
    assert fb.should_ask_consent({"consent_asked": True}) is False
    path = fb.save_state({"consent_asked": True, "level": "L1"}, fbdir)
    assert os.path.isfile(path)
    assert fb.should_ask_consent(fb.load_state(fbdir)) is False


def test_state_roundtrip_and_corrupt_file_tolerated(fbdir):
    os.makedirs(fbdir, exist_ok=True)
    with open(os.path.join(fbdir, "state.json"), "w", encoding="utf-8") as f:
        f.write("{not-json")
    assert fb.load_state(fbdir) == {}  # 读坏回初态（= 该问一次）


def test_feedback_dir_env_var_override(tmp_path, monkeypatch):
    # 任务书原拼 KBOLD_FEEDBACK_DIR 兼容读
    d = str(tmp_path / "alt")
    monkeypatch.setenv("KBOLD_FEEDBACK_DIR", d)
    monkeypatch.delenv("KBFOLD_FEEDBACK_DIR", raising=False)
    assert fb.default_feedback_dir() == d


# ---------------------------------------------------------------------------
# L1 三态点选：白名单内上行，白名单外丢弃（不夹带）
# ---------------------------------------------------------------------------

def test_l1_record_written_with_schema(fbdir):
    sdk = fb.FeedbackSdk(level="L1", feedback_dir=fbdir)
    res = sdk.record("L1", _l1_payload())
    assert res["dropped"] is False
    with open(res["path"], encoding="utf-8") as f:
        lines = [json.loads(x) for x in f if x.strip()]
    assert len(lines) == 1
    assert lines[0]["schema"] == "kbfold.feedback/v1"
    assert lines[0]["level"] == "L1"


def test_l1_result_whitelist_adopt_reject_rejudge(fbdir):
    sdk = fb.FeedbackSdk(level="L1", feedback_dir=fbdir)
    for value in ("采纳", "否决", "改判"):
        res = sdk.prepare_upload("L1", {"event_type": "vote", "result": value})
        assert res["ok"] is True
        assert res["upload"]["event"]["result"] == value


def test_l1_non_whitelist_result_not_carried(fbdir):
    """白名单外点选值（含夹带串）一律不进上行载荷——不得超出同意面。"""
    sdk = fb.FeedbackSdk(level="L1", feedback_dir=fbdir)
    res = sdk.prepare_upload("L1", {"event_type": "vote", "result": "改成 <PERSON_1> 的方案"})
    assert res["ok"] is True
    assert "result" not in res["upload"]["event"]


def test_l1_signal_and_confidence_summary(fbdir):
    sdk = fb.FeedbackSdk(level="L1", feedback_dir=fbdir)
    res = sdk.prepare_upload("L1", _l1_payload())
    ev = res["upload"]["event"]
    assert ev["signal_level"] == "S2" and ev["confidence"] == "0.85-0.95"
    # 越界信号级别/畸形置信区间一律丢弃
    res2 = sdk.prepare_upload("L1", {"event_type": "v", "signal_level": "S9", "confidence": "随便写写"})
    assert "signal_level" not in res2["upload"]["event"]
    assert "confidence" not in res2["upload"]["event"]


def test_record_level_above_consent_dropped(fbdir):
    sdk = fb.FeedbackSdk(level="L1", feedback_dir=fbdir)
    res = sdk.record("L2", _l2_payload())
    assert res["dropped"] is True and "level_below_consent" in res["reason"]
    assert not os.path.isfile(os.path.join(fbdir, "feedback.jsonl"))


# ---------------------------------------------------------------------------
# L2：模板命中率纯计数 + 规则 delta 白名单 + 目录结构 hash（只 hash 不报原名）
# ---------------------------------------------------------------------------

def test_l2_template_hits_counter_only(fbdir):
    sdk = fb.FeedbackSdk(level="L2", feedback_dir=fbdir)
    res = sdk.prepare_upload("L2", _l2_payload())
    assert res["ok"] is True
    assert res["upload"]["template_hits"]["tpl-meeting-minutes"] == 12
    # 值非非负整数 -> 拒绝
    bad = _l2_payload()
    bad["template_hits"] = {"tpl-x": "很多"}
    assert sdk.prepare_upload("L2", bad)["rejected"] is True


def test_l2_rule_delta_whitelist_rejects_rogue_field(fbdir):
    sdk = fb.FeedbackSdk(level="L2", feedback_dir=fbdir)
    bad = _l2_payload()
    bad["rule_delta"] = {"rule_id": "R-1", "note": "把话术原文夹带进来"}
    res = sdk.prepare_upload("L2", bad)
    assert res["rejected"] is True and "越权字段" in res["reason"]


def test_l2_structure_hash_16hex_and_dir_names_never_uploaded(fbdir):
    sdk = fb.FeedbackSdk(level="L2", feedback_dir=fbdir)
    res = sdk.prepare_upload("L2", _l2_payload())
    assert res["ok"] is True
    h = res["upload"]["structure_hash"]
    assert len(h) == 16 and all(c in "0123456789abcdef" for c in h)
    # 原始目录名（星海/远洋/2024）任何字段都不出现在上行载荷
    assert "星海" not in json.dumps(res["upload"], ensure_ascii=False)
    assert "远洋" not in json.dumps(res["upload"], ensure_ascii=False)
    assert "dir_names" not in res["upload"]


def test_dir_structure_hash_normalized_slots_isomorphic_same_hash():
    """同构树（日期/编号不同、槽位结构相同）同 hash；异构树异 hash；16 位 hex。"""
    a = fb.dir_structure_hash(["星海项目/2024合同", "会议记录/2024-06"])
    b = fb.dir_structure_hash(["远洋项目/2023合同", "会议记录/2025年01月"])
    c = fb.dir_structure_hash(["星海项目/随手记"])
    assert a == b and a != c
    for h in (a, b, c):
        assert len(h) == 16 and all(ch in "0123456789abcdef" for ch in h)


def test_l2_dir_names_at_l1_rejected(fbdir):
    """原始目录名名单只允许 L2+ 本机消费；L1 出现即拒绝（不越同意面）。"""
    sdk = fb.FeedbackSdk(level="L1", feedback_dir=fbdir)
    p = _l1_payload()
    p["dir_names"] = ["某项目/合同"]
    res = sdk.prepare_upload("L1", p)
    assert res["rejected"] is True


def test_l2_bad_structure_hash_passthrough_rejected(fbdir):
    """宿主自算 hash 直传：非 16 位 hex 一律拒绝。"""
    sdk = fb.FeedbackSdk(level="L2", feedback_dir=fbdir)
    p = _l1_payload()
    p["structure_hash"] = "ZZZZ-not-hex!"
    assert sdk.prepare_upload("L2", p)["rejected"] is True


# ---------------------------------------------------------------------------
# L3：六类 PII 槽位化（五类文本槽 + 目录槽）+ 残留拒绝反例
# ---------------------------------------------------------------------------

def test_l3_sample_sanitized_all_slot_types(fbdir):
    sdk = fb.FeedbackSdk(level="L3", feedback_dir=fbdir)
    raw = ("张伟让李娜联系甲科技有限公司，电话13812345678或13900139002，"
           "邮箱 a.b@corp.example.com，合同款￥12,800万")
    res = sdk.prepare_upload("L3", {"samples": [{"reason": "wrong_target", "sample": raw}]})
    assert res["ok"] is True
    out = res["upload"]["samples"][0]["sample"]
    # 六类槽位：PHONE/EMAIL/AMOUNT/ORG/PERSON（文本五类）＋ 目录槽另由结构 hash 覆盖
    for slot in ("<PHONE_1>", "<PHONE_2>", "<EMAIL_1>", "<ORG_1>", "<PERSON_1>", "<PERSON_2>", "<AMOUNT_1>"):
        assert slot in out, slot
    # 原文具体值一个都不残留
    for original in ("张伟", "李娜", "甲科技有限公司", "13812345678", "13900139002",
                     "a.b@corp.example.com", "12,800"):
        assert original not in json.dumps(res["upload"], ensure_ascii=False), original


def test_l3_raw_phone_residue_rejected(fbdir):
    """反例：未槽位化手机号残留在脱敏不覆盖的标量面（规则 delta）-> 拒绝上行，
    宁少上行一条不漏上行一段原文。"""
    sdk = fb.FeedbackSdk(level="L3", feedback_dir=fbdir)
    p = {"rule_delta": {"rule_id": "R-1", "field": "threshold", "before": "0.7", "after": "联系13912345678确认"},
         "samples": [{"reason": "x", "sample": "已槽位化<AMOUNT_1>事项"}]}
    res = sdk.prepare_upload("L3", p)
    assert res["ok"] is False and res["rejected"] is True
    assert res["reason"].startswith("residue_detected(phone)")


def test_l3_raw_email_residue_rejected(fbdir):
    sdk = fb.FeedbackSdk(level="L3", feedback_dir=fbdir)
    p = {"rule_delta": {"rule_id": "R-1", "field": "note", "before": "1", "after": "发到 user@corp.example.com"},
         "samples": [{"reason": "x", "sample": "已槽位化<PHONE_1>事项"}]}
    res = sdk.prepare_upload("L3", p)
    assert res["rejected"] is True and "email" in res["reason"]


def test_l3_without_samples_rejected(fbdir):
    sdk = fb.FeedbackSdk(level="L3", feedback_dir=fbdir)
    assert sdk.prepare_upload("L3", _l2_payload())["rejected"] is True


def test_l3_include_l2_surface(fbdir):
    """档位逐档叠加：L3 载荷同时携带 L1/L2 面（三态/计数/结构 hash/delta）。"""
    sdk = fb.FeedbackSdk(level="L3", feedback_dir=fbdir)
    p = _l2_payload()
    p["samples"] = [{"reason": "asr_miss", "sample": "金额<AMOUNT_1>事项"}]
    res = sdk.prepare_upload("L3", p)
    assert res["ok"] is True
    up = res["upload"]
    assert up["event"]["result"] == "否决"
    assert up["template_hits"]["channel-keyword"] == 3
    assert len(up["structure_hash"]) == 16
    assert up["rule_delta"]["after"] == "0.8"


# ---------------------------------------------------------------------------
# CLI：kbfold feedback on|off|show（子进程实测；不改既有子命令行为）
# ---------------------------------------------------------------------------

def run_cli(*argv, cwd=None):
    proc = subprocess.run([sys.executable, CLI] + list(argv), cwd=cwd or str(PUB_ROOT),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    return proc.returncode, proc.stdout.decode("utf-8"), proc.stderr.decode("utf-8")


def test_cli_feedback_on_off_show_roundtrip(tmp_path, monkeypatch):
    d = str(tmp_path / ".kbfold")
    monkeypatch.setenv("KBFOLD_FEEDBACK_DIR", d)  # 子进程直接继承该环境变量
    code, out, err = run_cli("feedback", "on")
    assert code == 0, err
    assert "已开启" in out and "L1" in out
    state = fb.load_state(d)
    assert state == {"consent_asked": True, "level": "L1"}

    code, out, err = run_cli("feedback", "on", "--level", "L2")
    assert code == 0, err
    assert fb.load_state(d)["level"] == "L2"

    code, out, err = run_cli("feedback", "show")
    assert code == 0, err
    assert "档位=L2" in out and "尚无上行记录" in out

    # SDK 落一条 L2 记录后 show 应逐条列出
    sdk = fb.FeedbackSdk(level="L2", feedback_dir=d)
    sdk.record("L2", _l2_payload())
    code, out, err = run_cli("feedback", "show")
    assert code == 0, err
    assert "#1" in out and "kbfold" in out

    code, out, err = run_cli("feedback", "off")
    assert code == 0, err
    assert "已关闭" in out
    assert fb.load_state(d)["level"] == "L0"
    # 关闭后 record 再丢弃（恢复零外连口径）
    res = fb.FeedbackSdk(level="L0", feedback_dir=d).record("L1", _l1_payload())
    assert res["dropped"] is True


def test_cli_feedback_on_bad_level_exit2(tmp_path, monkeypatch):
    monkeypatch.setenv("KBFOLD_FEEDBACK_DIR", str(tmp_path / ".kbfold"))
    code, out, err = run_cli("feedback", "on", "--level", "L9")
    assert code == 2 and "未知档位" in err


def test_cli_feedback_env_var_isolated(tmp_path):
    """落盘目录环境变量覆盖：不经夹具改 cwd 也不污染工作区（铁律 0.14）。"""
    d = str(tmp_path / "custom")
    env = dict(os.environ)
    env["KBFOLD_FEEDBACK_DIR"] = d
    proc = subprocess.run([sys.executable, CLI, "feedback", "on", "--level", "L3"],
                          cwd=str(PUB_ROOT), env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    assert proc.returncode == 0, proc.stderr.decode("utf-8")
    assert os.path.isfile(os.path.join(d, "state.json"))
    assert fb.load_state(d)["level"] == "L3"
