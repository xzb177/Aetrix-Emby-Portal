"""TG「更新入库」播报（tg_bot.updatedigest）测试。

覆盖口径：
- 格式：电视剧行 📺名 (年份) SxxExx（连续集连写范围、多段以「、」连）、
  电影行 🎬名 (年份)、无年份不带括号段；一条消息先剧集栏后电影栏。
- 只报已绑定身份（identity_gate 同源）：未绑定电影不报；集按所属剧
  判据（豆瓣-only 身份的剧其新集可报，未绑定剧的新集不报）；
  合并副本行、超 14 天窗口的存量不报。
- 防重复：发送成功才记 tg_update_digest_sent，第二批不重播；
  每栏 10 行上限＋「……本批共 N 部/剧」，余量顺延下一批。
- 首跑基线种子：上线前已在货架的条目记 baseline 不播报，之后
  新入库的正常播；种子时未绑定的行日后绑定仍可播。
- 配图：优先电影横幅图（本地 backdrop_path），无图纯文字；
  远程 backdrop 走 image_store.localize。
- 开关/间隔：总开关关不推；last_run 未到间隔不重复攒批。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

sys.path.insert(0, ".")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.emby_server import models as em
from backend.integrations import store
from backend.tg_bot import sender, updatedigest

GROUP_ID = -1001234567890
NOW = datetime(2026, 11, 3, 9, 30)

_seq = [0]


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    for tbl in (
        models.SystemConfig.__table__,
        models.TgUpdateDigestSent.__table__,
        em.MediaItem.__table__,
    ):
        tbl.create(engine, checkfirst=True)
    store.invalidate()
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def _set_cfg(db, key, value):
    row = db.query(models.SystemConfig).filter_by(key=key).first()
    if row:
        row.value = str(value)
    else:
        db.add(models.SystemConfig(key=key, value=str(value)))
    db.commit()
    store.invalidate()


def _ready(db):
    """群已配置＋基线已种子（跳过首跑种子，专测播报本体）。"""
    _set_cfg(db, "bot_group_ids", str(GROUP_ID))
    _set_cfg(db, updatedigest.SEEDED_KEY, "1")


def _item(db, item_type, name, *, year=None, tmdb=None, external=None,
          added=None, **kw):
    _seq[0] += 1
    it = em.MediaItem(
        guid=f"{_seq[0]:032x}",
        library_id=58,
        item_type=item_type,
        name=name,
        production_year=year,
        tmdb_id=tmdb,
        external_ids=external,
        date_added=added or (NOW - timedelta(hours=1)),
        **kw,
    )
    db.add(it)
    db.commit()
    return it


def _capture_send(monkeypatch):
    sent = {"messages": [], "photos": []}

    def fake_message(db, chat_id, text, reply_markup=None, parse_mode="HTML"):
        sent["messages"].append({"chat_id": chat_id, "text": text})
        return True, None

    def fake_photo(db, chat_id, caption, reply_markup=None, photo_path=None,
                   parse_mode="HTML"):
        sent["photos"].append(
            {"chat_id": chat_id, "caption": caption, "photo_path": photo_path}
        )
        return True, None

    monkeypatch.setattr(sender, "send_message", fake_message)
    monkeypatch.setattr(sender, "send_photo", fake_photo)
    return sent


# ---------- 格式 ----------

def test_episode_segments():
    assert updatedigest._episode_segments([38]) == "E38"
    assert updatedigest._episode_segments([7]) == "E07"
    assert updatedigest._episode_segments([15, 16, 17, 18]) == "E15-E18"
    assert updatedigest._episode_segments([18, 15, 16]) == "E15-E16、E18"
    assert updatedigest._episode_segments([188]) == "E188"


def test_lines_format():
    movie = em.MediaItem(name="梦寻苏毗女儿国", production_year=2026)
    assert updatedigest.format_movie_line(movie) == "🎬梦寻苏毗女儿国 (2026)"
    no_year = em.MediaItem(name="无年份片", production_year=None)
    assert updatedigest.format_movie_line(no_year) == "🎬无年份片"
    series = em.MediaItem(name="绝世战魂", production_year=2024)
    ep = em.MediaItem(episode_number=38)
    group = {"series": series, "season": 2, "episodes": [ep]}
    assert updatedigest.format_episode_line(group) == "📺绝世战魂 (2024) S02E38"


# ---------- 选取与播报 ----------

def test_push_mixed_batch(db, monkeypatch):
    _ready(db)
    _item(db, "movie", "梦寻苏毗女儿国", year=2026, tmdb="101")
    _item(db, "movie", "未认出身份的片", year=2026)  # 未绑定 → 不报
    series = _item(db, "series", "未解之谜：特别呈现", year=2023,
                   external='{"douban": "999"}')
    for n in (15, 16, 17, 18, 20):
        _item(db, "episode", f"第{n}集", series_id=series.id,
              season_number=2, episode_number=n)
    other = _item(db, "series", "没身份的剧", year=2025)  # 未绑定
    _item(db, "episode", "第1集", series_id=other.id,
          season_number=1, episode_number=1)

    sent = _capture_send(monkeypatch)
    res = updatedigest.push_digest(db, NOW)
    assert res["pushed"] and res["reason"] == "ok"
    assert sent["photos"] == []  # 全批无横幅图 → 纯文字
    assert len(sent["messages"]) == 1
    text = sent["messages"][0]["text"]
    assert text == (
        "电视剧更新\n"
        "📺未解之谜：特别呈现 (2023) S02E15-E18、E20\n"
        "\n"
        "电影更新\n"
        "🎬梦寻苏毗女儿国 (2026)"
    )
    # 已播记录落表；第二批无新货 → empty 且不重发
    assert db.query(models.TgUpdateDigestSent).count() == 6
    res2 = updatedigest.push_digest(db, NOW)
    assert res2["reason"] == "empty"
    assert len(sent["messages"]) == 1


def test_old_and_merged_excluded(db, monkeypatch):
    _ready(db)
    _item(db, "movie", "老片", year=2020, tmdb="1",
          added=NOW - timedelta(days=15))  # 超 14 天窗口
    anchor = _item(db, "movie", "锨片", year=2020, tmdb="2")
    _item(db, "movie", "合并副本", year=2020, tmdb="3", merged_into_id=anchor.id)
    sent = _capture_send(monkeypatch)
    res = updatedigest.push_digest(db, NOW)
    assert res["reason"] == "ok"
    text = sent["messages"][0]["text"]
    assert "🎬锨片 (2020)" in text
    assert "老片" not in text and "合并副本" not in text


def test_overflow_carries_to_next_batch(db, monkeypatch):
    _ready(db)
    for i in range(12):
        _item(db, "movie", f"片{i:02d}", year=2026, tmdb=str(100 + i),
              added=NOW - timedelta(hours=12 - i))
    sent = _capture_send(monkeypatch)
    res = updatedigest.push_digest(db, NOW)
    assert res["pushed"]
    text = sent["messages"][0]["text"]
    lines = text.splitlines()
    assert lines[0] == "电影更新"
    assert len([ln for ln in lines if ln.startswith("🎬")]) == 10
    assert lines[-1] == "……本批共 12 部"
    # 余量顺延：下一批把剩下 2 部报完，不丢不重
    res2 = updatedigest.push_digest(db, NOW)
    assert res2["pushed"]
    text2 = sent["messages"][1]["text"]
    assert len([ln for ln in text2.splitlines() if ln.startswith("🎬")]) == 2
    assert "本批共" not in text2
    assert db.query(models.TgUpdateDigestSent).count() == 12


def test_first_run_seeds_baseline(db, monkeypatch):
    _set_cfg(db, "bot_group_ids", str(GROUP_ID))
    old = _item(db, "movie", "存量片", year=2025, tmdb="7")
    sent = _capture_send(monkeypatch)
    res = updatedigest.push_digest(db, NOW)
    assert res["reason"] == "seeded"
    assert sent["messages"] == [] and sent["photos"] == []
    row = db.query(models.TgUpdateDigestSent).filter_by(item_id=old.id).one()
    assert row.kind == "baseline"
    # 上线后新入库的正常播，存量不翻旧账
    _item(db, "movie", "刚到的新片", year=2026, tmdb="8")
    res2 = updatedigest.push_digest(db, NOW)
    assert res2["pushed"]
    text = sent["messages"][0]["text"]
    assert "刚到的新片" in text and "存量片" not in text


def test_unbound_at_seed_reports_after_binding(db, monkeypatch):
    _set_cfg(db, "bot_group_ids", str(GROUP_ID))
    pending = _item(db, "movie", "后认出的片", year=2026)  # 种子时未绑定
    sent = _capture_send(monkeypatch)
    assert updatedigest.push_digest(db, NOW)["reason"] == "seeded"
    assert db.query(models.TgUpdateDigestSent).count() == 0
    pending.tmdb_id = "42"  # 后来认出身份
    db.commit()
    res = updatedigest.push_digest(db, NOW)
    assert res["pushed"]
    assert "后认出的片" in sent["messages"][0]["text"]


# ---------- 配图 / 开关 / 调度 ----------

def test_photo_prefers_movie_backdrop(db, monkeypatch, tmp_path):
    _ready(db)
    backdrop = tmp_path / "bd.jpg"
    backdrop.write_bytes(b"\xff\xd8\xff")
    _item(db, "movie", "有横幅的片", year=2026, tmdb="5",
          backdrop_path=str(backdrop))
    series = _item(db, "series", "剧", year=2026, tmdb="6",
                   backdrop_path=str(backdrop))
    _item(db, "episode", "第1集", series_id=series.id,
          season_number=1, episode_number=1)
    sent = _capture_send(monkeypatch)
    res = updatedigest.push_digest(db, NOW)
    assert res["pushed"]
    assert sent["messages"] == []
    assert len(sent["photos"]) == 1
    assert sent["photos"][0]["photo_path"] == str(backdrop)
    assert "🎬有横幅的片 (2026)" in sent["photos"][0]["caption"]


def test_remote_backdrop_goes_through_localize(db, monkeypatch, tmp_path):
    _ready(db)
    cached = tmp_path / "cached.jpg"
    cached.write_bytes(b"\xff\xd8\xff")
    monkeypatch.setattr(
        updatedigest.image_store, "localize", lambda url: str(cached)
    )
    _item(db, "movie", "远程横幅片", year=2026, tmdb="9",
          backdrop_image_url="https://img.example/bd.jpg")
    sent = _capture_send(monkeypatch)
    res = updatedigest.push_digest(db, NOW)
    assert res["pushed"]
    assert len(sent["photos"]) == 1
    assert sent["photos"][0]["photo_path"] == str(cached)


def test_disabled_and_no_group(db, monkeypatch):
    _ready(db)
    _item(db, "movie", "片", year=2026, tmdb="3")
    sent = _capture_send(monkeypatch)
    _set_cfg(db, updatedigest.ENABLED_KEY, "false")
    assert updatedigest.push_digest(db, NOW)["reason"] == "disabled"
    assert updatedigest.run_scheduled_tick(db, NOW) == []
    _set_cfg(db, updatedigest.ENABLED_KEY, "true")
    _set_cfg(db, "bot_group_ids", "")
    assert updatedigest.push_digest(db, NOW)["reason"] == "no_group"
    assert sent["messages"] == []


def test_hidden_items_excluded(db, monkeypatch):
    """PA-011 补漏：is_hidden=true 的带身份新行（隐形墓碑形态）不进播报候选，
    可见行照常播；隐藏集行所属剧也不许因它出现在电视栏。"""
    _ready(db)
    _item(db, "movie", "隐形墓碑片", year=2026, tmdb="201", is_hidden=True)
    _item(db, "movie", "正常新片", year=2026, tmdb="202")
    series = _item(db, "series", "普通新剧", year=2026,
                   external='{"douban": "777"}')
    _item(db, "episode", "第1集", series_id=series.id,
          season_number=1, episode_number=1, is_hidden=True)

    sent = _capture_send(monkeypatch)
    res = updatedigest.push_digest(db, NOW)
    assert res["pushed"] and res["reason"] == "ok"
    text = sent["messages"][0]["text"]
    assert "🎬正常新片 (2026)" in text
    assert "隐形墓碑片" not in text
    assert "普通新剧" not in text  # 唯一一集是隐藏行 → 整剧不报
    # 隐藏行不落已播记录（日后取消隐藏仍可正常播报）
    sent_names = db.query(models.TgUpdateDigestSent).count()
    assert sent_names == 1


def test_tick_interval_pacing(db, monkeypatch):
    _ready(db)
    _item(db, "movie", "第一批", year=2026, tmdb="1")
    _capture_send(monkeypatch)
    assert updatedigest.run_scheduled_tick(db, NOW) == ["digest:ok"]
    _item(db, "movie", "第二批", year=2026, tmdb="2")
    # 未到间隔（默认 30 分钟）不攒批
    assert updatedigest.run_scheduled_tick(db, NOW + timedelta(minutes=10)) == []
    # 到点后正常攒下一批
    assert updatedigest.run_scheduled_tick(db, NOW + timedelta(minutes=31)) == ["digest:ok"]
