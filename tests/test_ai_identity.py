"""AI 助手身份约束：被问到「你是什么模型」时统一回答「我是本站的 AI 客服助手」。

用户端不应该看到、也不应该能问出底层模型名：

- 服务端在管理员配置的系统提示词**后面**追加身份约束——存量部署的
  ``ai_system_prompt`` 已经落库，只改默认值对它们无效；放在代码里也保证管理员
  改提示词时不会把这条一起改掉；
- 用户端 ``/api/user/ai/ask`` 的响应里不再下发 model 字段（管理端「测试模型」仍回显）。
"""
from types import SimpleNamespace

from backend.integrations import ai


def test_guard_appended_to_admin_prompt():
    out = ai._system_prompt_with_guard("你是客服")
    assert out.startswith("你是客服")
    assert ai.IDENTITY_GUARD in out
    assert "AI 客服助手" in out


def test_guard_not_duplicated_when_already_present():
    once = ai._system_prompt_with_guard("你是客服")
    assert ai._system_prompt_with_guard(once) == once


def test_guard_used_when_admin_prompt_empty():
    assert ai._system_prompt_with_guard("") == ai.IDENTITY_GUARD
    assert ai._system_prompt_with_guard(None) == ai.IDENTITY_GUARD


def test_chat_payload_carries_guard(monkeypatch):
    """真发出去的 messages[0]（system）必须带身份约束"""
    import httpx

    seen = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "好的"}}],
                    "model": "stealth/space-bunny-alpha", "usage": {}}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            seen["payload"] = json
            return _Resp()

    settings = {
        "ai_base_url": "https://api.example.com/v1",
        "ai_model": "stealth/space-bunny-alpha",
        "ai_api_keys": "key-one",
        "ai_system_prompt": "你是客服",
    }
    monkeypatch.setattr(ai, "_settings", lambda db: settings)
    monkeypatch.setattr(httpx, "Client", _Client)

    result = ai.ask(object(), "你是什么模型？")
    assert result["ok"] is True
    system = seen["payload"]["messages"][0]
    assert system["role"] == "system"
    assert "你是客服" in system["content"]
    assert ai.IDENTITY_GUARD in system["content"]


def test_user_ask_response_has_no_model_field(monkeypatch):
    """用户端响应不带 model（气泡里不显示，接口里也不该有）"""
    from backend.api import assistant as assistant_api

    monkeypatch.setattr(ai, "available", lambda db: True)
    monkeypatch.setattr(ai, "daily_limit", lambda db: 20)
    monkeypatch.setattr(ai, "safe_consume", lambda db, uid, limit: (True, 1))
    monkeypatch.setattr(
        ai, "ask",
        lambda db, q, h=None: {"ok": True, "answer": "我是本站的 AI 客服助手",
                               "model": "stealth/space-bunny-alpha"})

    payload = assistant_api.assistant_ask(
        assistant_api.AskRequest(question="你是什么模型？"),
        SimpleNamespace(id=1), object())
    assert payload["answer"] == "我是本站的 AI 客服助手"
    assert "model" not in payload


# ---------- r30 F1：_today() 按 Asia/Shanghai 划分自然日 ----------

def test_today_uses_shanghai_day(monkeypatch):
    """UTC 2026-01-01 16:00 = 北京时间 2026-01-02 00:00：
    _today() 必须返回 2026-01-02（旧实现返回 2026-01-01，用户 00:00–08:00
    的提问被记到「昨天」，配额次日 08:00 才重置）。"""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    real_datetime = datetime

    class _FakeDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            instant = real_datetime(2026, 1, 1, 16, 0, tzinfo=ZoneInfo("UTC"))
            return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)

    monkeypatch.setattr(ai, "datetime", _FakeDateTime)
    assert ai._today() == "2026-01-02"


def test_today_fallback_without_tzdata(monkeypatch):
    """缺 tzdata 等极端情况回退为系统时间，不抛异常。"""
    from datetime import datetime

    real_datetime = datetime

    class _FakeDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                raise ZoneInfoNotFoundError("No time zone found with key Asia/Shanghai")
            return real_datetime(2026, 5, 6, 7, 8)

    from zoneinfo import ZoneInfoNotFoundError
    monkeypatch.setattr(ai, "datetime", _FakeDateTime)
    assert ai._today() == "2026-05-06"


# ---------- r30 F2：safe_consume 读写双失败仍放行 ----------

def test_safe_consume_read_also_fails_still_allows():
    """consume 写失败、used_today 读也失败时：放行且 used=0，不抛异常。"""
    from sqlalchemy.exc import SQLAlchemyError

    class _BadDB:
        def query(self, *a, **k):
            raise SQLAlchemyError("连接池耗尽")

    allowed, used = ai.safe_consume(_BadDB(), user_id=1)
    assert allowed is True
    assert used == 0
