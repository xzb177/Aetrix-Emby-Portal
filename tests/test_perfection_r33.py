"""第 33 轮完美主义迭代回归测试（R33）。

覆盖：
- P1-1：flat 列举快捷方式 A↔B 成环 → 有限步终止；快捷方式多层跟随（≤4 层）+ 环检测
- P1-2：resolve_shortcuts 开时，增量降级为全量（used_incremental_path=False）
- P2-1/P2-2/P2-3：subtree 与 flat 对同一盘、同一开关产出相同的 .strm 集合
  （子目录递归、别名两份、文件型快捷方式判型统一）
- P2-4：token=null 的 rclone.conf → DriveAuthError（不是 AttributeError）
- P2-5：_resolve_shortcut_target 401 时刷新凭据重试一次；仍失败记 warning 返回 None
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


def _file_meta(fid, name, parents, size=100, mime=None):
    m = {"id": fid, "name": name, "parents": list(parents),
         "trashed": False, "size": str(size)}
    if mime:
        m["mimeType"] = mime
    return m


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

    for tid, name, parents, size, mime in [
            ("fld_mv", "电影", ["fld_mp"], 0, FOLDER_MIME),
            ("fld_cycA", "CycA", ["fld_mp"], 0, FOLDER_MIME),
            ("fld_cycB", "CycB", ["fld_mp"], 0, FOLDER_MIME),
            ("f3", "m1.mkv", ["fld_mv"], 333, None)]:
        api_map[tid] = _file_meta(tid, name, parents, size, mime)

    fake = FakeDrive(entries, api_map)
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


EXPECTED = {
    # 原生路径
    ("MoviePilot/root.mkv", "f_root", 111),
    ("MoviePilot/剧集/国产剧/s1e1.mkv", "f1", 101),
    ("MoviePilot/电影/m1.mkv", "f3", 333),
    ("MoviePilot/电影/子目录/m2.mkv", "f4", 444),
    ("MoviePilot/CycA/ca.mkv", "fca", 501),
    ("MoviePilot/CycB/cb.mkv", "fcb", 502),
    # 别名两份（P2-1）：快捷方式→目录，含子目录递归（P2-2）
    ("MoviePilot/剧集/国产剧/合集/m1.mkv", "f3", 333),
    ("MoviePilot/剧集/国产剧/合集/子目录/m2.mkv", "f4", 444),
    # 文件型快捷方式（P2-3：快捷方式名/目标名任一带视频扩展名即产出）
    ("MoviePilot/剧集/link", "f3", 333),
    ("MoviePilot/剧集/link2.mkv", "f3", 333),
    # A↔B 成环：有限展开（P1-1）
    ("MoviePilot/CycA/toB/cb.mkv", "fcb", 502),
    ("MoviePilot/CycA/toB/toA/ca.mkv", "fca", 501),
    ("MoviePilot/CycB/toA/ca.mkv", "fca", 501),
    ("MoviePilot/CycB/toA/toB/cb.mkv", "fcb", 502),
    # 自环：展开一层即止
    ("MoviePilot/CycA/self/ca.mkv", "fca", 501),
    ("MoviePilot/CycA/self/toB/cb.mkv", "fcb", 502),
}


class TestR33P1P2Listing:
    def test_flat_shortcut_cycle_terminates(self, drive_env):
        items = _bounded(sg.iter_drive_videos(
            D, "", strict=True, subtree_listing=False, resolve_shortcuts=True))
        assert len(items) == len(EXPECTED), \
            f"数量不符（成环会导致无限产出）：{len(items)}"
        assert set(items) == EXPECTED
        # 每轮 1 次 files.get 烧配额的旧行为已消除：api_get 调用有界
        assert drive_env.api_calls < 100, drive_env.api_calls

    def test_subtree_flat_same_set(self, drive_env, monkeypatch):
        flat = set(_bounded(sg.iter_drive_videos(
            D, "", strict=True, subtree_listing=False, resolve_shortcuts=True)))
        sub = set(_bounded(sg.iter_drive_videos(
            D, "", strict=True, subtree_listing=True, resolve_shortcuts=True)))
        assert flat == sub == EXPECTED

    def test_shortcuts_off_unchanged(self, drive_env):
        # 开关默认关：快捷方式不当目录/文件展开（现状行为无回归）
        items = set(_bounded(sg.iter_drive_videos(
            D, "", strict=True, subtree_listing=True, resolve_shortcuts=False)))
        rels = {r for r, _, _ in items}
        assert "MoviePilot/剧集/国产剧/合集/m1.mkv" not in rels
        assert "MoviePilot/剧集/link" not in rels
        assert ("MoviePilot/电影/m1.mkv", "f3", 333) in items


class TestR33ShortcutResolve:
    def _fetch(self):
        def fetch(query, page_token=None):
            raise AssertionError("不应走到 files.list")
        fetch.token_box = {"token": "tok"}
        return fetch

    def test_multilayer_follow(self, monkeypatch):
        # 快捷方式→快捷方式→文件：有界多层跟随解析到最终目标
        from types import SimpleNamespace
        api_map = {
            "sc2": {"id": "sc2", "name": "c2", "mimeType": SHORTCUT_MIME,
                    "shortcutDetails": {"targetId": "fT"}, "trashed": False},
            "fT": {"id": "fT", "name": "t.mkv", "mimeType": None,
                   "trashed": False, "size": "7"},
        }
        monkeypatch.setattr(
            sg, "_drive_modules",
            lambda: SimpleNamespace(_api_get=lambda u, t, p: api_map[u.rsplit("/", 1)[-1]],
                                    _is_personal_drive_key=lambda did: False))
        item = {"id": "sc1", "shortcutDetails": {"targetId": "sc2"}}
        body = sg._resolve_shortcut_target(self._fetch(), item, D)
        assert body and body["id"] == "fT"

    def test_shortcut_cycle_returns_none(self, monkeypatch):
        # 快捷方式互指成环 → 环检测返回 None（不抛、不死循环）
        from types import SimpleNamespace
        api_map = {
            "scY": {"id": "scY", "mimeType": SHORTCUT_MIME,
                    "shortcutDetails": {"targetId": "scX"}, "trashed": False},
            "scX": {"id": "scX", "mimeType": SHORTCUT_MIME,
                    "shortcutDetails": {"targetId": "scY"}, "trashed": False},
        }
        monkeypatch.setattr(
            sg, "_drive_modules",
            lambda: SimpleNamespace(_api_get=lambda u, t, p: api_map[u.rsplit("/", 1)[-1]],
                                    _is_personal_drive_key=lambda did: False))
        item = {"id": "scX", "shortcutDetails": {"targetId": "scY"}}
        assert sg._resolve_shortcut_target(self._fetch(), item, D) is None

    def test_too_deep_returns_none(self, monkeypatch):
        # 链长超过上限（4 跳）→ 放弃解析返回 None；刚好 4 跳 → 正常解析
        from types import SimpleNamespace

        def chain_map(n_hops, prefix):
            # n_hops 次 shortcut→shortcut 跳，最后落到文件
            m = {}
            for i in range(1, n_hops + 1):
                nxt = f"{prefix}{i + 1}" if i < n_hops else f"{prefix}End"
                m[f"{prefix}{i}"] = {"id": f"{prefix}{i}", "mimeType": SHORTCUT_MIME,
                                     "shortcutDetails": {"targetId": nxt},
                                     "trashed": False}
            m[f"{prefix}End"] = {"id": f"{prefix}End", "mimeType": None,
                                 "trashed": False}
            return m

        api_map = chain_map(4, "a")
        api_map.update(chain_map(5, "b"))
        monkeypatch.setattr(
            sg, "_drive_modules",
            lambda: SimpleNamespace(_api_get=lambda u, t, p: api_map[u.rsplit("/", 1)[-1]],
                                    _is_personal_drive_key=lambda did: False))
        # 4 跳：a1→a2→a3→a4→aEnd（上限内）→ 解析成功
        item = {"id": "sx", "shortcutDetails": {"targetId": "a1"}}
        body = sg._resolve_shortcut_target(self._fetch(), item, D)
        assert body and body["id"] == "aEnd"
        # 5 跳：b1→…→b5→bEnd（超上限）→ 放弃解析返回 None
        item5 = {"id": "sy", "shortcutDetails": {"targetId": "b1"}}
        assert sg._resolve_shortcut_target(self._fetch(), item5, D) is None
        # 自环：首层即成环 → None
        assert sg._resolve_shortcut_target(
            self._fetch(), {"id": "sc0", "shortcutDetails": {"targetId": "sc0"}}, D) is None

    def test_401_refresh_then_retry(self, monkeypatch):
        # P2-5：401 → _get_token(force_refresh=True) 刷新一次后重试
        from types import SimpleNamespace
        calls = {"n": 0}
        target = {"id": "fT", "name": "t.mkv", "mimeType": None, "trashed": False}

        def api_get(url, token, params):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError("401")
            assert token == "fresh_tok"
            return target

        got = {}

        def fake_get_token(drive_id, force_refresh=False):
            got["drive_id"] = drive_id
            got["force"] = force_refresh
            return "fresh_tok"

        monkeypatch.setattr(
            sg, "_drive_modules",
            lambda: SimpleNamespace(_api_get=api_get,
                                    _is_personal_drive_key=lambda did: False))
        monkeypatch.setattr(sg, "_get_token", fake_get_token)
        fetch = self._fetch()
        item = {"id": "sc1", "shortcutDetails": {"targetId": "fT"}}
        body = sg._resolve_shortcut_target(fetch, item, "mydrive")
        assert body and body["id"] == "fT"
        assert got == {"drive_id": "mydrive", "force": True}
        assert fetch.token_box["token"] == "fresh_tok"
        assert calls["n"] == 2

    def test_401_refresh_fails_warns_and_none(self, monkeypatch, caplog):
        # P2-5：刷新仍失败 → warning 日志 + None（不再静默）
        import logging
        from types import SimpleNamespace

        def api_get(url, token, params):
            raise PermissionError("401")

        monkeypatch.setattr(
            sg, "_drive_modules",
            lambda: SimpleNamespace(_api_get=api_get,
                                    _is_personal_drive_key=lambda did: False))
        monkeypatch.setattr(sg, "_get_token",
                            lambda drive_id, force_refresh=False: None)
        with caplog.at_level(logging.WARNING, logger="backend.emby_server.strm_gen"):
            body = sg._resolve_shortcut_target(
                self._fetch(), {"id": "sc1", "shortcutDetails": {"targetId": "fT"}},
                "mydrive")
        assert body is None
        assert any("401" in r.getMessage() for r in caplog.records)


class TestR33P12IncrementalDowngrade:
    def test_incremental_downgrades_to_full_when_shortcuts_on(self, monkeypatch):
        from backend.emby_server import strm_source
        lister = strm_source.DriveChangesLister("did1", "SomeDir")
        monkeypatch.setattr(lister, "_resolve_shortcuts_cfg", lambda: True)
        calls = {}

        def fake_iter_drive_videos(drive_id, dir_paths, strict=True,
                                   subtree_listing=True, resolve_shortcuts=False):
            calls["resolve_shortcuts"] = resolve_shortcuts
            if False:
                yield

        monkeypatch.setattr(sg, "iter_drive_videos", fake_iter_drive_videos)
        assert list(lister.iter_videos(incremental=True)) == []
        assert lister.used_incremental_path is False
        assert calls["resolve_shortcuts"] is True

    def test_incremental_still_works_when_shortcuts_off(self, monkeypatch):
        from backend.emby_server import drive_changes as dc
        from backend.emby_server import strm_source
        lister = strm_source.DriveChangesLister("did1", "SomeDir")
        monkeypatch.setattr(lister, "_resolve_shortcuts_cfg", lambda: False)
        monkeypatch.setattr(lister, "_get_saved_token", lambda: "tok123")
        monkeypatch.setattr(dc, "list_changes", lambda token, drive_id: ([], "newtok"))
        assert list(lister.iter_videos(incremental=True)) == []
        assert lister.used_incremental_path is True


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
