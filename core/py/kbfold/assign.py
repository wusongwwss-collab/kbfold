# -*- coding: utf-8 -*-
"""kbfold.assign — 归属判定梯 L0-L6（纯函数：零 IO、零大模型、零文件系统）。

移植自冻结底本 ``kb-assign-engine.js``（JS→Py 移植件；搬运正本＝需求包 _assets 快照）。
层级契约（自上而下逐层命中，命中即返回；都不命中 ⇒ L6；分值为冻结契约，禁调参改名）：

  L0  user_explicit        用户显式选定                              0.95  可静默
  L1a name_exact           知识库名/别名 与 主题全等                  0.92  可静默
  L1b name_fuzzy_unique    模糊匹配且唯一命中（多命中 ⇒ 降级）        0.85  可静默
  L2  lexicon_hit          关键词/词表规则命中                        0.80  可静默
  L3  history_vote         历史投票（计分口径见下）                   0.75  可静默
  L4  local_score          端上本地打分（注入接口，数据源闭源）       0.40-0.70  只出建议
  L5  llm                  大模型兜底（默认关；开着也只读调用方候选） 0.50-0.70  永不静默
  L6  fallback             判不出                                    —     不落

硬规则（照拍板，禁放宽）：
  ① 静默门：silent = decided ∧ confidence ≥ 阈值 ∧ 层属可静默层 ∧ 非首次命中；
     阈值缺省 0.75，可被设置覆盖；≤0 / 'off' / 'none' / 非法 ⇒ 关闭静默落。
  ② 首次从严：候选知识库在历史归属中从未出现过 ⇒ silent=False，强制走建议。
  ③ 误归档代价 > 不归档：拿不准一律不静默；L4/L5 永不静默；歧义一律降级。
  ④ L6 保持现状：不写任何归属。

与底本的两处已声明差异（均为既定施工口径，非改判定）：
  - L4 数据源＝注入接口：底本经 require 引入同族抽取打分件（数据源闭源）；本移植把
    「本地打分」改为 ctx.opts.local_scorer(text, projects) 可注入回调，未注入 ⇒ 该层无结论
    （不猜；抽取/打分异常同样按该层无结论处理，与底本 try/catch 口径一致）。
  - L5 大模型兜底：候选仍由调用方给出，本件从不联网。

口径说明（供复核，同底本）：
  · 归一化 norm()：去首尾空白、转小写、剔除空白与常见连接符/括号；
  · 相似度 dice()：字符二元组 Dice 系数（0..1）；
  · L1b 模糊分：包含关系直接记 1.0，否则取 dice；达到 FUZZY_MIN(0.60) 的候选恰好 1 个才算唯一命中；
  · L3 计分（唯一最高分且 ≥ L3_MIN_VOTE=2 才判）：参会人重合每重合 1 人 +2（单行上限 3 人）；
    主题全等每项 +2（单行上限 2 项）；会议类型相同 +1；并列 ⇒ 降级；
  · L4 置信映射：confidence = clamp(0.40 + 0.06 × 打分, 0.40, 0.70)；
  · alternatives：优先 L4 打分榜（排除已判定库）前 2，无结果回落 L3 票榜、再回落 L1b 模糊分。
"""
from __future__ import annotations

import re
from math import isfinite

from ._compat import js_number

#: 静默落缺省阈值（契约冻结值；可被设置键覆盖）。
SILENT_DEFAULT_THRESHOLD = 0.75

#: 层级标识（自上而下判定序；契约逐字一致）。
LAYERS = ("L0", "L1a", "L1b", "L2", "L3", "L4", "L5", "L6")

#: 层中文可读名（用户可见，禁英文/禁黑话）。
LAYER_LABELS = {
    "L0": "会前已选定知识库",
    "L1a": "知识库名称完全一致",
    "L1b": "知识库名称模糊唯一命中",
    "L2": "关键词词表命中",
    "L3": "参会人与该知识库历史会议重合",
    "L4": "端上本地打分",
    "L5": "大模型兜底判定",
    "L6": "暂未归属",
}

#: 各层默认置信度（契约冻结；L4/L5 为区间 ⇒ 分开登记下界/上界）。
DEFAULT_CONFIDENCE = {
    "L0": 0.95, "L1a": 0.92, "L1b": 0.85, "L2": 0.80, "L3": 0.75,
    "L4_MIN": 0.40, "L4_MAX": 0.70, "L5_MIN": 0.50, "L5_MAX": 0.70,
}

#: 可静默层；L4/L5/L6 永不在内。
SILENT_LAYERS = ("L0", "L1a", "L1b", "L2", "L3")

#: L1b 模糊阈值：达到即参与「唯一命中」判定。
FUZZY_MIN = 0.60
#: L3 最低票（低于此分不判 L3）。
L3_MIN_VOTE = 2
#: L4 打分换算系数（confidence = 0.40 + 0.06 × 打分，封顶 0.70）。
L4_CONF_BASE = 0.40
L4_CONF_STEP = 0.06

_NORM_RE = re.compile(r"[\s（）()[\]【】{}·\-—_/|\\,，。.、;；:：'\"“”‘’]")


def norm(raw):
    """归一化：去首尾空白、转小写、剔除空白与常见连接符/括号（全等与包含判定的统一口径）。"""
    if raw is None:
        raw = ""
    return _NORM_RE.sub("", str(raw).strip().lower())


def bigrams(s):
    """字符二元组（中文场景下比字符集 Jaccard 更稳；长度 <2 时整串作为唯一样本）。"""
    t = str(s or "")
    if len(t) < 2:
        return [t] if t else []
    return [t[i:i + 2] for i in range(len(t) - 1)]


def dice(a, b):
    """二元组 Dice 系数（0..1）。"""
    A = bigrams(a)
    B = bigrams(b)
    if not A or not B:
        return 0.0
    pool = {}
    for x in A:
        pool[x] = pool.get(x, 0) + 1
    hit = 0
    for x in B:
        n = pool.get(x, 0)
        if n > 0:
            pool[x] = n - 1
            hit += 1
    return (2 * hit) / (len(A) + len(B))


def parse_silent_threshold(raw):
    """解析静默阈值。None ⇒ 缺省 0.75；''/off/none/0/负数/非数 ⇒ 0（关闭静默落，
    fail-safe：宁可不静默，不可误静默）。返回 0=关闭；>0=生效阈值（上限 1）。"""
    if raw is None:
        return SILENT_DEFAULT_THRESHOLD
    s = str(raw).strip().lower()
    if s == "":
        return 0.0
    if s in ("off", "none"):
        return 0.0
    v = js_number(s)
    if not isfinite(v) or v <= 0:
        return 0.0
    return min(1.0, v)


def title_candidates(meeting):
    """会议侧可用的标题串（L1 全等/模糊比对的一次性取值口）。"""
    out = []

    def push(x):
        v = str("" if x is None else x).strip()
        if v:
            out.append(v)

    m = meeting or {}
    push(m.get("subject") or m.get("topic"))
    for t in (m.get("topics") if isinstance(m.get("topics"), list) else []):
        push(t)
    return out


def meeting_text(meeting):
    """会议全文（L2 词表命中 / L4 本地打分的输入文本）：标题 + 议题 + 三底账 + 纪要文本。"""
    m = meeting or {}
    parts = []

    def push(x):
        v = str("" if x is None else x).strip()
        if v:
            parts.append(v)

    push(m.get("subject") or m.get("topic"))
    topics = m.get("topics")
    push("  ".join(topics) if isinstance(topics, list) else "")
    push(m.get("agenda_text"))
    push(m.get("background_text"))
    push(m.get("goal_text"))
    push(m.get("minutes_text"))
    return "\n".join(parts)


def fuzzy_score_of(project, titles):
    """知识库在某会议标题上的模糊分：包含 ⇒ 1.0；否则取二元组 Dice 的最大值。"""
    own = []

    def push(x):
        v = norm(x)
        if v:
            own.append(v)

    p = project or {}
    push(p.get("name"))
    for a in (p.get("aliases") if isinstance(p.get("aliases"), list) else []):
        push(a)
    if not own or not titles:
        return 0.0
    best = 0.0
    for nm in own:
        for ti in titles:
            t = norm(ti)
            if not t:
                continue
            if nm == t:
                s = 1.0  # 全等另走 L1a；此处兜底给满分
            elif nm in t or t in nm:
                s = 1.0
            else:
                s = dice(nm, t)
            if s > best:
                best = s
    return best


def decide(ctx):
    """归属判定主函数（纯函数）。ctx：{meeting, projects, history, explicitProjectId, opts}。
    opts.local_scorer(text, projects) → [{project_id, score, reasons}] 为 L4 注入接口
    （数据源闭源；未注入 ⇒ L4 无结论，不猜）。"""
    c = ctx or {}
    meeting = c.get("meeting") or {}
    projects = [p for p in (c.get("projects") if isinstance(c.get("projects"), list) else [])
                if isinstance(p, dict) and p.get("id") is not None and isfinite(js_number(p.get("id")))]
    history = [h for h in (c.get("history") if isinstance(c.get("history"), list) else [])
               if isinstance(h, dict) and h.get("project_id") is not None]
    opts = c.get("opts") or {}
    threshold = parse_silent_threshold(opts.get("silentThreshold"))
    first_hit_strict = opts.get("firstHitStrict") is not False  # 默认 True（显式 False 才关）
    llm_enabled = opts.get("llmEnabled") is True

    # L6 基底（判不出时逐字返回这一份）
    none = {
        "decided": False, "projectId": None, "projectName": "", "layer": "L6",
        "layerLabel": LAYER_LABELS["L6"], "confidence": 0, "silent": False,
        "reasons": [], "alternatives": [],
    }
    if not projects:
        return none

    by_id = {}
    for p in projects:
        by_id[int(js_number(p["id"]))] = p
    l1b_ambiguous = ""  # L1b 命中多个 ⇒ 记账，降级后在最终理由里如实写出

    def name_of(pid):
        p = by_id.get(int(pid))
        return str(p.get("name") or "") if p else ""

    titles = title_candidates(meeting)
    text = meeting_text(meeting)

    # 「首次命中」判据：该知识库在此前历史归属里出现过吗
    seen_pids = set(int(js_number(h["project_id"])) for h in history)

    def is_first_hit(pid):
        return int(pid) not in seen_pids

    def pack(layer, pid, confidence, reasons, alts):
        confident = js_number(confidence) if isfinite(js_number(confidence)) else 0
        silent_layer = layer in SILENT_LAYERS
        not_first = not (first_hit_strict is True and is_first_hit(pid))
        silent = bool(threshold > 0 and silent_layer and not_first and confident >= threshold)
        out = {
            "decided": True,
            "projectId": int(pid),
            "projectName": name_of(pid),
            "layer": layer,
            "layerLabel": LAYER_LABELS.get(layer, layer),
            "confidence": confident,
            "silent": silent,
            "reasons": [str(r) for r in (reasons or [])],
            "alternatives": (alts or [])[:2],
        }
        if silent_layer and not silent:
            if threshold <= 0:
                out["reasons"].append("静默落已关闭：只出建议不自动归属")
            elif confident < threshold:
                out["reasons"].append("置信度未达静默阈值（%.2f < %s）：只出建议" % (confident, threshold))
            elif first_hit_strict is True and is_first_hit(pid):
                out["reasons"].append("该知识库首次命中：从严走建议，不自动归属")
            else:
                out["reasons"].append("并列/歧义候选：只出建议，不自动归属")
        return out

    # ---- L4 打分底座（惰性一次算好：既供 L4 判定，也供 alternatives 回落） ----
    _l4 = None

    def l4_scored():
        nonlocal _l4
        if _l4 is not None:
            return _l4
        _l4 = []
        scorer = opts.get("local_scorer") if isinstance(opts, dict) else None
        if text and callable(scorer):
            try:
                scored = scorer(text, projects)
                _l4 = [
                    {"project_id": int(js_number(x.get("project_id"))),
                     "score": js_number(x.get("score")),
                     "reasons": [str(r) for r in (x.get("reasons") or [])]}
                    for x in (scored if isinstance(scored, list) else []) if isinstance(x, dict)
                ]
            except Exception:
                _l4 = []  # 抽取/打分异常 ⇒ 该层无结论（不猜；异常口径属调用方可观测面）
        return _l4

    # ---- L3 票榜（惰性；同时作为 alternatives 的第二回落） ----
    _l3 = None

    def l3_votes():
        nonlocal _l3
        if _l3 is not None:
            return _l3
        votes = {}  # pid -> {score, reasons[]}
        meet_parts = set(norm(x) for x in (meeting.get("participants") if isinstance(meeting.get("participants"), list) else []))
        meet_parts.discard("")
        meet_topics = set(norm(t) for t in titles)
        meet_topics.discard("")
        meet_type = norm(meeting.get("meeting_type"))
        for h in history:
            pid = int(js_number(h["project_id"]))
            if pid not in by_id:
                continue  # 已删/失效的知识库不参与投票
            rec = votes.get(pid) or {"score": 0, "reasons": []}
            hp = [norm(x) for x in (h.get("participants") if isinstance(h.get("participants"), list) else [])]
            hp = [x for x in hp if x]
            share = 0
            for x in hp:
                if x in meet_parts:
                    share += 1
            if share > 3:
                share = 3  # 单行参会人计分上限 3 人
            if share > 0:
                rec["score"] += 2 * share
                rec["reasons"].append("参会人重合 %d 人（会议 %s）" % (share, str(h.get("meeting_id") or "")))
            ht = [norm(t) for t in (h.get("topics") if isinstance(h.get("topics"), list) else [])]
            ht = [t for t in ht if t]
            t_share = 0
            for t in ht:
                if t in meet_topics:
                    t_share += 1
            if t_share > 2:
                t_share = 2  # 单行主题计分上限 2 项
            if t_share > 0:
                rec["score"] += 2 * t_share
                rec["reasons"].append("主题相同 %d 项（会议 %s）" % (t_share, str(h.get("meeting_id") or "")))
            if meet_type and norm(h.get("meeting_type")) == meet_type:
                rec["score"] += 1
                rec["reasons"].append("会议类型相同：" + str(meeting.get("meeting_type") or ""))
            votes[pid] = rec
        _l3 = sorted(
            ({"projectId": pid, "projectName": name_of(pid), "score": v["score"], "reasons": v["reasons"]}
             for pid, v in votes.items()),
            key=lambda x: (-x["score"], x["projectId"]),
        )
        return _l3

    def alternatives_of(chosen_pid):
        """备选（前 2，排除已判定库）：优先 L4 打分榜 → L3 票榜 → L1b 模糊分。"""
        out = []
        seen = {int(chosen_pid)}

        def take(lst):
            for x in lst:
                pid = int(js_number(x.get("projectId", x.get("project_id"))))
                if pid not in by_id or pid in seen:
                    continue
                seen.add(pid)
                nm = x.get("projectName") or x.get("name") or name_of(pid)
                out.append({"projectId": pid, "projectName": str(nm),
                            "score": js_number(x.get("score")) or 0})
                if len(out) >= 2:
                    return True
            return False

        if take(l4_scored()):
            return out
        if take(l3_votes()):
            return out
        take([{"projectId": p["id"], "score": float("%.2f" % (fuzzy_score_of(p, titles) * 10))}
              for p in projects])
        return out

    # ---------------- L0：用户显式选定 ----------------
    if c.get("explicitProjectId") is not None:
        pid = int(js_number(c["explicitProjectId"]))
        if pid in by_id:
            return pack("L0", pid, DEFAULT_CONFIDENCE["L0"],
                        ["会前已选定知识库「" + name_of(pid) + "」"], alternatives_of(pid))

    # ---------------- L1a：库名/别名全等 ----------------
    norm_titles = set(norm(t) for t in titles)
    norm_titles.discard("")
    hits = []
    for p in projects:
        names = [str(p.get("name") or "")] + [str(a) for a in
                 (p.get("aliases") if isinstance(p.get("aliases"), list) else [])]
        for nm in names:
            n = norm(nm)
            if n and n in norm_titles:
                hits.append({"pid": int(js_number(p["id"])), "name": str(nm), "title": nm})
                break
    if hits:
        # 全等命中多个（库名与别名撞车）⇒ 取库名命中的那个；仍多 ⇒ 不算 L1a（降级），防臆断
        by_name = [h for h in hits if norm(h["name"]) == norm(name_of(h["pid"]))]
        if len(by_name) == 1:
            pick = by_name[0]
        elif len(hits) == 1:
            pick = hits[0]
        else:
            pick = None
        if pick:
            return pack("L1a", pick["pid"], DEFAULT_CONFIDENCE["L1a"],
                        ["知识库名称与会议主题完全一致：「" + name_of(pick["pid"]) + "」"],
                        alternatives_of(pick["pid"]))

    # ---------------- L1b：模糊唯一命中 ----------------
    scored = [{"pid": int(js_number(p["id"])), "s": fuzzy_score_of(p, titles)} for p in projects]
    scored = [x for x in scored if x["s"] >= FUZZY_MIN]
    scored.sort(key=lambda x: (-x["s"], x["pid"]))
    if len(scored) == 1:
        w = scored[0]
        return pack("L1b", w["pid"], DEFAULT_CONFIDENCE["L1b"],
                    ["知识库名称模糊唯一命中：「" + name_of(w["pid"]) + "」（相似度 %.2f）" % w["s"]],
                    alternatives_of(w["pid"]))
    if len(scored) > 1:
        # 命中多个 ⇒ 不算 L1b（契约硬规则），降级走 L2/L3/L4
        l1b_ambiguous = "、".join("%s(%.2f)" % (name_of(x["pid"]), x["s"]) for x in scored[:3])

    # ---------------- L2：关键词/词表命中 ----------------
    nt = norm(text)
    hits2 = []
    for p in projects:
        kws = []
        for k in (p.get("keywords") if isinstance(p.get("keywords"), list) else []):
            k = str(k)
            n = norm(k)
            if n:
                kws.append({"raw": k, "n": n})
        matched = [k["raw"] for k in kws if k["n"] in nt]
        if matched:
            hits2.append({"pid": int(js_number(p["id"])), "n": len(matched), "matched": matched})
    hits2.sort(key=lambda x: (-x["n"], x["pid"]))
    if hits2 and (len(hits2) == 1 or hits2[0]["n"] > hits2[1]["n"]):  # 唯一最高分才判
        w = hits2[0]
        why = ["关键词命中：" + "、".join(w["matched"][:5])]
        if l1b_ambiguous:
            why.append("库名模糊命中多个（" + l1b_ambiguous + "）：降级按词表判定")
        return pack("L2", w["pid"], DEFAULT_CONFIDENCE["L2"], why, alternatives_of(w["pid"]))

    # ---------------- L3：历史投票 ----------------
    votes = l3_votes()
    if votes and votes[0]["score"] >= L3_MIN_VOTE and (len(votes) == 1 or votes[0]["score"] > votes[1]["score"]):
        w = votes[0]
        return pack("L3", w["projectId"], DEFAULT_CONFIDENCE["L3"], w["reasons"][:4],
                    alternatives_of(w["projectId"]))

    # ---------------- L4：端上本地打分（只出建议；数据源＝注入接口） ----------------
    scored4 = l4_scored()
    if scored4 and js_number(scored4[0].get("score")) > 0:
        top = scored4[0]
        pid = int(js_number(top["project_id"]))
        confidence = max(DEFAULT_CONFIDENCE["L4_MIN"],
                         min(DEFAULT_CONFIDENCE["L4_MAX"],
                             L4_CONF_BASE + L4_CONF_STEP * js_number(top["score"])))
        why = [str(r) for r in (top.get("reasons") or [])][:3]
        why.append("端上本地打分 %s" % js_number(top["score"]))
        if len(scored4) > 1 and js_number(scored4[1]["score"]) == js_number(top["score"]):
            why.append("存在并列候选，建议人工确认")
        return pack("L4", pid, confidence, why, alternatives_of(pid))

    # ---------------- L5：大模型兜底（默认关；开着也只读调用方给的候选，不落库） ----------------
    if llm_enabled:
        cands = []
        for x in (opts.get("llmCandidates") if isinstance(opts.get("llmCandidates"), list) else []):
            if not isinstance(x, dict):
                continue
            pid = int(js_number(x.get("projectId", x.get("project_id"))))
            conf = js_number(x.get("confidence"))
            if pid in by_id:
                cands.append({"pid": pid, "c": conf})
        if cands:
            w = cands[0]
            if isfinite(w["c"]):
                conf = max(DEFAULT_CONFIDENCE["L5_MIN"], min(DEFAULT_CONFIDENCE["L5_MAX"], w["c"]))
            else:
                conf = DEFAULT_CONFIDENCE["L5_MIN"] + (DEFAULT_CONFIDENCE["L5_MAX"] - DEFAULT_CONFIDENCE["L5_MIN"]) / 2
            return pack("L5", w["pid"], conf,
                        ["大模型兜底判定（本批不接真实模型，候选由调用方给出）"], alternatives_of(w["pid"]))

    # ---------------- L6：判不出（保持现状：不写归属） ----------------
    return none
