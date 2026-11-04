# -*- coding: utf-8 -*-
"""kbfold_feedback_net — KBFold 反馈上行**唯一网络面**（opt-in；默认路径零外连）。

边界（docs/FEEDBACK.md §3 与 `kbfold audit` 自证口径，2026-11-05 YQ-1154 D2）：
  - 本文件是全仓**唯一**允许 import 网络模块的源码件（audit_self 白名单豁免面，
    见 core/py/kbfold/service.py::_SANCTIONED_NETWORK_FILES；其余任何文件命中
    禁网模块即自证失败）；
  - 仅在用户显式执行 `kbfold feedback send` 时由宿主 CLI lazy import，
    plan/apply/undo/search/… 等默认路径零外连、零 import；
  - 只做「单次 POST + HMAC 签名头」，重试/退避/游标/回执/拒收策略归宿主 CLI
    （kbfold_cli.py），本模块不做任何策略决策。

传输契约（开源插件回流规范 v1 §5.2/§7.1；服务端正本 app/oss_feedback_routes.py）：
  - body = {"events": [<信封八字段>, ...]}（单次 ≤20 条、≤64KB）；
  - 签名头：X-OSS-Signature: t=<unix秒>,v1=HMAC-SHA256(secret, t+"."+raw_body)，
    t 偏差 >300s 服务端拒 403；头缺失 401 signature_missing；
  - 密钥：env ``NW_OSS_FEEDBACK_HMAC``（缺省＝无签头发送，宿主负责警告）；
  - 端点：env ``NW_OSS_FEEDBACK_ENDPOINT`` 优先，缺省正式端点。

零第三方依赖：仅 Python 标准库（urllib / hashlib / hmac / json / os / time）。
"""

import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request

DEFAULT_ENDPOINT = "https://www.easurwork.cn/api/oss-feedback/v1"
ENV_ENDPOINT = "NW_OSS_FEEDBACK_ENDPOINT"
ENV_HMAC = "NW_OSS_FEEDBACK_HMAC"


def endpoint_url():
    """上报端点：env NW_OSS_FEEDBACK_ENDPOINT 优先（联调/E2E 用），缺省正式端点。"""
    return (os.environ.get(ENV_ENDPOINT) or "").strip() or DEFAULT_ENDPOINT


def hmac_secret():
    """HMAC 密钥：env NW_OSS_FEEDBACK_HMAC；缺省空串＝无签头发送（宿主警告）。"""
    return (os.environ.get(ENV_HMAC) or "").strip()


def signature_header(raw_body):
    """按规范 §5.2 组签名头。返回 (header_value, warning)：
    - 有密钥 -> ("t=<unix>,v1=<64hex>", None)
    - 无密钥 -> (None, 警告文案)——宿主决定是否继续（规范口径：缺省无签头发送+警告）。"""
    secret = hmac_secret()
    if not secret:
        return None, ("未设置 %s：本次上行不带签名头"
                      "（服务端将按 401 signature_missing 拒收；生产装载前须配密钥）" % ENV_HMAC)
    t = int(time.time())
    mac = hmac.new(secret.encode("utf-8"),
                   ("%d." % t).encode("utf-8") + raw_body,
                   hashlib.sha256).hexdigest()
    return "t=%d,v1=%s" % (t, mac), None


def post_events(events, timeout=10.0):
    """单次 POST（**不重试不退避**——策略在宿主 CLI）。

    events: 信封 dict 列表（宿主已按 ≤20 条/批与 ≤64KB 自控；本模块只管传输）。
    返回 {"status": int|None, "body": dict|str|None, "error": str|None, "signed": bool}：
    - 2xx/4xx/5xx 一律带 status 返回，宿主分流（2xx 推游标 / 422 单条拒收 / 429·5xx 退避）；
    - 网络异常（URLError/超时/OSError）status=None，宿主按可重试面处理。
    """
    raw = json.dumps({"events": events}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        endpoint_url(), data=raw, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8",
                 "User-Agent": "kbfold-feedback/1.0"})
    sig, _warn = signature_header(raw)
    signed = bool(sig)
    if sig:
        req.add_header("X-OSS-Signature", sig)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", resp.getcode())
            raw_body = resp.read()
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            raw_body = e.read()
        except Exception:                                    # noqa: BLE001
            raw_body = b""
    except Exception as e:                                   # noqa: BLE001  # URLError/timeout/OSS错误
        return {"status": None, "body": None,
                "error": "%s: %s" % (type(e).__name__, e), "signed": signed}
    body = _parse_body(raw_body)
    return {"status": status, "body": body, "error": None, "signed": signed}


def _parse_body(raw_body):
    """响应体解析：JSON 优先，失败退原文（截断 500 字符，供 reason 提取与展示）。"""
    if not raw_body:
        return None
    try:
        return json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return raw_body.decode("utf-8", errors="replace")[:500]
