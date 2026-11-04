# -*- coding: utf-8 -*-
"""parity 机闸：公开仓 core 引擎 vs 主仓冻结底本 行为逐字等价测试（施工硬出口）。

做法：同一组场景（步骤脚本：先调用、后取输出）分别喂给——
  - 公开仓侧：core/py/kbfold/*（本仓移植件）
  - 底本侧：需求包 _assets/kbfold-baseline/ 冻结快照
    · JS 件经 node 子进程比对（底本为 JS，Python 底本模式不适用 ⇒ 机闸内嵌小 JS 调用器，
      此为与同族已发布包模式的一处已声明差异，不算造新逻辑）；
    · Py 件（dir_classify.py）以 importlib 按路径加载直接比对。
逐场景断言输出 JSON 逐字等价（JS 数字不分 int/float ⇒ 规范化后比对：
Python 侧整值浮点转 int、NaN 转 null，两侧同规则）。

底本缺失时本文件整体跳过（parity 属开发期机闸，非公开仓功能测试）。
底本目录可用环境变量 NW_KBFOLD_PARITY_SOURCE 覆盖（默认指向需求包快照）。

纪律：所有场景为固定夹具数据，禁依赖真实时间、网络与任何运行数据目录；
底本快照只读（冻结件，禁改写——改口径＝另立新快照目录）。
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

PUB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE_PY = os.path.join(PUB_ROOT, "core", "py")
if CORE_PY not in sys.path:
    sys.path.insert(0, CORE_PY)

# 开发机底本位置经环境变量 NW_KBFOLD_PARITY_SOURCE 注入（指向主仓需求包内的 kbfold-baseline 快照目录）；
# 未注入或路径不存在时机闸整体 skip，不影响公开仓功能测试。
BASELINE_DIR = os.environ.get("NW_KBFOLD_PARITY_SOURCE") or ""

MAIN_MISSING = not os.path.isdir(BASELINE_DIR) or not os.path.isfile(
    os.path.join(BASELINE_DIR, "kb-tpl-match.js"))

pytestmark = pytest.mark.skipif(
    MAIN_MISSING, reason="底本快照不存在（parity 机闸需同机执行）：{}".format(BASELINE_DIR))

pytest.importorskip("kbfold", reason="公开仓 core 未就位")

from kbfold import (assign, auto_confirm, capacity, chunk, classify,  # noqa: E402
                    dir_classify, project_match, rrf)

NODE = shutil.which("node")

# ---------------------------------------------------------------------------
# 底本 JS 调用器（内嵌小调用器：stdin 收步骤脚本，stdout 逐场景回 JSON）
# ---------------------------------------------------------------------------

JS_RUNNER = r"""
'use strict';
const fs = require('fs');
const path = require('path');
let raw = '';
fs.stdin = process.stdin;
process.stdin.setEncoding('utf8');
process.stdin.on('data', (d) => { raw += d; });
process.stdin.on('end', () => {
  const req = JSON.parse(raw);
  const mod = require(path.resolve(req.file));
  const env = {};
  const deref = (x) => (x && typeof x === 'object' && x.$ref !== undefined) ? env[x.$ref] : x;
  const deepDeref = (x) => {
    if (Array.isArray(x)) return x.map(deepDeref);
    if (x && typeof x === 'object') {
      if (x.$ref !== undefined) return deref(x);
      const o = {};
      for (const k of Object.keys(x)) o[k] = deepDeref(x[k]);
      return o;
    }
    return x;
  };
  const outputs = {};
  for (const step of req.steps) {
    const args = (step.args || []).map(deepDeref);
    const res = mod[step.call].apply(null, args);
    if (step.keep) env[step.keep] = res;
    if (step.output) outputs[step.output] = res;
  }
  process.stdout.write(JSON.stringify(outputs));
});
"""


@pytest.fixture(scope="module")
def js_env(tmp_path_factory):
    """JS 运行环境：把冻结快照的 JS 件拷进临时目录并打上 CommonJS 标记。

    为什么拷贝：Node 24 会沿目录树向上找 package.json，主仓的 "type": "module" 会把
    底本 .js 误判成 ES module（module.exports 直接炸）。快照本体只读（冻结件禁改写），
    故拷到临时工作目录、附 {"type":"commonjs"} 标记后经 node 子进程调用。"""
    if not NODE:
        pytest.skip("node 不可用（JS 底本 parity 需要 node 子进程）")
    work = tmp_path_factory.mktemp("parity_js")
    (work / "package.json").write_text('{"type": "commonjs"}', encoding="utf-8")
    for fn in os.listdir(BASELINE_DIR):
        if fn.endswith(".js"):
            shutil.copy2(os.path.join(BASELINE_DIR, fn), work / fn)
    runner = work / "_parity_js_runner.js"
    runner.write_text(JS_RUNNER, encoding="utf-8")
    return {"dir": str(work), "runner": str(runner)}


@pytest.fixture(scope="module")
def js_runner(js_env):
    return js_env["runner"]


def baseline_js(name, js_env):
    """底本 JS 件路径（临时运行目录内；冻结快照本体不动）。"""
    return os.path.join(js_env["dir"], name)


def run_js(js_runner, js_file, steps):
    req = json.dumps({"file": js_file, "steps": steps}, ensure_ascii=False)
    proc = subprocess.run([NODE, js_runner], input=req.encode("utf-8"),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError("JS 底本调用失败：%s" % proc.stderr.decode("utf-8", "replace")[:500])
    return json.loads(proc.stdout.decode("utf-8"))


_CAMEL_RE = re.compile(r"(?<!^)(?=[A-Z])")


def _py_name(name):
    """JS camelCase 调用名 → 公开仓 snake_case 函数名（底本与移植件的既有命名对照）。"""
    return _CAMEL_RE.sub("_", name).lower()


def run_py(mod, steps):
    env = {}
    outputs = {}

    def deref(x):
        if isinstance(x, dict) and "$ref" in x:
            return env[x["$ref"]]
        if isinstance(x, dict):
            return {k: deref(v) for k, v in x.items()}
        if isinstance(x, list):
            return [deref(v) for v in x]
        return x

    for step in steps:
        args = [deref(a) for a in (step.get("args") or [])]
        fname = _py_name(step["call"])
        fn = getattr(mod, fname, None) or getattr(mod, step["call"], None)
        if fn is None:
            raise AttributeError("%s / %s 均不存在" % (fname, step["call"]))
        res = fn(*args)
        if step.get("keep"):
            env[step["keep"]] = res
        if step.get("output"):
            outputs[step["output"]] = res
    return outputs


def _canon(x):
    if isinstance(x, bool):
        return x
    if isinstance(x, float):
        if x != x:  # NaN ⇒ JS JSON.stringify 为 null
            return None
        if x == int(x) and abs(x) < 1e15:
            return int(x)  # JS 数字无 int/float 之分
        return x
    if isinstance(x, int):
        return x
    if isinstance(x, dict):
        return {str(k): _canon(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_canon(v) for v in x]
    return x


def js_dumps(x):
    return json.dumps(_canon(x), sort_keys=True, ensure_ascii=False)


def assert_parity(label, pub_outputs, main_outputs):
    assert set(pub_outputs) == set(main_outputs), label
    for key in pub_outputs:
        pub_s = js_dumps(pub_outputs[key])
        main_s = js_dumps(main_outputs[key])
        assert pub_s == main_s, "%s / %s 不等：\n公开仓=%s\n底本  =%s" % (
            label, key, pub_s[:600], main_s[:600])


def load_py_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# 场景夹具（固定数据，两侧同喂）
# ---------------------------------------------------------------------------

TEMPLATES = [
    {"industry": "trade", "name": "trade-tpl", "version": 1, "kind": "industry",
     "match": {"keywords": ["合同", "星海"], "ext_signals": {"pdf": 0.9, "docx": 0.6},
               "min_docs": 2},
     "tree": [{"name": "01-原始资料", "children": [{"name": "需求", "children": []}]},
              {"name": "02-合同", "children": []},
              {"name": "03-会议记录", "children": []}]},
    {"industry": "software", "name": "software-tpl", "version": 2, "kind": "industry",
     "match": {"keywords": ["部署", "验收"], "path_regex": ["dev"], "ext_signals": {"py": 0.8}},
     "tree": [{"name": "01-方案设计", "children": []}, {"name": "02-开发", "children": []}]},
    {"industry": "generic", "name": "generic-tpl", "version": 1, "kind": "generic",
     "tree": [{"name": "文档", "children": []}, {"name": "影音", "children": []}]},
]

CTX_HIT = {"name": "星海项目资料", "dirPaths": ["星海项目/合同", "星海项目/会议记录"],
           "topNames": ["合同", "会议记录"], "extDist": {"pdf": 12, "docx": 4},
           "docCount": 16}

CTX_SOFT = {"name": "研发项目", "dirPaths": ["dev/部署手册"], "topNames": ["部署验收"],
            "extDist": {"py": 30, "md": 5}, "docCount": 35}

CTX_TIE = {"name": "未知资料堆", "dirPaths": ["misc"], "topNames": ["misc"],
           "extDist": {"txt": 3}, "docCount": 3}


def project_ctx(pid, name, **kw):
    d = {"id": pid, "name": name, "aliases": kw.get("aliases", []),
         "keywords": kw.get("keywords", [])}
    return d


# ---------------------------------------------------------------------------
# 场景一：文本工具（stripNo/wordsOf/similarity/levenshtein/jaccard）
# ---------------------------------------------------------------------------

def test_parity_tpl_text_utils(js_runner, js_env):
    steps = [
        {"call": "stripNo", "args": ["04-采购与询价"], "output": "o1"},
        {"call": "stripNo", "args": ["12、会议记录"], "output": "o2"},
        {"call": "wordsOf", "args": ["采购与询价/合同评审"], "output": "o3"},
        {"call": "similarity", "args": ["采购与询价", "采购询价"], "output": "o4"},
        {"call": "similarity", "args": ["会议记录", "会议纪要"], "output": "o5"},
        {"call": "levenshtein", "args": ["kitten", "sitting"], "output": "o6"},
        {"call": "jaccard", "args": [["合同", "评审"], ["合同", "归档"]], "output": "o7"},
        # vocabOf 返回 Set（JSON.stringify 为 {}），不直接出参；经 keywordsOf（由段名派生词表）间接覆盖
        {"call": "keywordsOf", "args": [TEMPLATES[0]], "output": "o8"},
        {"call": "keywordsOf", "args": [{"tree": [{"name": "02-合同", "children": []},
                                                  {"name": "会议记录", "children": []}]}], "output": "o9"},
    ]
    pub = run_py(classify, steps)
    main = run_js(js_runner, baseline_js("kb-tpl-match.js", js_env), steps)
    assert_parity("tpl-text-utils", pub, main)


# ---------------------------------------------------------------------------
# 场景二：S1 显式声明命中（confidence=high，分数抬到高置信带）
# ---------------------------------------------------------------------------

def test_parity_tpl_s1_explicit(js_runner, js_env):
    steps = [
        {"call": "collectSignals", "args": [dict(CTX_HIT, declaredIndustry="trade")], "keep": "sig"},
        {"call": "decide", "args": [{"$ref": "sig"}, TEMPLATES, {}], "output": "o"},
    ]
    pub = run_py(classify, steps)
    main = run_js(js_runner, baseline_js("kb-tpl-match.js", js_env), steps)
    assert_parity("tpl-s1-explicit", pub, main)


# ---------------------------------------------------------------------------
# 场景三：无声明三信号加权（mid / path_regex 硬闸判 0）
# ---------------------------------------------------------------------------

def test_parity_tpl_weighted(js_runner, js_env):
    steps = [
        {"call": "collectSignals", "args": [CTX_HIT], "keep": "sig1"},
        {"call": "decide", "args": [{"$ref": "sig1"}, TEMPLATES, {}], "output": "o1"},
        {"call": "collectSignals", "args": [CTX_SOFT], "keep": "sig2"},
        {"call": "decide", "args": [{"$ref": "sig2"}, TEMPLATES, {}], "output": "o2"},
        {"call": "collectSignals", "args": [CTX_TIE], "keep": "sig3"},
        {"call": "decide", "args": [{"$ref": "sig3"}, TEMPLATES,
                                    {"rules": {"weights": {"structure": 0.5, "name": 0.4, "prior": 0.1}}}],
         "output": "o3"},
    ]
    pub = run_py(classify, steps)
    main = run_js(js_runner, baseline_js("kb-tpl-match.js", js_env), steps)
    assert_parity("tpl-weighted", pub, main)


# ---------------------------------------------------------------------------
# 场景四：平手不猜（tie band ⇒ low + 灰字建议）
# ---------------------------------------------------------------------------

def test_parity_tpl_tie(js_runner, js_env):
    steps = [
        {"call": "collectSignals", "args": [CTX_TIE], "keep": "sig"},
        {"call": "decide", "args": [{"$ref": "sig"}, TEMPLATES, {"tieGap": 1.0}], "output": "o"},
    ]
    pub = run_py(classify, steps)
    main = run_js(js_runner, baseline_js("kb-tpl-match.js", js_env), steps)
    assert_parity("tpl-tie", pub, main)


# ---------------------------------------------------------------------------
# 场景五：instantiate（模板 → 视图树，暂空段保留 pending）
# ---------------------------------------------------------------------------

def test_parity_tpl_instantiate(js_runner, js_env):
    steps = [
        {"call": "collectSignals", "args": [CTX_HIT], "keep": "sig"},
        {"call": "instantiate", "args": [TEMPLATES[0], {"topNames": ["合同", "过程稿"]}], "output": "o1"},
        {"call": "instantiate", "args": [TEMPLATES[0], CTX_HIT], "output": "o2"},
    ]
    pub = run_py(classify, steps)
    main = run_js(js_runner, baseline_js("kb-tpl-match.js", js_env), steps)
    assert_parity("tpl-instantiate", pub, main)


# ---------------------------------------------------------------------------
# 场景六：assign L0 显式选定 + alternatives
# ---------------------------------------------------------------------------

def test_parity_assign_l0(js_runner, js_env):
    projects = [project_ctx(1, "星海项目"), project_ctx(2, "远洋项目", aliases=["YY"])]
    ctx = {"meeting": {"subject": "星海项目周会"}, "projects": projects,
           "history": [{"meeting_id": 9, "project_id": 2, "participants": [], "topics": []}],
           "explicitProjectId": 1, "opts": {}}
    steps = [{"call": "decide", "args": [ctx], "output": "o"}]
    pub = run_py(assign, steps)
    main = run_js(js_runner, baseline_js("kb-assign-engine.js", js_env), steps)
    assert_parity("assign-l0", pub, main)


# ---------------------------------------------------------------------------
# 场景七：L1a 全等 / L1b 模糊唯一 / 首次从严（silent=False 理由串）
# ---------------------------------------------------------------------------

def test_parity_assign_l1(js_runner, js_env):
    projects = [project_ctx(1, "星海项目", keywords=["星海"]),
                project_ctx(2, "远洋项目")]
    base = {"projects": projects}
    steps = [
        {"call": "decide", "args": [dict(base, meeting={"subject": "星海项目评审会"},
                                        history=[], opts={})], "output": "l1a_first"},
        {"call": "decide", "args": [dict(base, meeting={"subject": "星海项目评审会"},
                                        history=[{"meeting_id": 1, "project_id": 1}],
                                        opts={"silentThreshold": 0.75})], "output": "l1a_silent"},
        {"call": "decide", "args": [dict(base, meeting={"subject": "关于星海项目的事项"},
                                        history=[{"meeting_id": 1, "project_id": 1}],
                                        opts={})], "output": "l1b"},
        {"call": "decide", "args": [dict(base, meeting={"subject": "星海与远洋联席"},
                                        history=[], opts={})], "output": "ambiguous"},
    ]
    pub = run_py(assign, steps)
    main = run_js(js_runner, baseline_js("kb-assign-engine.js", js_env), steps)
    assert_parity("assign-l1", pub, main)


# ---------------------------------------------------------------------------
# 场景八：L2 词表命中（唯一最高分）/ 歧义降级理由串
# ---------------------------------------------------------------------------

def test_parity_assign_l2(js_runner, js_env):
    projects = [project_ctx(1, "项目甲", keywords=["采购", "合同"]),
                project_ctx(2, "项目乙", keywords=["人事"])]
    ctx = {"meeting": {"subject": "例行会", "topics": ["采购合同条款讨论"],
                       "minutes_text": "采购流程与合同模板"}, "projects": projects,
           "history": [{"meeting_id": 3, "project_id": 1}], "opts": {}}
    steps = [{"call": "decide", "args": [ctx], "output": "o"}]
    pub = run_py(assign, steps)
    main = run_js(js_runner, baseline_js("kb-assign-engine.js", js_env), steps)
    assert_parity("assign-l2", pub, main)


# ---------------------------------------------------------------------------
# 场景九：L3 历史投票（参会人 +2/主题 +2/类型 +1，唯一最高分才判）
# ---------------------------------------------------------------------------

def test_parity_assign_l3(js_runner, js_env):
    projects = [project_ctx(1, "星海项目"), project_ctx(2, "远洋项目")]
    ctx = {
        "meeting": {"subject": "星海项目阶段汇报", "meeting_type": "project_review",
                    "participants": ["张三", "李四"]},
        "projects": projects,
        "history": [
            {"meeting_id": 11, "project_id": 1, "participants": ["张三", "李四", "王五", "赵六"],
             "topics": ["星海项目阶段汇报"], "meeting_type": "project_review"},
            {"meeting_id": 12, "project_id": 1, "participants": ["李四"],
             "topics": [], "meeting_type": "other"},
            {"meeting_id": 13, "project_id": 2, "participants": ["张三"], "topics": []},
        ],
        "opts": {"silentThreshold": 0.75},
    }
    steps = [{"call": "decide", "args": [ctx], "output": "o"}]
    pub = run_py(assign, steps)
    main = run_js(js_runner, baseline_js("kb-assign-engine.js", js_env), steps)
    assert_parity("assign-l3", pub, main)


# ---------------------------------------------------------------------------
# 场景十：L5 大模型兜底（候选注入）与 L6 判不出 / 阈值解析
# ---------------------------------------------------------------------------

def test_parity_assign_l5_l6(js_runner, js_env):
    projects = [project_ctx(1, "星海项目"), project_ctx(2, "远洋项目")]
    steps = [
        {"call": "decide",
         "args": [{"meeting": {"subject": "无关会议"}, "projects": projects, "history": [],
                   "opts": {"llmEnabled": True,
                            "llmCandidates": [{"projectId": 2, "confidence": 0.66}]}}],
         "output": "l5"},
        {"call": "decide",
         "args": [{"meeting": {"subject": "无关会议"}, "projects": projects, "history": [],
                   "opts": {}}], "output": "l6"},
        {"call": "parseSilentThreshold", "args": ["off"], "output": "t1"},
        {"call": "parseSilentThreshold", "args": ["0.9"], "output": "t2"},
        {"call": "parseSilentThreshold", "args": [None], "output": "t3"},
    ]
    pub = run_py(assign, steps)
    main = run_js(js_runner, baseline_js("kb-assign-engine.js", js_env), steps)
    assert_parity("assign-l5-l6", pub, main)


# ---------------------------------------------------------------------------
# 场景十一：chunk 分块 512/64/4（滑窗 + 尾块并入 + display 档）
# ---------------------------------------------------------------------------

def test_parity_chunk_split(js_runner, js_env):
    text = "甲" * 1300 + "\n" + "乙" * 100 + "。" + "丙" * 300
    steps = [
        {"call": "splitText", "args": [text], "output": "o1"},
        {"call": "splitText", "args": [text, {"child": 200, "overlap": 40}], "output": "o2"},
        {"call": "splitText", "args": ["short"], "output": "o3"},
        {"call": "splitText", "args": [""], "output": "o4"},
        {"call": "chunkDisplay", "args": ["第一段。\n第二段。\n" + "长" * 700], "output": "o5"},
        {"call": "chunkPages", "args": [[{"num": 1, "text": "页一" * 300},
                                         {"num": 2, "text": "页二"}]], "output": "o6"},
        {"call": "parentIdxOf", "args": [5], "output": "o7"},
        {"call": "chunkDocument", "args": [{"text": "丁" * 900}], "output": "o8"},
    ]
    pub = run_py(chunk, steps)
    main = run_js(js_runner, baseline_js("kb-chunk.js", js_env), steps)
    assert_parity("chunk-split", pub, main)


# ---------------------------------------------------------------------------
# 场景十二：chunkBlocks（块感知 R1-R6：标题前缀/表格管道表/页眉剔除）
# ---------------------------------------------------------------------------

def test_parity_chunk_blocks(js_runner, js_env):
    blocks = [
        {"label": "Page-Header", "text": "第 1 页"},
        {"label": "Title", "text": "采购方案", "headingPath": ["采购方案"], "order": 1},
        {"label": "Text", "text": "正文第一段，描述采购背景与目标。" * 20,
         "headingPath": ["采购方案", "背景"], "order": 2},
        {"label": "Table", "text": "", "tableHtml": "<table><tr><th>品目</th><th>数量</th></tr>"
         "<tr><td>服务器</td><td>2</td></tr></table>", "headingPath": ["采购方案", "清单"], "order": 3},
    ]
    audit = []
    steps = [
        {"call": "chunkBlocks", "args": [blocks, {"pageNum": 1}], "output": "o1"},
        {"call": "chunkBlocks", "args": [blocks, {"pageNum": 1, "child": 100,
                                                  "parentMax": 5000, "audit": audit}], "output": "o2"},
    ]
    pub = run_py(chunk, steps)
    main = run_js(js_runner, baseline_js("kb-chunk.js", js_env), steps)
    assert_parity("chunk-blocks", pub, main)


# ---------------------------------------------------------------------------
# 场景十三：readorder 两栏版式（列左边界聚类 + tolK 三档）
# ---------------------------------------------------------------------------

def _two_col_boxes():
    boxes = []
    for i in range(6):
        boxes.append({"x0": 50, "y0": 100 + i * 30, "x1": 250, "y1": 100 + i * 30 + 20})
    for i in range(6):
        boxes.append({"x0": 400, "y0": 100 + i * 30, "x1": 600, "y1": 100 + i * 30 + 20})
    boxes.append({"x0": 50, "y0": 40, "x1": 600, "y1": 70})   # 跨栏标题
    return boxes


def test_parity_readorder_columns(js_runner, js_env):
    boxes = _two_col_boxes()
    steps = [
        {"call": "analyzeOrder", "args": [boxes, {"pageW": 660, "pageH": 340}], "output": "o1"},
        {"call": "analyzeOrder", "args": [boxes, {"pageW": 660, "pageH": 340, "tolK": 0.4}], "output": "o2"},
        {"call": "analyzeOrder", "args": [boxes, {"pageW": 660, "pageH": 340, "tolK": 1.0}], "output": "o3"},
    ]
    pub = run_py(readorder_mod(), steps)
    main = run_js(js_runner, baseline_js("kb-readorder.js", js_env), steps)
    assert_parity("readorder-columns", pub, main)


def readorder_mod():
    from kbfold import readorder
    return readorder


# ---------------------------------------------------------------------------
# 场景十四：readorder 单栏回退 / 竖排放弃分栏 / 表格行防护
# ---------------------------------------------------------------------------

def test_parity_readorder_single(js_runner, js_env):
    single = [{"x0": 50, "y0": 30 + i * 40, "x1": 600, "y1": 30 + i * 40 + 20} for i in range(8)]
    vertical = [{"x0": 100, "y0": 30 + i * 90, "x1": 130, "y1": 30 + i * 90 + 80} for i in range(4)]
    table = []
    for r in range(5):
        for c, cx in enumerate((50, 200, 400)):
            table.append({"x0": cx, "y0": 40 + r * 30, "x1": cx + (90 if c == 0 else 130),
                          "y1": 40 + r * 30 + 20})
    steps = [
        {"call": "analyzeOrder", "args": [single, {"pageW": 660, "pageH": 400}], "output": "o1"},
        {"call": "analyzeOrder", "args": [vertical, {"pageW": 300, "pageH": 400}], "output": "o2"},
        {"call": "analyzeOrder", "args": [table, {"pageW": 600, "pageH": 250}], "output": "o3"},
        {"call": "analyzeOrder", "args": [single, {"columnMode": "single"}], "output": "o4"},
    ]
    pub = run_py(readorder_mod(), steps)
    main = run_js(js_runner, baseline_js("kb-readorder.js", js_env), steps)
    assert_parity("readorder-single", pub, main)


# ---------------------------------------------------------------------------
# 场景十五：rrf 三路融合（同路去重 / 归一 / 稳定序 / rerank 权重 0.7）
# ---------------------------------------------------------------------------

def test_parity_rrf(js_runner, js_env):
    channels = [
        {"name": "semantic", "hits": [{"file_id": 3, "name": "c"}, {"file_id": 1, "name": "a",
                                                                      "parentText": "全文"},
                                       {"file_id": 3, "name": "dup"}]},
        {"name": "fts", "hits": [{"file_id": 2}, {"file_id": 3, "page": 4}]},
        {"name": "keyword", "hits": [{"file_id": 1}, {"file_id": 4}, {"file_id": "bad"}, {"file_id": 0}]},
        {"name": "empty", "hits": []},
    ]
    rerank = [{"file_id": 1, "score": 0.2}, {"file_id": 2, "score": 0.9}]
    steps = [
        {"call": "fuse", "args": [channels], "output": "pure"},
        {"call": "fuse", "args": [channels, rerank], "output": "reranked"},
        {"call": "fuse", "args": [channels, None, {"k": 1}], "output": "k1"},
        {"call": "fuse", "args": [[], None], "output": "empty"},
    ]
    pub = run_py(rrf, steps)
    main = run_js(js_runner, baseline_js("kb-rank-fusion.js", js_env), steps)
    assert_parity("rrf", pub, main)


# ---------------------------------------------------------------------------
# 场景十六：capacity 四闸（ok / warn / danger / 未查询 seen=false）
# ---------------------------------------------------------------------------

def test_parity_capacity(js_runner, js_env):
    lib_ok = {"chunks": 500, "chunksVec": 500, "filesVec": 30, "itemsVec": 10, "dim": 1024}
    lib_big = {"chunks": 95000, "chunksVec": 99999, "filesVec": 300, "itemsVec": 100, "dim": 1024}
    live = {"totalBytes": 640 * 1024 * 1024,
            "entries": [{"name": "kb_chunks", "libId": "L1", "cacheDir": "C:/cache/L1",
                         "n": 99999, "matrixBytes": 409 * 1024 * 1024, "queryMs": 620.4},
                        {"name": "file_embeddings", "libId": "L1", "cacheDir": "C:/cache/L1",
                         "n": 300, "matrixBytes": 1024 * 1024, "queryMs": 12}]}
    steps = [
        {"call": "buildCapacity", "args": [{"lib": lib_ok, "live": None, "activePrefix": ""}], "output": "ok"},
        {"call": "buildCapacity", "args": [{"lib": lib_big, "live": live,
                                            "activePrefix": "C:/cache/L1"}], "output": "danger"},
        {"call": "buildCapacity", "args": [{"lib": lib_ok, "activePrefix": "",
                                            "thresholds": {"chunksPerLib": 600}}], "output": "warn"},
        {"call": "fmtChunks", "args": [123456], "output": "f1"},
        {"call": "fmtChunks", "args": [860], "output": "f2"},
        {"call": "fmtBytes", "args": [1610612736], "output": "f3"},
        {"call": "fmtBytes", "args": [3 * 1024 * 1024], "output": "f4"},
        {"call": "fmtBytes", "args": [500], "output": "f5"},
    ]
    pub = run_py(capacity, steps)
    main = run_js(js_runner, baseline_js("kb-capacity.js", js_env), steps)
    assert_parity("capacity", pub, main)


# ---------------------------------------------------------------------------
# 场景十七：project_match 三级匹配（歧义禁猜）
# ---------------------------------------------------------------------------

def test_parity_project_match(js_runner, js_env):
    rows = [{"id": 1, "name": "星海项目"}, {"id": 2, "name": "星海二期"}, {"id": 3, "name": "弈枢设计资料"}]
    steps = [
        {"call": "matchProjectByName", "args": [rows, " 弈枢设计资料 "], "output": "exact"},
        {"call": "matchProjectByName", "args": [rows, "星海"], "output": "ambiguous"},
        {"call": "matchProjectByName", "args": [rows, "设计资料"], "output": "contains"},
        {"call": "matchProjectByName", "args": [rows, "弈枢"], "output": "prefix"},
        {"call": "matchProjectByName", "args": [rows, "不存在的库"], "output": "nf"},
        {"call": "matchProjectByName", "args": [rows, ""], "output": "empty"},
    ]
    pub = run_py(project_match, steps)
    main = run_js(js_runner, baseline_js("kb-project-match.js", js_env), steps)
    assert_parity("project-match", pub, main)


# ---------------------------------------------------------------------------
# 场景十八：auto_confirm（高置信整批自动 / 保守整批制 / 全载体批 / 阈值解析）
# ---------------------------------------------------------------------------

def test_parity_auto_confirm(js_runner, js_env):
    cands = [
        {"confidence": 0.92, "mapping": {"XH": "02-合同"}, "status": "candidate"},
        {"confidence": 0.85, "mapping": {"XH": "03-会议"}, "status": "candidate"},
        {"confidence": 0.95, "mapping": {}, "status": "picked"},
    ]
    low = [{"confidence": 0.4, "mapping": {}, "status": "candidate"}]
    carrier = [{"confidence": 0.95, "mapping": {}, "status": "candidate"}]
    steps = [
        {"call": "decideAutoConfirm", "args": [cands, 0.8], "output": "ok"},
        {"call": "decideAutoConfirm", "args": [cands + low, 0.8], "output": "low"},
        {"call": "decideAutoConfirm", "args": [carrier, 0.8], "output": "carrier"},
        {"call": "decideAutoConfirm", "args": [cands, 0], "output": "disabled"},
        {"call": "decideAutoConfirm", "args": [[{"confidence": None, "mapping": {"a": 1}}], 0.8], "output": "nullconf"},
        {"call": "parseThreshold", "args": ["off"], "output": "t1"},
        {"call": "parseThreshold", "args": ["0.9"], "output": "t2"},
        {"call": "parseThreshold", "args": [""], "output": "t3"},
    ]
    pub = run_py(auto_confirm, steps)
    main = run_js(js_runner, baseline_js("kb-auto-confirm.js", js_env), steps)
    assert_parity("auto-confirm", pub, main)


# ---------------------------------------------------------------------------
# 场景十九：dir_classify（Py→Py 逐行对照：词表分类 / 分档 / 规则合并）
# ---------------------------------------------------------------------------

def test_parity_dir_classify_pypy():
    main_path = os.path.join(BASELINE_DIR, "dir_classify.py")
    main_mod = load_py_module("main_dir_classify", main_path)
    cand = {"name": "Obsidian Vault", "path_shape": "APPDATA_ROAMING/<vendor>/obsidian",
            "root_hint": "appdata_roaming",
            "evidence": {"registry": {"display_name": "Obsidian", "publisher": "Dynalist"},
                         "markers": [".obsidian"], "ext_top": [".md"],
                         "file_count_bucket": "101-1000"},
            "first_seen": "2026-01-01"}
    steps = [
        {"call": "classify_by_vocab", "args": [cand], "output": "cls"},
        {"call": "decide", "args": [cand, {"type": "knowledge_base", "confidence": 0.92}], "output": "plan"},
        {"call": "should_auto_adopt", "args": [cand, {"type": "knowledge_base", "confidence": 0.92},
                                               {"action": "exclude", "item": {"value": "x"}}], "output": "adopt"},
        {"call": "merge_rule_items",
         "args": [{"exclude": {"items": [{"match": "dirname", "value": "旧目录"}]}},
                  [{"match": "dirname", "value": "新目录"}, {"match": "dirname", "value": "旧目录"}]],
         "output": "merge"},
        {"call": "is_safe_rule_name", "args": ["data"], "output": "safe1"},
        {"call": "is_safe_rule_name", "args": ["星海项目资料"], "output": "safe2"},
        {"call": "sanitize_candidate", "args": [dict(cand, fp="a" * 64)], "output": "san"},
        {"call": "parse_llm_result", "args": ['{"type":"db","confidence":0.7,"reason":"数据库目录"}'], "output": "llm"},
    ]
    pub = run_py(dir_classify, steps)
    main = run_py(main_mod, steps)
    assert_parity("dir-classify-pypy", pub, main)
    # GENERIC_NAMES 为同一份（公开规范承诺：不另抄一份）
    assert dir_classify.GENERIC_NAMES == main_mod.GENERIC_NAMES
