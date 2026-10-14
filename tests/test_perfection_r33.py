"""第 33 轮完美主义迭代回归测试（R33）。

覆盖（2026-10-14 08:28 快捷方式解析功能整体取消后，快捷方式相关
P1-1/P1-2/P2-1/P2-2/P2-3/P2-5 的实现与回归测试已同步删除；列举遇到
快捷方式条目时当普通条目处理——无视频扩展名则自然跳过）：
- 快捷方式跳过：开关删除后，快捷方式不再展开/跟随，不产出别名路径
- P2-4：token=null 的 rclone.conf → DriveAuthError（不是 AttributeError）
- P2-6：DB 键空 + /tmp 有值 → 读到 /tmp 的值
- P2-7：last 为未来时间戳 → 被 clamp 为 0（不跳过）

不碰网络：_page_fetcher / _drive_modules / _get_token 全部 monkeypatch。
"""
import itertools
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.emby_server import strm_gen as sg

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
D = "drive_r33"


def _fld(fid, name, parents):
    return {"id": fid, "name": name, "parents": list(parents),
            "mimeType": FOLDER_MIME}


def _file(fid, name, parents, size=100):
    return {"id": fid, "name": name, "parents": list(parents), "size": str(size)}


def _sc(fid, name, parents, target_id):
    return {"id": fid, "name": name, "parents": list(parents),
            "mimeType": SHORTCUT_MIME,
            "shortcutDetails": {"targetId": target_id}}


def _match(e, q):
    if "trashed=false" in q and e.get("trashed"):
        return False
    m = re.search(r"mimeType='([^']+)'", q)
    if m and e.get("mimeType") != m.group(1):
        return False
    ids = re.findall(r"'([^']+)' in parents", q)
    if ids and not any(i in (e.get("parents") or []) for i in ids):
        return False
    return True


class FakeDrive:
    """模拟 files.list（分页单页返回即可）+ files.get（快捷方式解析）。"""

    def __init__(self, entries, api_map=None, api_fail=None):
        self.entries = entries
        self.api_map = api_map or {}
        self.api_fail = api_fail  # 例外：id -> Exception（或 Exception 工厂）
        self.api_calls = 0
        self.token_box = {"token": "tok"}

    def fetch(self, query, page_token=None):
        return [e for e in self.entries if _match(e, query)], None

    def api_get(self, url, token, params):
        self.api_calls += 1
        tid = url.rsplit("/", 1)[-1]
        fail = self.api_fail.get(tid) if self.api_fail else None
        if fail is not None:
            raise fail() if callable(fail) else fail
        meta = self.api_map.get(tid)
        if meta is None:
            return None
        if isinstance(meta, Exception):
            raise meta
        return meta


@pytest.fixture()
def drive_env(monkeypatch):
    entries = []
    api_map = {}

    def add(e):
        entries.append(e)
        return e

    # —— 基础结构 ——
    add(_fld("fld_mp", "MoviePilot", [D]))
    add(_file("f_root", "root.mkv", ["fld_mp"], size=111))
    add(_fld("fld_tv", "剧集", ["fld_mp"]))
    add(_fld("fld_cd", "国产剧", ["fld_tv"]))
    add(_file("f1", "s1e1.mkv", ["fld_cd"], size=101))
    add(_fld("fld_mv", "电影", ["fld_mp"]))
    add(_file("f3", "m1.mkv", ["fld_mv"], size=333))
    add(_fld("fld_sub", "子目录", ["fld_mv"]))
    add(_file("f4", "m2.mkv", ["fld_sub"], size=444))
    # 快捷方式→目录（目标在源子树内）：别名应产出两份
    add(_sc("sc_dir", "合集", ["fld_cd"], "fld_mv"))
    # 文件型快捷方式：快捷方式名无扩展名 / 有扩展名（P2-3：任一带扩展名即产出）
    add(_sc("sc_link", "link", ["fld_tv"], "f3"))
    add(_sc("sc_link2", "link2.mkv", ["fld_tv"], "f3"))
    # A↔B 成环（P1-1）
    add(_fld("fld_cycA", "CycA", ["fld_mp"]))
    add(_file("fca", "ca.mkv", ["fld_cycA"], size=501))
    add(_sc("scAB", "toB", ["fld_cycA"], "fld_cycB"))
    add(_fld("fld_cycB", "CycB", ["fld_mp"]))
    add(_file("fcb", "cb.mkv", ["fld_cycB"], size=502))
    add(_sc("scBA", "toA", ["fld_cycB"], "fld_cycA"))
    # 自环
    add(_sc("sc_self", "self", ["fld_cycA"], "fld_cycA"))

    fake = FakeDrive(entries)
    monkeypatch.setattr(sg, "_page_fetcher", lambda drive_id: _wrap_fetch(fake))
    monkeypatch.setattr(sg, "_get_token", lambda did=None, **k: "tok")
    from types import SimpleNamespace
    monkeypatch.setattr(
        sg, "_drive_modules",
        lambda: SimpleNamespace(_api_get=fake.api_get,
                                _is_personal_drive_key=lambda did: False))
    # fetch 需要是可调用 + 带 token_box
    return fake


def _wrap_fetch(fake):
    def fetch(query, page_token=None):
        return fake.fetch(query, page_token)
    fetch.token_box = fake.token_box
    return fetch


def _bounded(gen, limit=5000):
    """有界消费：回归成环时不断言挂死，而是 fail（islice 截断后数量对不上）。"""
    return list(itertools.islice(gen, limit))


class TestR33ShortcutSkipped:
    def test_shortcuts_are_skipped_no_alias_output(self, drive_env):
        # 快捷方式解析功能已取消：快捷方式条目当普通条目处理——
        # · 指向目录的快捷方式不展开（无别名路径、目标不再入库）；
        # · 名无视频扩展名的快捷方式（剧集/link）自然跳过；
        # · 名带视频扩展名的快捷方式（link2.mkv）按旧默认行为当普通文件产出
        #   （用快捷方式自己的 id/size，不跟随目标）。
        items = set(_bounded(sg.iter_drive_videos(
            D, "", strict=True, subtree_listing=True)))
        rels = {r for r, _, _ in items}
        assert "MoviePilot/剧集/国产剧/合集/m1.mkv" not in rels
        assert "MoviePilot/剧集/link" not in rels
        assert ("MoviePilot/剧集/link2.mkv", "sc_link2", 0) in items
        assert ("MoviePilot/电影/m1.mkv", "f3", 333) in items
        flat_items = set(_bounded(sg.iter_drive_videos(
            D, "", strict=True, subtree_listing=False)))
        assert flat_items == items


class TestR33P24NonDictToken:
    def test_null_token_conf_raises_drive_auth_error(self, tmp_path, monkeypatch):
        from backend.emby_server import drive_changes as dc
        conf = tmp_path / "rclone.conf"
        conf.write_text("[myremote]\nclient_id = cid\nclient_secret = csec\n"
                        "token = null\n", encoding="utf-8")
        monkeypatch.setattr(dc, "RCLONE_CONF", str(conf))
        # token=null 是合法 JSON 但非对象 → None（不是 AttributeError）
        assert dc._oauth_conf_for_remote("myremote") is None
        with pytest.raises(dc.DriveAuthError):
            dc._oauth_token_for_remote("myremote")

    def test_numeric_token_conf_raises_drive_auth_error(self, tmp_path, monkeypatch):
        from backend.emby_server import drive_changes as dc
        conf = tmp_path / "rclone.conf"
        conf.write_text("[myremote]\nclient_id = cid\nclient_secret = csec\n"
                        "token = 123\n", encoding="utf-8")
        monkeypatch.setattr(dc, "RCLONE_CONF", str(conf))
        assert dc._oauth_conf_for_remote("myremote") is None
        with pytest.raises(dc.DriveAuthError):
            dc._oauth_token_for_remote("myremote")


@pytest.fixture()
def maint_db(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from backend import models
    from backend.integrations import store
    store.invalidate()
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        store.invalidate()


def _set_key(db, key, value):
    from backend import models
    from backend.integrations import store
    db.query(models.SystemConfig).filter_by(key=key).delete()
    db.add(models.SystemConfig(key=key, value=value))
    db.commit()
    store.invalidate()


class TestR33Maintenance:
    def test_read_last_ts_db_empty_falls_back_to_tmp(self, maint_db, monkeypatch, tmp_path):
        # P2-6：DB 读通但键空 → 继续走 /tmp（不再直接返 0.0）
        from backend.emby_server import maintenance
        last_file = tmp_path / "last"
        last_file.write_text("1700000000", encoding="utf-8")
        monkeypatch.setattr(maintenance, "STRM_REFRESH_LAST_FILE", str(last_file))
        assert maintenance._read_last_refresh_ts(maint_db) == 1700000000.0

    def test_read_last_ts_db_wins(self, maint_db, monkeypatch, tmp_path):
        from backend.emby_server import maintenance
        last_file = tmp_path / "last"
        last_file.write_text("1700000000", encoding="utf-8")
        monkeypatch.setattr(maintenance, "STRM_REFRESH_LAST_FILE", str(last_file))
        _set_key(maint_db, maintenance.STRM_REFRESH_LAST_KEY, "1800000000")
        assert maintenance._read_last_refresh_ts(maint_db) == 1800000000.0

    def test_tick_future_last_clamped(self, maint_db, monkeypatch):
        # P2-7：last 为未来时间戳 → clamp 为 0（不跳过，继续走 no_dir 分支）
        import time as _time
        from backend.emby_server import maintenance
        monkeypatch.setattr(maintenance, "_refresh_ts_session", lambda: maint_db)
        monkeypatch.setattr(maintenance, "_strm_refresh_settings",
                            lambda db=None: ("/nonexistent-r33-dir", 3600))
        _set_key(maint_db, maintenance.STRM_REFRESH_LAST_KEY,
                 str(int(_time.time()) + 100000))
        result = maintenance.strm_sig_refresh_tick(db=maint_db)
        assert result == {"skipped": True, "reason": "no_dir"}

    def test_tick_fresh_last_still_throttled(self, maint_db, monkeypatch):
        # 对照：last 新鲜时节流仍生效（clamp 没把正常逻辑搞坏）
        import time as _time
        from backend.emby_server import maintenance
        monkeypatch.setattr(maintenance, "_refresh_ts_session", lambda: maint_db)
        monkeypatch.setattr(maintenance, "_strm_refresh_settings",
                            lambda db=None: ("/nonexistent-r33-dir", 3600))
        _set_key(maint_db, maintenance.STRM_REFRESH_LAST_KEY, str(int(_time.time())))
        result = maintenance.strm_sig_refresh_tick(db=maint_db)
        assert result == {"skipped": True, "reason": "interval"}
