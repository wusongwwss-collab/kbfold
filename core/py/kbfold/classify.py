# -*- coding: utf-8 -*-
"""kbfold.classify — 四层信号（S1-S4）采集与本地加权判分（纯函数、零 IO、零依赖）。

移植自冻结底本 ``kb-tpl-match.js``（JS→Py 移植件；搬运正本＝需求包 _assets 快照，
移植只翻语义、不改任何判定口径）。逐条出处对照：

  - 权重兜底 DEFAULT_WEIGHTS（structure 0.6 / name 0.3 / prior 0.1）与阈值兜底
    DEFAULT_THRESHOLDS（high 0.62 / low 0.34）：底本同值冻结，禁调参改名后当成新设计。
  - 平手带 TIE_GAP = 0.05；名称信号饱和长度 NAME_FULL_LEN = 8；建议条数 SUGGEST_MAX = 2。
  - 四层信号（判分顺序 S1 > S2+S3+S4 加权）：
      S1 显式声明（最强信号，命中即高置信）
      S2 名称：库名/根路径名/一级夹名 × 模板 keywords（命中长度和 / 8 记满）
      S3 结构：一级夹名集合 vs 模板段名集合（编辑距离归一最优相似度均值）
      S4 内容：扩展名分布 × 模板 ext_signals（与 S3 取高者，不双计）
  - 不猜原则：平手或低于阈值 ⇒ confidence='low'、best=None，附灰字建议列表；
    模板缺失不伪造行业。

全部输入为结构元数据（夹名/扩展名计数等），不含文件内容；同一输入两次判分完全一致。
"""
from __future__ import annotations

import json
import math
import re

from ._compat import js_number, js_round, num_or, ordered_set

# ── 内置兜底参数（与底本 kb-tpl-match.js 同值冻结；权重/阈值均可被调用方 rules 覆盖） ──
DEFAULT_WEIGHTS = {"structure": 0.6, "name": 0.3, "prior": 0.1}
DEFAULT_THRESHOLDS = {"high": 0.62, "low": 0.34}
#: 平手带：top1 与 top2 之差小于该值 ⇒ 视为「平手」（不猜，除非 top1 已过高阈值）
TIE_GAP = 0.05
#: 名称信号饱和长度：命中关键词的长度和达到该值 ⇒ name 分记满（长词命中优先，见 name_score_of）
NAME_FULL_LEN = 8
#: 建议列表最多给几条（低置信灰字建议列表）
SUGGEST_MAX = 2

_STRIP_NO_RE = re.compile(r"^[0-9]+[._\-、\s]*")
_WORDS_SPLIT_RE = re.compile(r"[与和及或/、·,，;；:：\-\s（）()【】\[\]]+")
_TRAIL_SLASH_RE = re.compile(r"[\\/]+$")
_PATH_SPLIT_RE = re.compile(r"[\\/]")


def _num(v, dflt):
    """底本 `_num` 同构：非法/缺失 ⇒ 兜底。
    （Python dict 缺键经 .get 得 None ≙ 底本 undefined ⇒ Number(NaN) ⇒ 兜底；
    显式 0/合法值照常取用。）"""
    if v is None:
        return dflt
    return num_or(v, dflt)


def norm_weights(rules):
    """权重归一（纯函数）：读 rules.weights.{structure_signal,name_signal,template_prior}，
    亦可直接给 {structure,name,prior}；任一项缺失/非法 ⇒ 该项取内置兜底；三项和为 0 ⇒ 整体回落兜底。
    返回三项非负且和为 1。"""
    w = None
    if isinstance(rules, dict):
        cand = rules.get("weights")
        if isinstance(cand, dict):
            w = cand
    if w is None:
        w = rules if isinstance(rules, dict) else {}

    def pick(a, b, dflt):
        if a in w:
            return max(0.0, _num(w[a], dflt))
        if b in w:
            return max(0.0, _num(w[b], dflt))
        return dflt

    structure = pick("structure_signal", "structure", DEFAULT_WEIGHTS["structure"])
    name = pick("name_signal", "name", DEFAULT_WEIGHTS["name"])
    prior = pick("template_prior", "prior", DEFAULT_WEIGHTS["prior"])
    total = structure + name + prior
    if not total > 0:
        return dict(DEFAULT_WEIGHTS)
    return {"structure": structure / total, "name": name / total, "prior": prior / total}


def thresholds_of(rules):
    """阈值（纯函数）：rules.mech_thresholds.{high,low} 可调；非法/缺省 ⇒ 内置兜底；
    保证 0 < low < high ≤ 1。"""
    t = {}
    if isinstance(rules, dict):
        cand = rules.get("mech_thresholds")
        if isinstance(cand, dict):
            t = cand
    high = _num(t.get("high"), DEFAULT_THRESHOLDS["high"])
    low = _num(t.get("low"), DEFAULT_THRESHOLDS["low"])
    high = min(1.0, max(0.0, high))
    low = min(1.0, max(0.0, low))
    if not low < high:
        high = DEFAULT_THRESHOLDS["high"]
        low = DEFAULT_THRESHOLDS["low"]
    if low <= 0:
        low = DEFAULT_THRESHOLDS["low"]
    return {"high": high, "low": low}


# ============================== 文本工具（机械、可解释） ==============================

def strip_no(name):
    """去编号前缀：'04-采购与询价' → '采购与询价'。"""
    return _STRIP_NO_RE.sub("", str(name or "")).strip()


def words_of(name):
    """名称按分隔符切词元（中文长串不切，交给相似度）；长度 ≥2 的片段入集。"""
    raw = strip_no(name)
    if not raw:
        return []
    parts = _WORDS_SPLIT_RE.split(raw)
    out = []
    for p in parts:
        if p and len(p) >= 2:
            out.append(p)
    if not out and len(raw) >= 2:
        out.append(raw)
    return out


def vocab_of(names):
    """词元集（小写；含「段名本身」与派生词元）——模板与项目两侧同口径。
    返回保序集合（dict 键序，JS Set 等价形态）。"""
    out = ordered_set()
    src = names if isinstance(names, list) else [names]
    for n in src:
        raw = strip_no(n).lower()
        if raw:
            out[raw] = True
        for w in words_of(n):
            out[w.lower()] = True
    return out


def levenshtein(a, b):
    """编辑距离（滚动数组，零依赖）。"""
    s = str(a or "")
    t = str(b or "")
    if s == t:
        return 0
    if not len(s):
        return len(t)
    if not len(t):
        return len(s)
    prev = list(range(len(t) + 1))
    cur = [0] * (len(t) + 1)
    for i in range(1, len(s) + 1):
        cur[0] = i
        for j in range(1, len(t) + 1):
            cost = 0 if s[i - 1] == t[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev, cur = cur, prev
    return prev[len(t)]


def similarity(a, b):
    """编辑距离归一相似度 ∈ [0,1]（1=完全相同）；含「一方是另一方子串」的保守加成。"""
    s = str(a or "").strip().lower()
    t = str(b or "").strip().lower()
    if not s or not t:
        return 0.0
    if s == t:
        return 1.0
    d = levenshtein(s, t) / max(len(s), len(t))
    base = max(0.0, 1.0 - d)
    if t in s or s in t:
        return max(base, 0.6)  # 子串（去虚词/去编号后的常见形态）
    return base


def jaccard(a, b):
    """Jaccard（词元集）。"""
    A = a if isinstance(a, dict) else ordered_set(a or [])
    B = b if isinstance(b, dict) else ordered_set(b or [])
    na, nb = len(A), len(B)
    if not na or not nb:
        return 0.0
    inter = 0
    for x in A:
        if x in B:
            inter += 1
    return inter / (na + nb - inter)


# ============================== 模板特征（消费面） ==============================

def match_of(tpl):
    """match 归一：字符串 JSON 或已解析对象 → {keywords, path_regex, ext_signals, min_docs}。"""
    m = None
    if isinstance(tpl, dict):
        m = tpl.get("match") or tpl.get("match_json")
    if isinstance(m, str):
        try:
            m = json.loads(m)
        except Exception:
            m = None
    if not isinstance(m, dict):
        m = {}
    kw = []
    if isinstance(m.get("keywords"), list):
        for x in m["keywords"]:
            v = strip_no(x).lower()
            if v:
                kw.append(v)
    pr = []
    if isinstance(m.get("path_regex"), list):
        for x in m["path_regex"]:
            v = str(x or "")
            if v:
                pr.append(v)
    es = m.get("ext_signals") if isinstance(m.get("ext_signals"), dict) else {}
    ext = {}
    for k in es.keys():
        key = str(k).lower()
        if key.startswith("."):
            key = key[1:]
        w = max(0.0, min(1.0, _num(es[k], 0)))
        if key and w > 0:
            ext[key] = w
    has_match = bool(isinstance(tpl, dict) and (tpl.get("match") is not None or tpl.get("match_json") is not None))
    return {
        "keywords": kw,
        "path_regex": pr,
        "ext_signals": ext,
        "min_docs": max(0.0, _num(m.get("min_docs"), 0)),
        "has_match": has_match,
    }


def segments_of(tpl):
    """模板段名集（树的一级＋各级全部段名；去编号前保留原名入集）。"""
    tree = tpl.get("tree") if isinstance(tpl, dict) and isinstance(tpl.get("tree"), list) else []
    l1 = []
    all_segs = []

    def walk(nodes, depth):
        for n in nodes if isinstance(nodes, list) else []:
            nm = str((n or {}).get("name") or "").strip()
            if not nm:
                continue
            all_segs.append(nm)
            if depth == 0:
                l1.append(nm)
            walk((n or {}).get("children"), depth + 1)

    walk(tree, 0)
    return {"l1": l1, "all": all_segs}


def keywords_of(tpl):
    """模板关键词（判分燃料）：契约优先 match.keywords；match 未下发 ⇒ 由段名派生（同底本口径）。"""
    m = match_of(tpl)
    if m["keywords"]:
        return m["keywords"]
    return list(vocab_of(segments_of(tpl)["all"]).keys())


def prior_of(tpl):
    """模板先验（weight=prior）：有 match 且 kind=industry ⇒ 1；view/generic ⇒ 0.5；无 match ⇒ 0.5。"""
    kind = str((tpl or {}).get("kind") or "").strip() if isinstance(tpl, dict) else ""
    if kind == "generic" or kind == "view":
        return 0.5
    if not match_of(tpl)["has_match"]:
        return 0.5  # 过渡期（match 未下发）不给满先验，防与已备燃料的模板抢分
    return 1 if kind == "industry" else 0.6


# ============================== S1-S4 信号采集 ==============================

def explicit_industry_of(ctx):
    """S1 显式声明（最强信号）：优先级 行业设置声明 > 记忆 > 申请页行业 > 纠错口述。
    无任何声明 ⇒ None（不猜）。"""
    c = ctx if isinstance(ctx, dict) else {}
    cands = [
        (c.get("declaredIndustry"), "declared"),
        (c.get("memoryIndustry") or c.get("defaultIndustry"), "memory"),
        (c.get("applyIndustry"), "apply_page"),
        (c.get("oralIndustry"), "oral"),
    ]
    for v, src in cands:
        s = str(v or "").strip()
        if s:
            return {"industry": s, "source": src}
    return None


def collect_signals(ctx):
    """四信号采集（纯函数）：调用方把已采集到的元数据交进来，本函数只做归一。
    ctx 全部为结构元数据（夹名/扩展名计数），不含任何文件内容。"""
    c = ctx if isinstance(ctx, dict) else {}
    top_names = [str(x or "").strip() for x in (c.get("topNames") if isinstance(c.get("topNames"), list) else [])]
    top_names = [x for x in top_names if x]
    dir_paths = [str(x or "") for x in (c.get("dirPaths") if isinstance(c.get("dirPaths"), list) else [])]
    dir_paths = [x for x in dir_paths if x]
    base_names = []
    for d in dir_paths:
        trimmed = _TRAIL_SLASH_RE.sub("", d)
        parts = _PATH_SPLIT_RE.split(trimmed)
        base_names.append(parts[-1] if parts else "")
    name_vocab = vocab_of([c.get("name")] + base_names + top_names)
    ext_dist = {}
    if isinstance(c.get("extDist"), dict):
        for k in c["extDist"].keys():
            key = str(k or "").lower()
            if key.startswith("."):
                key = key[1:]
            n = _num(c["extDist"][k], 0)
            if key and n > 0:
                ext_dist[key] = n
    return {
        "s1": explicit_industry_of(c),
        "s2": {"name": str(c.get("name") or "").strip(), "baseNames": base_names,
               "topNames": top_names, "vocab": name_vocab, "dirPaths": dir_paths},
        "s3": {"topNames": top_names, "vocab": vocab_of(top_names)},
        "s4": {"extDist": ext_dist, "docCount": max(0.0, _num(c.get("docCount"), 0))},
    }


# ============================== 判分（三信号加权） ==============================

def name_score_of(signals, kw_list):
    """S2 名称分：命中关键词的长度和 / NAME_FULL_LEN（长词命中优先）。"""
    if not kw_list:
        return {"score": 0, "hits": []}
    vocab = signals["s2"]["vocab"]
    length = 0
    hits = []
    for w in kw_list:
        if not w:
            continue
        hit = w in vocab
        if not hit and len(w) >= 2:
            # 词元集未直命中 ⇒ 退一步做子串判定（词表词未切分时的常见形态）
            for v in vocab:
                if w in v:
                    hit = True
                    break
        if hit:
            length += len(w)
            hits.append(w)
    return {"score": min(1.0, length / NAME_FULL_LEN) if hits else 0, "hits": hits}


def structure_score_of(signals, segs):
    """S3 结构分：项目一级夹名逐个 vs 模板段名取最优相似度，返回均值（无一级夹 ⇒ 0）。"""
    tops = signals["s3"]["topNames"]
    if not tops or not segs["all"]:
        return {"score": 0, "pairs": []}
    cands = [strip_no(n).lower() for n in segs["all"]]
    cands = [c for c in cands if c]
    total = 0.0
    pairs = []
    for top in tops:
        t = strip_no(top).lower()
        best = 0.0
        best_name = ""
        for c in cands:
            sim = similarity(t, c)
            if sim > best:
                best = sim
                best_name = c
        total += best
        if best >= 0.5:
            pairs.append({"from": top, "to": best_name, "sim": js_round(best * 100) / 100})
    return {"score": total / len(tops), "pairs": pairs}


def content_score_of(signals, m):
    """S4 内容分：扩展名分布 × 模板 ext_signals（按文档占比加权求和，归一）。"""
    ext = signals["s4"]["extDist"]
    keys = list(ext.keys())
    if not keys or not m["ext_signals"]:
        return {"score": 0, "hits": []}
    total = 0
    for k in keys:
        total += ext[k]
    total = total or 1
    score = 0.0
    hits = []
    for k in keys:
        w = m["ext_signals"].get(k)
        if not w:
            continue
        score += (ext[k] / total) * w
        hits.append(k)
    return {"score": min(1.0, score), "hits": hits}


def path_gate_ok(signals, m):
    """path_regex 硬闸：非空且无一命中 ⇒ 该模板判 0 分（可解释，不静默）。"""
    if not m["path_regex"]:
        return True
    paths = signals["s2"]["dirPaths"]
    usable = 0
    for rx in m["path_regex"]:
        try:
            re_compiled = re.compile(rx, re.IGNORECASE)
        except Exception:
            re_compiled = None
        if re_compiled is None:
            continue  # 坏正则：不因模板笔误把候选判死（保守放行）
        usable += 1
        for p in paths:
            if re_compiled.search(str(p)):
                return True
    return usable == 0  # 正则全不可用 ⇒ 保守放行；有可用正则且无一命中 ⇒ 判死


def score_one(signals, tpl, weights):
    """单模板判分（纯函数）。"""
    # 权重三段齐备才采用（防调用方递坏值 ⇒ NaN 分数静默流出）；缺省/坏值 ⇒ 内置兜底
    # 判据同底本：三键齐全（缺键 = undefined = NaN = 不齐备）且 Number(v) 均有限，且和 > 0
    ok_w = (
        isinstance(weights, dict)
        and all(k in weights and math.isfinite(js_number(weights[k]))
                for k in ("structure", "name", "prior"))
        and (js_number(weights["structure"]) + js_number(weights["name"])
             + js_number(weights["prior"])) > 0
    )
    w = weights if ok_w else dict(DEFAULT_WEIGHTS)
    m = match_of(tpl)
    segs = segments_of(tpl)
    kw = keywords_of(tpl)
    gate = path_gate_ok(signals, m)
    name_r = name_score_of(signals, kw) if gate else {"score": 0, "hits": []}
    struct_r = structure_score_of(signals, segs) if gate else {"score": 0, "pairs": []}
    content_r = content_score_of(signals, m) if gate else {"score": 0, "hits": []}
    # 结构分量＝一级夹名相似度与内容分布取高者（同属「实据」层，避免双计）
    structure = max(struct_r["score"], content_r["score"])
    parts = {
        "structure": js_round(structure * 1000) / 1000,
        "name": js_round(name_r["score"] * 1000) / 1000,
        "prior": prior_of(tpl),
    }
    score = (parts["structure"] * w["structure"] + parts["name"] * w["name"]
             + parts["prior"] * w["prior"]) if gate else 0
    return {
        "industry": str((tpl or {}).get("industry") or ""),
        "template_id": (tpl or {}).get("id"),
        "template_name": str((tpl or {}).get("name") or ""),
        "template_version": (tpl or {}).get("version"),
        "score": js_round(score * 1000) / 1000,
        "parts": parts,
        "gate": gate,
        "hits": {
            "keywords": name_r["hits"],
            "ext": content_r["hits"],
            "structure": struct_r["pairs"],
        },
    }


def industry_eq(a, b):
    """行业值等价判定（S1 命中判据）：完全相等，或一方包含另一方（去空白小写）。"""
    x = str(a or "").strip().lower()
    y = str(b or "").strip().lower()
    if not x or not y:
        return False
    return x == y or x in y or y in x


def signals_of(sc):
    """命中的信号名清单（S 编号）。"""
    out = []
    if sc["hits"]["keywords"]:
        out.append("s2")
    if sc["hits"]["structure"]:
        out.append("s3")
    if sc["hits"]["ext"]:
        out.append("s4")
    return out


def decide(signals, templates, opts=None):
    """主入口：对一批项目上下文与一个模板集判分，产出置信三态与本套建议。"""
    o = opts if isinstance(opts, dict) else {}
    weights = o.get("weights") or norm_weights(o.get("rules"))
    th = o.get("thresholds") or thresholds_of(o.get("rules"))
    gap = _num(o.get("tieGap"), TIE_GAP)
    tpl_list = [t for t in (templates if isinstance(templates, list) else [])
                if isinstance(t, dict)]
    explicit = signals.get("s1") if isinstance(signals, dict) else None

    out = {
        "confidence": "none",
        "best": None,
        "ranked": [],
        "suggestions": [],
        "basis": None,
        "signals": [],
        "explicit": explicit,
    }

    # ① S1 显式声明 = 最强信号：命中任一模板行业即高置信
    if explicit:
        hit = None
        for t in tpl_list:
            if industry_eq(t.get("industry"), explicit["industry"]):
                hit = t
                break
        if hit is not None:
            sc = score_one(signals, hit, weights)
            sc["score"] = max(sc["score"], 0.9)  # 显式声明 ⇒ 分数抬到高置信带（判据仍是「用户说了」）
            out["confidence"] = "high"
            out["best"] = sc
            out["ranked"] = [sc]
            out["signals"] = ["s1"]
            out["basis"] = {"industry": sc["industry"], "template_id": sc["template_id"],
                            "template_name": sc["template_name"],
                            "template_version": sc["template_version"], "score": sc["score"],
                            "source": explicit["source"], "grade": "high"}
            return out
        out["confidence"] = "low"  # 声明了但没有该行业模板 ⇒ 交调用方走本地骨架（机械）
        out["signals"] = ["s1"]
        return out

    # ② 无声明 ⇒ 三信号加权判分
    ranked = [score_one(signals, t, weights) for t in tpl_list]
    ranked = [s for s in ranked if s["score"] > 0]
    # 底本排序：(b.score - a.score) || industry localeCompare —— 先按名称序稳定排，再按分数降序稳定排
    ranked.sort(key=lambda s: s["industry"])
    ranked.sort(key=lambda s: -s["score"])
    out["ranked"] = ranked
    if not ranked:
        out["confidence"] = "low"  # 无候选（模板集空/全不匹配）⇒ 不猜
        return out

    top1 = ranked[0]
    top2 = ranked[1] if len(ranked) > 1 else None
    # 平手带：`< gap - ε` 用整数化余量抵浮点表示误差
    tie = top2 is not None and (top1["score"] - top2["score"]) < gap - 1e-9
    if top1["score"] >= th["high"]:
        out["confidence"] = "high"
        out["best"] = top1
        out["signals"] = signals_of(top1)
    elif top1["score"] >= th["low"] and not tie:
        out["confidence"] = "mid"
        out["best"] = top1
        out["signals"] = signals_of(top1)
    else:
        out["confidence"] = "low"  # 平手 / 低于阈值 ⇒ 不猜
        out["suggestions"] = [
            {"industry": s["industry"], "template_id": s["template_id"],
             "template_name": s["template_name"], "template_version": s["template_version"],
             "score": s["score"]}
            for s in ranked[:SUGGEST_MAX]
        ]
    if out["best"]:
        out["basis"] = {
            "industry": out["best"]["industry"], "template_id": out["best"]["template_id"],
            "template_name": out["best"]["template_name"],
            "template_version": out["best"]["template_version"], "score": out["best"]["score"],
            "source": "mechanical", "grade": out["confidence"],
        }
    return out


# ============================== 实例化（模板 → 视图树） ==============================

def instantiate(tpl, ctx=None):
    """由模板实例化视图树（1/2/3 级；含编号——落库恒用正名）。
    「暂空段保留」：本批资料未命中的段不删，标 pending:true（前瞻段种子位）。"""
    c = ctx if isinstance(ctx, dict) else {}
    segs_names = [str(x or "").strip() for x in (c.get("topNames") if isinstance(c.get("topNames"), list) else [])]
    segs_names = [x for x in segs_names if x]
    tree = tpl.get("tree") if isinstance(tpl, dict) and isinstance(tpl.get("tree"), list) else []
    # 每片资料（一级夹）→ 模板段名（按相似度取最优，低于 0.5 不硬套 ⇒ 计入 pending 报告）
    rules = []

    def walk(nodes, parents):
        for n in nodes if isinstance(nodes, list) else []:
            nm = str((n or {}).get("name") or "").strip()
            if not nm:
                continue
            kids = (n or {}).get("children") if isinstance((n or {}).get("children"), list) else []
            if kids:
                walk(kids, parents + [nm])
                continue
            rules.append({"path": "/".join(parents + [nm]), "top": parents[0] if parents else nm,
                          "words": list(vocab_of([nm] + parents).keys())})

    walk(tree, [])
    mapping = []
    hit_tops = ordered_set()
    for top in segs_names:
        best = None
        for r in rules:
            sim = max(similarity(top, r["path"]), similarity(strip_no(top), strip_no(r["path"])))
            if best is None or sim > best["sim"]:
                best = {"sim": sim, "rule": r}
        if best is not None and best["sim"] >= 0.5:
            mapping.append({"from": top, "to": best["rule"]["path"], "sim": js_round(best["sim"] * 100) / 100})
            hit_tops[best["rule"]["top"]] = True
        else:
            mapping.append({"from": top, "to": "",
                            "sim": js_round(best["sim"] * 100) / 100 if best is not None else 0})
    view = []
    for l1 in tree:
        l1name = str((l1 or {}).get("name") or "")
        used = l1name in hit_tops
        l2_list = (l1 or {}).get("children") if isinstance((l1 or {}).get("children"), list) else []
        view.append({
            "name": l1name,
            "pending": not used,
            "children": [
                {
                    "name": str((l2 or {}).get("name") or ""),
                    "children": [
                        {"name": str((l3 or {}).get("name") or ""), "children": []}
                        for l3 in ((l2 or {}).get("children") if isinstance((l2 or {}).get("children"), list) else [])
                    ],
                }
                for l2 in l2_list
            ],
        })
    pending = [x["name"] for x in view if x["pending"]]
    return {
        "name": str((tpl or {}).get("name") or ""),
        "tree": view,
        "mapping": mapping,
        "used": list(hit_tops.keys()),
        "pending": pending,
        "rules": rules,
    }
