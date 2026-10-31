# tests/test_invitation_code_log_mask.py
"""A-128 回归：邀请码原文不许进日志。

审计 R48 实证：emby_portal.py:494 与 invitation.py:157/179/230 四处
logger 行记了仍有效的邀请码原文，且裸 ``code=`` 片段全程旁路
log_redact（无 ``[?&]`` 前缀、值后无引号）。

本文件第一部分是静态门禁：直接扫两文件源码里的 logger 调用，
断言没有任何一处把原始码变量（code_str / req.invitation_code /
invite_code）当参数传进日志、且格式串里不再出现 ``code=%s``。
修法是调用点改记码 id（invitation.py 三处，行对象在手）或掩码
（emby_portal.py 一处，行对象不在手），**不是**把裸 ``code=``
写进脱敏词表（upstream 任务 kind 码会误伤，见 R48 报告）。

门禁只读源码、不 import 业务模块，任何 Python 环境都能跑；
第二部分的掩码单测与行为测试 import 业务模块，与仓库其他
邀请测试同一口径（SQLite 内存库 + 直接调 apply_invitation）。
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, ".")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.api.invitation import _mask_invite_code, apply_invitation

REPO = Path(__file__).resolve().parents[1]
TARGETS = [
    REPO / "backend" / "api" / "invitation.py",
    REPO / "backend" / "api" / "emby_portal.py",
]
# 原始码变量名：出现在 logger 调用里即判定泄漏（code_id / 掩码不在此列）。
RAW_CODE_VARS = ("code_str", "req.invitation_code", "invite_code")


def _logger_calls(source: str) -> list[str]:
    """用括号配对抠出源码里全部 logger.*(...) 调用文本（含跨行调用）。"""
    calls: list[str] = []
    for m in re.finditer(r"logger\.\w+\s*\(", source):
        i = m.end() - 1  # 指向左括号
        depth = 0
        quote: str | None = None
        escaped = False
        j = i
        while j < len(source):
            ch = source[j]
            if quote is not None:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == quote:
                    quote = None
            else:
                if ch in ("'", '"'):
                    quote = ch
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        calls.append(source[m.start():j + 1])
                        break
            j += 1
    return calls


def _split_top_level_args(inner: str) -> list[str]:
    """按顶层逗号切分调用参数（括号/引号内的逗号不切）。"""
    args: list[str] = []
    depth = 0
    quote: str | None = None
    escaped = False
    start = 0
    for j, ch in enumerate(inner):
        if quote is not None:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
        else:
            if ch in ("'", '"'):
                quote = ch
            elif ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
            elif ch == "," and depth == 0:
                args.append(inner[start:j])
                start = j + 1
    args.append(inner[start:])
    return [a.strip() for a in args]


def _first_string_literal(text: str) -> str | None:
    """取调用第一个字符串字面量的内容（即 logger 的格式串），没有则返回 None。"""
    m = re.search(r"""(['"])((?:\\\1|.)*?)\1""", text, re.DOTALL)
    return m.group(2) if m else None


def test_no_logger_call_logs_raw_invitation_code():
    """门禁：两文件里任何 logger 调用都不许裸传原始码、格式串不许含 code=%s。

    注意：``_mask_invite_code(req.invitation_code)`` 这种经掩码函数
    包过的传参是允许的——判定只认“裸变量本身就是 %s 实参”
    （如 ``..., req.invitation_code,``）或格式串含 ``code=%s``。
    """
    offenders: list[str] = []
    for path in TARGETS:
        assert path.is_file(), f"目标文件缺失: {path}"
        for call in _logger_calls(path.read_text(encoding="utf-8")):
            fmt = _first_string_literal(call)
            inner = call[call.index("(", call.index("logger.")) + 1:-1]
            args = _split_top_level_args(inner)
            bad: list[str] = []
            if fmt is not None and "code=%s" in fmt:
                bad.append("format-has-code=%s")
            for a in args[1:]:
                if a in RAW_CODE_VARS:
                    bad.append(f"bare-arg:{a}")
            if bad:
                offenders.append(f"{path.name}: {call[:160]!r} ({bad})")
    assert not offenders, "以下 logger 调用仍记邀请码原文:\n" + "\n".join(offenders)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ABCDEFGH", "A******H"),  # 8 位码只露首尾各 1 位
        ("ABCDE", "A***E"),
        ("AB", "****"),  # 短码全掩
        ("A", "****"),
        ("", ""),
        (None, ""),
    ],
)
def test_mask_invite_code(raw, expected):
    assert _mask_invite_code(raw) == expected


def _make_user(db, username: str) -> models.WebUser:
    cols = {c.name for c in models.WebUser.__table__.columns}
    kwargs = {"username": username}
    for field, value in (("password_hash", "x"), ("is_active", True)):
        if field in cols:
            kwargs[field] = value
    user = models.WebUser(**kwargs)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_apply_invitation_whitelist_miss_logs_code_id_not_raw_code(caplog):
    """行为回归：白名单未命中路径的日志含 code_id、不含码原文。

    旧代码在该路径记 ``code=ABCDEFGH``；修后记 ``code_id=<行 id>``。
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    models.Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)
    db = SessionLocal()
    try:
        inviter = _make_user(db, "a128-inviter")
        code = models.InvitationCode(
            code="ABCDEFGH",
            user_id=inviter.id,
            is_active=True,
            max_uses=100,
            use_count=0,
            whitelist="someone-else",
        )
        db.add(code)
        db.commit()
        db.refresh(code)
        invitee = _make_user(db, "a128-invitee")
        with caplog.at_level(logging.INFO, logger="backend.api.invitation"):
            result = apply_invitation(db, invitee, "ABCDEFGH")
        assert result == {"applied": False}
        assert "ABCDEFGH" not in caplog.text
        assert f"code_id={code.id}" in caplog.text
    finally:
        db.close()
