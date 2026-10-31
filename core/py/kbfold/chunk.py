# -*- coding: utf-8 -*-
"""kbfold.chunk — 父子分块（纯函数模块：不碰 IO、不碰存储）。

移植自冻结底本 ``kb-chunk.js``（JS→Py 移植件）。参数为冻结契约，禁调参改名：
  子块 512 / 相邻滑窗重叠 64 / 父块 = 连续 4 子块（默认档 4×512 = 2048）。
检索命中子块 ⇒ 回灌所属父块进上下文（长文命中段不再被「整篇前 N 字」截掉）。

档位注册表 PROFILES（唯一事实源）：
  display：条目面展示口径——600 字段落聚合、无重叠；
  embed：  嵌入检索口径——子块 512 · 重叠 64 · 父块连续 4 子块。
缺省行为不读本表（仍走各自缺省常量）；消费方显式传 opts 时才生效档位驱动。

块感知切块（R1-R6 口径，同底本）：
  R1 块是最小不可分单元（单块 >size 才切，切点只落在行/句/硬切）；R2 headingPath 进 text 前缀；
  R3 parentIdx＝同一 page+headingPath 连续块（并按 parentMax 守回灌预算）；R4 表格整块、
  text 用管道表、HTML 绝不进 text；R5 页眉页脚入块前剔除（审计走 opts.audit）；R6 超长表格按行切、
  每片复写表头。切块序列＝输入块顺序（引擎已按 order 排好），不重排。
"""
from __future__ import annotations

import math
import re

from ._compat import js_number, js_round

CHILD_MAX = 512   # 子块尺寸默认/兜底值（改值仍须过正本评审）
OVERLAP = 64      # 相邻子块滑窗重叠（防关键词骑缝断义）
PARENT_GROUP = 4  # 父块 = 连续 4 子块

PROFILES = {
    "display": {"id": "display", "kind": "paragraph", "size": 600, "overlap": 0},
    "embed": {"id": "embed", "kind": "sliding", "child": CHILD_MAX, "childMax": CHILD_MAX,
              "overlap": OVERLAP, "parentGroup": PARENT_GROUP},
}

FURNITURE_LABELS = ("Page-Header", "Page-Footer")

#: 父块回灌文本硬上限（E2 夹点，单一事实源）：任何父组文本 ≤ 上限在结构上成立。
PARENT_TEXT_MAX = 2200

_CRLF_RE = re.compile(r"\r\n")
_PARA_SPLIT_RE = re.compile(r"\n+")
_LINE_SPLIT_RE = re.compile(r"\n+")
_TAG_RE = re.compile(r"<[^>]*>")
_NBSP_RE = re.compile(r"&nbsp;", re.IGNORECASE)
_LT_RE = re.compile(r"&lt;", re.IGNORECASE)
_GT_RE = re.compile(r"&gt;", re.IGNORECASE)
_QUOT_RE = re.compile(r"&quot;", re.IGNORECASE)
_SQUOT_RE = re.compile(r"&#39;")
_AMP_RE = re.compile(r"&amp;", re.IGNORECASE)
_MULTI_WS_RE = re.compile(r"[ \t\u3000]{2,}")
_TR_RE = re.compile(r"<tr[^>]*>([\s\S]*?)</tr>", re.IGNORECASE)
_CELL_RE = re.compile(r"<(?:td|th)[^>]*>([\s\S]*?)</(?:td|th)>", re.IGNORECASE)
_BLOCK_CLOSE_RE = re.compile(r"</(?:tr|p|div|li|h[1-6])>", re.IGNORECASE)


def _int_or(v, dflt):
    """Number(v) 整数化（JS ToIntegerOrInfinity 语义：向零取整；NaN/缺省 ⇒ dflt）。"""
    n = js_number(v)
    return int(n) if math.isfinite(n) else dflt


def chunk_display(text, opts=None):
    """display 档统一入口：按段落聚合、单块 ≤size 字、超长段落按 size 硬切（原序保持、无重叠）。"""
    size = max(64, _int_or((opts or {}).get("size"), PROFILES["display"]["size"])
               or PROFILES["display"]["size"])
    t = _CRLF_RE.sub("\n", str(text or ""))
    paras = [s for s in _PARA_SPLIT_RE.split(t) if s.strip()]
    out = []
    buf = ""
    for p in paras:
        if len((buf + "\n" + p).strip()) <= size:
            buf = (buf + "\n" + p) if buf else p
            continue
        if buf:
            out.append(buf)
        if len(p) <= size:
            buf = p
            continue
        i = 0
        while i < len(p):
            c = p[i:i + size].strip()
            if c:
                out.append(c)
            i += size
        buf = ""
    if buf:
        out.append(buf)
    return [c for c in out if c.strip()]


def split_text(text, opts=None):
    """整文滑窗切块：step = child - overlap；短文单块；空文空数组。
    尾块若不足 overlap 量级则并入前块（避免产生近空块白占一行向量）。"""
    o = opts or {}
    cap = max(64, _int_or(o.get("childMax"), CHILD_MAX) or CHILD_MAX)      # 档位上限（缺省 512 ⇒ 行为不变）
    child = max(64, min(_int_or(o.get("child"), cap) or cap, cap))
    # 注意：缺省/非法 overlap 兜底（底本实炸教训：NaN 步长循环一轮即止）——isFinite 兜底
    if "overlap" in o:
        ov_raw = js_number(o["overlap"])            # null ⇒ 0（与 Number(null) 同口径）
    else:
        ov_raw = float(OVERLAP)                     # undefined ⇒ 缺省
    overlap = int(max(0.0, min(ov_raw if math.isfinite(ov_raw) else float(OVERLAP), child - 1)))
    t = str(text or "").strip()
    if not t:
        return []
    step = child - overlap
    out = []
    start = 0
    while start < len(t):
        piece = t[start:start + child]
        if not piece.strip():
            break
        # 尾块并入：剩余不足一个 step 时并入前块（前块允许略超 step 但 ≤ child+step）
        if out and start + child >= len(t) and len(piece) <= overlap:
            out[-1] = out[-1] + piece
            break
        out.append(piece)
        if start + child >= len(t):
            break
        start += step
    return out


def chunk_pages(pages, opts=None):
    """逐页切块：页内滑切、块带页号；空页跳过（不产空块）。
    pages 形如 [{num:1, text:'...'}]；字符串页项按无页号处理。"""
    out = []
    for pg in (pages or []):
        num = _int_or(pg.get("num") if isinstance(pg, dict) else None, 0)
        num = num if num > 0 else 0
        text = pg if isinstance(pg, str) else str((pg or {}).get("text") or "")
        for piece in split_text(text, opts):
            out.append({"text": piece, "page": num})
    return out


def chunk_document(doc, opts=None):
    """文档级切块入口：有 pages ⇒ 逐页切；无 pages ⇒ 整文切（page=0）。
    仅当页级 blocks 存在才走块感知路（chunk_blocks）；混排页 ⇒ 有 blocks 的页走新路、其余页回落。"""
    d = doc or {}
    if isinstance(d.get("pages"), list) and d["pages"]:
        if any(isinstance(pg, dict) and isinstance(pg.get("blocks"), list) for pg in d["pages"]):
            out = []
            for pg in d["pages"]:
                pn = _int_or(pg.get("num") if isinstance(pg, dict) else None, 0)
                num = pn if pn > 0 else 0
                if isinstance(pg, dict) and isinstance(pg.get("blocks"), list):
                    o2 = dict(opts or {})
                    o2["pageNum"] = num
                    out.extend(chunk_blocks(pg["blocks"], o2))
                else:
                    out.extend(chunk_pages([pg], opts))
            return out
        return chunk_pages(d["pages"], opts)
    return [{"text": t, "page": 0} for t in split_text(d.get("text"), opts)]


def parent_idx_of(chunk_idx):
    """父块组号（存储面消费）：子块 i 属于父块 floor(i / PARENT_GROUP)。"""
    return math.floor(js_number(chunk_idx) / PARENT_GROUP)


# ===== 块感知切块（R1-R6）=====

def _pipe_cell(s):
    """单元格清洗：去标签 + 解码常用实体 + 空白归一（管道表单元格）。"""
    out = str("" if s is None else s)
    out = _TAG_RE.sub(" ", out)
    out = _NBSP_RE.sub(" ", out)
    out = _LT_RE.sub("<", out)
    out = _GT_RE.sub(">", out)
    out = _QUOT_RE.sub('"', out)
    out = _SQUOT_RE.sub("'", out)
    out = _AMP_RE.sub("&", out)
    out = _MULTI_WS_RE.sub(" ", out)
    return out.strip()


def _table_to_pipe(html):
    """HTML 表格 → 管道式纯文本表（逐行一条、列以 ' | ' 分隔）。HTML 绝不进 text（R4）。"""
    rows = []
    for m in _TR_RE.finditer(str(html or "")):
        cells = [_pipe_cell(c.group(1)) for c in _CELL_RE.finditer(m.group(1))]
        if cells:
            rows.append(" | ".join(cells))
    if rows:
        return rows
    # 兜底：无标准行结构 ⇒ 去标签后按行
    txt = _pipe_cell(_BLOCK_CLOSE_RE.sub("\n", str(html or "")))
    return [s.strip() for s in txt.split("\n") if s.strip()] if txt else []


def _sentences(text):
    """句/行单元：句末标点/换行处断开（只在块内部用，块之间永不合并）。"""
    out = []
    buf = []
    for ch in str(text):
        buf.append(ch)
        if ch in "。！？；.!?;\n":
            out.append("".join(buf))
            buf = []
    if buf:
        out.append("".join(buf))
    return out


def _split_block_text(text, cap):
    """正文块内切分：行 → 句 → 硬切；拼装时单元间以换行分隔；无重叠（块边界即语义边界）。"""
    units = []
    for line in [s for s in _LINE_SPLIT_RE.split(str(text)) if s.strip()]:
        if len(line) <= cap:
            units.append(line)
            continue
        for sent in _sentences(line):
            if len(sent) <= cap:
                units.append(sent)
                continue
            i = 0
            while i < len(sent):
                units.append(sent[i:i + cap])
                i += cap
    out = []
    cur = ""
    for u in units:
        if cur and len(cur) + 1 + len(u) > cap:
            out.append(cur)
            cur = ""
        cur = (cur + "\n" + u) if cur else u
    if cur:
        out.append(cur)
    return [s for s in out if s.strip()]


def _split_table_rows(rows, cap):
    """表格按行切：首行＝表头，每片复写表头；单行自身超 cap 时对该行硬切（每片仍带表头，R6）。"""
    if not rows:
        return []
    header = rows[0]
    if len(header) > cap:
        return [header[i:i + cap] for i in range(0, len(header), cap)]
    pieces = []
    cur = header
    for row in rows[1:]:
        if cur and len(cur) + 1 + len(row) <= cap:
            cur = cur + "\n" + row
            continue
        if cur != header:
            pieces.append(cur)
        if len(header) + 1 + len(row) <= cap:
            cur = header + "\n" + row
            continue
        avail = max(32, cap - len(header) - 1)
        for i in range(0, len(row), avail):
            pieces.append(header + "\n" + row[i:i + avail])
        cur = ""
    if cur:
        pieces.append(cur)
    return pieces


def chunk_blocks(blocks, opts=None):
    """块感知切块（R1-R6）。blocks：[{label?, bbox?, order?, text?, tableHtml?, headingPath?}]。
    opts：childMax=块上限（缺省 512）；child=本次尺寸；pageNum=本页页号（页码只从这里来）；
    parentMax=父块字符预算（缺省 4×child）；audit=传入列表则收集被剔除的页眉页脚与预算夹取留痕。"""
    o = opts or {}
    cap = max(64, _int_or(o.get("childMax"), CHILD_MAX) or CHILD_MAX)
    size = max(64, min(_int_or(o.get("child"), cap) or cap, cap))
    pn = _int_or(o.get("pageNum"), 0)
    page = pn if pn > 0 else max(0, _int_or(o.get("page"), 0))
    parent_max_requested = max(size, _int_or(o.get("parentMax"), PARENT_GROUP * size))
    # E2：夹住父块预算（不放大成本护栏）；被夹时留痕（audit），不抛错。
    parent_max = min(parent_max_requested, PARENT_TEXT_MAX)
    parent_max_clamped = None
    if parent_max_requested > PARENT_TEXT_MAX:
        parent_max_clamped = {"requested": parent_max_requested, "applied": parent_max,
                              "cap": PARENT_TEXT_MAX}
    audit = o.get("audit") if isinstance(o.get("audit"), list) else None
    out = []
    head_path = []
    parent_idx = -1
    parent_key = None
    parent_used = 0

    for b in (blocks if isinstance(blocks, list) else []):
        if not b:
            continue
        if isinstance(b.get("headingPath"), list):
            head_path = [s for s in (str("" if x is None else x) for x in b["headingPath"]) if s]
        label = str(b.get("label") or "Text")
        if label in FURNITURE_LABELS:
            # R5：入块前剔除；页码取 page.num（不用页眉文本）；审计可选
            if audit is not None:
                audit.append({"page": page, "label": label,
                              "text": str("" if b.get("text") is None else b.get("text")),
                              "bbox": b.get("bbox") if isinstance(b.get("bbox"), list) else None,
                              "order": b.get("order")})
            continue
        is_table = bool(b.get("tableHtml") or b.get("isTable"))
        body = str("" if b.get("text") is None else b.get("text"))
        table_html = str(b["tableHtml"]) if (is_table and b.get("tableHtml")) else ""
        if is_table:
            # R4：文本面只放管道表；已有 text 若含 HTML 则从 tableHtml 重建
            if not body.strip() or "<" in body:
                body = "\n".join(_table_to_pipe(table_html or body))
            body = _TAG_RE.sub("", body)  # 双保险：HTML 永不进 text
        if not body.strip():
            continue

        # R2：标题路径拼成块首前缀（最深一级标题 + ' — '），heading 留完整路径
        prefix = (head_path[-1] + " — ") if head_path else ""
        if len(prefix) > cap - 32:
            prefix = prefix[:cap - 32]
        heading = " > ".join(head_path)
        room = max(32, cap - len(prefix))
        pieces = (_split_table_rows(body.split("\n"), room) if is_table
                  else ([body] if len(body) + len(prefix) <= size else _split_block_text(body, room)))

        for k, piece in enumerate(pieces):
            text = prefix + piece
            if len(text) > cap:
                text = text[:cap]  # 防御（构造上不会触发）
            # R3：同一 page+headingPath 的连续块归一个父块；超 parentMax 另起（守回灌预算）
            key = str(page) + "|" + heading
            if parent_key != key or parent_used + len(text) > parent_max:
                parent_idx += 1
                parent_key = key
                parent_used = 0
            chunk = {
                "text": text, "page": page, "parentIdx": parent_idx,
                "label": label, "blockLabel": label,
                "heading": heading, "headingPathArr": list(head_path),
            }
            if isinstance(b.get("bbox"), list):
                chunk["bbox"] = b["bbox"]
            if math.isfinite(js_number(b.get("order"))):
                chunk["order"] = math.trunc(js_number(b["order"]))
            if is_table:
                chunk["isTable"] = True
                if table_html and k == 0:
                    chunk["tableHtml"] = table_html  # 超长表切分：HTML 只挂首片（避免重复占位）
            out.append(chunk)
            parent_used += len(text)

    if parent_max_clamped and audit is not None:
        audit.append({"kind": "parentMaxClamped", "page": page,
                      "requested": parent_max_clamped["requested"],
                      "applied": parent_max_clamped["applied"],
                      "cap": parent_max_clamped["cap"]})
    return out
