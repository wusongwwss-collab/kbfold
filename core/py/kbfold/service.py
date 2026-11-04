# -*- coding: utf-8 -*-
"""kbfold.service — 壳层共用服务面（缝合层：信号采集 → core 输入 → 壳输出）。

架构口径：新写的只有缝合层与壳（判定逻辑全部在 core 各件，本模块不重写任何判定）。
CLI 与 MCP 两个壳共用本模块，保证「同一输入同一答案」跨壳一致：
  - load_rules：规则装载（.yml 走 DSL 子集解析器 / .json 直读 / --demo 内置演示规则）
  - judge_root：扫描 + 信号采集 + 逐件判定 + plan IR（纯判定，可选落盘 plan.json）
  - apply_renames / undo_batch：rename-only 档执行与回滚（只在 CLI 壳暴露，MCP 不注册）
  - build_index / search_root：分块索引（FTS5）与三路检索融合（RRF k=60）
  - explain_path / scan_report：explain 与 scan 读数
  - audit_self：零外连自证（import 清单 + 禁网模块扫描 + 删除动作零命中）
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time

from . import journal, manifest, plan as plan_mod, rules as rules_mod
from .chunk import PARENT_GROUP, split_text

KB_DIR = ".kb"
MANIFEST_NAME = "manifest.json"
JOURNAL_NAME = "journal.jsonl"
PLAN_NAME = "plan.json"
RULES_LOCK_NAME = "rules.lock.yaml"
INDEX_NAME = "index.sqlite"

#: 演示规则（--demo 兜底；虚构项目名，断网可跑；与公开规范示例同构）
DEMO_RULES = {
    "version": 1,
    "root": ".",
    "basis": "kbfold:generic/v1",
    "kbs": [
        {
            "name": "星海项目",
            "id": "XH-2024",
            "detect": {"markers": [".xhproject"], "keywords": ["星海", "XH-2024"]},
            "paths": [
                {"match": "**/合同/**", "to": "02-合同"},
                {"match": "**/会议记录/**", "to": "03-会议"},
                {"match": "**/过程稿/**", "to": "@exclude"},
            ],
        },
        {
            "name": "远洋项目",
            "id": "YY-2025",
            "detect": {"markers": [".yyproject"], "keywords": ["远洋", "YY-2025"]},
            "paths": [
                {"match": "**/图纸/**", "to": "04-图纸"},
            ],
        },
    ],
}

# 审计口径：禁网模块清单（audit 自证；core 只许标准库且不含其中任何一项）
_AUDIT_SCAN_ROOTS = ("core/py", "hosts")
_NETWORK_MODULES = frozenset({
    "socket", "ssl", "urllib", "urllib.request", "urllib.parse", "http", "http.client",
    "requests", "httpx", "aiohttp", "ftplib", "smtplib", "poplib", "imaplib",
    "telnetlib", "xmlrpc", "xmlrpc.client", "websocket", "selenium", "pycurl",
})
_IMPORT_RE = re.compile(r"^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_.]*)", re.MULTILINE)
#: 删除动作自证：源码内不得出现「un」「link」拼接出的标准库删除调用（拼接扫描，避免本件自证词污染检索面）
_DEL_A = "un"
_DEL_B = "link"
#: 反馈上行唯一网络面（2026-11-05 YQ-1154 D2；opt-in 且默认关闭——`kbfold feedback send`
#: 显式触发时才 lazy import）。除本清单内文件外，任何文件命中禁网模块仍即自证失败。
_SANCTIONED_NETWORK_FILES = frozenset({"hosts/cli/kbfold_feedback_net.py"})


def kb_path(root, name):
    return os.path.join(root, KB_DIR, name)


def load_rules(rules_path=None, demo=False):
    """规则装载：显式路径（.yml=DSL 子集 / .json）> 演示规则。返回 (rules_dict, rules_text, source)。"""
    if rules_path:
        with open(rules_path, "r", encoding="utf-8") as f:
            text = f.read()
        if rules_path.lower().endswith(".json"):
            return json.loads(text), text, rules_path
        return rules_mod.parse_rules_text(text), text, rules_path
    if demo:
        text = json.dumps(DEMO_RULES, ensure_ascii=False, indent=2)
        return json.loads(text), text, "demo"
    # 未指定：试 root 的 kbfold.yml / kbfold.json 由调用方决定；此处直接演示规则兜底
    text = json.dumps(DEMO_RULES, ensure_ascii=False, indent=2)
    return json.loads(text), text, "demo"


def _is_text_file(rel):
    text_exts = (".txt", ".md", ".markdown", ".csv", ".log", ".yml", ".yaml", ".json",
                 ".py", ".js", ".ts", ".html", ".css", ".xml", ".tex", ".srt")
    return rel.lower().endswith(text_exts)


def judge_root(root, rules, write=False):
    """扫描 + 信号采集 + 逐件判定 + plan IR。write=True 时落盘 plan.json 与 rules.lock.yaml
    （仅 .kb/ 内的格式契约件，不动任何用户文件）。"""
    root = os.path.abspath(root)
    rules_text = json.dumps(rules, ensure_ascii=False, sort_keys=True)
    locked = rules_mod.rules_locked_hash(rules_text)
    scan = manifest.scan_files(root)
    judged = {}
    for rel in scan:
        marker_kb, name_hits, keyword_hits = manifest.collect_signals(rules, rel, scan[rel], root)
        judged[rel] = rules_mod.judge_file(rules, rel, marker_kb=marker_kb,
                                           name_hits=name_hits, keyword_hits=keyword_hits)
    # settled 终态集：已定案文件不再重复判定（沿用既有归属）
    mf_path = kb_path(root, MANIFEST_NAME)
    if os.path.isfile(mf_path):
        try:
            mf = manifest.load_manifest(mf_path)
            for rel, rec in mf.get("files", {}).items():
                if rec.get("settled") and rel in judged:
                    judged[rel] = {"kb": rec.get("kb"), "target": rec.get("target"),
                                   "confidence": rec.get("confidence"),
                                   "reason": "settled:已定案（" + str(rec.get("reason") or "") + "）",
                                   "via": "settled", "candidates": []}
        except manifest.ManifestError:
            pass  # 台账损坏如实按无台账处理（不猜；audit/explain 可见）
    batch = manifest.make_batch_id(locked, "|".join(sorted(judged)), time.time())
    p = plan_mod.build_plan(root, rules, locked, scan, judged, batch=batch)
    if write:
        os.makedirs(kb_path(root, ""), exist_ok=True)
        plan_mod.dump_plan(kb_path(root, PLAN_NAME), p)
        lock_lines = ["# kbfold rules.lock（本批生效规则快照）",
                      "# sha256: " + locked.split(":", 1)[1], ""]
        with open(kb_path(root, RULES_LOCK_NAME), "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lock_lines) + rules_text + "\n")
    return p


def apply_renames(root, plan_data, rules=None, rename_only=True):
    """执行方案（rename-only 档）：先记账再动手（journal 写不进就不动手）。
    只做同卷改名/新建目录；不删除任何文件。执行后原写 manifest 台账（explain 的依据）。
    返回 {"done": n, "journal": path, "batch": id, "manifest": path}。"""
    root = os.path.abspath(root)
    jp = kb_path(root, JOURNAL_NAME)
    done = 0
    for d in plan_data.get("mkdir", []):
        target = os.path.join(root, d["path"].replace("/", os.sep))
        if not os.path.isdir(target):
            journal.append_record(jp, plan_data.get("batch", ""), "mkdir", "", d["path"], "", "", int(time.time()))
            os.makedirs(target, exist_ok=True)
            done += 1
    for r in plan_data.get("renames", []):
        src_rel, dst_rel = r["from"], r["to"]
        src = os.path.join(root, src_rel.replace("/", os.sep))
        dst = os.path.join(root, dst_rel.replace("/", os.sep))
        if not os.path.isfile(src):
            continue  # 现场已变：跳过并在结果里如实呈现（不猜不硬造）
        if os.path.exists(dst):
            continue  # 目的地被占：跳过（占位改名属 undo 段口径；执行段宁可不动作）
        h = manifest.hash_file(src)
        # ④ 枢纽：先记账再动手
        journal.append_record(jp, plan_data.get("batch", ""), "rename", src_rel, dst_rel, h, h, int(time.time()))
        os.makedirs(os.path.dirname(dst) or root, exist_ok=True)
        os.rename(src, dst)  # 同卷改名，系统级原子
        done += 1
    mf_path = _write_manifest(root, plan_data, rules)
    return {"done": done, "journal": jp, "batch": plan_data.get("batch", ""),
            "manifest": mf_path}


def _write_manifest(root, plan_data, rules):
    """apply 后原写 manifest.json：现扫描逐件记 size/mtime/hash + 判定（kb/target/confidence/
    reason 人话/settled）。判定口径与 judge_root 完全一致（重判一次，成本可接受）。"""
    root = os.path.abspath(root)
    rename_by_to = {r["to"]: r for r in plan_data.get("renames", [])}
    rename_by_from = {r["from"]: r for r in plan_data.get("renames", [])}
    inbox_by_from = {i["from"]: i for i in plan_data.get("inbox", [])}
    excluded_from = {e["from"] for e in plan_data.get("excluded", [])}
    scan = manifest.scan_files(root)
    files = {}
    for rel, meta in scan.items():
        rec = dict(meta)
        if rel in rename_by_to:
            r = rename_by_to[rel]
            rec.update({"kb": r.get("kb"), "target": os.path.dirname(r["to"]).split("/")[-1],
                        "confidence": r.get("confidence"), "reason": r.get("reason"),
                        "settled": False})
        elif rel in rename_by_from:
            r = rename_by_from[rel]
            rec.update({"kb": r.get("kb"), "target": os.path.dirname(r["from"]).split("/")[-1],
                        "confidence": r.get("confidence"), "reason": r.get("reason"),
                        "settled": False})
        elif rel in excluded_from:
            rec.update({"kb": None, "target": "@exclude", "confidence": None,
                        "reason": "@exclude（仍登记指纹）", "settled": False})
        elif rel in inbox_by_from:
            rec.update({"kb": None, "target": "@inbox", "confidence": None,
                        "reason": inbox_by_from[rel].get("reason"), "settled": False})
        elif rules is not None:
            marker_kb, name_hits, keyword_hits = manifest.collect_signals(rules, rel, meta, root)
            j = rules_mod.judge_file(rules, rel, marker_kb=marker_kb,
                                     name_hits=name_hits, keyword_hits=keyword_hits)
            rec.update({"kb": j.get("kb"), "target": j.get("target"),
                        "confidence": j.get("confidence"), "reason": j.get("reason"),
                        "settled": False})
        full = os.path.join(root, rel.replace("/", os.sep))
        try:
            rec["hash"] = manifest.hash_file(full)
        except OSError:
            rec["hash"] = ""
        files[rel] = rec
    mf = {"version": manifest.MANIFEST_VERSION, "pid": plan_data.get("batch", ""),
          "rules_locked": plan_data.get("rules_locked", ""), "files": files}
    mf_path = kb_path(root, MANIFEST_NAME)
    manifest.dump_manifest(mf_path, mf)
    return mf_path


def undo_batch(root, batch):
    """undo 三段算法执行：① hash 校验停 ② 占位改名 ③ 逆序执行。"""
    root = os.path.abspath(root)
    jp = kb_path(root, JOURNAL_NAME)
    pu = journal.plan_undo(jp, batch)
    conflicts = journal.verify_undo_targets(pu, root, manifest.hash_file)
    if conflicts:
        return {"undone": 0, "stopped": "hash-conflict", "conflicts": conflicts}
    occupants = journal.find_conflict_occupants(pu, root)
    for occ in occupants:
        src = os.path.join(root, occ["from"].replace("/", os.sep))
        dst = os.path.join(root, occ["to"].replace("/", os.sep))
        if os.path.isfile(src) and not os.path.exists(dst):
            os.rename(src, dst)  # ② 占位改名（不覆盖任何文件）
    undone = 0
    for op in pu["ops"]:
        src = os.path.join(root, op["from"].replace("/", os.sep))
        dst = os.path.join(root, op["to"].replace("/", os.sep))
        if not os.path.isfile(src):
            continue
        os.makedirs(os.path.dirname(dst) or root, exist_ok=True)
        journal.append_record(jp, batch, "undo", op["from"], op["to"],
                              op["hash_before"], op["hash_after"], int(time.time()))
        os.rename(src, dst)  # ③ 逆序回滚
        undone += 1
    return {"undone": undone, "stopped": None, "conflicts": []}


def build_index(root, rules):
    """分块索引：子块 512 / 重叠 64 / 父块连续 4 子块（冻结参数）；FTS5 全文 + 关键词备援。
    仅读文本类文件内容（索引本职）；其余格式留 adapter（core 不内置）。"""
    root = os.path.abspath(root)
    index_path = kb_path(root, INDEX_NAME)
    os.makedirs(kb_path(root, ""), exist_ok=True)
    conn = sqlite3.connect(index_path)
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5("
                     "content, file, page, parent_idx, tokenize='unicode61')")
        conn.execute("CREATE TABLE IF NOT EXISTS meta (file TEXT PRIMARY KEY, size INTEGER, mtime REAL)")
        conn.execute("DELETE FROM chunks")
        conn.execute("DELETE FROM meta")
        scan = manifest.scan_files(root)
        n_chunks = 0
        for rel in sorted(scan):
            if not _is_text_file(rel):
                continue
            full = os.path.join(root, rel.replace("/", os.sep))
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            pieces = split_text(text)
            for i, piece in enumerate(pieces):
                parent_idx = i // PARENT_GROUP
                conn.execute("INSERT INTO chunks VALUES (?,?,?,?)",
                             (piece, rel, 0, parent_idx))
                n_chunks += 1
            conn.execute("INSERT INTO meta VALUES (?,?,?)", (rel, scan[rel]["size"], scan[rel]["mtime"]))
        conn.commit()
        return {"index": index_path, "files": len(scan), "chunks": n_chunks}
    finally:
        conn.close()


def search_root(root, query, limit=8):
    """三路检索 + RRF 融合（只读名次不读分数，k=60）。结果带 [n] 引用编号，可回溯到文件与块。

    三路：语义路（本地嵌入端点缺席 ⇒ 静默跳过，不报错）/ 内容路（FTS5；索引缺席 ⇒ 本路跳过）/
    关键词路（文件名与路径 LIKE + 中文二元组，从不缺席）。"""
    from . import rrf as rrf_mod
    root = os.path.abspath(root)
    q = str(query or "").strip()
    channels = []

    # ① 语义路：开源版无内置嵌入端点 ⇒ 静默跳过（不报错，公开规范口径）
    # ② 内容路：FTS5（索引存在才有；2 字词退化为 LIKE）
    index_path = kb_path(root, INDEX_NAME)
    fts_hits = []
    if os.path.isfile(index_path) and q:
        try:
            conn = sqlite3.connect("file:%s?mode=ro" % index_path.replace("\\", "/"), uri=True)
            try:
                if len(q) <= 2:
                    rows = conn.execute(
                        "SELECT file, parent_idx, MIN(rowid) FROM chunks WHERE content LIKE ? "
                        "GROUP BY file, parent_idx LIMIT ?", ("%" + q + "%", limit)).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT file, parent_idx, MIN(rowid) FROM chunks WHERE chunks MATCH ? "
                        "GROUP BY file, parent_idx LIMIT ?", (q, limit)).fetchall()
                for rank, (f, pi, _rid) in enumerate(rows, start=1):
                    fts_hits.append({"file_id": rank, "name": f, "folder": "fts",
                                     "parentText": "%s#%s" % (f, pi)})
            finally:
                conn.close()
        except sqlite3.Error:
            fts_hits = []  # 索引损坏 ⇒ 本路跳过（降级不崩溃）
    channels.append({"name": "fts", "hits": fts_hits})

    # ③ 关键词路：文件名/路径 LIKE + 中文二元组
    kw_hits = []
    if q:
        bigrams = [q[i:i + 2] for i in range(len(q) - 1)] or [q]
        scan = manifest.scan_files(root)
        scored = []
        for rel in scan:
            hay = rel.lower()
            score = 3 if q.lower() in hay else (2 if any(b in hay for b in bigrams) else 0)
            if score:
                scored.append((score, rel))
        scored.sort(key=lambda x: (-x[0], x[1]))
        for rank, (_s, rel) in enumerate(scored[:limit], start=1):
            kw_hits.append({"file_id": 100000 + rank, "name": rel, "folder": "keyword"})
    channels.append({"name": "keyword", "hits": kw_hits})

    fused = rrf_mod.fuse(channels, None, {"k": 60})
    results = []
    for i, item in enumerate(fused[:limit], start=1):
        results.append({
            "n": i,
            "file": item.get("name") or "",
            "source": item.get("folder") or "",
            "score": item.get("score"),
            "ref": "[%d] %s" % (i, item.get("name") or ""),
        })
    return {"query": q, "channels": {c["name"]: len(c["hits"]) for c in channels},
            "results": results}


def explain_path(root, target):
    """回答「这个文件为什么被放在这里」：读 manifest.reason（人话字段，永远保留且明文）。"""
    root = os.path.abspath(root)
    mf_path = kb_path(root, MANIFEST_NAME)
    rel = str(target).replace("\\", "/").strip("/")
    if os.path.isfile(mf_path):
        mf = manifest.load_manifest(mf_path)
        rec = mf.get("files", {}).get(rel)
        if rec:
            return {"found": True, "file": rel, "kb": rec.get("kb"), "target": rec.get("target"),
                    "confidence": rec.get("confidence"), "reason": rec.get("reason"),
                    "settled": rec.get("settled", False)}
    p_path = kb_path(root, PLAN_NAME)
    if os.path.isfile(p_path):
        p = plan_mod.load_plan(p_path)
        for item in p.get("inbox", []):
            if item.get("from") == rel:
                return {"found": True, "file": rel, "kb": None, "target": "@inbox",
                        "confidence": None, "reason": item.get("reason"),
                        "candidates": item.get("candidates", []), "settled": False}
    return {"found": False, "file": rel,
            "reason": "台账与方案里都没有该文件的判定记录（可能从未 plan 过）"}


def scan_report(root):
    """只读扫描读数：乱度指标（顶层散件数/扩展名分布/目录深度）＋ 候选库清单（标记文件探测）。
    不读文件内容，只元数据。"""
    root = os.path.abspath(root)
    scan = manifest.scan_files(root)
    top_files = 0
    ext_dist = {}
    depth_max = 0
    top_dirs = set()
    for rel, meta in scan.items():
        parts = [p for p in rel.split("/") if p]
        depth_max = max(depth_max, len(parts))
        if len(parts) == 1:
            top_files += 1
        else:
            top_dirs.add(parts[0])
        ext = os.path.splitext(rel)[1].lower() or "(无扩展名)"
        ext_dist[ext] = ext_dist.get(ext, 0) + 1
    markers_found = []
    rules_dict, _text, _src = load_rules(None, demo=True)
    for kb in rules_dict.get("kbs", []):
        for m in kb.get("detect", {}).get("markers", []):
            for rel in scan:
                if rel.endswith("/" + m) or rel == m:
                    markers_found.append({"kb": kb.get("id"), "marker": m, "file": rel})
    clutter = round(top_files / max(1, len(scan)) * 100, 1)
    return {"root": root.replace("\\", "/"), "files": len(scan), "top_dirs": len(top_dirs),
            "top_files": top_files, "max_depth": depth_max, "clutter_pct": clutter,
            "ext_top": sorted(ext_dist.items(), key=lambda x: -x[1])[:8],
            "candidate_kbs": markers_found}


def audit_self(repo_root):
    """零外连自证：① 逐文件 import 清单；② 禁网模块扫描（命中即非零退出）；③ 删除动作零命中
    （全仓源码扫「un」「link」拼接串与 os.replace 之外的写法面）；④ 结论行。

    2026-11-05 YQ-1154 D2 起：反馈上行件（_SANCTIONED_NETWORK_FILES）为**唯一**网络面
    豁免——opt-in（默认关）且发送前逐条展示确认；豁免面单独报出（sanctioned_network），
    其余文件命中禁网模块仍即自证失败。"""
    repo_root = os.path.abspath(repo_root)
    imports = {}
    violations_net = []
    violations_del = []
    sanctioned = {}                                    # rel -> [module, ...]（唯一豁免面）
    needle = _DEL_A + _DEL_B
    for sub in _AUDIT_SCAN_ROOTS:
        base = os.path.join(repo_root, sub.replace("/", os.sep))
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for fn in filenames:
                if not fn.endswith((".py", ".js", ".mjs")):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, repo_root).replace("\\", "/")
                try:
                    with open(full, "r", encoding="utf-8", errors="replace") as f:
                        src = f.read()
                except OSError:
                    continue
                mods = sorted({m.split(".")[0] for m in _IMPORT_RE.findall(src)})
                imports[rel] = mods
                for m in mods:
                    if m in _NETWORK_MODULES or (m + ".").startswith(tuple(x + "." for x in _NETWORK_MODULES if "." in x)):
                        if rel in _SANCTIONED_NETWORK_FILES:
                            sanctioned.setdefault(rel, []).append(m)
                        else:
                            violations_net.append({"file": rel, "module": m})
                if needle in src:
                    violations_del.append(rel)
    stdlib_ok = True
    if violations_net or violations_del:
        stdlib_ok = False
        verdict = "自证失败：存在禁网模块或删除动作命中（见明细）"
    elif sanctioned:
        verdict = ("默认路径零外连；唯一网络面＝反馈上行件（opt-in，默认关闭）：%s；删除动作零命中"
                   % ", ".join(sorted(sanctioned)))
    else:
        verdict = "零外连：core 与 hosts 的 import 清单内无任何网络模块；删除动作零命中"
    return {"ok": stdlib_ok, "verdict": verdict,
            "network_violations": violations_net,
            "delete_violations": violations_del,
            "sanctioned_network": [{"file": rel, "modules": mods}
                                   for rel, mods in sorted(sanctioned.items())],
            "imports": imports}
