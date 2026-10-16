"""prune 必须把演员头像当成「被引用」：referenced_basenames 覆盖 emby_people

回归背景：演员头像 URL 存在 emby_people.image（远程 URL），localize 落盘后
prune() 只认 MediaItem.poster_path/backdrop_path，把头像当孤儿删掉 →
详情页每次打开都现拉 TMDB（生产 86k 演员行、抽样 300 个磁盘 0 命中）。
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from backend import models as _core_models  # noqa: E402,F401 — 注册 web_users 等共享 Base 表
from backend.emby_server import image_store  # noqa: E402
from backend.emby_server import models as em  # noqa: E402


@pytest.fixture()
def db_session(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBY_IMAGE_DIR", str(tmp_path))
    engine = create_engine("sqlite://")
    em.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        yield s, tmp_path
    finally:
        s.close()
        engine.dispose()


def _lib(s):
    lib = s.query(em.Library).first()
    if lib is None:
        lib = em.Library(guid="lib", name="库", collection_type="movies", paths="/media")
        s.add(lib)
        s.flush()
    return lib


def _add_item(s, guid, poster_path=None, backdrop_path=None):
    it = em.MediaItem(guid=guid, name=guid, item_type="movie", library_id=_lib(s).id,
                      poster_path=poster_path, backdrop_path=backdrop_path)
    s.add(it)
    s.flush()
    return it


def _add_person(s, item_id, name, image):
    p = em.EmbyPerson(item_id=item_id, name=name, image=image, sort_order=0)
    s.add(p)
    s.flush()
    return p


class TestReferencedBasenamesPeople:
    def test_people_avatar_included(self, db_session):
        s, _tmp = db_session
        it = _add_item(s, "m1")
        url = "https://image.tmdb.org/t/p/w185/abc123.jpg"
        _add_person(s, it.id, "演员甲", url)
        s.commit()
        found = image_store.referenced_basenames(s)
        expect = os.path.basename(image_store.local_path(url))
        assert expect in found, "演员头像的本地文件名必须被视为引用"

    def test_people_avatar_variants(self, db_session):
        s, _tmp = db_session
        it = _add_item(s, "m1")
        for i, url in enumerate([
            "https://image.tmdb.org/t/p/w185/a.jpg",
            "http://cdn.example.com/x.png",
            "", None, "not-a-url",
        ]):
            _add_person(s, it.id, f"演员{i}", url)
        s.commit()
        found = image_store.referenced_basenames(s)
        assert os.path.basename(image_store.local_path(
            "https://image.tmdb.org/t/p/w185/a.jpg")) in found
        assert os.path.basename(image_store.local_path(
            "http://cdn.example.com/x.png")) in found
        # 空/None/非 http 不得污染集合
        assert not any(n.startswith("not-a-url") for n in found)

    def test_media_item_paths_unchanged(self, db_session):
        s, tmp_path = db_session
        poster = tmp_path / "p1.jpg"
        poster.write_bytes(b"x")
        backdrop = tmp_path / "b1.jpg"
        backdrop.write_bytes(b"x")
        _add_item(s, "m1", poster_path=str(poster), backdrop_path=str(backdrop))
        _add_item(s, "m2", poster_path="https://img/remote.jpg")  # 远程 URL 不算
        s.commit()
        found = image_store.referenced_basenames(s)
        assert "p1.jpg" in found
        assert "b1.jpg" in found
        assert not any("remote" in n for n in found)

    def test_over_limit_evicts_thumbs_not_originals(self, db_session, monkeypatch):
        """超限第二遍：被引用保护的缩略图可淘汰，原图永不删"""
        s, tmp_path = db_session
        monkeypatch.setenv("EMBY_IMAGE_CACHE_MB", "1")  # 1MB 上限
        digest = "abcdef1234567890abcdef12"
        orig = tmp_path / f"{digest}.jpg"
        orig.write_bytes(b"o" * (100 * 1024))          # 原图 100KB（被引用保护）
        thumb = tmp_path / f"{digest}_w320.jpg"
        thumb.write_bytes(b"t" * (2 * 1024 * 1024))     # 缩略图 2MB（被引用保护）
        _add_item(s, "m1", poster_path=str(orig))
        s.commit()
        result = image_store.prune(s)
        assert os.path.isfile(str(orig)), "被引用的原图永不淘汰"
        assert not os.path.isfile(str(thumb)), "超限时可再生的缩略图应被淘汰"
        assert result["removed"] >= 1
        assert result["freed_bytes"] >= 2 * 1024 * 1024

    def test_prune_keeps_avatar_files(self, db_session):
        """端到端：prune 不得删除演员头像文件（含其缩略图变体）"""
        s, tmp_path = db_session
        monkeypatch_dir = str(tmp_path)
        os.environ["EMBY_IMAGE_DIR"] = monkeypatch_dir
        it = _add_item(s, "m1")
        url = "https://image.tmdb.org/t/p/w185/keepme.jpg"
        _add_person(s, it.id, "演员乙", url)
        s.commit()
        avatar_path = image_store.local_path(url)
        with open(avatar_path, "wb") as fh:
            fh.write(b"fake-avatar-bytes")
        # 造一个同名缩略图变体
        thumb = os.path.join(monkeypatch_dir,
                             os.path.basename(avatar_path).rsplit(".", 1)[0] + "_w320.jpg")
        with open(thumb, "wb") as fh:
            fh.write(b"fake-thumb-bytes")
        # 造一个真正的孤儿文件（应被清理）
        orphan = os.path.join(monkeypatch_dir, "orphan123.jpg")
        with open(orphan, "wb") as fh:
            fh.write(b"orphan")
        # 把孤儿文件 mtime 推到保护期之外
        old = 0
        os.utime(orphan, (old, old))
        result = image_store.prune(s)
        assert os.path.isfile(avatar_path), "演员头像原图被 prune 误删"
        assert os.path.isfile(thumb), "演员头像缩略图被 prune 误删"
        assert not os.path.isfile(orphan), "真正的孤儿文件应被清理"
        assert result["removed"] >= 1
