"""Episodes 接口对不匹配的 SeasonId 必须容错，不能静默返回空。

生产事故：iOS 客户端在剧集详情页「第几集」整片空白。
日志显示它对两部不同的剧都发了同一个 SeasonId（某部剧的季）::

    GET /emby/Shows/925dceb4(黑夜告白)/Episodes?SeasonId=1df96b03(非来不可的季)  200
    GET /emby/Shows/d168729b(Grow Up Show)/Episodes?SeasonId=1df96b03(非来不可的季) 200

旧实现只要能按 guid 查到那个 season，就无条件把 ``parent_id == season.id``
套上去——而它不是本剧的季，于是 200 + 空 Items。客户端无法区分「这季没集」
和「参数错了」，只能显示空白。

正确行为：SeasonId 不属于本剧时**忽略这个筛选**，按本剧口径返回全部集。
宁可多给，不可给空。
"""
import os
import tempfile
import uuid
from datetime import datetime

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db")
os.close(_fd)

from backend import database as _dbmod  # noqa: E402
from sqlalchemy import create_engine as _ce  # noqa: E402
from sqlalchemy.orm import sessionmaker as _sm  # noqa: E402

# 必须走 configure_session_local：直接赋值会把 SessionLocal 代理顶掉，
# 之后各模块 import 到的是被冻结的真 factory，又会查旧库。

from backend.database import init_db  # noqa: E402


import pytest  # noqa: E402

from backend.emby_server import api  # noqa: E402
from backend.emby_server import models as em  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _hygiene_module_engine():
    """测试卫生：本文件独占临时库在模块夹具内换绑、模块结束原样还原。

    导入期顶层换绑会让全局 engine/SessionLocal 在 collection 后停在本文件
    的临时库上，后面的文件/用例撞 no such table 或读错库（#1668 同族）；
    换绑只在本模块用例期间生效。
    """
    _hyg_saved_engine = _dbmod.engine
    _hyg_saved_factory = _dbmod.SessionLocal._factory
    _hyg_saved_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = f"sqlite:///{_tmppath}"
    _dbmod.engine = _ce(os.environ["DATABASE_URL"])
    _dbmod.configure_session_local(_sm(bind=_dbmod.engine))
    init_db()
    _hyg_engine = _dbmod.engine
    try:
        yield
    finally:
        _dbmod.engine = _hyg_saved_engine
        _dbmod.configure_session_local(_hyg_saved_factory)
        if _hyg_saved_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = _hyg_saved_url
        _hyg_engine.dispose()

def _mk(name, item_type, **kw):
    return em.MediaItem(guid=uuid.uuid4().hex, name=name, item_type=item_type,
                        library_id=1, date_added=datetime.now(), **kw)


@pytest.fixture()
def shows():
    """建两部剧各一季；剧 A 有 3 集。返回 (A_series, A_season, B_season)"""
    db = _dbmod.SessionLocal()
    try:
        a_series = _mk("黑夜告白", "series")
        b_series = _mk("非来不可", "series")
        db.add_all([a_series, b_series])
        db.flush()
        a_season = _mk("第 1 季", "season", series_id=a_series.id,
                       parent_id=a_series.id, season_number=1)
        b_season = _mk("第 1 季", "season", series_id=b_series.id,
                       parent_id=b_series.id, season_number=1)
        db.add_all([a_season, b_season])
        db.flush()
        for i in range(3):
            db.add(_mk(f"第 {i + 1} 集", "episode", series_id=a_series.id,
                       parent_id=a_season.id, season_number=1, episode_number=i + 1))
        db.commit()
        yield a_series, a_season, b_season
        db.query(em.MediaItem).delete()
        db.commit()
    finally:
        db.close()


def _episodes(db, series, season):
    """复刻 get_episodes 修复后的筛选逻辑"""
    query = db.query(em.MediaItem).filter(
        em.MediaItem.series_id == series.id, em.MediaItem.item_type == "episode")
    if season and api._season_belongs_to(season, series):
        query = query.filter(em.MediaItem.parent_id == season.id)
    return query.count()


def test_mismatched_season_id_returns_episodes_not_empty(shows):
    """核心回归：带别的剧的 SeasonId，必须返回本剧全部集，不能是空"""
    a_series, _a_season, b_season = shows
    db = _dbmod.SessionLocal()
    try:
        assert not api._season_belongs_to(b_season, a_series)
        assert _episodes(db, a_series, b_season) == 3, \
            "不匹配时应忽略筛选并返回本剧全部集"
    finally:
        db.close()


def test_matching_season_id_still_filters(shows):
    """匹配的 SeasonId 行为不变：仍按季过滤"""
    a_series, a_season, _ = shows
    db = _dbmod.SessionLocal()
    try:
        assert api._season_belongs_to(a_season, a_series)
        assert _episodes(db, a_series, a_season) == 3
    finally:
        db.close()


def test_no_season_id_returns_all(shows):
    a_series, _, _ = shows
    db = _dbmod.SessionLocal()
    try:
        assert _episodes(db, a_series, None) == 3
    finally:
        db.close()


def test_season_belongs_to_rejects_non_season():
    """episode/movie 之类的条目不能当季用"""
    assert not api._season_belongs_to(em.MediaItem(item_type="episode"),
                                      em.MediaItem(item_type="series"))
    assert not api._season_belongs_to(em.MediaItem(item_type="movie"),
                                      em.MediaItem(item_type="series"))


def test_season_compare_against_season_itself():
    """show 本身是 season 时，只有它自己算「属于」"""
    a = em.MediaItem(id=1, item_type="season", series_id=9)
    b = em.MediaItem(id=2, item_type="season", series_id=9)
    assert api._season_belongs_to(a, a)
    assert not api._season_belongs_to(b, a)


def test_season_of_same_series_belongs():
    a_series = em.MediaItem(id=1, item_type="series")
    s1 = em.MediaItem(id=2, item_type="season", series_id=1)
    s2 = em.MediaItem(id=3, item_type="season", series_id=1)
    assert api._season_belongs_to(s1, a_series)
    assert api._season_belongs_to(s2, a_series)


def test_no_unconditional_season_filter_in_source():
    """防回归：源码里不能再出现「查到季就无条件套 parent_id」的老写法

    真正的端到端验证（带真实鉴权打 EA 接口）在部署后对生产做，
    这里只守住逻辑本身，避免以后改回去。
    """
    import inspect

    src = inspect.getsource(api.get_episodes)
    assert "_season_belongs_to" in src, "get_episodes 必须校验季是否属于本剧"
    # 老写法：if season: 直接 filter，中间没有归属判断
    assert "if season:" not in src, "不能只判断季存在就套用，必须校验归属"
