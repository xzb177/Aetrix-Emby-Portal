"""UserData.PlayedPercentage 口径回归（r11）

真实 Emby/Jellyfin 按 ``position_ticks / run_time_ticks * 100`` 计算该字段
（证据：Jellyfin 真实响应 dump——PlaybackPositionTicks=5918547800 /
RunTimeTicks=56177598464 → PlayedPercentage=10.535423303637，精确吻合；
watchstate#459）。三方客户端用该字段画"继续观看"进度条；恒 0.0 会让
看到一半的片子进度条显示 0%。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from types import SimpleNamespace

from backend.emby_server.api import _user_data_dto

# 10_000_000 ticks = 1 秒；用一部 100 分钟的电影做分母
RUNTIME = 100 * 60 * 10_000_000


def _umd(position_ticks=0, played=False):
    return SimpleNamespace(
        playback_position_ticks=position_ticks,
        play_count=1 if played else 0,
        played=played,
        is_favorite=False,
        last_played_at=None,
    )


def test_played_percentage_computed_from_position():
    """看到 40% → 字段约 40（真实 Emby 口径），而不是恒 0.0"""
    d = _user_data_dto(_umd(position_ticks=int(RUNTIME * 0.4)), "item1",
                       runtime_ticks=RUNTIME)
    assert d["PlaybackPositionTicks"] == int(RUNTIME * 0.4)
    assert d["PlayedPercentage"] == __import__("pytest").approx(40.0, abs=0.01)


def test_played_percentage_zero_without_position():
    d = _user_data_dto(_umd(), "item1", runtime_ticks=RUNTIME)
    assert d["PlayedPercentage"] == 0.0


def test_played_percentage_zero_without_runtime():
    """没有时长分母时保持 0.0（不编造）"""
    d = _user_data_dto(_umd(position_ticks=12345), "item1", runtime_ticks=0)
    assert d["PlayedPercentage"] == 0.0
    d2 = _user_data_dto(_umd(position_ticks=12345), "item1")
    assert d2["PlayedPercentage"] == 0.0


def test_played_percentage_clamped():
    """position 超过 runtime（ticks 口径错位）时钳到 100，不溢出"""
    d = _user_data_dto(_umd(position_ticks=RUNTIME * 3), "item1",
                       runtime_ticks=RUNTIME)
    assert d["PlayedPercentage"] == 100.0


def test_played_percentage_no_umd():
    d = _user_data_dto(None, "item1", runtime_ticks=RUNTIME)
    assert d["PlayedPercentage"] == 0.0
    assert d["ItemId"] == "item1"
    assert d["Key"] == "item1"
