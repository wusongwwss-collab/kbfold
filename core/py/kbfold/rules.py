# -*- coding: utf-8 -*-
"""kbfold.rules — 规则 DSL v1（kbfold.yml）解析、校验与判定。

口径正本＝公开规范「规则 DSL」定稿（字段表、三个特殊去向、冲突裁决四条、通用名黑名单），
本件为该冻结口径的实现载体（规则 DSL 是公开规范，非代码搬运）。

字段集（v1 冻结）：version/root/basis/kbs[].name/id/template/
  kbs[].detect.markers/detect.keywords/kbs[].paths[].match/paths[].to；
特殊去向：@exclude（仍登记指纹）与 @inbox；不认识的 version 报错退出，不做猜测性兼容。

冲突裁决四条（同一定稿口径，逐条可复核）：
  1. 多条路径规则命中 ⇒ 最具体的胜：路径段更多者胜；同样多则书写顺序后者覆盖前者；
  2. 具体程度打平（目标不同）⇒ 不猜，进 _inbox/ 并附候选；
  3. S1 显式声明（标记文件）与路径规则冲突 ⇒ S1 胜；
  4. 一条都没命中 ⇒ 落 basis 通用基底；仍无解 ⇒ _inbox/。

黑名单：目标目录名命中 GENERIC_NAMES（与 dir_classify.py 同一份，不另抄一份）⇒
启动时直接报错（RulesError），错误文案与公开规范样例逐字一致。
"""
from __future__ import annotations

import hashlib
import re

from .dir_classify import GENERIC_NAMES

DSL_VERSION = 1
SPECIAL_TARGETS = ("@exclude", "@inbox")
#: 置信三态分流（公开规范冻结口径）：≥0.90 直落；0.70-0.89 建议；<0.70 或平手 进 _inbox/
DIRECT_CONF_MIN = 0.90
SUGGEST_CONF_MIN = 0.70

#: 判定梯置信取值（公开规范 L0-L6 冻结分值，禁调参改名）
CONF_MARKER = 0.95      # L0 显式标记
CONF_NAME_EXACT = 0.92  # L1a 名称全等
CONF_KEYWORD = 0.80     # L3 词表命中


class RulesError(Exception):
    """规则 DSL 结构/黑名单错误（启动即报错退出，不做猜测性兼容）。"""


# ---------------------------------------------------------------------------
# 极小 YAML 子集解析器（零第三方依赖；覆盖 DSL 字段面：嵌套映射 / 列表 / 行内列表 / 注释）
# ---------------------------------------------------------------------------

def _parse_scalar(raw):
    s = raw.strip()
    if not s:
        return None
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(x) for x in _split_inline(inner)]
    low = s.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~"):
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def _split_inline(inner):
    """行内列表切分（感知引号）。"""
    out = []
    buf = []
    quote = None
    for ch in inner:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            continue
        if ch == ",":
            out.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if "".join(buf).strip():
        out.append("".join(buf))
    return [x.strip() for x in out if x.strip()]


def _parse_yaml_block(lines, idx, indent):
    """递归下降解析一个缩进块；返回 (obj, next_idx)。仅覆盖 DSL 需要的子集。"""
    result = None
    while idx < len(lines):
        line = lines[idx]
        if not line.strip() or line.strip().startswith("#"):
            idx += 1
            continue
        cur_indent = len(line) - len(line.lstrip(" "))
        if cur_indent < indent:
            break
        if cur_indent > indent:
            raise RulesError("规则文件缩进错误（第 %d 行）" % (idx + 1))
        stripped = line.strip()
        if stripped.startswith("- "):
            if result is None:
                result = []
            if not isinstance(result, list):
                raise RulesError("规则文件结构错误：列表与映射混排（第 %d 行）" % (idx + 1))
            item_text = stripped[2:].strip()
            if not item_text:
                sub, idx = _parse_yaml_block(lines, idx + 1, indent + 2)
                result.append(sub)
                continue
            m = re.match(r"^([^:]+):\s*(.*)$", item_text)
            if m and not item_text.startswith(("[", "{", '"', "'")):
                # 映射型列表项：- key: value（后续更深缩进行并入该映射）
                item = {m.group(1).strip(): _parse_scalar(m.group(2))}
                sub, idx = _parse_yaml_block(lines, idx + 1, indent + 2)
                if isinstance(sub, dict):
                    item.update(sub)
                result.append(item)
                continue
            result.append(_parse_scalar(item_text))
            idx += 1
            continue
        m = re.match(r"^([^:]+):\s*(.*)$", stripped)
        if not m:
            raise RulesError("规则文件结构错误：无法识别的行（第 %d 行）" % (idx + 1))
        if result is None:
            result = {}
        if not isinstance(result, dict):
            raise RulesError("规则文件结构错误：映射与列表混排（第 %d 行）" % (idx + 1))
        key = m.group(1).strip()
        value_text = m.group(2).strip()
        if value_text:
            result[key] = _parse_scalar(value_text)
            idx += 1
        else:
            # 行内注释或空值 ⇒ 看下一行缩进决定嵌套
            sub, idx = _parse_yaml_block(lines, idx + 1, indent + 2)
            if sub is None:
                # 嵌套块为空且下一行是同级键 ⇒ 空映射
                sub = {}
            result[key] = sub
    return result, idx


def parse_rules_text(text):
    """解析规则文件文本（YAML 子集）⇒ dict；解析失败抛 RulesError。"""
    lines = text.replace("\r\n", "\n").replace("\t", "  ").split("\n")
    obj, _ = _parse_yaml_block(lines, 0, 0)
    if obj is None:
        raise RulesError("规则文件为空")
    if not isinstance(obj, dict):
        raise RulesError("规则文件顶层必须是映射")
    return obj


# ---------------------------------------------------------------------------
# 校验（启动即报错口径）
# ---------------------------------------------------------------------------

def validate_rules(rules):
    """结构校验；返回错误列表（空 = 通过）。不认识版本/黑名单命中等在此逐条报。"""
    errors = []
    if not isinstance(rules, dict):
        return ["规则顶层必须是映射"]
    version = rules.get("version")
    if version != DSL_VERSION:
        errors.append("不认识的规则版本：%r（当前只支持 version: %d，不做猜测性兼容）" % (version, DSL_VERSION))
    if not str(rules.get("root") or "").strip():
        errors.append("缺少 root（扫描根目录）")
    if not str(rules.get("basis") or "").strip():
        errors.append("缺少 basis（L0 通用基底，任何项目都没认出来时用它兜底，不可删除）")
    kbs = rules.get("kbs")
    if not isinstance(kbs, list) or not kbs:
        errors.append("缺少 kbs（至少要有一个知识库定义）")
        kbs = []
    for i, kb in enumerate(kbs):
        if not isinstance(kb, dict):
            errors.append("kbs[%d] 必须是映射" % i)
            continue
        if not str(kb.get("name") or "").strip():
            errors.append("kbs[%d].name 缺失（人类可读名字）" % i)
        if not str(kb.get("id") or "").strip():
            errors.append("kbs[%d].id 缺失（短编号，用于投影目录前缀）" % i)
        paths = kb.get("paths")
        if paths is not None:
            if not isinstance(paths, list):
                errors.append("kbs[%d].paths 必须是列表" % i)
                continue
            for j, pr in enumerate(paths):
                if not isinstance(pr, dict):
                    errors.append("kbs[%d].paths[%d] 必须是映射" % (i, j))
                    continue
                if not str(pr.get("match") or "").strip():
                    errors.append("kbs[%d].paths[%d].match 缺失（glob 写法）" % (i, j))
                to = pr.get("to")
                if not str(to or "").strip():
                    errors.append("kbs[%d].paths[%d].to 缺失（目标目录）" % (i, j))
                    continue
                to_s = str(to).strip()
                if to_s in SPECIAL_TARGETS:
                    continue
                # 黑名单启动报错（公开规范样例文案，逐字一致）
                for seg in re.split(r"[\\/]+", to_s):
                    seg_norm = seg.strip().strip("/\\").lower()
                    if seg_norm in GENERIC_NAMES:
                        errors.append(
                            "规则错误：目标目录 \"%s\" 属于通用名黑名单（GENERIC_NAMES，共 40+ 项）\n"
                            "  这类目录名不携带任何场景信息，放进规则等于把判定权交给猜测。\n"
                            "  换成场景名，例如 \"01-原始资料/data\"。" % to_s)
                        break
    return errors


def rules_locked_hash(rules_text):
    """rules.lock 的 sha256 指纹（与 manifest 的 rules_locked 字段互相印证）。"""
    return "sha256:" + hashlib.sha256(rules_text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# glob 匹配（DSL 的 match 写法，如 **/合同/**）
# ---------------------------------------------------------------------------

def glob_to_regex(pattern):
    """DSL glob → 正则：`**/` 匹配零或多级目录段（可整体缺席）、`**` 跨段任意、
    `*` 单段内任意、`?` 单字符；其余字面。"""
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:[^/]+/)*")
            i += 3
            continue
        c = pattern[i]
        if c == "*":
            if i + 1 < len(pattern) and pattern[i + 1] == "*":
                out.append(".*")
                i += 2
                continue
            out.append("[^/\\\\]*")
            i += 1
            continue
        if c == "?":
            out.append("[^/\\\\]")
            i += 1
            continue
        out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$", re.IGNORECASE)


def _norm_relpath(p):
    return str(p or "").replace("\\", "/").strip("/")


def path_specificity(pattern):
    """路径段数（「最具体的胜」判据：路径段更多者胜）。"""
    return len([s for s in _norm_relpath(pattern).split("/") if s != ""])


def match_path_rule(pattern, relpath):
    """路径规则命中判定（glob 对相对路径；分隔符统一 /）。"""
    return glob_to_regex(pattern).match(_norm_relpath(relpath)) is not None


# ---------------------------------------------------------------------------
# 判定（冲突裁决四条）
# ---------------------------------------------------------------------------

def _kb_path_rules(kb):
    paths = kb.get("paths")
    return [p for p in (paths if isinstance(paths, list) else []) if isinstance(p, dict)]


def _best_target_in_kb(kb, relpath):
    """库内最具体路径规则（同样多则书写顺序后者覆盖前者）；返回 (target, rule) 或 None。"""
    hits = []
    for order, pr in enumerate(_kb_path_rules(kb)):
        pat = str(pr.get("match") or "")
        if pat and match_path_rule(pat, relpath):
            hits.append({"target": str(pr.get("to") or "").strip(),
                         "spec": path_specificity(pat), "order": order})
    if not hits:
        return None
    max_spec = max(h["spec"] for h in hits)
    finalists = [h for h in hits if h["spec"] == max_spec]
    return finalists[-1]  # 书写顺序后者覆盖前者


def judge_file(rules, relpath, marker_kb=None, name_hits=None, keyword_hits=None):
    """单文件判定（冲突裁决四条；纯函数）。

    marker_kb：S1 显式标记命中的库 id（无则 None）；
    name_hits：库名与路径分量全等命中的库 id 列表；
    keyword_hits：{kb_id: [命中关键词]}（L3 词表）。
    返回 {kb, target, confidence, reason, via, candidates}；
    target ∈ 目录名 | '@exclude' | '@inbox'；confidence 为 None 表示「路径规则（人已表态）」。
    """
    kbs = [k for k in (rules.get("kbs") if isinstance(rules.get("kbs"), list) else [])
           if isinstance(k, dict)]
    by_id = {str(k.get("id") or ""): k for k in kbs if k.get("id")}
    rel = _norm_relpath(relpath)
    segments = [s for s in rel.split("/") if s]

    # ③ S1 显式声明与路径规则冲突 ⇒ S1 胜
    if marker_kb and marker_kb in by_id:
        kb = by_id[marker_kb]
        best = _best_target_in_kb(kb, rel)
        if best is None:
            # 无路径规则 ⇒ 落库根（以库 id 为前缀的顶层目录）
            target = str(kb.get("id") or "")
        else:
            target = best["target"]
        return {"kb": marker_kb, "target": target, "confidence": CONF_MARKER,
                "reason": "L0:显式标记", "via": "s1", "candidates": []}

    # ① 多条路径规则命中 ⇒ 最具体的胜；② 具体程度打平且目标不同 ⇒ 不猜
    all_hits = []
    for kb in kbs:
        best = _best_target_in_kb(kb, rel)
        if best is not None:
            all_hits.append({"kb": str(kb.get("id") or ""), "target": best["target"],
                             "spec": best["spec"], "order": best["order"]})
    if all_hits:
        max_spec = max(h["spec"] for h in all_hits)
        finalists = [h for h in all_hits if h["spec"] == max_spec]
        targets = {h["target"] for h in finalists}
        if len(targets) == 1:
            w = finalists[-1]
            return {"kb": w["kb"], "target": w["target"], "confidence": None,
                    "reason": "路径规则（最具体命中）", "via": "path-rule", "candidates": []}
        # 平手不猜：进 _inbox/ 并附候选
        return {"kb": None, "target": "@inbox", "confidence": None,
                "reason": "路径规则平手（不猜）", "via": "tie",
                "candidates": [{"kb": h["kb"], "target": h["target"]} for h in finalists[:2]]}

    # L1a 名称全等：知识库名与目录名完全一致
    name_hits = [h for h in (name_hits or []) if h in by_id]
    if len(set(name_hits)) == 1:
        kb = by_id[name_hits[0]]
        best = _best_target_in_kb(kb, rel)
        target = best["target"] if best else str(kb.get("id") or "")
        return {"kb": name_hits[0], "target": target, "confidence": CONF_NAME_EXACT,
                "reason": "L1a:知识库名称完全一致", "via": "name-exact", "candidates": []}

    # L3 词表命中（唯一最高分才判，并列 ⇒ 不猜）
    keyword_hits = {k: v for k, v in (keyword_hits or {}).items() if k in by_id and v}
    if keyword_hits:
        top = max(len(v) for v in keyword_hits.values())
        leaders = sorted(k for k, v in keyword_hits.items() if len(v) == top)
        if len(leaders) == 1:
            kb = by_id[leaders[0]]
            best = _best_target_in_kb(kb, rel)
            target = best["target"] if best else str(kb.get("id") or "")
            return {"kb": leaders[0], "target": target, "confidence": CONF_KEYWORD,
                    "reason": "L3:词表命中（%s）" % "、".join(keyword_hits[leaders[0]][:5]),
                    "via": "keyword", "candidates": []}

    # ④ 一条都没命中 ⇒ 落 basis 通用基底；仍无解 ⇒ _inbox/
    return {"kb": None, "target": "@inbox", "confidence": None,
            "reason": "无命中，落 basis 通用基底仍无解", "via": "basis", "candidates": []}


def triage(confidence):
    """三态分流：≥0.90 直落 / 0.70-0.89 建议 / <0.70 或 None（平手）待归。"""
    if confidence is None:
        return "inbox"
    if confidence >= DIRECT_CONF_MIN:
        return "direct"
    if confidence >= SUGGEST_CONF_MIN:
        return "suggest"
    return "inbox"


def suggest_sections(total_files):
    """目录规模跟着文件量伸缩（公开规范冻结阈值，工程判断非校准值）。
    返回 (建议段数下限, 建议段数上限)。"""
    if total_files < 50:
        return (1, 3)
    if total_files <= 500:
        return (4, 6)
    return (8, 8)
