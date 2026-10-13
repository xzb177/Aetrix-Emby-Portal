"""日志脱敏：密钥 / 令牌不得进日志（用户铁律）。

背景：TMDB 的 api_key 拼在请求 URL 查询参数里，httpx 的 INFO 日志会打印
完整请求 URL，导致 4 把 key 明文进了 ``docker logs aetrix-worker``。
同类问题：管理后台「测试 TMDB key」接口（``admin_scrape._test_one_key``）、
MoviePilot 的 ``?token=``、Telegram Bot API 的 ``/bot<token>/`` 路径。

方案（防住所有出口，而不只是调 httpx 日志级别）：
在 root logger 上装两道防线 ——

1. ``SecretRedactFilter``：在格式化**之前**改写 ``record.msg`` / ``record.args``。
   logger 级别的 filter 先于所有 handler 执行，所以 uvicorn 自带的
   access/error handler 也会被覆盖到。
2. ``SecretRedactFormatter``：在最终输出文本上再脱敏一次。
   覆盖 ``logger.exception`` / ``exc_info=True`` 的堆栈文本（filter 够不到那里，
   堆栈是在 formatter 里从 traceback 对象渲染的）。

脱敏规则（见 ``_PATTERNS``）：
- URL 查询参数 ``api_key / apikey / token / access_token / client_secret /
  secret / password`` 的值 → ``***``
- JSON / dict 风格 ``"api_key": "xxx"`` → ``"api_key": "***"``
- Telegram Bot API 路径 ``/bot<token>/`` → ``/bot***/``

注意：
- 只做**日志输出层**脱敏，不碰业务逻辑、不改请求本身；
- 脱敏函数本身绝不能抛异常（filter 里全程 try/except，日志链路不能崩）；
- 历史日志里已有的明文清不掉（docker 日志靠轮转自然淘汰），本模块只防新增。
"""

from __future__ import annotations

import logging
import re

# ([?&]api_key=)(SECRET...) -> \1***
_PATTERNS: list[tuple[re.Pattern, str]] = [
    (
        re.compile(
            r"([?&](?:api_key|apikey|access_token|client_secret|secret|password|auth_token|token)=)"
            r"([^&\s\"'<>]*)",
            re.IGNORECASE,
        ),
        r"\1***",
    ),
    # "api_key": "SECRET" / 'token'='SECRET'（JSON、dict repr、日志 k=v 片段）
    (
        re.compile(
            r"(['\"]?(?:api_key|apikey|access_token|client_secret|secret|password|auth_token|token)['\"]?"
            r"\s*[:=]\s*['\"])"
            r"([^'\",}\s]+)"
            r"(['\"])",
            re.IGNORECASE,
        ),
        r"\1***\3",
    ),
    # https://api.telegram.org/bot<TOKEN>/sendMessage -> /bot***/
    (
        re.compile(r"(api\.telegram\.org/bot)[^/\s\"'?]+", re.IGNORECASE),
        r"\1***",
    ),
]

# 快筛：没有这些词就直接跳过正则（每条日志都过一遍，省 CPU）
_TRIGGER_WORDS = ("api_key", "apikey", "token", "/bot", "secret", "password")


def redact_secrets(text):
    """把文本里的密钥/令牌明文替换为 ***；非字符串原样返回。"""
    if not isinstance(text, str) or not text:
        return text
    lowered = text.lower()
    if not any(w in lowered for w in _TRIGGER_WORDS):
        return text
    for rx, repl in _PATTERNS:
        text = rx.sub(repl, text)
    return text


class SecretRedactFilter(logging.Filter):
    """格式化前改写 record.msg / record.args：所有 handler 通吃。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact_secrets(record.msg)
            args = record.args
            if args:
                if isinstance(args, dict):
                    record.args = {k: redact_secrets(v) for k, v in args.items()}
                elif isinstance(args, (tuple, list)):
                    record.args = type(args)(redact_secrets(a) for a in args)
        except Exception:
            pass  # 脱敏绝不能把日志链路搞崩
        return True


class SecretRedactFormatter(logging.Formatter):
    """最终输出文本再脱敏一次：覆盖 exc_info 堆栈里的 URL。"""

    def format(self, record: logging.LogRecord) -> str:
        return redact_secrets(super().format(record))


_INSTALLED = False


def install_secret_redaction() -> None:
    """安装脱敏（幂等）：root logger 挂 filter；root 的 handlers 换脱敏 formatter；
    uvicorn 的 access/error logger 也挂 filter（它们有自己的 handler，propagate=False）。

    在 worker.py / main.py 的 ``logging.basicConfig(...)`` 之后调用。
    """
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    filt = SecretRedactFilter()
    root = logging.getLogger()
    root.addFilter(filt)
    for h in root.handlers:
        orig = h.formatter
        if isinstance(orig, SecretRedactFormatter):
            continue
        fmt_str = orig._style._fmt if orig is not None else None  # noqa: SLF001
        datefmt = getattr(orig, "datefmt", None)
        h.setFormatter(SecretRedactFormatter(fmt_str, datefmt=datefmt))
    # uvicorn 自带 handler 的 logger（dictConfig 不会清掉 logger 上已有的 filter）
    for name in ("uvicorn.access", "uvicorn.error", "uvicorn", "httpx", "httpcore"):
        logging.getLogger(name).addFilter(filt)
