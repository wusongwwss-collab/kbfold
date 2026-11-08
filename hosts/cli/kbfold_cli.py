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
  feedback  匿名改进反馈（on|off|show|send|clear；默认 L0 关＝零网络调用，口径见 docs/FEEDBACK.md）：
            on=开启（写同意位）｜off=关闭（恢复零外连）｜show=待上行逐条查看｜
            send=发送（先逐条展示最终脱敏形态，确认后才上行）｜clear=清空本地待上行记录

三承诺：不删文件（代码里没有删除动作）/ 不联网（audit 自证：默认路径零外连，唯一网络面＝
反馈上行件 kbfold_feedback_net.py——opt-in、默认关、发送前逐条展示确认；其余路径零 import）/
不猜（置信不足一律进 _inbox/）。
启动：python hosts/cli/kbfold_cli.py <子命令> …
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_CORE_PY = os.path.join(os.path.dirname(os.path.dirname(_HERE)), "core", "py")
if _CORE_PY not in sys.path:
    sys.path.insert(0, _CORE_PY)

from kbfold import feedback as feedback_mod  # noqa: E402
from kbfold import journal, rules as rules_mod  # noqa: E402
from kbfold import service  # noqa: E402

# 唯一网络面（kbfold_feedback_net）**lazy 装载**：仅 `feedback send` 实际发送时 import，
# 其余子命令/默认路径零网络 import（audit 自证口径；文件本身是 audit 唯一豁免件）。
feedback_net = None


def _ensure_net():
    global feedback_net
    if feedback_net is None:
        try:
            import kbfold_feedback_net as _fnet
        except ImportError:                                  # 包形态导入时的同目录回退
            _here = os.path.dirname(os.path.abspath(__file__))
            if _here not in sys.path:
                sys.path.insert(0, _here)
            import kbfold_feedback_net as _fnet
        feedback_net = _fnet
    return feedback_net


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
    for s in res.get("sanctioned_network") or []:
        print("  反馈上行面（opt-in，默认关；audit 唯一豁免）：%s（%s）"
              % (s["file"], ", ".join(s["modules"])))
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


# ---------------------------------------------------------------------------
# feedback 子命令组（on/off/show/send/clear）——YQ-1154 D2（2026-11-05）
#
# 口径正本：docs/FEEDBACK.md §4-§5（三档/开关/脱敏）＋ 开源插件回流规范 v1
# §3.1（五铁律）§4.2（A/B 渠道通路）§5（schema/信封/禁入）§6（终端形态文案）。
# 本组只做：同意位/游标/回执/拒收标记的本地落盘 + 发送编排；唯一网络传输面在
# kbfold_feedback_net.py（opt-in，lazy import；audit 唯一豁免件）。
# ---------------------------------------------------------------------------

_LEVEL_HINT = {
    "L1": "被拒绝的归档建议三态点选（采纳/否决/改判＋信号级别/置信区间；不含路径与文件名）",
    "L2": "＋模板命中率计数、规则 delta（字段白名单）、目录结构 hash（只 hash 不报原始名）",
    "L3": "＋本地脱敏失败样例（槽位化＋残留检测通过才上行）",
}

# on 确认文案（照规范 §5.1 文案逐字；「您的评价」等引号语＝对外一句话解释）
_ON_COPY = (
    "  本档上行内容（L1）：您点选的评价（有用/无用/误触发；采纳/否决/改判）——「您的评价」",
    "  升档追加（L2）：规则/参数调整方向（改前值→改后值）、匿名功能使用计数、目录结构指纹（仅哈希）——「大家用出来的改法方向」",
    "  升档追加（L3）：出错时的脱敏故障样例（本机槽位化＋残留检测通过后才可发）——「出错时的匿名故障单」",
    "  永不上行（任何档）：会议录音与转写文本、知识库文件内容与文件名、目录路径原文、账号/邮箱/设备号、IP——「反馈无法也无意关联到个人」",
)

# 本地发送状态文件（游标/拒收标记；与 SDK 的 state.json、feedback.jsonl 同目录独立存放）
_CURSOR_FILE = "cursor.json"
_REJECTED_FILE = "rejected.jsonl"
_RECEIPTS_DIR = "sentreceipts"

# 5xx/网络异常指数退避：重试 ≤3 次，间隔 base*1,2,4 秒（测试可调小 _BACKOFF_BASE）
_BACKOFF_BASE = 1.0
_SEND_RETRIES = 3
# 一次性终止的 4xx（端点/签名/请求构造类配置错误）：不烧本地记录，交用户查配置
_FATAL_HTTP = frozenset({400, 401, 403, 404, 413})

DELETION_URL = "https://www.easurwork.cn/open-source/feedback"


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _stdin_isatty():
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except Exception:                                        # noqa: BLE001
        return False


def _product_version():
    """product_version＝仓 version（package.json；≤16 字符），读不到退 SDK_VERSION。"""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        with open(os.path.join(root, "package.json"), encoding="utf-8") as f:
            v = str(json.load(f).get("version") or "").strip()
        if v:
            return v[:16]
    except (OSError, ValueError):
        pass
    return feedback_mod.SDK_VERSION


def _load_cursor(base):
    """游标：feedback.jsonl 内已处理到的字节偏移（append-only，游标只前进）。"""
    cur = {"sent_offset": 0, "sent_count": 0, "updated_ts": ""}
    path = os.path.join(base, _CURSOR_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            cur["sent_offset"] = int(data.get("sent_offset") or 0)
            cur["sent_count"] = int(data.get("sent_count") or 0)
            cur["updated_ts"] = str(data.get("updated_ts") or "")
    except (OSError, ValueError):
        pass
    # 文件比游标还小（本地记录被清空/重置）-> 游标归零，防永久卡死
    try:
        if cur["sent_offset"] > os.path.getsize(os.path.join(base, feedback_mod.FEEDBACK_LOG_FILE)):
            cur["sent_offset"] = 0
    except OSError:
        cur["sent_offset"] = 0
    return cur


def _save_cursor(base, cur):
    cur["updated_ts"] = _now_iso()
    os.makedirs(base, exist_ok=True)
    with open(os.path.join(base, _CURSOR_FILE), "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)


def _read_feedback_records(base):
    """读全部反馈记录：[{line_no, start, end, rec}]（行号 1 起；append-only 故行号稳定）。"""
    path = os.path.join(base, feedback_mod.FEEDBACK_LOG_FILE)
    items = []
    try:
        with open(path, encoding="utf-8") as f:
            offset = 0
            line_no = 0
            for raw in f:
                line_no += 1
                start, end = offset, offset + len(raw.encode("utf-8"))
                offset = end
                text = raw.strip()
                if not text:
                    continue
                try:
                    rec = json.loads(text)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    items.append({"line_no": line_no, "start": start, "end": end, "rec": rec})
    except OSError:
        return []
    return items


def _pending_items(base):
    cur = _load_cursor(base)
    return [it for it in _read_feedback_records(base) if it["end"] > cur["sent_offset"]]


def _advance_cursor(base, item, as_sent=False):
    cur = _load_cursor(base)
    cur["sent_offset"] = max(cur["sent_offset"], item["end"])
    if as_sent:
        cur["sent_count"] = int(cur.get("sent_count") or 0) + 1
    _save_cursor(base, cur)


def _mark_rejected(base, item, reason):
    """单条永久跳过：rejected.jsonl 追加留痕 + 游标越过该条（不再重发）。"""
    os.makedirs(base, exist_ok=True)
    entry = {"line_no": item["line_no"], "level": item["rec"].get("level", ""),
             "reason": str(reason)[:200], "ts": _now_iso()}
    with open(os.path.join(base, _REJECTED_FILE), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    _advance_cursor(base, item)


def _load_rejected(base):
    path = os.path.join(base, _REJECTED_FILE)
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    out.append(row)
    except OSError:
        return []
    return out


def _fb_summary(rec):
    """show 内容摘要（照规范 §6.2 形态：级别/摘要/时间）。"""
    payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
    level = rec.get("level", "")
    parts = []
    if level == "L1":
        event = payload.get("event") if isinstance(payload.get("event"), dict) else payload
        result = event.get("result") or ""
        sig = event.get("signal_level") or ""
        conf = event.get("confidence") or ""
        head = "评价：" + (result or "（未点选）")
        extra = " ".join(x for x in (sig, conf) if x)
        parts.append(head + (("（%s）" % extra) if extra else ""))
    else:
        if level != "L3" or payload.get("event"):
            event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
            if event.get("result"):
                parts.append("评价：" + str(event["result"]))
        hits = payload.get("template_hits")
        if isinstance(hits, dict) and hits:
            parts.append("计数 %d 项" % len(hits))
        delta = payload.get("rule_delta")
        if isinstance(delta, dict) and delta:
            parts.append("规则delta(%s)" % delta.get("field", "?"))
        if payload.get("structure_hash"):
            parts.append("结构hash " + str(payload["structure_hash"])[:8])
        samples = payload.get("samples")
        if isinstance(samples, list) and samples:
            parts.append("失败样例 %d 条（已脱敏）" % len(samples))
    text = "·".join(parts) if parts else json.dumps(payload, ensure_ascii=False)
    if len(text) > 60:
        text = text[:57] + "..."
    return text


def _reason_from_body(body):
    """从端点错误体提 reason 码（{"ok":false,"msg":码,"errors":[{"index","reason"}]}）。"""
    if isinstance(body, dict):
        errors = body.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            return str(errors[0].get("reason") or "")
        if body.get("msg"):
            return str(body["msg"])
    return ""


def _wire_payload(level, upload):
    """SDK upload 对象 -> 端点载荷（服务端键白名单：L1 只认 event；L2/L3 禁含 event）。"""
    if level == "L1":
        return {"event": upload.get("event") or {}}
    keys = ("rule_delta", "template_hits", "structure_hash", "samples")
    return {k: upload[k] for k in keys if k in upload}


def _build_envelope(level, upload, nonce):
    """规范 §5.2 信封八字段（schema_version 为字符串 "1"；nonce=幂等键 16-32 hex）。"""
    return {
        "schema_version": "1",
        "product": "kbfold",
        "product_version": _product_version(),
        "channel": "github_npm",
        "feedback_level": level,
        "nonce": nonce,
        "ts": _now_iso(),
        "payload": _wire_payload(level, upload),
    }


def _write_receipt(base, code, envelope, resp_body):
    """回执码本地留存（30 天内申请删除的凭据；§6.3）。"""
    d = os.path.join(base, _RECEIPTS_DIR)
    os.makedirs(d, exist_ok=True)
    content = {
        "receipt_code": code,
        "product": envelope["product"],
        "nonce": envelope["nonce"],
        "ts": envelope["ts"],
        "feedback_level": envelope["feedback_level"],
        "response": resp_body,
        "sent_at": _now_iso(),
        "deletion_url": DELETION_URL,
    }
    path = os.path.join(d, "%s.txt" % code)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(content, f, ensure_ascii=False, indent=2)
    return path


def _send_one(url, envelope):
    """单信封发送（含 5xx/网络指数退避 ≤3 次）。返回 ("ok", body) / ("reject", reason)
    / ("abort", msg)。422＝单条永久拒收；429/5xx/网络异常＝可重试；其余 4xx＝终止。"""
    net = _ensure_net()
    last = "未知错误"
    for attempt in range(_SEND_RETRIES + 1):
        if attempt:
            time.sleep(_BACKOFF_BASE * (2 ** (attempt - 1)))
        r = net.post_events([envelope], timeout=10.0, endpoint=url)  # T13 #8：用宿主解析好的端点
        st = r.get("status")
        if st is not None and 200 <= st < 300:
            return "ok", r.get("body")
        if st == 422:
            return "reject", _reason_from_body(r.get("body")) or "http:422"
        if st is None:
            last = "网络异常（%s）" % r.get("error")
        elif st == 429 or st >= 500:
            reason = _reason_from_body(r.get("body"))
            last = "HTTP %s%s" % (st, ("（%s）" % reason) if reason else "")
        else:
            return ("abort", "HTTP %s（%s）——端点/签名配置类错误，未标记拒收、未推进游标"
                    % (st, _reason_from_body(r.get("body")) or "请求被拒"))
    return "abort", "%s：重试 %d 次后放弃" % (last, _SEND_RETRIES)


def _fb_on(args, base, state):
    level = (getattr(args, "level", None) or getattr(args, "level_opt", None) or "L1")
    level = str(level).upper()
    if level not in ("L1", "L2", "L3"):
        print("✗ 未知档位：%s（可选 L1/L2/L3，默认 L1）" % args.level, file=sys.stderr)
        return 2
    state["consent_asked"] = True
    state["level"] = level
    state["ts"] = _now_iso()
    state.pop("off_ts", None)
    feedback_mod.save_state(state, base)
    print("✓ 匿名改进反馈已开启：档位 %s（同意位已写入：%s）"
          % (level, os.path.join(base, feedback_mod.FEEDBACK_STATE_FILE)))
    for line in _ON_COPY:
        print(line)
    print("  待上行查看：kbfold feedback show；发送：kbfold feedback send"
          "（发送前逐条展示最终脱敏形态，确认后才上行）；随时关闭：kbfold feedback off")
    return 0


def _fb_off(args, base, state):
    state["consent_asked"] = True
    state["level"] = "L0"
    state["off_ts"] = _now_iso()
    feedback_mod.save_state(state, base)
    print("✓ 反馈已关闭：恢复零外连（L0）；关闭标记已写入（90 天静默）。已上行的匿名数据可申请删除。")
    print("  本机历史记录仍可查：kbfold feedback show；清空本地待上行记录：kbfold feedback clear")
    return 0


def _fb_show(args, base, state):
    sdk_level = state.get("level", "L0")
    asked = bool(state.get("consent_asked"))
    cur = _load_cursor(base)
    log_path = os.path.join(base, feedback_mod.FEEDBACK_LOG_FILE)
    pending = _pending_items(base)
    print("当前状态：档位=%s（consent_asked=%s）" % (sdk_level, asked))
    print("待上行 %d 条 · 存于本机 %s · 尚未发送" % (len(pending), log_path))
    if pending:
        print(" #   级别   内容摘要                                    时间")
        for i, it in enumerate(pending, 1):
            rec = it["rec"]
            ts = str(rec.get("ts", ""))
            short = ts[5:16] if len(ts) >= 16 else ts       # MM-DD HH:MM（§6.2 形态）
            print(" %-3d %-6s %-44s %s" % (i, rec.get("level", "?"), _fb_summary(rec), short))
    else:
        print("（无待上行记录）")
    rejected = _load_rejected(base)
    if rejected:
        print("本地拒收 %d 条（永久跳过，不再重发）：" % len(rejected))
        for row in rejected:
            print("  ✗ #%s [%s] %s（%s）" % (row.get("line_no"), row.get("level"),
                                             row.get("reason"), row.get("ts", "")))
    if int(cur.get("sent_count") or 0) > 0:
        print("已发送 %d 条（回执码存本机 %s；30 天内可凭码申请删除）"
              % (cur["sent_count"], os.path.join(base, _RECEIPTS_DIR)))
    print(_FB_PENDING_FOOTER)
    return 0


def _fb_send(args, base, state):
    level = state.get("level", "L0")
    if not state.get("consent_asked") or level not in ("L1", "L2", "L3"):
        print("✗ 反馈未开启：先执行 kbfold feedback on [L1|L2|L3]（发送前提＝明确同意）", file=sys.stderr)
        return 3
    pending = _pending_items(base)
    if not pending:
        print("（无待上行记录）")
        return 0
    if not args.yes and not _stdin_isatty():
        print("✗ 非交互环境（stdin 非 TTY）须显式 --yes 才能发送"
              "（无确认即上行不合规；交互终端可直接 kbfold feedback send）", file=sys.stderr)
        return 4

    sdk = feedback_mod.FeedbackSdk(level=level, feedback_dir=base,
                                   package_version=_product_version())
    send_list = []
    for it in pending:
        rec = it["rec"]
        rec_level = rec.get("level")
        if rec_level not in ("L1", "L2", "L3"):
            it["reason"] = "schema:level"
        else:
            r = sdk.prepare_upload(rec_level, rec.get("payload") or {})
            if r.get("ok"):
                up = r["upload"]
                # 服务端契约预检（与端点 422 同判据，省一次必败往返）：
                # L1 event 不得为空（schema:event）；L2+ 载荷不得为空（schema:payload）。
                if rec_level == "L1" and not up.get("event"):
                    it["reason"] = "schema:event"
                elif not _wire_payload(rec_level, up):
                    it["reason"] = "schema:payload"
                else:
                    it["upload"] = up
                    send_list.append(it)
            else:
                it["reason"] = r.get("reason", "prepare_rejected")
        if "reason" in it:
            _mark_rejected(base, it, it["reason"])

    print("将上行的最终脱敏形态（逐条；原文已在本机槽位化，不含任何路径/文件名/身份信息）：")
    for it in send_list:
        print("  #%d [%s]" % (it["line_no"], it["rec"].get("level")))
        for line in json.dumps(it["upload"], ensure_ascii=False, indent=2, sort_keys=True).splitlines():
            print("    " + line)
    for it in pending:
        if "reason" in it:
            print("  ✗ #%d 本地预检拒收（%s）——已永久跳过" % (it["line_no"], it["reason"]))
    if not send_list:
        print("（无可上行记录）")
        return 0

    if not args.yes:
        print("即将上行 %d 条匿名反馈（签名传输，不含任何账号信息）" % len(send_list))
        try:
            ans = input("确认发送？[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans != "y":
            print("已取消（未发送；非交互环境请加 --yes）")
            return 0

    net = _ensure_net()
    if not net.hmac_secret():
        print("⚠ %s" % net.signature_header(b"")[1], file=sys.stderr)
    url = net.endpoint_url(base)   # T13 #8：env > 配置文件(.kbfold/endpoint.json) > 缺省
    sent, rejected_rows = [], []
    abort_msg = None
    for it in send_list:
        nonce = secrets.token_hex(16)
        envelope = _build_envelope(it["rec"]["level"], it["upload"], nonce)
        kind, info = _send_one(url, envelope)
        if kind == "ok":
            body = info if isinstance(info, dict) else {}
            code = str(body.get("receipt") or nonce[:6])
            _write_receipt(base, code, envelope, body)
            _advance_cursor(base, it, as_sent=True)
            sent.append((it, code))
        elif kind == "reject":
            _mark_rejected(base, it, info)
            rejected_rows.append((it, info))
        else:
            abort_msg = str(info)
            break

    remaining = len(send_list) - len(sent) - len(rejected_rows)
    print("发送完成：已上行 %d 条；拒收 %d 条；未发送 %d 条" % (len(sent), len(rejected_rows), max(remaining, 0)))
    for it, code in sent:
        print("  ✓ #%d 已上行，回执码 %s" % (it["line_no"], code))
    for it, reason in rejected_rows:
        print("  ✗ #%d 服务端拒收（%s）——已永久跳过" % (it["line_no"], reason))
    if abort_msg:
        print("✗ 发送中止：%s（游标未推进，待上行记录保留，可稍后重试）" % abort_msg, file=sys.stderr)
    if sent:
        print("如需删除，请 30 天内凭上述回执码到 %s 申请删除。" % DELETION_URL)
    return 1 if abort_msg else 0


def _fb_clear(args, base, state):
    """本地清空（不需联网）：清待上行记录 + 游标 + 拒收标记；保留同意位与已发回执。"""
    cur = _load_cursor(base)
    n_pending = len(_pending_items(base))
    log_path = os.path.join(base, feedback_mod.FEEDBACK_LOG_FILE)
    cleared_total = 0
    if os.path.isfile(log_path):
        with open(log_path, encoding="utf-8") as f:
            cleared_total = sum(1 for line in f if line.strip())
        open(log_path, "w", encoding="utf-8").close()          # 截断清空（不删文件）
    cur["sent_offset"] = 0
    _save_cursor(base, cur)
    rej_path = os.path.join(base, _REJECTED_FILE)
    n_rejected = len(_load_rejected(base))
    if os.path.isfile(rej_path):
        open(rej_path, "w", encoding="utf-8").close()
    print("✓ 本地待上行记录已清空（本次清掉 %d 条，其中待上行 %d 条；文件已截断、未删除）"
          % (cleared_total, n_pending))
    if n_rejected:
        print("  拒收标记已重置（%d 条）" % n_rejected)
    print("  保留：同意开关状态（state.json）与已发送回执（%s/，30 天内申请删除仍需凭码）；此操作不联网。"
          % _RECEIPTS_DIR)
    return 0


_FB_PENDING_FOOTER = "[feedback send 上行]   [feedback clear 清空本地]"


def cmd_feedback(args):
    """匿名改进反馈命令组（docs/FEEDBACK.md §4；规范 §4.2 通路定案）。

    on/off/show/clear 全程零外连（本地落盘）；send 为唯一联网动作（opt-in，
    先逐条展示最终脱敏形态再确认，非交互环境须显式 --yes）。"""
    base = feedback_mod.default_feedback_dir()
    state = feedback_mod.load_state(base)
    action = args.action
    if action == "on":
        return _fb_on(args, base, state)
    if action == "off":
        return _fb_off(args, base, state)
    if action == "show":
        return _fb_show(args, base, state)
    if action == "send":
        return _fb_send(args, base, state)
    if action == "clear":
        return _fb_clear(args, base, state)
    print("✗ 未知 feedback 动作：%s" % action, file=sys.stderr)
    return 2


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

    p_fb = sub.add_parser("feedback", help="匿名改进反馈（on|off|show|send|clear；默认 L0 关）")
    p_fb.add_argument("action", choices=["on", "off", "show", "send", "clear"],
                      help="on=开启 off=关闭 show=查看待上行 send=发送（先展示再确认） clear=清空本地")
    p_fb.add_argument("level", nargs="?", default=None,
                      help="on 时选档位（L1|L2|L3；默认 L1）")
    p_fb.add_argument("--level", dest="level_opt", default=None, help=argparse.SUPPRESS)
    p_fb.add_argument("--yes", action="store_true",
                      help="send：跳过 y/N 确认（stdin 非 TTY 时必带，否则退出码 4）")
    p_fb.set_defaults(fn=cmd_feedback)
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
