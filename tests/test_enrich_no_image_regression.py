"""回归：已绑定 TMDB、enrich=done 却零图片（2026-11-01 用户截图亲抓）。

生产实证（17 条样本，证据见 PR body）：其中 8 条 TMDB 明明有图，
补资料段（identify_two_stage.apply_fill）在 2026-10-31 那轮也确实
「填上了」poster_path/backdrop_path（enrich_stage_records 的
filled_fields 为证）——但填的是 TMDB **相对路径原值**（"/xxx.jpg"），
而 poster_path 的语义是本地文件路径：图片接口把它当本地文件找，
找不到 → _queue_image_repair 把它清成 NULL 并打 repair 标记 →
条目最终四项图片字段全空、repair 标记无人消费、干净度巡检又被
7 天 min-age 与 file_fingerprint 非空两道门挡住（剧是容器条目、
指纹在集上），永远自愈不了。

钉死：
① 补资料段落 TMDB 图必须走现行图片落库口径（_set_image）：远程
   全量 URL 落 primary_image_url / backdrop_image_url；poster_path
   只许是本地化后的本地路径，绝不许是 TMDB 相对路径原值；
② 已有 primary_image_url 时补资料段不许再往 poster_path 写相对
   路径（同缺陷的潜伏形态）；
③ 两段式补资料段把图补上后，repair 标记必须清掉（现行写库口径
   一致），否则修好的行还挂着修复请求；
④ 干净度巡检：带 repair 标记 = 客户端已实证图坏，绕过 7 天
   min-age 与指纹门（剧容器无指纹）；无 repair 的新鲜行仍不动。
"""
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import enrich_worker as ew
from backend.emby_server import identify_two_stage as ts
from backend.emby_server import models as em
from backend.emby_server import tmdb as tmdb_mod


@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


def _make_item(db, name="敦煌英雄", **kw):
    lib = em.Library(guid=uuid.uuid4().hex, name="华语电影",
                     collection_type="movies")
    db.add(lib)
    db.flush()
    kw.setdefault("enrich_status", "pending")
    item_type = kw.pop("item_type", "movie")
    production_year = kw.pop("production_year", 2026)
    item = em.MediaItem(
        guid=uuid.uuid4().hex, library_id=lib.id, item_type=item_type,
        name=name, sort_name=name, file_path=f"/m/{uuid.uuid4().hex}.mkv",
        production_year=production_year, **kw)
    db.add(item)
    db.flush()
    return item


_DETAILS = {
    "overview": "详情简介",
    "vote_average": 5.0,
    "poster_path": "/tmdb_poster.jpg",
    "backdrop_path": "/tmdb_backdrop.jpg",
}


# ---------- ① 图片落库口径 ----------

def test_fill_lands_tmdb_images_as_remote_urls(db):
    item = _make_item(db, tmdb_id="1089598")
    filled = ts.apply_fill(db, item, {"tmdb_details": dict(_DETAILS)})
    base = tmdb_mod.image_base()
    # 远程全量 URL 落 *_image_url（与现行 _set_image 同口径，客户端可直接取）
    assert item.primary_image_url == f"{base}/w500/tmdb_poster.jpg"
    assert item.backdrop_image_url == f"{base}/w1280/tmdb_backdrop.jpg"
    # poster_path/backdrop_path 只许本地路径：未预热时为空，绝不许是
    # TMDB 相对路径原值（原值会被图片接口当本地文件、404 后清掉）
    assert item.poster_path != "/tmdb_poster.jpg"
    assert item.backdrop_path != "/tmdb_backdrop.jpg"
    assert not item.poster_path and not item.backdrop_path
    assert "primary_image_url" in filled
    assert "backdrop_image_url" in filled


def test_fill_does_not_write_raw_poster_when_remote_url_exists(db):
    item = _make_item(
        db, tmdb_id="1089598",
        primary_image_url="https://image.tmdb.org/t/p/w500/old.jpg")
    ts.apply_fill(db, item, {"tmdb_details": dict(_DETAILS)})
    assert item.primary_image_url.endswith("/old.jpg")
    assert not item.poster_path  # 不许把相对路径塞进本地路径字段


def test_fill_side_poster_still_lands_as_local_path(db):
    """side 本地图（本地文件路径）仍直落 poster_path，行为不变。"""
    item = _make_item(db, tmdb_id="1089598")
    filled = ts.apply_fill(db, item, {"poster": "/mnt/media/poster.jpg",
                                      "tmdb_details": {}})
    assert item.poster_path == "/mnt/media/poster.jpg"
    assert "poster_path" in filled


# ---------- ③ repair 标记消费 ----------

def test_two_stage_fill_clears_repair_flag_after_image_landed(db, monkeypatch):
    monkeypatch.setattr(ts, "IDENTIFY_STAGE_ENABLED", 0)
    monkeypatch.setattr(ts, "FILL_STAGE_ENABLED", 1)
    item = _make_item(db, tmdb_id="1089598", enrich_claim_token="tok",
                      repair_requested_at=datetime.now())
    ew._enrich_apply_two_stage(db, item, {"tmdb_details": dict(_DETAILS)})
    assert item.enrich_status == "done"
    assert item.primary_image_url  # 图已落远程地址
    assert item.repair_requested_at is None  # 修好了就清标记


# ---------- ④ 干净度巡检绕过口 ----------

def _patrol_fetcher(tid, kind):
    return {"id": int(tid), "title": "敦煌英雄",
            "release_date": "2026-09-25"}


@pytest.fixture()
def _patrol_cfg():
    ew._enrich_tuning_cache["at"] = 0.0
    ew._enrich_tuning_cache["cfg"] = None
    yield
    ew._enrich_tuning_cache["at"] = 0.0
    ew._enrich_tuning_cache["cfg"] = None


def test_patrol_requeues_fresh_repair_flagged_series_without_fingerprint(
        db, _patrol_cfg):
    """样本《青衿志》形态：done+已绑定+零图+repair 标记+新鲜+无指纹（剧）。"""
    item = _make_item(
        db, name="青衿志", item_type="series", tmdb_id="283167",
        enrich_status="done", production_year=2025,
        last_scraped_at=datetime.now(),
        repair_requested_at=datetime.now(),
        file_fingerprint=None)
    fetcher = lambda tid, kind: {"id": int(tid), "name": "青衿志",
                                 "first_air_date": "2025-01-01"}
    stats = ew.dirty_done_patrol(db, details_fetcher=fetcher)
    db.refresh(item)
    assert item.enrich_status == "pending"
    assert item.tmdb_id == "283167"  # 绑定没问题，只补图，不清 id
    assert stats["by_reason"].get("no_poster") == 1


def test_patrol_still_leaves_fresh_unflagged_row(db, _patrol_cfg):
    """无 repair 的新鲜行仍受 7 天 min-age 保护，不动（防每日空转）。"""
    item = _make_item(
        db, tmdb_id="1089598", enrich_status="done",
        last_scraped_at=datetime.now(),
        file_fingerprint="fp-fresh")
    stats = ew.dirty_done_patrol(db, details_fetcher=_patrol_fetcher)
    db.refresh(item)
    assert item.enrich_status == "done"
    assert stats["requeued"] == 0
