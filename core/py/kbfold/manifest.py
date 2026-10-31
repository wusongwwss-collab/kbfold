# -*- coding: utf-8 -*-
"""kbfold.manifest — 指纹台账（增量与移动识别唯一依据）＋ 四态比对（纯判定面 + JSON 读写）。

库格式 v0.1 冻结口径（字段只增不删不改义；不认识的 version 报错退出，不做猜测性兼容）：
  manifest.json = {version:1, pid, rules_locked, files:{"<相对路径>":
      {size, mtime, hash, kb, target, confidence, reason, settled}}}

增量六步：扫描 → 指纹 → 比对 manifest → 只对变化集判定 → 只动差异的投影与索引 → 原子提交。
指纹先比 size + mtime，只有这俩变了才去算 SHA-256（十万文件上把 IO 压下来的关键口径）。
变化四态：新增 / 变更（同路径 hash 变了）/ 移动（同 hash 新路径，只改链接不重解析）/
消失（有记录盘上没有 ⇒ 打 tombstone，绝不删索引，可撤回）。
防抖两机制：settled 终态集（已定案文件后续批次不再判定）；组合键幂等
（batch = hash(规则版本 + 变化集)，同一规则同一批变化重跑不产生第二批动作）。

分片存储：按顶层目录切片是大目录上的实现面优化；v0.1 读写器为单文件形态，
字段只增不删（读方遇到不认识的 version 报错退出）。
"""
from __future__ import annotations

import hashlib
import json
import os

MANIFEST_VERSION = 1


class ManifestError(Exception):
    """manifest 格式错误（含不认识版本：报错退出，不做猜测性兼容）。"""


def hash_file(path):
    """SHA-256 文件指纹（惰性：仅在 size+mtime 变化时调用）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return "sha256:" + h.hexdigest()


def hash_text(text):
    """文本指纹（组合键幂等用）。"""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def scan_files(root, exclude_dirs=(".kb", ".kbfold")):
    """只读扫描：walk 收集相对路径与元数据（不读文件内容）。
    返回 {relpath: {"size": int, "mtime": float}}（POSIX 风格相对路径）。"""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace("\\", "/")
        if rel_dir == ".":
            dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
        elif any(part in exclude_dirs for part in rel_dir.split("/")):
            dirnames[:] = []
            continue
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = (fn if rel_dir == "." else rel_dir + "/" + fn)
            try:
                st = os.stat(full)
            except OSError:
                continue  # 扫描间隙被移走的文件：如实跳过
            out[rel] = {"size": int(st.st_size), "mtime": float(st.st_mtime)}
    return out


def diff_manifest(old_files, new_scan, root):
    """四态比对（新增/变更/移动/消失）。

    old_files：manifest 的 files 段（含 size/mtime/hash）；new_scan：scan_files 产物。
    返回 {"added":{rel:scan}, "changed":{rel:hash}, "moved":{new_rel:old_rel},
          "disappeared":{rel:rec}, "unchanged_count":int}
    变更判据：size 或 mtime 任一变了才计算 SHA-256（惰性指纹口径）。
    """
    added = {}
    changed = {}
    disappeared = {}
    unchanged = 0
    for rel, rec in (old_files or {}).items():
        scan = (new_scan or {}).get(rel)
        if scan is None:
            disappeared[rel] = rec
            continue
        if rec.get("size") == scan["size"] and rec.get("mtime") == scan["mtime"]:
            unchanged += 1
            continue
        full = os.path.join(root, rel.replace("/", os.sep))
        new_hash = hash_file(full) if os.path.isfile(full) else None
        if new_hash is not None and new_hash == rec.get("hash"):
            unchanged += 1  # 内容未变（触碰了时间戳）：不进变化集
            continue
        changed[rel] = new_hash or ""
    for rel in (new_scan or {}):
        if rel not in (old_files or {}):
            added[rel] = new_scan[rel]

    # 「移动」= 同 hash 新路径（最值钱的一态：只改链接，不重解析）
    moved = {}
    if disappeared and added:
        disp_hashes = {}
        for rel, rec in disappeared.items():
            disp_hashes.setdefault(rec.get("hash"), []).append(rel)
        add_hashes = {}
        for rel in list(added.keys()):
            full = os.path.join(root, rel.replace("/", os.sep))
            if os.path.isfile(full):
                add_hashes.setdefault(hash_file(full), []).append(rel)
        for h, old_rels in disp_hashes.items():
            new_rels = add_hashes.get(h)
            if not new_rels:
                continue
            old_rels = sorted(old_rels)
            new_rels = sorted(new_rels)
            for old_rel, new_rel in zip(old_rels, new_rels):
                moved[new_rel] = old_rel
                added.pop(new_rel, None)
                disappeared.pop(old_rel, None)
    return {"added": added, "changed": changed, "moved": moved,
            "disappeared": disappeared, "unchanged_count": unchanged}


def load_manifest(path):
    """读 manifest.json；不认识的 version ⇒ ManifestError（报错退出口径）。"""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ManifestError("manifest 顶层必须是对象")
    if data.get("version") != MANIFEST_VERSION:
        raise ManifestError(
            "不认识的 manifest 版本：%r（当前只支持 version: %d，不做猜测性兼容）"
            % (data.get("version"), MANIFEST_VERSION))
    if not isinstance(data.get("files"), dict):
        raise ManifestError("manifest.files 必须是对象")
    return data


def dump_manifest(path, manifest):
    """写 manifest.json（原子：临时文件 + 替换；不动任何用户文件）。"""
    if manifest.get("version") != MANIFEST_VERSION:
        raise ManifestError("manifest.version 必须为 %d" % MANIFEST_VERSION)
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)


def make_batch_id(rules_locked, changeset_key, ts):
    """组合键幂等：batch = hash(规则版本 + 变化集)；格式 YYYYMMDD-HHMM-<hash8>（同账本样例）。"""
    digest = hash_text(str(rules_locked) + "|" + str(changeset_key))
    stamp = None
    try:
        import time
        stamp = time.strftime("%Y%m%d-%H%M", time.localtime(ts))
    except Exception:
        stamp = "00000000-0000"
    return "%s-%s" % (stamp, digest.replace("sha256:", "")[:8])


def collect_signals(rules, rel, new_scan_entry, root):
    """为单文件采集判定信号（S1 标记 / L1a 名称全等 / L3 词表），只读元数据不读内容。

    返回 (marker_kb, name_hits, keyword_hits)。S1：同目录（及向上目录）存在某库
    detect.markers 里的标记文件 ⇒ 该库胜；名称全等：路径某段 == 库名；词表：路径文本
    命中 detect.keywords。"""
    kbs = [k for k in (rules.get("kbs") if isinstance(rules.get("kbs"), list) else [])
           if isinstance(k, dict)]
    marker_kb = None
    segments = [s for s in str(rel).replace("\\", "/").strip("/").split("/") if s]
    # S1：沿路径各级目录找标记文件
    for i in range(len(segments) - 1, -1, -1):
        dir_rel = "/".join(segments[:i])
        for kb in kbs:
            detect = kb.get("detect") if isinstance(kb.get("detect"), dict) else {}
            markers = detect.get("markers") if isinstance(detect.get("markers"), list) else []
            for m in markers:
                marker_rel = (dir_rel + "/" + str(m)) if dir_rel else str(m)
                full = os.path.join(root, marker_rel.replace("/", os.sep))
                if os.path.isfile(full):
                    marker_kb = str(kb.get("id") or "")
                    break
            if marker_kb:
                break
        if marker_kb:
            break
    name_hits = []
    keyword_hits = {}
    for kb in kbs:
        name = str(kb.get("name") or "").strip()
        if name and name in segments:
            name_hits.append(str(kb.get("id") or ""))
        detect = kb.get("detect") if isinstance(kb.get("detect"), dict) else {}
        kws = detect.get("keywords") if isinstance(detect.get("keywords"), list) else []
        hits = [str(k) for k in kws if str(k) and str(k) in str(rel)]
        if hits:
            keyword_hits[str(kb.get("id") or "")] = hits
    return marker_kb, name_hits, keyword_hits
