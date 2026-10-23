"""单集级别 enrich 刮削测试（v2.50.0）

覆盖：
- TmdbClient.season_episodes：季接口返回集列表（mock _get）
- TmdbClient.find_episode：按集号找到对应集
- TmdbClient.apply_episode：标题/简介/剧照落库规则
"""
import pytest
from types import SimpleNamespace

from backend.emby_server import tmdb as tmdb_mod
from backend.emby_server.tmdb import TmdbClient


@pytest.fixture(autouse=True)
def _clean_cache():
    TmdbClient._cache.clear()
    yield
    TmdbClient._cache.clear()


def _client(monkeypatch) -> TmdbClient:
    monkeypatch.delenv("TMDB_API_KEYS", raising=False)
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    return TmdbClient()


def _mock_season_response():
    return {
        "episodes": [
            {
                "episode_number": 1,
                "name": "摩登到访",
                "overview": "第一集的简介",
                "still_path": "/still1.jpg",
            },
            {
                "episode_number": 2,
                "name": "Episode 2",  # TMDB 占位标题
                "overview": "",
                "still_path": None,
            },
            {
                "episode_number": 3,
                "name": "",
                "overview": "第三集简介",
                "still_path": "/still3.jpg",
            },
        ]
    }


def test_season_episodes_returns_list(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(client, "_get", lambda path, params: _mock_season_response())
    # 禁用磁盘缓存，避免测试污染
    monkeypatch.setattr(tmdb_mod.tmdb_cache, "load_details", lambda *a: (False, None))
    monkeypatch.setattr(tmdb_mod.tmdb_cache, "save_details", lambda *a, **k: True)

    eps = client.season_episodes("12345", 1)
    assert eps is not None
    assert len(eps) == 3
    assert eps[0]["name"] == "摩登到访"
    # 第二次走 L1 缓存，不再调 _get
    called = []
    monkeypatch.setattr(client, "_get", lambda path, params: called.append(1) or _mock_season_response())
    eps2 = client.season_episodes("12345", 1)
    assert eps2 is not None
    assert called == []


def test_find_episode_by_number(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(client, "_get", lambda path, params: _mock_season_response())
    monkeypatch.setattr(tmdb_mod.tmdb_cache, "load_details", lambda *a: (False, None))
    monkeypatch.setattr(tmdb_mod.tmdb_cache, "save_details", lambda *a, **k: True)

    ep = client.find_episode("12345", 1, 1)
    assert ep is not None
    assert ep["name"] == "摩登到访"

    ep2 = client.find_episode("12345", 1, 99)
    assert ep2 is None


def _episode_item(name="第1集", overview=""):
    return SimpleNamespace(name=name, overview=overview)


def test_apply_episode_real_title_overwrites_generic():
    """当前是"第X集"占位，TMDB 有真实标题 → 覆盖"""
    item = _episode_item(name="第1集", overview="")
    data = {"name": "摩登到访", "overview": "简介", "still_path": "/s.jpg"}
    result = TmdbClient.apply_episode(item, data)
    assert result["updated"] is True
    assert item.name == "摩登到访"
    assert item.overview == "简介"
    assert result["still_path"] == "/s.jpg"


def test_apply_episode_placeholder_not_overwrite():
    """TMDB 是占位标题（Episode 2）→ 不覆盖"""
    item = _episode_item(name="第2集", overview="")
    data = {"name": "Episode 2", "overview": "", "still_path": None}
    result = TmdbClient.apply_episode(item, data)
    assert item.name == "第2集"  # 保留
    assert result["updated"] is False


def test_apply_episode_keep_existing_real_title():
    """已有真实标题 → 不覆盖"""
    item = _episode_item(name="真实标题", overview="已有简介")
    data = {"name": "TMDB标题", "overview": "TMDB简介", "still_path": "/s.jpg"}
    result = TmdbClient.apply_episode(item, data)
    assert item.name == "真实标题"
    assert item.overview == "已有简介"  # 不覆盖已有简介
    # 但 still_path 照常返回（调用方决定是否用）
    assert result["still_path"] == "/s.jpg"


def test_apply_episode_empty_tmdb_name():
    """TMDB 标题为空 → 保留"第X集"，只补简介"""
    item = _episode_item(name="第3集", overview="")
    data = {"name": "", "overview": "第三集简介", "still_path": "/s3.jpg"}
    result = TmdbClient.apply_episode(item, data)
    assert item.name == "第3集"
    assert item.overview == "第三集简介"
    assert result["updated"] is True


def test_apply_episode_none_data():
    item = _episode_item()
    result = TmdbClient.apply_episode(item, None)
    assert result["updated"] is False
    assert result["still_path"] is None
