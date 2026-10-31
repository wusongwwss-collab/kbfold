# -*- coding: utf-8 -*-
"""feedback — KBFold 反馈 SDK（Python 侧；口径正本 = docs/FEEDBACK.md）。

定位（docs/FEEDBACK.md §1-§4 三档定义，逐条落实）：
  反馈默认关（L0=零网络调用）、首启问一次（should_ask_consent）、随时可关。
  三档：L1=被拒绝的归档建议三态点选（采纳/否决/改判，附信号级别与置信区间，
  不含路径与文件名）；L2=加模板命中率计数 + 规则 delta（字段白名单）+ 目录结构
  hash（目录名先归一化成类型槽再 sha256 截断 16 位，只上行 hash 不上行原始名）；
  L3=加本地脱敏失败样例（先槽位化再入载荷）。

职责边界（与 quickcue feedback.py 同形，拷改底本）：
  - 本模块只做「档位状态机 + 本地落盘 + 上行预检与脱敏」，**不内置任何 UI、
    不内置任何网络调用**（零依赖之外不出网）；上行动作归宿主/云端侧。
  - 本地落盘路径：``.kbfold/feedback.jsonl``（相对当前工作目录；目录可用环境
    变量 ``KBFOLD_FEEDBACK_DIR`` 覆盖，兼容任务书原拼 ``KBOLD_FEEDBACK_DIR``）。
    反馈目录独立于 ``.kb/``（删除该目录即回到「从未询问」初态，见 FEEDBACK.md §4）。
  - ``sanitize`` 把中文语境 PII 槽位化（手机号/金额/人名/公司名/邮箱 五类正则槽
    ＋目录名归一化类型槽 日期/合同/编号/通用名，合称六类槽位），原文永不出本地；
    ``prepare_upload`` 对最终载荷做原文残留检测（未槽位化的手机号/邮箱模式命中
    即拒绝上行，宁少上行一条不漏上行一段原文）。

接口面：
  - should_ask_consent(state) -> bool     纯函数：consent_asked 未置位 -> True；
                                          宿主据此问一次并置位，之后永不再问。
  - sanitize(text, slots=None) -> str     本地 PII 槽位化（slots 跨多次调用共享序号）。
  - detect_residue(payload) -> list[str]  原文残留检测（返回命中的 PII 类型列表）。
  - dir_structure_hash(dir_names) -> str  目录树归一化指纹（sha256 截断 16 hex）。
  - FeedbackSdk(level="L0", feedback_dir=None, package_version=...).record(level, payload)
  - FeedbackSdk(...).prepare_upload(level, payload)
  - load_state(feedback_dir) / save_state(feedback_dir, state)  首启问一次/档位持久化。

零第三方依赖：仅 Python 标准库（re / json / os / time / hashlib / datetime）。
"""

import hashlib
import json
import os
import re
import time
from datetime import datetime

SDK_SCHEMA = "kbfold.feedback/v1"
SDK_VERSION = "0.1.0"

# 档位：L0=关（默认）；L1/L2/L3 逐档叠加（高档位内容包含低档位内容）
LEVEL_ORDER = {"L0": 0, "L1": 1, "L2": 2, "L3": 3}

FEEDBACK_ENV_DIR = "KBFOLD_FEEDBACK_DIR"
FEEDBACK_ENV_DIR_ALT = "KBOLD_FEEDBACK_DIR"  # 任务书原文拼写，兼容读

FEEDBACK_STATE_FILE = "state.json"
FEEDBACK_LOG_FILE = "feedback.jsonl"

# L2 规则 delta 只允许这些字段（结构层面保证「不含话术原文」）
_RULE_DELTA_KEYS = ("rule_id", "field", "before", "after")

# L1 三态点选值白名单（docs/FEEDBACK.md §1：采纳 / 否决 / 改判）
_RESULT_VALUES = ("采纳", "否决", "改判")

# L1 判定信号摘要白名单：四层信号 S1-S4（判定梯四层信号口径）
_SIGNAL_LEVELS = ("S1", "S2", "S3", "S4")
# 置信区间：数字/区间符号/空白组成的短串（如 "0.85-0.95"），其余形态一律丢弃
_CONFIDENCE_RE = re.compile(r"^[0-9.+\-~<>= ]{1,16}$")

# L2 模板命中率：纯计数面（键=模板/通道 id，值=非负整数计数）
_TEMPLATE_HITS_MAX_KEYS = 50
_TEMPLATE_KEY_CAP = 64


# ------------------------------------------------------------------
# 首启问一次（纯函数，无 UI、无副作用）
# ------------------------------------------------------------------
def should_ask_consent(state):
    """首启问一次状态机：state 由宿主持久化，含 consent_asked 标志。

    - state 缺失/非 dict/无 consent_asked 键/为假 -> True（该问了）
    - consent_asked 为真 -> False（问过一次，永不再问）
    置位动作归宿主（问完后由宿主把 state["consent_asked"] 置 True 落盘，
    或直接用 save_state 落 state.json）。
    """
    if not isinstance(state, dict):
        return True
    return not bool(state.get("consent_asked"))


def default_feedback_dir():
    """落盘目录：KBFOLD_FEEDBACK_DIR（兼容 KBOLD_FEEDBACK_DIR）优先，否则 <cwd>/.kbfold。"""
    env = os.environ.get(FEEDBACK_ENV_DIR) or os.environ.get(FEEDBACK_ENV_DIR_ALT)
    return env or os.path.join(os.getcwd(), ".kbfold")


def load_state(feedback_dir=None):
    """读反馈状态（档位/consent_asked）；无 state.json 或读坏 -> {}（初态）。"""
    base = feedback_dir or default_feedback_dir()
    path = os.path.join(base, FEEDBACK_STATE_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state, feedback_dir=None):
    """落反馈状态（宿主问完一次后置 consent_asked=True、或 feedback on/off 时用）。"""
    if not isinstance(state, dict):
        raise ValueError("state 须为对象")
    base = feedback_dir or default_feedback_dir()
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, FEEDBACK_STATE_FILE)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    return path


# ------------------------------------------------------------------
# 本地 PII 槽位化（中文语境正则；同槽去重：<PHONE_1>/<PHONE_2> …）
# （照拷 quickcue feedback.py：五类文本 PII 槽 + 目录名类型槽，合称六类）
# ------------------------------------------------------------------
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# 手机号：11 位连写，或 3-4-4 带空格/连字符分隔
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)|(?<!\d)1[3-9]\d{2}[ \-]\d{4}[ \-]\d{4}(?!\d)")
# 金额：¥/￥ 前缀，或数字 + 元/块/万/亿 后缀（中文语境，允许数字与单位间空白）
_AMOUNT_RE = re.compile(
    r"(?:[￥¥]\s*\d[\d,，]*(?:\.\d+)?(?:万|千万|百万|亿)?)"
    r"|(?:\d[\d,，]*(?:\.\d+)?\s*(?:万|千万|百万|亿)?\s*(?:元|块钱?))"
    r"|(?:\d[\d,，]*(?:\.\d+)?\s*(?:万|亿)(?![\u4e00-\u9fa5A-Za-z0-9]))"
)
# 公司/机构：实体名 + 常见机构后缀
_ORG_RE = re.compile(r"[\u4e00-\u9fa5A-Za-z0-9]{2,16}?(?:股份有限公司|有限公司|集团公司|集团|科技公司|银行|大学|医院|研究院)")
_ORG_SUFFIXES = ("股份有限公司", "集团公司", "有限公司", "科技公司", "研究院", "集团", "银行", "大学", "医院")
# 实体名部分出现这些功能词/动词 -> 收窄到其后（「让李娜联系甲科技有限公司」-> 甲科技有限公司）
_ORG_NAME_STOP = set("让请和与跟向对给找叫去来在由把被从至到是说讲问已曾还没不未正将想"
                     "联系告知提供接待负责表示认为建议要求协助沟通对接汇报")
# 人名：常见姓氏 + 1~2 个汉字（启发式）
_SURNAMES = ("王李张刘陈杨黄赵吴周徐孙马朱胡郭何林罗高郑梁谢宋唐许韩冯邓曹彭"
             "曾肖田董潘袁蔡蒋余于杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖"
             "贾夏韦付方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱"
             "严覃武戴莫孔向汤")
_NAME_RE = re.compile(r"[" + _SURNAMES + r"][\u4e00-\u9fa5]{1,2}")
# 姓氏后紧跟这些字视为非人名（「马上」「付款」等常用词误报抑制）
_NAME_FOLLOW_STOP = set("上下来去的是在和与说会能要不很路天年月日点次个中时为款货钱费单")
# 人名尾部这些虚词/动词回退掉（「张伟说」-> 槽位化「张伟」留「说」）
_NAME_TRAILING_TRIM = set("说的了是在和与等吗呢吧啊对不对也很但就都又还把被向往联系")

_PII_ORDER = ("email", "phone", "amount", "org", "name")
_PII_SLOT = {"email": "EMAIL", "phone": "PHONE", "amount": "AMOUNT", "org": "ORG", "name": "PERSON"}

# 残留检测：只押高精度模式（手机号 / 邮箱）——槽位化对这两类是确定性的，
# 因此最终载荷里再出现即视为「原文残留」，直接拒绝上行。
_RESIDUE_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_RESIDUE_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


class _SanitizeState:
    """槽位序号表：同一原文同槽复用，不同原文按出现顺序递增（跨文本可共享）。"""

    def __init__(self):
        self._slots = {}  # (pii_type, original) -> "<PHONE_1>"

    def slot_for(self, pii_type, original):
        key = (pii_type, original)
        if key not in self._slots:
            self._slots[key] = "<%s_%d>" % (_PII_SLOT[pii_type], len(
                [k for k in self._slots if k[0] == pii_type]) + 1)
        return self._slots[key]


def sanitize(text, slots=None):
    """本地 PII 槽位化：手机号-><PHONE_N> 金额-><AMOUNT_N> 人名-><PERSON_N>
    公司名-><ORG_N> 邮箱-><EMAIL_N>。同序号去重（两个不同人名 -> PERSON_1/PERSON_2）。
    非字符串输入原样返回；slots 传 dict 可跨多次调用共享序号（prepare_upload 内部用它）。
    """
    if not isinstance(text, str) or not text:
        return text
    state = slots if isinstance(slots, _SanitizeState) else _SanitizeState()

    def _sub_by_type(pii_type, pattern):
        def _repl(m):
            return state.slot_for(pii_type, m.group(0))
        return pattern.sub(_repl, text)

    # 应用顺序：email（防其中数字被手机号吃掉）-> phone -> amount -> org -> name
    text = _sub_by_type("email", _EMAIL_RE)
    text = _sub_by_type("phone", _PHONE_RE)
    text = _sub_by_type("amount", _AMOUNT_RE)

    def _org_repl(m):
        token = m.group(0)
        suffix = next((s for s in _ORG_SUFFIXES if token.endswith(s)), "")
        if not suffix:
            return token
        name_part = token[: len(token) - len(suffix)]
        cut = max((i for i, ch in enumerate(name_part) if ch in _ORG_NAME_STOP), default=-1)
        if cut >= 0:
            prefix = name_part[: cut + 1]  # 功能词前缀原样保留（不吞正文）
            name_part = name_part[cut + 1:]
        else:
            prefix = ""
        if len(name_part) < 1:
            return token
        return prefix + state.slot_for("org", name_part + suffix)

    text = _ORG_RE.sub(_org_repl, text)

    def _name_repl(m):
        token = m.group(0)
        # 姓氏后紧跟虚词 -> 非人名，跳过
        if len(token) >= 2 and token[1] in _NAME_FOLLOW_STOP:
            return token
        # 尾部虚词/动词回退但**保留原字**（张伟说 -> <PERSON_N>说，不吞正文）
        tail = ""
        while len(token) > 2 and token[-1] in _NAME_TRAILING_TRIM:
            tail = token[-1] + tail
            token = token[:-1]
        if len(token) < 2:
            return m.group(0)
        return state.slot_for("name", token) + tail

    text = _NAME_RE.sub(_name_repl, text)
    return text


def detect_residue(payload):
    """原文残留检测：递归扫描载荷（dict/list/str），返回命中的未槽位化 PII 类型
    列表（"phone" / "email"，去重保序）。空列表 = 干净。"""
    hits = []

    def _walk(node):
        if isinstance(node, str):
            if _RESIDUE_PHONE_RE.search(node) and "phone" not in hits:
                hits.append("phone")
            if _RESIDUE_EMAIL_RE.search(node) and "email" not in hits:
                hits.append("email")
        elif isinstance(node, dict):
            for v in node.values():
                _walk(v)
        elif isinstance(node, (list, tuple)):
            for v in node:
                _walk(v)

    _walk(payload)
    return hits


# ------------------------------------------------------------------
# 目录名归一化类型槽 + 结构 hash（第 6 类槽位：日期/合同/编号/通用名）
# ------------------------------------------------------------------
# 日期目录：2024 / 2024-06 / 2024年6月 / 2024.06.01 / 2024年6月1日 等
_DIR_DATE_RE = re.compile(r"^(19|20)\d{2}([年\-/.]\d{1,2}月?([\-/.]?\d{1,2}日?)?)?$")
# 编号目录：字母前缀+数字串（XH-2024-001 / 001 / v2 等）
_DIR_NUM_RE = re.compile(r"^[A-Za-z]{0,8}[-_ ]?\d+([-_ ]?\d+)*$")
# 合同类目录关键词
_DIR_CONTRACT_WORDS = ("合同", "协议", "契约", "contract")


def _dir_slot(segment):
    """单个目录名 -> 类型槽标签（不保留原名任何片段）。"""
    seg = segment.strip()
    if not seg:
        return "empty"
    if _DIR_DATE_RE.match(seg):
        return "date"
    if any(w in seg.lower() for w in _DIR_CONTRACT_WORDS):
        return "contract"
    if _DIR_NUM_RE.match(seg):
        return "number"
    return "generic"


def dir_structure_hash(dir_names, now=None):
    """目录树归一化指纹（docs/FEEDBACK.md §1 L2 ③）：

    - 每个目录名先归一化成类型槽（日期/合同/编号/通用名），**原始目录名不进报文**；
    - 槽位路径排序后拼树、sha256、**截断 16 位 hex**；
    - hash 不可逆：同构树（日期/编号不同、结构相同）同 hash，异构树异 hash。
    dir_names 传相对路径列表（posix 风格 "项目A/会议记录" 均可，逐段槽化）。
    """
    lines = []
    for name in dir_names if isinstance(dir_names, (list, tuple)) else []:
        if not isinstance(name, str) or not name.strip():
            continue
        norm = name.replace("\\", "/").strip("/")
        slots = "/".join(_dir_slot(seg) for seg in norm.split("/") if seg)
        if slots:
            lines.append(slots)
    lines.sort()
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return digest[:16]


# ------------------------------------------------------------------
# 反馈 SDK 主体
# ------------------------------------------------------------------
def _now_iso(now=None):
    ts = time.time() if now is None else now
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


class FeedbackSdk:
    """反馈 SDK：档位状态机（默认 L0=关）+ 本地落盘 + 上行预检与脱敏。

    不内置 UI（首启询问用 should_ask_consent 纯函数，UI 归宿主）；
    不内置网络调用（prepare_upload 只产「上行载荷」，上行动作归宿主/云端侧）。
    """

    def __init__(self, level="L0", feedback_dir=None, package_version=SDK_VERSION, now=None):
        if level not in LEVEL_ORDER:
            raise ValueError("未知反馈档位：%r（可选 L0/L1/L2/L3）" % (level,))
        self._level = level
        self._dir = feedback_dir  # None = 走 default_feedback_dir()（每次读环境变量）
        self._package_version = package_version
        self._now = now

    # -- 档位状态机 ------------------------------------------------
    @property
    def level(self):
        return self._level

    def set_level(self, level):
        if level not in LEVEL_ORDER:
            raise ValueError("未知反馈档位：%r（可选 L0/L1/L2/L3）" % (level,))
        self._level = level

    def _feedback_path(self):
        base = self._dir if self._dir else default_feedback_dir()
        return os.path.join(base, FEEDBACK_LOG_FILE)

    # -- record：本地落盘（默认 L0 直接丢弃） ------------------------
    def record(self, level, payload):
        """反馈落本地 .kbfold/feedback.jsonl（JSONL 追加一行）。

        - 当前档位为 L0（关）-> 丢弃，返回 {"dropped": True, "reason": ...}
        - 请求档位高于已开启档位 -> 丢弃（不得超出用户同意的范围）
        - 否则追加落盘并返回 {"dropped": False, "path": ...}
        """
        if level not in LEVEL_ORDER or level == "L0":
            raise ValueError("record 档位须为 L1/L2/L3，收到 %r" % (level,))
        if self._level == "L0":
            return {"dropped": True, "reason": "feedback_off(L0)"}
        if LEVEL_ORDER[level] > LEVEL_ORDER[self._level]:
            return {"dropped": True, "reason": "level_below_consent(%s<%s)" % (self._level, level)}
        path = self._feedback_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        line = json.dumps({
            "schema": SDK_SCHEMA,
            "ts": _now_iso(self._now),
            "level": level,
            "payload": payload,
            "sdk_version": SDK_VERSION,
        }, ensure_ascii=False)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return {"dropped": False, "path": path}

    # -- prepare_upload：上行预检与脱敏（不联网） --------------------
    def prepare_upload(self, level, payload):
        """上行预检与脱敏：只产「上行载荷」，**不做任何网络调用**。

        - L1 只含：事件类型/三态点选值（采纳/否决/改判，白名单外一律丢弃）/
          信号级别（S1-S4）/置信区间——不含路径、不含文件名
        - L2 加模板命中率（键值均纯计数面）+ 规则 delta（四键白名单）+
          目录结构 hash（原始目录名只在本机槽化后 hash，不进载荷；
          载荷里出现 dir_names 原始名单 -> 直接拒绝）
        - L3 加脱敏失败样例（先 sanitize 再入载荷）
        - 载荷组装后做原文残留检测：含未槽位化手机号/邮箱 -> rejected:True+reason
        返回 {"ok": True, "upload": {...}} 或 {"ok": False, "rejected": True, "reason": "..."}。
        """
        if level not in LEVEL_ORDER or level == "L0":
            raise ValueError("prepare_upload 档位须为 L1/L2/L3，收到 %r" % (level,))
        if self._level == "L0":
            return {"ok": False, "rejected": True, "reason": "feedback_off(L0)"}
        if LEVEL_ORDER[level] > LEVEL_ORDER[self._level]:
            return {"ok": False, "rejected": True,
                    "reason": "level_below_consent(%s<%s)" % (self._level, level)}
        if not isinstance(payload, dict):
            return {"ok": False, "rejected": True, "reason": "payload 须为对象"}

        # 原始目录名禁入载荷：dir_names 只允许在本机消费（槽化+hash），任何档位均不透传
        if "dir_names" in payload and LEVEL_ORDER[level] < LEVEL_ORDER["L2"]:
            return {"ok": False, "rejected": True, "reason": "dir_names 仅 L2+ 允许（且只 hash 不透传）"}

        upload = {
            "schema": SDK_SCHEMA,
            "level": level,
            "package_version": self._package_version,
            "ts": _now_iso(self._now),
            "event": self._extract_event(payload),
        }
        if LEVEL_ORDER[level] >= LEVEL_ORDER["L2"]:
            delta = self._extract_rule_delta(payload)
            if isinstance(delta, dict) and delta.get("__rejected__"):
                return {"ok": False, "rejected": True, "reason": delta["__rejected__"]}
            if delta is not None:
                upload["rule_delta"] = delta
            hits = self._extract_template_hits(payload)
            if isinstance(hits, dict) and hits.get("__rejected__"):
                return {"ok": False, "rejected": True, "reason": hits["__rejected__"]}
            if hits is not None:
                upload["template_hits"] = hits
            structure = self._extract_structure(payload)
            if isinstance(structure, dict) and structure.get("__rejected__"):
                return {"ok": False, "rejected": True, "reason": structure["__rejected__"]}
            if structure is not None:
                upload["structure_hash"] = structure["hash"]
        if LEVEL_ORDER[level] >= LEVEL_ORDER["L3"]:
            samples = payload.get("samples")
            if not isinstance(samples, list) or not samples:
                return {"ok": False, "rejected": True, "reason": "L3 须带非空 samples 列表"}
            slots = _SanitizeState()
            upload["samples"] = [
                {"reason": s.get("reason", ""), "sample": sanitize(s.get("sample", ""), slots)}
                for s in samples if isinstance(s, dict)
            ]

        residues = detect_residue(upload)
        if residues:
            return {"ok": False, "rejected": True,
                    "reason": "residue_detected(%s)" % "+".join(residues)}
        return {"ok": True, "upload": upload}

    # -- 载荷装配内部件 ---------------------------------------------
    @staticmethod
    def _extract_event(payload):
        """L1 最小面：事件类型/三态点选值/信号级别/置信区间（值一律标量字符串化；
        三态与信号级别走白名单，白名单外一律丢弃——不夹带）。"""
        event = {}
        if "event_type" in payload:
            event["type"] = str(payload["event_type"])
        result = payload.get("result")
        if isinstance(result, str) and result in _RESULT_VALUES:
            event["result"] = result  # 白名单外（含任意自定义串）一律不进载荷
        signal = payload.get("signal_level")
        if isinstance(signal, str) and signal.strip().upper() in _SIGNAL_LEVELS:
            event["signal_level"] = signal.strip().upper()
        confidence = payload.get("confidence")
        if isinstance(confidence, str) and _CONFIDENCE_RE.match(confidence.strip()):
            event["confidence"] = confidence.strip()
        return event

    @staticmethod
    def _extract_rule_delta(payload):
        """L2 规则 delta：字段白名单（rule_id/field/before/after），值限标量——
        结构层面保证不含话术原文；越权字段直接拒绝。"""
        delta = payload.get("rule_delta")
        if delta is None:
            return None
        if not isinstance(delta, dict):
            return {"__rejected__": "rule_delta 须为对象"}
        for k in delta:
            if k not in _RULE_DELTA_KEYS:
                return {"__rejected__": "rule_delta 含越权字段:%s" % k}
        out = {}
        for k in _RULE_DELTA_KEYS:
            if k not in delta:
                continue
            v = delta[k]
            if not isinstance(v, (str, int, float, bool)) and v is not None:
                return {"__rejected__": "rule_delta.%s 须为标量" % k}
            out[k] = v
        return out

    @staticmethod
    def _extract_template_hits(payload):
        """L2 模板命中率：键=模板/通道 id（短串），值=非负整数纯计数；
        非法键/值直接拒绝（结构层面保证纯计数面）。"""
        hits = payload.get("template_hits")
        if hits is None:
            return None
        if not isinstance(hits, dict) or not hits:
            return {"__rejected__": "template_hits 须为非空对象"}
        if len(hits) > _TEMPLATE_HITS_MAX_KEYS:
            return {"__rejected__": "template_hits 超量(>%d)" % _TEMPLATE_HITS_MAX_KEYS}
        out = {}
        for k, v in hits.items():
            if not isinstance(k, str) or not k or len(k) > _TEMPLATE_KEY_CAP:
                return {"__rejected__": "template_hits 键须为 ≤%d 字符的非空串" % _TEMPLATE_KEY_CAP}
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                return {"__rejected__": "template_hits.%s 须为非负整数" % k}
            out[k] = v
        return out

    @staticmethod
    def _extract_structure(payload):
        """L2 目录结构指纹：payload.dir_names（原始名，仅本机消费）-> 槽化+hash；
        或直接传 16 位 hex 的 structure_hash 透传（宿主已自算）。二者取其一。"""
        if "dir_names" in payload:
            names = payload["dir_names"]
            if not isinstance(names, (list, tuple)) or not names:
                return {"__rejected__": "dir_names 须为非空列表"}
            return {"hash": dir_structure_hash(names)}
        given = payload.get("structure_hash")
        if given is None:
            return None
        if not isinstance(given, str) or not re.fullmatch(r"[0-9a-f]{16}", given):
            return {"__rejected__": "structure_hash 须为 16 位 hex（sha256 截断）"}
        return {"hash": given}


# JS 侧同形入口（createFeedbackSdk）的 Python 对照物
def create_feedback_sdk(**kwargs):
    return FeedbackSdk(**kwargs)
