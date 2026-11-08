# -*- coding: utf-8 -*-
"""kbfold feedback 命令面测试（YQ-1154 D2 · 2026-11-05）。

覆盖面（任务书 D2 交付件 3）：
  on（同意位/确认文案/档位）/ off（关闭标记）/ show（待上行表格+拒收+回执提示）/
  send（本地 mock HTTP：信封八字段/游标推进/回执码留存/422 单条拒收/5xx 退避≤3/
  HMAC 签名头/无签名警告/端点 env 覆盖/非 TTY 须 --yes 退出码 4/未开反馈退出码 3）/
  clear（本地清空不联网，保留回执与同意位）。

测试策略：进程内调 kbfold_cli.main(...)；HTTP 用标准库 http.server 本地 mock 端点
（env NW_OSS_FEEDBACK_ENDPOINT 注入）；反馈目录用 tmp_path（YQ-1119 隔离纪律：
禁依赖工作区真实状态、夹具一律 tmp_path）。
"""

import hashlib
import hmac as hmac_lib
import json
import os
import re
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_CLI_DIR = os.path.join(os.path.dirname(_HERE), "hosts", "cli")
if _CLI_DIR not in sys.path:
    sys.path.insert(0, _CLI_DIR)
_CORE_PY = os.path.join(os.path.dirname(_HERE), "core", "py")
if _CORE_PY not in sys.path:
    sys.path.insert(0, _CORE_PY)

import kbfold_cli  # noqa: E402
import kbfold_feedback_net as feedback_net  # noqa: E402
from kbfold import feedback as feedback_mod  # noqa: E402


# ---------------------------------------------------------------------------
# 夹具：临时反馈目录 + 本地 mock 端点
# ---------------------------------------------------------------------------

class _MockEndpoint:
    """标准库 mock 端点：记录每次 POST（path/headers/raw/json），按队列回状态码。

    queue=[(status, body), ...]；多于一条时逐条弹出，最后一条常驻（重复请求复用）。"""

    def __init__(self, queue=None):
        self.queue = list(queue) if queue else [(200, {"accepted": 1, "deduped": 0})]
        self.requests = []
        self._server = None

    def start(self):
        outer = self

        class _H(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    parsed = None
                outer.requests.append({"path": self.path, "headers": dict(self.headers),
                                       "raw": raw, "json": parsed})
                if len(outer.queue) > 1:
                    status, body = outer.queue.pop(0)
                else:
                    status, body = outer.queue[0]
                payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):            # noqa: D401 静默
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return "http://127.0.0.1:%d/mock/oss-feedback/v1" % self._server.server_address[1]

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()


@pytest.fixture
def fb_env(tmp_path, monkeypatch):
    """隔离反馈目录（tmp_path；YQ-1119）+ 清掉端点/密钥环境变量。"""
    fbdir = tmp_path / "fbhome"
    monkeypatch.setenv("KBFOLD_FEEDBACK_DIR", str(fbdir))
    monkeypatch.delenv("NW_OSS_FEEDBACK_ENDPOINT", raising=False)
    monkeypatch.delenv("NW_OSS_FEEDBACK_HMAC", raising=False)
    return fbdir


@pytest.fixture
def mock_ep():
    ep = _MockEndpoint()
    yield ep
    ep.stop()


def _run(*argv, tty=False, input_fn=None):
    """进程内跑 CLI：tty 控制 _stdin_isatty 打桩；input_fn 控制确认输入打桩。"""
    monkey_tty = pytest.MonkeyPatch()
    monkey_tty.setattr(kbfold_cli, "_stdin_isatty", lambda: tty)
    if input_fn is not None:
        monkey_tty.setattr("builtins.input", lambda *a, **k: input_fn)
    try:
        return kbfold_cli.main(list(argv))
    finally:
        monkey_tty.undo()


def _seed(fbdir, level, payload, consent="L1"):
    """用 SDK 正途落一条反馈（record），并置同意位（=feedback on 效果）。"""
    sdk = feedback_mod.FeedbackSdk(level=consent, feedback_dir=str(fbdir))
    r = sdk.record(level, payload)
    assert not r.get("dropped"), r
    feedback_mod.save_state({"consent_asked": True, "level": consent,
                             "ts": datetime.now().isoformat(timespec="seconds")},
                            str(fbdir))
    return r


def _state(fbdir):
    return feedback_mod.load_state(str(fbdir))


def _cursor(fbdir):
    path = os.path.join(str(fbdir), "cursor.json")
    if not os.path.isfile(path):
        return {"sent_offset": 0, "sent_count": 0, "updated_ts": ""}   # 无游标=零处理
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _header(req_headers, name):
    """大小写不敏感取请求头（urllib 头名规范化为 X-oss-signature 形态）。"""
    return {str(k).lower(): v for k, v in req_headers.items()}.get(name.lower())


def _rejected(fbdir):
    path = os.path.join(str(fbdir), "rejected.jsonl")
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


def _receipts(fbdir):
    d = os.path.join(str(fbdir), "sentreceipts")
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


_L1_PAYLOAD = {"event_type": "reco_decision", "result": "否决",
               "signal_level": "S2", "confidence": "0.6-0.8"}


# ---------------------------------------------------------------------------
# on / off：同意位与确认文案（规范 §5.1 文案）
# ---------------------------------------------------------------------------

def test_feedback_on_writes_consent_and_spec_copy(fb_env, capsys):
    code = _run("feedback", "on", "L2")
    assert code == 0
    st = _state(fb_env)
    assert st["consent_asked"] is True
    assert st["level"] == "L2"
    assert st["ts"]                                    # 同意位带时间戳
    out = capsys.readouterr().out
    assert "您点选的评价" in out and "「您的评价」" in out          # §5.1 L1 文案
    assert "「大家用出来的改法方向」" in out and "「出错时的匿名故障单」" in out
    assert "永不上行" in out and "反馈无法也无意关联到个人" in out    # §5.1 永不上行
    assert "feedback send" in out and "feedback off" in out


def test_feedback_on_default_level_l1_and_invalid_level(fb_env, capsys):
    assert _run("feedback", "on") == 0
    assert _state(fb_env)["level"] == "L1"
    capsys.readouterr()
    assert _run("feedback", "on", "L9") == 2            # 非法档位用法错误
    assert "L9" in capsys.readouterr().err


def test_feedback_off_writes_marker(fb_env, capsys):
    _run("feedback", "on", "L1")
    capsys.readouterr()
    assert _run("feedback", "off") == 0
    st = _state(fb_env)
    assert st["level"] == "L0" and st["consent_asked"] is True and st["off_ts"]
    assert "恢复零外连" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# show：待上行表格（§6.2 形态）
# ---------------------------------------------------------------------------

def test_feedback_show_lists_pending_with_summary(fb_env, capsys):
    _seed(fb_env, "L1", dict(_L1_PAYLOAD))
    assert _run("feedback", "show") == 0
    out = capsys.readouterr().out
    assert "待上行 1 条" in out and "尚未发送" in out
    assert "L1" in out and "评价：否决（S2 0.6-0.8）" in out
    assert "[feedback send 上行]" in out and "[feedback clear 清空本地]" in out


def test_feedback_show_empty_and_sent_hint(fb_env, capsys):
    assert _run("feedback", "show") == 0
    assert "（无待上行记录）" in capsys.readouterr().out
    # 伪造一次已发送游标（回执提示面）
    _seed(fb_env, "L1", dict(_L1_PAYLOAD))
    kbfold_cli._advance_cursor(str(fb_env), kbfold_cli._pending_items(str(fb_env))[0], as_sent=True)
    capsys.readouterr()
    assert _run("feedback", "show") == 0
    out = capsys.readouterr().out
    assert "已发送 1 条" in out and "sentreceipts" in out


# ---------------------------------------------------------------------------
# send：同意位 / TTY 降级 / 确认交互
# ---------------------------------------------------------------------------

def test_send_requires_consent_exit3(fb_env, mock_ep, capsys):
    monkey_ep = pytest.MonkeyPatch()
    monkey_ep.setenv("NW_OSS_FEEDBACK_ENDPOINT", mock_ep.start())
    try:
        assert _run("feedback", "send", "--yes") == 3   # 未开启 → 提示先 on，退出码 3
        assert "feedback on" in capsys.readouterr().err
        assert mock_ep.requests == []                   # 未同意零请求
    finally:
        mock_ep.stop()
        monkey_ep.undo()


def test_send_non_tty_requires_yes_exit4(fb_env, mock_ep, capsys):
    _seed(fb_env, "L1", dict(_L1_PAYLOAD))
    monkey_ep = pytest.MonkeyPatch()
    monkey_ep.setenv("NW_OSS_FEEDBACK_ENDPOINT", mock_ep.start())
    try:
        assert _run("feedback", "send") == 4            # stdin 非 TTY 且无 --yes
        assert "--yes" in capsys.readouterr().err
        assert mock_ep.requests == []
    finally:
        mock_ep.stop()
        monkey_ep.undo()


def test_send_confirm_prompt_cancel_and_accept(fb_env, mock_ep, capsys):
    _seed(fb_env, "L1", dict(_L1_PAYLOAD))
    monkey_ep = pytest.MonkeyPatch()
    monkey_ep.setenv("NW_OSS_FEEDBACK_ENDPOINT", mock_ep.start())
    try:
        # 交互 TTY + 回答 n → 取消，零请求
        assert _run("feedback", "send", tty=True, input_fn="n") == 0
        out = capsys.readouterr().out
        assert "即将上行 1 条匿名反馈" in out and "已取消" in out
        assert mock_ep.requests == []
        # 交互 TTY + 回答 y → 发送
        assert _run("feedback", "send", tty=True, input_fn="y") == 0
        capsys.readouterr()
        assert len(mock_ep.requests) == 1
    finally:
        mock_ep.stop()
        monkey_ep.undo()


# ---------------------------------------------------------------------------
# send：成功路径（信封/游标/回执码）
# ---------------------------------------------------------------------------

def test_send_success_envelope_cursor_receipt(fb_env, mock_ep, capsys):
    _seed(fb_env, "L1", dict(_L1_PAYLOAD))
    monkey_ep = pytest.MonkeyPatch()
    monkey_ep.setenv("NW_OSS_FEEDBACK_ENDPOINT", mock_ep.start())
    try:
        assert _run("feedback", "send", "--yes") == 0
    finally:
        mock_ep.stop()
        monkey_ep.undo()
    out = capsys.readouterr().out
    # 请求面：{"events":[信封八字段]}，L1 载荷只含 event
    assert len(mock_ep.requests) == 1
    req = mock_ep.requests[0]
    assert req["path"] == "/mock/oss-feedback/v1"        # env 覆盖端点生效
    assert list(req["json"].keys()) == ["events"]
    ev = req["json"]["events"][0]
    assert set(ev.keys()) == {"schema_version", "product", "product_version", "channel",
                              "feedback_level", "nonce", "ts", "payload"}
    assert ev["schema_version"] == "1"                   # 服务端契约：字符串 "1"
    assert ev["product"] == "kbfold" and ev["channel"] == "github_npm"
    assert ev["feedback_level"] == "L1"
    assert re.fullmatch(r"[0-9a-f]{32}", ev["nonce"])    # token_hex(16)=32 hex（幂等键）
    datetime.fromisoformat(ev["ts"])                     # ISO8601
    assert ev["payload"] == {"event": {"type": "reco_decision", "result": "否决",
                                       "signal_level": "S2", "confidence": "0.6-0.8"}}
    assert ev["product_version"] == _repo_version()
    # 发送前打印最终脱敏形态 diff（只 upload 对象）
    assert "将上行的最终脱敏形态" in out and "kbfold.feedback/v1" in out
    # 游标推进 + 回执码留存（服务端无 receipt → nonce 前 6 位）
    cur = _cursor(fb_env)
    assert cur["sent_offset"] > 0 and cur["sent_count"] == 1
    receipts = _receipts(fb_env)
    assert receipts == [ev["nonce"][:6] + ".txt"]
    with open(os.path.join(str(fb_env), "sentreceipts", receipts[0]), encoding="utf-8") as f:
        receipt = json.load(f)
    assert receipt["nonce"] == ev["nonce"] and receipt["receipt_code"] == ev["nonce"][:6]
    # 30 天删除口径文案（§6.3）
    assert "30 天内" in out and "回执码" in out
    # 再发一次 → 无待上行、零新增请求（游标不重发）
    monkey_ep2 = pytest.MonkeyPatch()
    monkey_ep2.setenv("NW_OSS_FEEDBACK_ENDPOINT", "http://127.0.0.1:9/never")
    try:
        assert _run("feedback", "send", "--yes") == 0
        assert "（无待上行记录）" in capsys.readouterr().out
    finally:
        monkey_ep2.undo()


def test_send_receipt_code_from_server_response(fb_env, mock_ep, capsys):
    _seed(fb_env, "L1", dict(_L1_PAYLOAD))
    monkey_ep = pytest.MonkeyPatch()
    monkey_ep.setenv("NW_OSS_FEEDBACK_ENDPOINT", mock_ep.start())
    mock_ep.queue = [(200, {"accepted": 1, "deduped": 0, "receipt": "a1b2c3d9"})]
    try:
        assert _run("feedback", "send", "--yes") == 0
    finally:
        mock_ep.stop()
        monkey_ep.undo()
    assert _receipts(fb_env) == ["a1b2c3d9.txt"]         # 响应含回执码则用之
    assert "a1b2c3d9" in capsys.readouterr().out


def test_send_cursor_advance_no_resend(fb_env, mock_ep, capsys):
    # 第一条：否决；发送后追加第二条：改判 → 第二次只发新条
    _seed(fb_env, "L1", dict(_L1_PAYLOAD, result="否决"))
    monkey_ep = pytest.MonkeyPatch()
    monkey_ep.setenv("NW_OSS_FEEDBACK_ENDPOINT", mock_ep.start())
    try:
        assert _run("feedback", "send", "--yes") == 0
        _seed(fb_env, "L1", dict(_L1_PAYLOAD, result="改判"))
        assert _run("feedback", "send", "--yes") == 0
    finally:
        mock_ep.stop()
        monkey_ep.undo()
    assert len(mock_ep.requests) == 2
    second = mock_ep.requests[1]["json"]["events"][0]["payload"]["event"]
    assert second["result"] == "改判"                    # 第二次只含新记录
    assert _cursor(fb_env)["sent_count"] == 2


# ---------------------------------------------------------------------------
# send：422 单条拒收 / 5xx 退避 / 本地预检拒收
# ---------------------------------------------------------------------------

def test_send_422_marks_rejected_permanent_skip(fb_env, mock_ep, capsys):
    _seed(fb_env, "L1", dict(_L1_PAYLOAD))
    monkey_ep = pytest.MonkeyPatch()
    monkey_ep.setenv("NW_OSS_FEEDBACK_ENDPOINT", mock_ep.start())
    mock_ep.queue = [(422, {"ok": False, "msg": "validation_failed",
                            "errors": [{"index": 0, "reason": "whitelist:result"}]})]
    try:
        assert _run("feedback", "send", "--yes") == 0    # 处理完队列（拒收也是终态）
    finally:
        mock_ep.stop()
        monkey_ep.undo()
    out = capsys.readouterr().out
    assert "whitelist:result" in out                     # reason 码展示
    rows = _rejected(fb_env)
    assert len(rows) == 1 and rows[0]["reason"] == "whitelist:result"
    assert _cursor(fb_env)["sent_offset"] > 0            # 游标越过该条
    assert _receipts(fb_env) == []
    # 永久跳过：再 send 无待上行、不再发请求
    monkey_ep2 = pytest.MonkeyPatch()
    monkey_ep2.setenv("NW_OSS_FEEDBACK_ENDPOINT", "http://127.0.0.1:9/never")
    try:
        assert _run("feedback", "send", "--yes") == 0
        assert "（无待上行记录）" in capsys.readouterr().out
    finally:
        monkey_ep2.undo()
    # show 里可见拒收留痕
    assert _run("feedback", "show") == 0
    assert "whitelist:result" in capsys.readouterr().out


def test_send_5xx_backoff_three_retries_then_abort(fb_env, mock_ep, capsys, monkeypatch):
    _seed(fb_env, "L1", dict(_L1_PAYLOAD))
    monkeypatch.setattr(kbfold_cli, "_BACKOFF_BASE", 0.01)   # 退避调小（测试不真睡）
    monkey_ep = pytest.MonkeyPatch()
    monkey_ep.setenv("NW_OSS_FEEDBACK_ENDPOINT", mock_ep.start())
    mock_ep.queue = [(503, {"ok": False, "msg": "storage_unavailable"})]
    try:
        assert _run("feedback", "send", "--yes") == 1    # 全队列中止 → 退出码 1
    finally:
        mock_ep.stop()
        monkey_ep.undo()
    assert len(mock_ep.requests) == 4                    # 初次 + 重试 3 次
    assert _cursor(fb_env)["sent_offset"] == 0           # 游标未推进
    assert _rejected(fb_env) == []                       # 5xx 不烧记录
    assert "重试 3 次后放弃" in capsys.readouterr().err


def test_send_5xx_then_success_recovers(fb_env, mock_ep, capsys, monkeypatch):
    _seed(fb_env, "L1", dict(_L1_PAYLOAD))
    monkeypatch.setattr(kbfold_cli, "_BACKOFF_BASE", 0.01)
    monkey_ep = pytest.MonkeyPatch()
    monkey_ep.setenv("NW_OSS_FEEDBACK_ENDPOINT", mock_ep.start())
    mock_ep.queue = [(503, {"ok": False, "msg": "storage_unavailable"}),
                     (200, {"accepted": 1, "deduped": 0})]
    try:
        assert _run("feedback", "send", "--yes") == 0    # 重试第 1 次成功
    finally:
        mock_ep.stop()
        monkey_ep.undo()
    assert len(mock_ep.requests) == 2
    assert _cursor(fb_env)["sent_count"] == 1


def test_send_local_residue_reject_no_http(fb_env, mock_ep, capsys):
    # confidence 位置夹带 11 位手机号（通过 SDK confidence 白名单正则、进上传载荷）
    # → prepare_upload 原文残留检测命中 → 本地标 rejected，零网络请求
    _seed(fb_env, "L1", {"event_type": "reco_decision", "result": "否决",
                         "confidence": "13812345678"})
    monkey_ep = pytest.MonkeyPatch()
    monkey_ep.setenv("NW_OSS_FEEDBACK_ENDPOINT", mock_ep.start())
    try:
        assert _run("feedback", "send", "--yes") == 0
    finally:
        mock_ep.stop()
        monkey_ep.undo()
    assert mock_ep.requests == []
    rows = _rejected(fb_env)
    assert len(rows) == 1 and "residue" in rows[0]["reason"]


def test_send_l2_wire_payload_no_event_key(fb_env, mock_ep, capsys):
    # L2 记录：服务端 L2_KEYS 禁含 event → wire 载荷只含 rule_delta/template_hits/structure_hash
    _seed(fb_env, "L2", {"event_type": "reco_decision", "result": "采纳",
                         "rule_delta": {"rule_id": "R1", "field": "capacity",
                                        "before": 3, "after": 5},
                         "template_hits": {"tpl_stage8": 3},
                         "structure_hash": "0123456789abcdef"}, consent="L2")
    monkey_ep = pytest.MonkeyPatch()
    monkey_ep.setenv("NW_OSS_FEEDBACK_ENDPOINT", mock_ep.start())
    try:
        assert _run("feedback", "send", "--yes") == 0
    finally:
        mock_ep.stop()
        monkey_ep.undo()
    ev = mock_ep.requests[0]["json"]["events"][0]
    assert ev["feedback_level"] == "L2"
    assert ev["payload"] == {"rule_delta": {"rule_id": "R1", "field": "capacity",
                                            "before": 3, "after": 5},
                             "template_hits": {"tpl_stage8": 3},
                             "structure_hash": "0123456789abcdef"}
    assert _cursor(fb_env)["sent_count"] == 1


# ---------------------------------------------------------------------------
# send：HMAC 签名头 / 无签名警告 / 端点覆盖
# ---------------------------------------------------------------------------

def test_send_hmac_signature_header(fb_env, mock_ep, capsys, monkeypatch):
    _seed(fb_env, "L1", dict(_L1_PAYLOAD))
    secret = "nw-oss-test-secret"
    monkey_ep = pytest.MonkeyPatch()
    monkey_ep.setenv("NW_OSS_FEEDBACK_ENDPOINT", mock_ep.start())
    monkey_ep.setenv("NW_OSS_FEEDBACK_HMAC", secret)
    try:
        assert _run("feedback", "send", "--yes") == 0
    finally:
        mock_ep.stop()
        monkey_ep.undo()
    header = _header(mock_ep.requests[0]["headers"], "X-OSS-Signature")
    assert header
    m = re.fullmatch(r"t=(\d{1,13}),v1=([0-9a-f]{64})", header)
    assert m, header                                     # 规范 §5.2 头格式
    expected = hmac_lib.new(secret.encode("utf-8"),
                            ("%s." % m.group(1)).encode("utf-8") + mock_ep.requests[0]["raw"],
                            hashlib.sha256).hexdigest()
    assert expected == m.group(2)                        # HMAC-SHA256(secret, t+"."+raw_body)
    assert "NW_OSS_FEEDBACK_HMAC" not in capsys.readouterr().err


def test_send_unsigned_warns_but_sends(fb_env, mock_ep, capsys):
    _seed(fb_env, "L1", dict(_L1_PAYLOAD))
    monkey_ep = pytest.MonkeyPatch()
    monkey_ep.setenv("NW_OSS_FEEDBACK_ENDPOINT", mock_ep.start())
    try:
        assert _run("feedback", "send", "--yes") == 0    # 缺省＝无签头发送+警告
    finally:
        mock_ep.stop()
        monkey_ep.undo()
    assert _header(mock_ep.requests[0]["headers"], "X-OSS-Signature") is None
    assert "NW_OSS_FEEDBACK_HMAC" in capsys.readouterr().err


def test_endpoint_env_override_and_default_constant(fb_env, mock_ep):
    # 缺省端点＝规范 §7.1 正式端点
    assert feedback_net.DEFAULT_ENDPOINT == "https://www.easurwork.cn/api/oss-feedback/v1"
    assert feedback_net.ENV_ENDPOINT == "NW_OSS_FEEDBACK_ENDPOINT"
    # env 覆盖生效（上面全部 mock 用例即走覆盖端点；此处再专项断言 path）
    _seed(fb_env, "L1", dict(_L1_PAYLOAD))
    monkey_ep = pytest.MonkeyPatch()
    monkey_ep.setenv("NW_OSS_FEEDBACK_ENDPOINT", mock_ep.start())
    try:
        assert _run("feedback", "send", "--yes") == 0
    finally:
        mock_ep.stop()
        monkey_ep.undo()
    assert mock_ep.requests[0]["path"] == "/mock/oss-feedback/v1"


# ---------------------------------------------------------------------------
# 端点外置（T13 #8，2026-11-08）：env > 配置文件 .kbfold/endpoint.json > 代码缺省
# ---------------------------------------------------------------------------

def _write_endpoint_cfg(fbdir, obj):
    fbdir.mkdir(parents=True, exist_ok=True)
    (fbdir / feedback_net.CONFIG_ENDPOINT_FILE).write_text(
        json.dumps(obj), encoding="utf-8")


def test_endpoint_config_file_overrides_default(fb_env, mock_ep):
    """配置文件键 endpoint 覆盖代码缺省：send 走 mock path（不设 env）。"""
    _seed(fb_env, "L1", dict(_L1_PAYLOAD))
    _write_endpoint_cfg(fb_env, {"endpoint": mock_ep.start()})
    try:
        assert _run("feedback", "send", "--yes") == 0
    finally:
        mock_ep.stop()
    assert mock_ep.requests[0]["path"] == "/mock/oss-feedback/v1"


def test_endpoint_env_beats_config_file(fb_env, mock_ep):
    """env 优先于配置文件：两处都设时走 env 端点，配置文件不生效。"""
    _write_endpoint_cfg(fb_env, {"endpoint": "https://config.invalid/v1"})
    url = feedback_net.endpoint_url(str(fb_env))
    monkey_ep = pytest.MonkeyPatch()
    monkey_ep.setenv("NW_OSS_FEEDBACK_ENDPOINT", mock_ep.start())
    try:
        assert feedback_net.endpoint_url(str(fb_env)).startswith("http://127.0.0.1:")
    finally:
        monkey_ep.undo()
    assert url == "https://config.invalid/v1"          # env 缺席时配置文件生效


def test_endpoint_config_file_bad_or_empty_falls_back(fb_env):
    """坏 JSON / 空值 / 缺文件 / base 缺席：一律回退代码缺省，不炸。"""
    assert feedback_net.endpoint_url(None) == feedback_net.DEFAULT_ENDPOINT
    assert feedback_net.endpoint_url(str(fb_env)) == feedback_net.DEFAULT_ENDPOINT
    _write_endpoint_cfg(fb_env, {"endpoint": "   "})
    assert feedback_net.endpoint_url(str(fb_env)) == feedback_net.DEFAULT_ENDPOINT
    (fb_env / feedback_net.CONFIG_ENDPOINT_FILE).write_text(
        "{ not-json", encoding="utf-8")
    assert feedback_net.endpoint_url(str(fb_env)) == feedback_net.DEFAULT_ENDPOINT


# ---------------------------------------------------------------------------
# clear：本地清空（不联网），保留同意位与回执
# ---------------------------------------------------------------------------

def test_clear_resets_local_but_keeps_state_and_receipts(fb_env, mock_ep, capsys):
    _seed(fb_env, "L1", dict(_L1_PAYLOAD))
    monkey_ep = pytest.MonkeyPatch()
    monkey_ep.setenv("NW_OSS_FEEDBACK_ENDPOINT", mock_ep.start())
    try:
        assert _run("feedback", "send", "--yes") == 0    # 产游标+回执
    finally:
        mock_ep.stop()
        monkey_ep.undo()
    _seed(fb_env, "L1", dict(_L1_PAYLOAD, result="改判"))  # 再落一条待上行
    capsys.readouterr()
    receipts_before = _receipts(fb_env)
    assert receipts_before                               # 前置：确有回执
    assert _run("feedback", "clear") == 0
    out = capsys.readouterr().out
    assert "已清空" in out and "不联网" in out
    assert _pending_items_count(fb_env) == 0             # feedback.jsonl 已截断
    assert _cursor(fb_env)["sent_offset"] == 0
    assert _rejected(fb_env) == []
    assert _receipts(fb_env) == receipts_before          # 已发回执保留（30 天删除凭据）
    st = _state(fb_env)
    assert st["consent_asked"] is True                   # 同意位保留（clear≠off）
    assert _run("feedback", "show") == 0
    assert "已发送 1 条" in capsys.readouterr().out


def _pending_items_count(fbdir):
    return len(kbfold_cli._pending_items(str(fbdir)))


def _repo_version():
    root = os.path.dirname(_HERE)                        # tests/ -> 仓根
    with open(os.path.join(root, "package.json"), encoding="utf-8") as f:
        return json.load(f)["version"]


# ---------------------------------------------------------------------------
# audit：唯一网络面豁免后仍自证通过（其余文件禁网模块零命中）
# ---------------------------------------------------------------------------

def test_audit_still_green_with_sanctioned_surface(capsys):
    code = kbfold_cli.main(["audit"])
    out = capsys.readouterr().out
    assert code == 0
    assert "禁网模块命中：0" in out and "删除动作命中：0" in out
    assert "hosts/cli/kbfold_feedback_net.py" in out      # 豁免面明示
