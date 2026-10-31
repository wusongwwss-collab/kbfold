# -*- coding: utf-8 -*-
"""未知软件目录候选 · 分类与规则生成（纯逻辑，零网络、零 DB）。

Py→Py 整件搬运（唯一一处 Py→Py 搬运件；搬运正本＝需求包 _assets 快照 dir_classify.py，
本件除文件头说明外与底本逐行等价，parity 机闸逐行对照）。

职责：把端侧上报的候选（已脱敏）分类成「软件类型」，按分档表决定处置
（整棵排除 / 特殊区 / 只挂链 / 待人工），并生成 exclude.items 规则条目。

拍板口径：
  · 高置信自动 + 其余人工：置信 ≥0.9 且类型在硬排除表内 ⇒ 可自动并入草稿并发布；其余进人工队列；
  · 只回传「软件安装登记 + 结构指纹」候选（本模块只消费，不再放宽）；
  · 类型分档：知识库/代码仓库/AI/密码/虚拟机/数据库/备份/财务/游戏/浏览器 ⇒ 排除；
    聊天 ⇒ 特殊区（只收文档）；网盘 ⇒ 只挂链；other ⇒ 待人工；
  · 代码仓库自动扫描排除，手工登记保留（与本模块无关，本模块只产规则）。

安全红线：
  1. **模型不得决定规则值**：value 一律取候选目录名（经安全名校验），模型给的 value 忽略；
  2. 通用目录名（data/config/cache/app…）一律不许进规则（会误排用户资料），只进待人工；
  3. 规则只增不减：已存在的同值条目不动、用户手动禁用的条目不自动启用；
  4. 单值 ≤200 字符（服务端校验上限）、category ≤30、note ≤100。

用法：
    cls = classify_by_vocab(cand) or parse_llm_result(llm_text)
    plan = decide(cand, cls)
    if should_auto_adopt(cand, cls, plan): rules, added = merge_rule_items(rules, [plan["item"]])
"""
from __future__ import annotations

import json
import re
from typing import Any

# ---------------------------------------------------------------------------
# 一、类型词表与分档表（拍板 §6.3）
# ---------------------------------------------------------------------------
TYPES = (
    "knowledge_base", "code_repo", "ai_tool", "chat", "cloud_sync", "password",
    "finance", "vm", "db", "backup", "game", "browser", "system", "other",
)

# 处置：exclude=整棵排除；special_zone=特殊区（只收文档）；mount_only=只挂链；pending=人工
TYPE_POLICY: dict[str, str] = {
    "knowledge_base": "exclude",
    "code_repo": "exclude",
    "ai_tool": "exclude",
    "password": "exclude",
    "vm": "exclude",
    "db": "exclude",
    "backup": "exclude",
    "finance": "exclude",
    "game": "exclude",
    "browser": "exclude",
    "system": "exclude",
    "chat": "special_zone",
    "cloud_sync": "mount_only",
    "other": "pending",
}

# 硬排除类型：只有这些类型允许高置信自动发布（聊天/网盘/other 永不自动）
HARD_EXCLUDE_TYPES = frozenset(t for t, a in TYPE_POLICY.items() if a == "exclude")

# 类型 → 规则分类标签（≤30 字）
TYPE_CATEGORY = {
    "knowledge_base": "知识库工具",
    "code_repo": "代码仓库与工具链",
    "ai_tool": "AI工具数据",
    "password": "隐私敏感",
    "vm": "虚拟机与容器",
    "db": "数据库目录",
    "backup": "备份快照",
    "finance": "财务默认",
    "game": "游戏与媒体",
    "browser": "浏览器数据",
    "system": "系统目录",
}

AUTO_ADOPT_MIN_CONFIDENCE = 0.9

# 通用目录名：**绝不进规则**（会误排用户资料），哪怕模型判成硬排除类型
# （KBFold 规则 DSL 的启动黑名单沿用本表同一份，不另抄一份——公开规范承诺口径。）
GENERIC_NAMES = frozenset({
    "data", "config", "configs", "cache", "caches", "temp", "tmp", "app", "apps",
    "user", "users", "files", "file", "docs", "documents", "desktop", "downloads",
    "backup", "backups", "log", "logs", "db", "database", "storage", "store",
    "src", "source", "bin", "lib", "obj", "out", "output", "build", "dist",
    "项目", "资料", "文档", "备份", "数据", "配置", "缓存", "临时",
})

# 本地词表（免费优先；未命中才调模型）。全部小写子串匹配。
VOCAB: dict[str, tuple[str, ...]] = {
    "knowledge_base": (
        "obsidian", "logseq", "siyuan", "思源", "dendron", "trilium", "joplin", "notion",
        "evernote", "印象笔记", "wiznote", "为知笔记", "youdaonote", "有道云笔记", "语雀",
        "yuque", "onenote", "zotero", "anki", "tiddlywiki", "foam", "知识库", "笔记",
    ),
    "code_repo": (
        "git", "svn", "mercurial", "idea", "pycharm", "webstorm", "clion", "goland",
        "rubymine", "phpstorm", "rider", "androidstudio", "android studio", "xcode",
        "vscode", "eclipse", "netbeans", "source", "repos", "repository",
    ),
    "ai_tool": (
        "zcode", "workbuddy", "trae", "codebuddy", "codex", "cursor", "windsurf",
        "claude", "gemini", "qwen", "qoder", "doubao", "豆包", "dsh", "deepseek",
        "ollama", "lm studio", "lmstudio", "cherry studio", "chatbox", "lobechat",
        "anythingllm", "comfyui", "stable diffusion", "modelscope", "huggingface",
        "hermes", "hindsight", "mimosa",
    ),
    "chat": (
        "wechat", "weixin", "微信", "qq", "dingtalk", "钉钉", "feishu", "lark", "飞书",
        "telegram", "whatsapp", "slack", "discord", "skype",
    ),
    "cloud_sync": (
        "onedrive", "dropbox", "baidu", "百度网盘", "坚果云", "nutstore", "syncthing",
        "icloud", "google drive", "微云", "阿里云盘", "夸克网盘",
    ),
    "password": ("keepass", "1password", "bitwarden", "lastpass", "密码", "凭据", "password"),
    "vm": ("vmware", "virtualbox", "hyper-v", "docker", "wsl", "虚拟机", "qemu", "parallels"),
    "db": ("postgres", "mysql", "redis", "mongodb", "sqlite", "数据库", "pgdata"),
    "backup": ("backup", "备份", "snapshot", "快照", "dump"),
    "finance": ("报销", "财务", "工资", "发票", "税务", "账"),
    "game": ("steam", "epic games", "游戏", "wegame", "battle.net"),
    "browser": ("chrome", "edge", "firefox", "浏览器", "brave", "opera"),
    "system": ("windows", "system32", "program files", "programdata", "driver", "驱动"),
}

# ---------------------------------------------------------------------------
# 二、上报体脱敏（服务端强校验；不符即丢条）
# ---------------------------------------------------------------------------
_ROOT_HINTS = frozenset({
    "appdata_roaming", "appdata_local", "appdata_other", "programdata",
    "documents", "userprofile", "drive_root", "other",
})
_BUCKETS = frozenset({"0-9", "10-100", "101-1000", ">1000"})
_FP_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_NAME_RE = re.compile(r"^[^\x00-\x1f\\/:*?\"<>|]{1,80}$")
_MARKER_RE = re.compile(r"^[\w.*\- ]{1,40}$", re.UNICODE)
_EXT_RE = re.compile(r"^\.[a-z0-9]{1,10}$")


def _s(v: Any, cap: int) -> str:
    if not isinstance(v, str):
        return ""
    return v.strip()[:cap]


def sanitize_candidate(raw: Any) -> dict | None:
    """单条候选脱敏：白名单键外一律丢弃；形状不合返回 None。

    额外硬闸：路径形状里出现真实用户名（`X:\\Users\\<非%开头>`）⇒ 丢弃（防端侧脱敏失效）。
    """
    if not isinstance(raw, dict):
        return None
    fp = _s(raw.get("fp"), 64).lower()
    if not _FP_RE.match(fp):
        return None
    name = _s(raw.get("name"), 80)
    if not name or not _SAFE_NAME_RE.match(name):
        return None
    shape = _s(raw.get("path_shape"), 180)
    if not shape:
        return None
    if re.search(r"[A-Za-z]:\\Users\\[^%]", shape):
        return None                                  # 未脱敏：含真实用户目录
    hint = _s(raw.get("root_hint"), 30).lower()
    if hint not in _ROOT_HINTS:
        hint = "other"
    ev = raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {}
    reg = ev.get("registry") if isinstance(ev.get("registry"), dict) else None
    registry = None
    if reg:
        registry = {
            "display_name": _s(reg.get("display_name"), 80),
            "publisher": _s(reg.get("publisher"), 60),
            "version": _s(reg.get("version"), 30),
        }
    markers = []
    for m in (ev.get("markers") if isinstance(ev.get("markers"), list) else []):
        s = _s(m, 40)
        if s and _MARKER_RE.match(s):
            markers.append(s)
    ext_top = []
    for e in (ev.get("ext_top") if isinstance(ev.get("ext_top"), list) else []):
        s = _s(e, 12).lower()
        if s and _EXT_RE.match(s):
            ext_top.append(s)
    bucket = _s(ev.get("file_count_bucket"), 12)
    if bucket not in _BUCKETS:
        bucket = "0-9"
    first_seen = _s(raw.get("first_seen"), 40)
    return {
        "fp": fp,
        "name": name,
        "path_shape": shape,
        "root_hint": hint,
        "evidence": {
            **({"registry": registry} if registry else {}),
            "markers": markers[:8],
            "ext_top": ext_top[:3],
            "file_count_bucket": bucket,
        },
        "first_seen": first_seen,
    }


# ---------------------------------------------------------------------------
# 三、分类：本地词表 → （未命中时）模型
# ---------------------------------------------------------------------------
def _candidate_text(c: dict) -> str:
    ev = c.get("evidence") or {}
    reg = ev.get("registry") or {}
    return " ".join([
        str(c.get("name") or ""),
        str(c.get("path_shape") or ""),
        str(reg.get("display_name") or ""),
        str(reg.get("publisher") or ""),
    ]).lower()


def classify_by_vocab(c: dict) -> dict | None:
    """本地词表分类；命中返回 {type, confidence, reason, source}，否则 None。"""
    text = _candidate_text(c)
    if not text:
        return None
    for typ, words in VOCAB.items():
        for w in words:
            if w and w in text:
                return {
                    "type": typ,
                    "confidence": 0.92,
                    "reason": "词表命中：" + w,
                    "source": "vocab",
                }
    return None


LLM_SYSTEM_PROMPT = (
    "你是软件目录分类器。只输出一个 JSON 对象，不要任何解释或代码块标记。\n"
    "字段：type（枚举）、confidence（0-1 小数）、should_exclude（布尔）、reason（≤60 字）。\n"
    "type 枚举：knowledge_base, code_repo, ai_tool, chat, cloud_sync, password, finance, "
    "vm, db, backup, game, browser, system, other。\n"
    "判定依据只有：目录名、占位化路径、安装登记（显示名/发行商）、结构标记、扩展名分布。\n"
    "拿不准就给 other 且 confidence<0.6；不要编造。"
)


def build_llm_prompt(c: dict) -> str:
    """构造模型输入：只含脱敏后的候选字段（无路径明文/无文件名/无内容）。"""
    return LLM_SYSTEM_PROMPT + "\n候选：" + json.dumps(c, ensure_ascii=False, sort_keys=True)


def parse_llm_result(text: Any, candidate: dict | None = None) -> dict | None:
    """解析模型输出；非 JSON/枚举不合/置信越界 ⇒ None（绝不半信半疑地采纳）。

    安全：模型给的 `value` 一律忽略——规则值只能取候选目录名（见 build_rule_item）。
    """
    if not isinstance(text, str):
        return None
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        d = json.loads(s)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    typ = str(d.get("type") or "").strip().lower()
    if typ not in TYPES:
        return None
    try:
        conf = float(d.get("confidence"))
    except Exception:
        return None
    if not (0.0 <= conf <= 1.0):
        return None
    reason = _s(d.get("reason"), 120)
    return {
        "type": typ,
        "confidence": conf,
        "should_exclude": bool(d.get("should_exclude")),
        "reason": reason,
        "source": "llm",
    }


# ---------------------------------------------------------------------------
# 四、处置与规则生成
# ---------------------------------------------------------------------------
def is_safe_rule_name(name: Any) -> bool:
    """规则值安全名：非空、非通用词、无路径分隔符/控制字符。"""
    s = str(name or "").strip()
    if not s or s.lower() in GENERIC_NAMES:
        return False
    if not _SAFE_NAME_RE.match(s):
        return False
    return len(s) >= 2


def build_rule_item(c: dict, cls: dict) -> dict | None:
    """生成 exclude.items 条目；名称不安全/类型无分类标签 ⇒ None。"""
    typ = str((cls or {}).get("type") or "")
    cat = TYPE_CATEGORY.get(typ)
    name = str((c or {}).get("name") or "").strip()
    if not cat or not is_safe_rule_name(name):
        return None
    return {
        "value": name,
        "match": "dirname",
        "layer": "dir",
        "category": cat,
        "note": ("自动识别：" + typ)[:100],
        "enabled": True,
    }


def decide(c: dict, cls: dict | None) -> dict:
    """按分档表产出处置计划。

    返回 {action: exclude|special_zone|mount_only|pending|reject, item?, reason}
    """
    if not cls or cls.get("type") not in TYPES:
        return {"action": "pending", "reason": "未分类"}
    typ = cls["type"]
    policy = TYPE_POLICY.get(typ, "pending")
    if policy != "exclude":
        return {"action": policy, "reason": "分档表：" + typ}
    item = build_rule_item(c, cls)
    if not item:
        return {"action": "pending", "reason": "名称不安全或类型无分类标签"}
    return {"action": "exclude", "item": item, "reason": "分档表：" + typ}


def should_auto_adopt(c: dict, cls: dict | None, plan: dict | None) -> bool:
    """高置信自动发布的判据（拍板 §1）：≥0.9 + 硬排除类型 + 计划为 exclude + 安全名。"""
    if not cls or not plan or plan.get("action") != "exclude":
        return False
    if cls.get("type") not in HARD_EXCLUDE_TYPES:
        return False
    try:
        conf = float(cls.get("confidence") or 0)
    except Exception:
        return False
    return conf >= AUTO_ADOPT_MIN_CONFIDENCE and bool(plan.get("item"))


def merge_rule_items(rules: Any, items: Any) -> tuple[dict, list]:
    """把条目并入规则体（**只增不减**）。

    - 同 (match, value 小写) 已存在 ⇒ 跳过（不覆盖分类/说明，也不重新启用被手动禁用的条目）；
    - 返回新规则对象与本次真正新增的条目列表；形状异常 ⇒ 原样返回、零新增。
    """
    if not isinstance(rules, dict):
        return rules, []
    ex = rules.get("exclude")
    if not isinstance(ex, dict):
        return rules, []
    existing = ex.get("items")
    if not isinstance(existing, list):
        return rules, []
    seen = set()
    for it in existing:
        if isinstance(it, dict):
            seen.add((str(it.get("match") or "").lower(), str(it.get("value") or "").strip().lower()))
    added = []
    new_items = list(existing)
    for it in (items if isinstance(items, list) else []):
        if not isinstance(it, dict):
            continue
        key = (str(it.get("match") or "").lower(), str(it.get("value") or "").strip().lower())
        if not key[1] or key in seen:
            continue
        seen.add(key)
        new_items.append(dict(it))
        added.append(dict(it))
    if not added:
        return rules, []
    out = json.loads(json.dumps(rules, ensure_ascii=False))
    out["exclude"]["items"] = new_items
    return out, added
