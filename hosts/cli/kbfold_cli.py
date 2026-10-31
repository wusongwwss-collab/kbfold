#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kbfold_cli — KBFold · 知识库整备台 命令行壳（零第三方依赖，Python ≥3.10 标准库 only）。

子命令（Q1=B 四能力口径：扫描 / 匹配 / 方案 / 回滚；discover 不在本版）：
  plan      生成整备方案预览（--demo 断网可跑；默认落盘 .kb/plan.json + rules.lock.yaml）
  apply     执行方案（默认 --rename-only 档＝第一枪档 1；先记账再动手，账写不进就不动手）
  undo      按 batch 回滚（undo 三段：hash 校验停 / 占位改名 / 逆序）
  index     构建分块索引（子块 512 / 重叠 64 / 父块连续 4 子块；FTS5）
  search    三路检索 + RRF 融合（k=60），结果带 [n] 引用编号
  audit     零外连自证：import 清单 + 禁网模块扫描 + 删除动作零命中
  explain   回答「这个文件为什么被放在这里」（读 manifest.reason 人话字段）
  validate  规则 DSL 结构校验（黑名单/冲突/version；不认识版本报错退出）
  feedback  匿名改进反馈开关（on|off|show；默认 L0 关＝零网络调用，口径见 docs/FEEDBACK.md）

三承诺：不删文件（代码里没有删除动作）/ 不联网（audit 自证；feedback 档位状态机
只做本地落盘与上行预检，本壳不发起任何网络请求）/ 不猜（置信不足一律进 _inbox/）。
启动：python hosts/cli/kbfold_cli.py <子命令> …
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_CORE_PY = os.path.join(os.path.dirname(os.path.dirname(_HERE)), "core", "py")
if _CORE_PY not in sys.path:
    sys.path.insert(0, _CORE_PY)

from kbfold import feedback as feedback_mod  # noqa: E402
from kbfold import journal, rules as rules_mod  # noqa: E402
from kbfold import service  # noqa: E402


def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def _load_rules_args(args):
    return service.load_rules(args.rules, demo=args.demo)


def cmd_plan(args):
    rules, _text, source = _load_rules_args(args)
    errs = rules_mod.validate_rules(rules)
    if errs:
        print("✗ 规则校验未通过：", file=sys.stderr)
        for e in errs:
            print("  - " + e, file=sys.stderr)
        return 2
    p = service.judge_root(args.root, rules, write=not args.demo)
    if args.json:
        _print(p)
    else:
        print(_summary_text(p))
        if not args.demo:
            print("\n方案已落盘：" + service.kb_path(os.path.abspath(args.root), service.PLAN_NAME))
            print("规则快照：" + service.kb_path(os.path.abspath(args.root), service.RULES_LOCK_NAME))
        else:
            print("\n（--demo 预览：未写任何文件）")
    return 0


def _summary_text(p):
    lines = [
        "整备方案预览（batch=%s）" % p.get("batch", ""),
        "  纳入既有目录（adopt）：%d 处" % p["stats"]["adopt"],
        "  需新建目录（mkdir）：%d 处" % p["stats"]["mkdir"],
        "  拟改名平移：%d 件（rename-only 档）" % p["stats"]["renames"],
        "  待归 _inbox（不猜，附候选）：%d 件" % p["stats"]["inbox"],
        "  永不入投影（@exclude，仍记指纹）：%d 件" % p["stats"]["excluded"],
    ]
    for item in (p.get("inbox") or [])[:8]:
        cand = "、".join(sorted({c.get("target", "") for c in item.get("candidates", []) if c.get("target")}))
        lines.append("    - %s：%s%s" % (item.get("from"), item.get("reason"),
                                         ("（候选：" + cand + "）") if cand else ""))
    return "\n".join(lines)


def cmd_apply(args):
    rules, _text, _source = _load_rules_args(args)
    errs = rules_mod.validate_rules(rules)
    if errs:
        print("✗ 规则校验未通过（apply 前置）：", file=sys.stderr)
        for e in errs:
            print("  - " + e, file=sys.stderr)
        return 2
    p = service.judge_root(args.root, rules, write=True)
    if not args.rename_only:
        print("✗ v0.1 只提供 rename-only 档（第一枪档 1）；移动/重排档未开放", file=sys.stderr)
        return 2
    res = service.apply_renames(args.root, p, rules=rules, rename_only=True)
    print("执行完成（rename-only）：动作 %d 件，batch=%s" % (res["done"], res["batch"]))
    print("账本：" + res["journal"])
    print("回滚：python %s undo %s --batch %s" % (sys.argv[0], args.root, res["batch"]))
    return 0


def cmd_undo(args):
    res = service.undo_batch(args.root, args.batch)
    if res["stopped"]:
        print("✗ 回滚中止：%s" % res["stopped"], file=sys.stderr)
        _print(res["conflicts"])
        return 1
    print("回滚完成：%d 件（batch=%s，undo 也已追加记账）" % (res["undone"], args.batch))
    return 0


def cmd_index(args):
    rules, _text, _source = _load_rules_args(args)
    res = service.build_index(args.root, rules)
    print("索引完成：%s（文件 %d，子块 %d；512/64/4 冻结参数）"
          % (res["index"], res["files"], res["chunks"]))
    return 0


def cmd_search(args):
    res = service.search_root(args.root, args.query, limit=args.limit)
    print("检索「%s」：内容路 %d 命中 · 关键词路 %d 命中（语义路：本地嵌入端点缺席，静默跳过）"
          % (res["query"], res["channels"].get("fts", 0), res["channels"].get("keyword", 0)))
    for r in res["results"]:
        print("  %s  (score=%s, via=%s)" % (r["ref"], r["score"], r["source"]))
    if not res["results"]:
        print("  （无命中）")
    return 0


def cmd_audit(args):
    repo_root = args.repo or os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    res = service.audit_self(repo_root)
    print("kbfold audit —— 零外连自证")
    print("扫描面：core/py + hosts（逐文件 import 清单）")
    for rel in sorted(res["imports"]):
        mods = res["imports"][rel]
        print("  %s: %s" % (rel, ", ".join(mods) if mods else "（无 import）"))
    print("禁网模块命中：%d" % len(res["network_violations"]))
    print("删除动作命中：%d" % len(res["delete_violations"]))
    print("结论：%s" % res["verdict"])
    return 0 if res["ok"] else 1


def cmd_explain(args):
    res = service.explain_path(args.root, args.path)
    if not res.get("found"):
        print("✗ " + res.get("reason", "无记录"), file=sys.stderr)
        return 1
    _print(res)
    return 0


def cmd_validate(args):
    if not os.path.isfile(args.rules):
        print("✗ 规则文件不存在：" + args.rules, file=sys.stderr)
        return 2
    rules, _text, _src = service.load_rules(args.rules)
    errs = rules_mod.validate_rules(rules)
    if errs:
        print("✗ 规则校验未通过（%d 条）：" % len(errs), file=sys.stderr)
        for e in errs:
            print("  - " + e, file=sys.stderr)
        return 1
    print("✓ 规则校验通过：version=%s，kbs=%d（黑名单/冲突/version 均无异常）"
          % (rules.get("version"), len(rules.get("kbs", []))))
    return 0


_LEVEL_HINT = {
    "L1": "被拒绝的归档建议三态点选（采纳/否决/改判＋信号级别/置信区间；不含路径与文件名）",
    "L2": "＋模板命中率计数、规则 delta（字段白名单）、目录结构 hash（只 hash 不报原始名）",
    "L3": "＋本地脱敏失败样例（槽位化＋残留检测通过才上行）",
}


def cmd_feedback(args):
    """匿名改进反馈开关（docs/FEEDBACK.md §4）：on 默认进 L1；off 恢复零外连；
    show 逐条列出本机落盘的上行记录（时间/档位/脱敏后内容摘要）。"""
    base = feedback_mod.default_feedback_dir()
    state = feedback_mod.load_state(base)
    action = args.action

    if action == "on":
        level = (args.level or "L1").upper()
        if level not in ("L1", "L2", "L3"):
            print("✗ 未知档位：%s（可选 L1/L2/L3，默认 L1）" % args.level, file=sys.stderr)
            return 2
        state["consent_asked"] = True
        state["level"] = level
        feedback_mod.save_state(state, base)
        print("✓ 反馈已开启：档位 %s（状态：%s）" % (level, os.path.join(base, feedback_mod.FEEDBACK_STATE_FILE)))
        print("  本档上行内容：" + _LEVEL_HINT[level])
        print("  任何档位永不上行：文件名、目录原名、文件内容、路径原文、人名、公司、金额、电话、邮箱。")
        return 0

    if action == "off":
        state["consent_asked"] = True
        state["level"] = "L0"
        feedback_mod.save_state(state, base)
        print("✓ 反馈已关闭：恢复零外连（L0）；已上行的匿名数据可申请删除。")
        print("  上行记录本机可查：kbfold feedback show")
        return 0

    # show：逐条列出本机上行记录（无论当前开关状态）
    sdk_level = state.get("level", "L0")
    asked = bool(state.get("consent_asked"))
    print("当前状态：档位=%s（consent_asked=%s）；记录目录：%s" % (sdk_level, asked, base))
    log_path = os.path.join(base, feedback_mod.FEEDBACK_LOG_FILE)
    if not os.path.isfile(log_path):
        print("（尚无上行记录）")
        return 0
    count = 0
    with open(log_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            count += 1
            summary = json.dumps(rec.get("payload", {}), ensure_ascii=False)
            if len(summary) > 80:
                summary = summary[:77] + "..."
            print("  #%d %s [%s] %s" % (count, rec.get("ts", ""), rec.get("level", ""), summary))
    if count == 0:
        print("（尚无上行记录）")
    return 0


def build_parser():
    ap = argparse.ArgumentParser(prog="kbfold", description="KBFold · 知识库整备台（开源版 v0.1）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_plan = sub.add_parser("plan", help="生成整备方案（--demo 断网预览）")
    p_plan.add_argument("root", help="扫描根目录")
    p_plan.add_argument("--rules", help="规则文件路径（.yml/.json；缺省用演示规则）")
    p_plan.add_argument("--demo", action="store_true", help="演示模式：只预览不落盘")
    p_plan.add_argument("--json", action="store_true", help="输出完整 plan IR JSON")
    p_plan.set_defaults(fn=cmd_plan)

    p_apply = sub.add_parser("apply", help="执行方案（默认 rename-only 档）")
    p_apply.add_argument("root")
    p_apply.add_argument("--rules", help="规则文件路径")
    p_apply.add_argument("--demo", action="store_true")
    p_apply.add_argument("--rename-only", action="store_true", default=True,
                         help="（默认即此档）只做改名平移，不删除任何文件")
    p_apply.set_defaults(fn=cmd_apply)

    p_undo = sub.add_parser("undo", help="按 batch 回滚（undo 三段算法）")
    p_undo.add_argument("root")
    p_undo.add_argument("--batch", required=True, help="要回滚的批次号")
    p_undo.set_defaults(fn=cmd_undo)

    p_idx = sub.add_parser("index", help="构建分块索引（FTS5；512/64/4）")
    p_idx.add_argument("root")
    p_idx.add_argument("--rules")
    p_idx.add_argument("--demo", action="store_true")
    p_idx.set_defaults(fn=cmd_index)

    p_search = sub.add_parser("search", help="三路检索 + RRF 融合（k=60）")
    p_search.add_argument("root")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=8)
    p_search.set_defaults(fn=cmd_search)

    p_audit = sub.add_parser("audit", help="零外连自证（import 清单 + 禁网扫描）")
    p_audit.add_argument("--repo", help="仓根（缺省按本文件位置推断）")
    p_audit.set_defaults(fn=cmd_audit)

    p_exp = sub.add_parser("explain", help="这个文件为什么被放在这里")
    p_exp.add_argument("root")
    p_exp.add_argument("path", help="相对路径")
    p_exp.set_defaults(fn=cmd_explain)

    p_val = sub.add_parser("validate", help="规则 DSL 结构校验")
    p_val.add_argument("--rules", required=True, help="规则文件路径（.yml/.json）")
    p_val.set_defaults(fn=cmd_validate)

    p_fb = sub.add_parser("feedback", help="匿名改进反馈开关（on|off|show；默认 L0 关）")
    p_fb.add_argument("action", choices=["on", "off", "show"], help="on=开启 off=关闭 show=查看上行记录")
    p_fb.add_argument("--level", help="on 时选档位（L1/L2/L3；默认 L1）")
    p_fb.set_defaults(fn=cmd_feedback)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
