#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kbfold_server — KBFold stdio MCP 服务器（五只读工具；Python 标准库，零第三方依赖）。

协议：JSON-RPC 2.0 over stdio（每行一条 JSON 消息，换行分帧）；
Windows 注意：stdio 编码显式设置为 UTF-8（本模块启动即 reconfigure）。

工具面（tools/list 可见，tools/call 调用；全部只读——AI 只能预演，落地由人执行，
apply/undo/archive 不注册为工具，属既定的 AI 边界取舍）：
  kb.scan      只读扫描根目录：乱度读数 + 候选库清单（标记文件探测；不读文件内容）
  kb.plan      只读生成整备方案预演（adopt/mkdir/平移/_inbox 分列；不写任何文件）
  kb.search    只读三路检索 + RRF 融合（k=60），结果带 [n] 引用编号
  kb.explain   只读回答「这个文件为什么被放在这里」（读 manifest.reason 人话字段）
  kb.validate  只读规则 DSL 结构校验（YAML 解析留宿主侧：本服务器不引入 YAML 依赖，
               传已解析的 rules 对象或走 CLI 校验规则文件）

引用纪律（写进工具 description，宿主 AI 必读）：
  ① 引用检索结果必须带 [n] 编号；② 不得编造未出现在结果里的文件；
  ③ plan 只是预演，任何落地动作引导用户走 CLI；④ _inbox 里的文件不得替用户猜；
  ⑤ 索引缺席时如实说「仅关键词路」。

启动：python hosts/mcp/kbfold_server.py
调试：向 stdin 逐行写 JSON-RPC 请求，响应逐行出现在 stdout。
"""

import json
import os
import sys

# 引擎导入：把 core/py 加入模块搜索路径（相对本文件 ../../core/py）
_HERE = os.path.dirname(os.path.abspath(__file__))
_CORE_PY = os.path.join(os.path.dirname(os.path.dirname(_HERE)), "core", "py")
if _CORE_PY not in sys.path:
    sys.path.insert(0, _CORE_PY)

from kbfold import rules as rules_mod  # noqa: E402
from kbfold import service  # noqa: E402

SERVER_NAME = "kbfold"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"

SERVER_INFO = {"name": SERVER_NAME, "version": SERVER_VERSION}
CAPABILITIES = {"tools": {}}

_RULES_NOTE = ("规则面：传入已解析的 rules 对象（不传则用内置演示规则；"
               "YAML 文本请先在宿主侧解析或改用 CLI 的 validate 子命令）。")


def _tool_definitions():
    return [
        {
            "name": "kb.scan",
            "description": "只读扫描知识库根目录：乱度读数（散件占比/扩展名分布/深度）"
                           "＋候选库清单（标记文件探测）。不读文件内容，只元数据。"
                           "对应 CLI：kbfold plan。",
            "inputSchema": {
                "type": "object",
                "properties": {"root": {"type": "string", "description": "扫描根目录"}},
                "required": ["root"],
            },
        },
        {
            "name": "kb.plan",
            "description": "只读生成整备方案预演（adopt/mkdir/改名平移/_inbox 分列清单）。"
                           "AI 只能预演，落地由人执行：写盘动作只在 CLI（kbfold plan / apply "
                           "--rename-only / undo），本工具不注册任何写盘能力。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "root": {"type": "string", "description": "扫描根目录"},
                    "rules": {"type": "object", "description": "已解析的规则对象（可选；" + _RULES_NOTE + "）"},
                },
                "required": ["root"],
            },
        },
        {
            "name": "kb.search",
            "description": "只读三路检索 + RRF 融合（k=60）：语义路（本地嵌入端点缺席时静默跳过）、"
                           "内容路（FTS5 索引缺席时本路跳过）、关键词路（从不缺席）。"
                           "引用纪律：回答必须带 [n] 编号且不得编造结果之外的文件。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "root": {"type": "string"},
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 8},
                },
                "required": ["root", "query"],
            },
        },
        {
            "name": "kb.explain",
            "description": "只读回答「这个文件为什么被放在这里」：读 manifest 的 reason 人话字段"
                           "（该字段永远保留且为明文）。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "root": {"type": "string"},
                    "path": {"type": "string", "description": "相对路径"},
                },
                "required": ["root", "path"],
            },
        },
        {
            "name": "kb.validate",
            "description": "只读规则 DSL 结构校验（version/黑名单/冲突四条）。规则以已解析对象传入；"
                           + _RULES_NOTE,
            "inputSchema": {
                "type": "object",
                "properties": {"rules": {"type": "object", "description": "已解析的规则对象"}},
                "required": [],
            },
        },
    ]


# ---------------------------------------------------------------------------
# 工具实现（全部只读；不写任何文件）
# ---------------------------------------------------------------------------

def _run_scan(args):
    root = str(args.get("root") or ".")
    return service.scan_report(root)


def _run_plan(args):
    root = str(args.get("root") or ".")
    rules = args.get("rules") if isinstance(args.get("rules"), dict) else None
    if rules is None:
        rules, _text, _src = service.load_rules(None, demo=True)
    errs = rules_mod.validate_rules(rules)
    if errs:
        return {"error": True, "errors": errs}
    # 只读预演：不落盘（write=False）
    return service.judge_root(root, rules, write=False)


def _run_search(args):
    root = str(args.get("root") or ".")
    query = str(args.get("query") or "")
    limit = int(args.get("limit") or 8)
    return service.search_root(root, query, limit=limit)


def _run_explain(args):
    root = str(args.get("root") or ".")
    target = str(args.get("path") or "")
    return service.explain_path(root, target)


def _run_validate(args):
    rules = args.get("rules") if isinstance(args.get("rules"), dict) else None
    if rules is None:
        rules, _text, _src = service.load_rules(None, demo=True)
        source = "demo"
    else:
        source = "caller"
    errs = rules_mod.validate_rules(rules)
    return {"valid": not errs, "source": source, "errors": errs,
            "version": rules.get("version"), "kbs": len(rules.get("kbs", []))}


_TOOL_IMPL = {
    "kb.scan": _run_scan,
    "kb.plan": _run_plan,
    "kb.search": _run_search,
    "kb.explain": _run_explain,
    "kb.validate": _run_validate,
}


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 over stdio
# ---------------------------------------------------------------------------

def _reply(req_id, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result},
                                ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _error(req_id, code, message):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id,
                                 "error": {"code": code, "message": message}},
                                ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(req):
    method = req.get("method")
    req_id = req.get("id")
    params = req.get("params") or {}
    if method == "initialize":
        _reply(req_id, {"protocolVersion": PROTOCOL_VERSION,
                        "capabilities": CAPABILITIES, "serverInfo": SERVER_INFO})
    elif method == "notifications/initialized":
        pass  # 通知无需应答
    elif method == "tools/list":
        _reply(req_id, {"tools": _tool_definitions()})
    elif method == "tools/call":
        name = str(params.get("name") or "")
        impl = _TOOL_IMPL.get(name)
        if impl is None:
            _error(req_id, -32602, "未知工具：%s（本服务器只提供五只读工具）" % name)
            return
        try:
            result = impl(params.get("arguments") or {})
            payload = json.dumps(result, ensure_ascii=False)
            if len(payload) > 400000:
                result = {"error": True, "message": "结果过大，请缩小扫描面"}
                payload = json.dumps(result, ensure_ascii=False)
            _reply(req_id, {"content": [{"type": "text", "text": payload}]})
        except Exception as exc:  # 工具内异常如实回报，不崩进程
            _reply(req_id, {"content": [{"type": "text",
                                         "text": json.dumps({"error": True,
                                                             "message": "%s: %s" % (type(exc).__name__, exc)},
                                                            ensure_ascii=False)}],
                             "isError": True})
    elif method == "ping":
        _reply(req_id, {})
    elif req_id is not None:
        _error(req_id, -32601, "未知方法：%s" % method)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            _error(None, -32700, "JSON 解析失败")
            continue
        handle(req)


if __name__ == "__main__":
    main()
