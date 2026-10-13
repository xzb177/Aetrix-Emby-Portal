"""日志脱敏测试：密钥/令牌不得进日志。

覆盖：
1. redact_secrets 纯函数：httpx 请求行、?token=、Telegram /bot 路径、JSON 风格
2. 端到端：真实 httpx 请求打到本地桩服务，INFO 日志里只有 api_key=***，无明文
3. 堆栈路径：exc_info 的 traceback 文本也被脱敏
4. 误伤检查：已掩码的 ****1234、普通文本原样不动
"""

from __future__ import annotations

import io
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from backend.log_redact import (
    SecretRedactFilter,
    SecretRedactFormatter,
    install_secret_redaction,
    redact_secrets,
)


def test_redact_httpx_request_line():
    line = (
        'HTTP Request: GET https://api.themoviedb.org/3/configuration'
        '?language=zh-CN&api_key=SECRET123abc "HTTP/1.1 200 OK"'
    )
    out = redact_secrets(line)
    assert "SECRET123abc" not in out
    assert "api_key=***" in out
    assert "language=zh-CN" in out  # 非密钥参数保留


def test_redact_token_param():
    out = redact_secrets("GET https://mp.example.com/api/v1/subscribe/list?token=MPTOKEN999")
    assert "MPTOKEN999" not in out
    assert "token=***" in out


def test_redact_telegram_bot_path():
    out = redact_secrets("POST https://api.telegram.org/bot123456:ABC-DEF1234ghIkl/sendMessage")
    assert "123456:ABC-DEF1234ghIkl" not in out
    assert "api.telegram.org/bot***" in out


def test_redact_json_style():
    out = redact_secrets('payload: {"api_key": "JSONSECRET1"} done')
    assert "JSONSECRET1" not in out
    assert '"api_key": "***"' in out


def test_no_false_positive_on_masked_and_plain():
    # 已掩码的只显示后四位：不动
    assert redact_secrets("TMDB 密钥 ****1234 冷却 300 秒") == "TMDB 密钥 ****1234 冷却 300 秒"
    # 普通文本：不动
    plain = "enrich done 7365 pending 82187 probe 失败率 5.32%"
    assert redact_secrets(plain) == plain
    # 非字符串：原样返回
    assert redact_secrets(None) is None
    assert redact_secrets(123) == 123


class _Stub(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


@pytest.fixture()
def stub_server():
    srv = HTTPServer(("127.0.0.1", 0), _Stub)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def _capture_logger(name: str):
    """给指定 logger 挂 StringIO handler（脱敏 formatter），返回 (logger, buf)。"""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(SecretRedactFormatter("%(name)s - %(levelname)s - %(message)s"))
    lg = logging.getLogger(name)
    lg.addFilter(SecretRedactFilter())
    lg.addHandler(handler)
    lg.setLevel(logging.INFO)
    lg.propagate = False
    return lg, buf, handler


def test_e2e_httpx_info_log_has_no_plaintext_key(stub_server):
    """真实 httpx 请求：INFO 日志里的 URL 只有脱敏后的 api_key。"""
    import httpx

    raw_key = "E2E_REAL_KEY_9f8e7d6c"
    lg, buf, handler = _capture_logger("test-redact-e2e")
    hx = logging.getLogger("httpx")
    hx.addFilter(SecretRedactFilter())
    hx.setLevel(logging.INFO)
    old_propagate, old_level = hx.propagate, hx.level
    hx.propagate = False
    hx.handlers = []
    hx.addHandler(handler)
    try:
        # trust_env=False：不读沙箱的代理环境变量（测试只关心日志脱敏本身）
        with httpx.Client(trust_env=False) as client:
            client.get(f"{stub_server}/3/configuration", params={"api_key": raw_key}, timeout=5)
    finally:
        hx.removeHandler(handler)
        hx.propagate, hx.level = old_propagate, old_level
        lg.removeHandler(handler)
    out = buf.getvalue()
    assert "HTTP Request: GET" in out  # 日志本身还在（信号保留）
    assert raw_key not in out, f"明文 key 进了日志: {out}"
    assert "api_key=***" in out


def test_traceback_is_redacted():
    lg, buf, handler = _capture_logger("test-redact-tb")
    raw_key = "TB_SECRET_112233"
    try:
        try:
            raise ValueError(f"boom https://api.themoviedb.org/3/x?api_key={raw_key}")
        except ValueError:
            lg.exception("test traceback")
    finally:
        lg.removeHandler(handler)
    out = buf.getvalue()
    assert raw_key not in out, f"堆栈里有明文 key: {out}"
    assert "api_key=***" in out


def test_install_is_idempotent_and_covers_root(capsys):
    install_secret_redaction()
    install_secret_redaction()  # 第二次是空操作，不抛异常
    root = logging.getLogger()
    assert any(isinstance(f, SecretRedactFilter) for f in root.filters)
