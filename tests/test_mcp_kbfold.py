# -*- coding: utf-8 -*-
"""MCP 壳真握手测试：真 initialize + tools/list + tools/call（假绿灯判据：dump-config 不算数）。

启动方式：子进程跑 hosts/mcp/kbfold_server.py，stdin/stdout 逐行 JSON-RPC 2.0。
工具面验收：五只读工具可见可调；apply/undo 不在工具面（AI 只能预演，落地由人执行）。
"""

import json
import os
import subprocess
import sys

import pytest

PUB_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE_PY = os.path.join(PUB_ROOT, "core", "py")
if CORE_PY not in sys.path:
    sys.path.insert(0, CORE_PY)

SERVER = os.path.join(PUB_ROOT, "hosts", "mcp", "kbfold_server.py")
DEMO_ROOT = os.path.join(PUB_ROOT, "examples", "demo", "fixtures")

REQUIRED_TOOLS = {"kb.scan", "kb.plan", "kb.search", "kb.explain", "kb.validate"}
FORBIDDEN_TOOLS = {"kb.apply", "kb.undo", "kb.archive", "apply", "undo"}


class McpClient:
    """行分帧 JSON-RPC 客户端（真进程、真 stdio）。"""

    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, SERVER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def send(self, obj):
        self.proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def recv(self, timeout=30):
        # 简单行读（subprocess.stdout 无 selectors 友好超时；测试面消息量小，阻塞可接受）
        line = self.proc.stdout.readline()
        if not line:
            err = self.proc.stderr.read().decode("utf-8", "replace")
            raise RuntimeError("MCP 服务器无响应（stderr=%s）" % err[:400])
        return json.loads(line.decode("utf-8"))

    def request(self, method, params=None, req_id=None):
        req_id = req_id if req_id is not None else next(self._ids)
        self.send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
        while True:
            resp = self.recv()
            if resp.get("id") == req_id:
                return resp

    _ids = iter(range(1, 10000))

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        self.proc.wait(timeout=10)


@pytest.fixture()
def client():
    c = McpClient()
    yield c
    c.close()


def test_mcp_initialize_handshake(client):
    resp = client.request("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "kbfold-test", "version": "0"},
    })
    result = resp["result"]
    assert result["serverInfo"]["name"] == "kbfold"
    assert result["protocolVersion"] == "2024-11-05"
    assert "tools" in result["capabilities"]
    # 通知无需应答，但发出去完成真握手
    client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})


def test_mcp_tools_list_five_readonly(client):
    client.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
    resp = client.request("tools/list")
    names = {t["name"] for t in resp["result"]["tools"]}
    assert REQUIRED_TOOLS <= names
    assert not (names & FORBIDDEN_TOOLS), "apply/undo 不得注册为 MCP 工具（AI 只能预演）"
    # 引用纪律写进 description
    by_name = {t["name"]: t for t in resp["result"]["tools"]}
    assert "[n]" in by_name["kb.search"]["description"]
    assert "落地由人执行" in by_name["kb.plan"]["description"]


def test_mcp_call_kb_validate(client):
    client.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
    resp = client.request("tools/call", {
        "name": "kb.validate",
        "arguments": {"rules": {"version": 1, "root": ".", "basis": "g",
                                "kbs": [{"name": "A", "id": "A1",
                                         "paths": [{"match": "**/x/**", "to": "01-区"}]}]}},
    })
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["valid"] is True
    # 黑名单命中 ⇒ invalid
    resp2 = client.request("tools/call", {
        "name": "kb.validate",
        "arguments": {"rules": {"version": 1, "root": ".", "basis": "g",
                                "kbs": [{"name": "A", "id": "A1",
                                         "paths": [{"match": "**/x/**", "to": "data"}]}]}},
    })
    payload2 = json.loads(resp2["result"]["content"][0]["text"])
    assert payload2["valid"] is False
    assert any("GENERIC_NAMES" in e for e in payload2["errors"])


def test_mcp_call_kb_plan_readonly(client):
    client.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
    before = set()
    for dirpath, _dirs, files in os.walk(DEMO_ROOT):
        before.update(os.path.join(dirpath, f) for f in files)
    resp = client.request("tools/call", {"name": "kb.plan",
                                         "arguments": {"root": DEMO_ROOT}})
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["version"] == 1
    assert payload["stats"]["renames"] >= 1
    # S1 标记命中：星海项目下合同文件 → XH-2024 / 02-合同
    hit = [r for r in payload["renames"] if "采购合同" in r["from"]]
    assert hit and hit[0]["kb"] == "XH-2024" and "/02-合同/" in hit[0]["to"]
    # @exclude：过程稿永不入投影
    assert any("过程稿" in e["from"] for e in payload["excluded"])
    # 散件不猜：进 _inbox
    assert any("随手记" in i["from"] for i in payload["inbox"])
    # 只读：扫描面无新增文件
    after = set()
    for dirpath, _dirs, files in os.walk(DEMO_ROOT):
        after.update(os.path.join(dirpath, f) for f in files)
    assert after == before


def test_mcp_call_kb_scan_and_search(client):
    client.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
    resp = client.request("tools/call", {"name": "kb.scan", "arguments": {"root": DEMO_ROOT}})
    scan = json.loads(resp["result"]["content"][0]["text"])
    assert scan["files"] >= 6
    assert scan["candidate_kbs"] and scan["candidate_kbs"][0]["kb"] == "XH-2024"

    resp2 = client.request("tools/call", {"name": "kb.search",
                                          "arguments": {"root": DEMO_ROOT, "query": "采购合同"}})
    search = json.loads(resp2["result"]["content"][0]["text"])
    assert search["channels"]["keyword"] >= 1
    assert search["results"] and search["results"][0]["ref"].startswith("[1] ")


def test_mcp_call_kb_explain_and_unknown_tool(client):
    client.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
    resp = client.request("tools/call", {"name": "kb.explain",
                                         "arguments": {"root": DEMO_ROOT, "path": "不存在.txt"}})
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["found"] is False
    resp2 = client.request("tools/call", {"name": "kb.apply", "arguments": {}})
    assert resp2["error"]["code"] == -32602  # 未注册工具（写盘面只在 CLI）
