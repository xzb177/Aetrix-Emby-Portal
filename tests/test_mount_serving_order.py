# -*- coding: utf-8 -*-
"""挂载直读分支顺序修复（2026-10-30）：判定点前移到 fast302 之前。

背景实证：#1496 的挂载分支原在 video_stream 尾部（_play_target
之后），但生产 drive_direct_play_enabled=true 时，fast302 快路径对
带 drive_file_id 的 Drive 条目先做存在性校验并直接 302 返回——
生产 mount 条目全是 mount://3 的 Drive 条目，永远到不了尾部分支
（2026-10-30 取证：对 item 688899 四次请求全 302、打点全是
drive_exists_check、mount_serving 零出现、挂载缓存零字节）。

本文件以**生产同口径**钉死新顺序：
- 开关显式写 SystemConfig：drive_direct_play_enabled=true（生产
  组合）、drive_relay_enabled=false（生产现状，中继块不拦截）；
- 条目同时带 drive_file_id 与 mount://3 路径（生产条目形态）；
- 请求身份无绑定、无用户副本（门禁 proceed 播共享原件的主线）。

钉死：
1. 白名单 Drive 条目 → serve_file 出流（206 + 字节一致 +
   mount_serving 打点），不是 302；
2. 非白名单 Drive 条目 → 仍走 fast302 的 302 快路径，行为不变；
3. 白名单内但挂载点无文件 → 503，且不回落 302；
4. 无根映射 / 非 mount 路径 → 不介入，走原流程。
全部离线（sqlite 内存库 + tmp_path 假挂载点 + 存在性校验网络桩）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from backend import models  # noqa: F401 — 注册表
from backend.database import Base
from backend.emby_server import api
from backend.emby_server import models as em  # noqa: F401 — 注册表
from backend.emby_server import mount_serving as ms
from backend.emby_server import playback_strategy as _strategy
from backend.emby_server.mounts import PlayTarget
from backend.integrations import store

FID = "10D7XSebAGTApCabnMu57nRxHEZz-sX1E"
REL = "MoviePilot/电影/动画电影/大唐妖探 (2026)/大唐妖探 (2026) - 2160p WEB-DL.mp4"
FILE_PATH = f"mount://3/{REL}"
DATA = bytes(range(256)) * 64


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(em.StorageMount(
        id=3, name="gdrive-direct", mount_type="drive",
        path="drive:0AERUVu4kfWEfUk9PVA", is_enabled=True))
    session.commit()
    store.invalidate()
    try:
        yield session
    finally:
        store.invalidate()
        session.close()


@pytest.fixture(autouse=True)
def _clear_drive_exists_cache():
    api._drive_exists_cache.clear()
    yield
    api._drive_exists_cache.clear()


def _write_config(db, **rows):
    for key, value in rows.items():
        db.add(models.SystemConfig(key=key, value=value))
    db.commit()
    store.invalidate()


def _prod_config(db, *, ids=None, roots=None):
    """生产同口径：直链开关开（生产组合）、中继关（生产现状）。"""
    rows = {
        _strategy.KEY_DRIVE_DIRECT_PLAY: "true",
        _strategy.KEY_DRIVE_RELAY: "false",
    }
    if ids is not None:
        rows[ms.KEY_MOUNT_SERVING_MOUNT_IDS] = ids
    if roots is not None:
        rows[ms.KEY_MOUNT_SERVING_LOCAL_ROOTS] = roots
    _write_config(db, **rows)


def _item(path=FILE_PATH, fid=FID):
    return SimpleNamespace(
        id=688899, container="mp4", drive_file_id=fid,
        file_path=path, library=None)


def _user():
    return SimpleNamespace(id=1)


def _request(range_header=None):
    headers = []
    if range_header:
        headers.append((b"range", range_header.encode()))
    scope = {"type": "http", "method": "GET",
             "path": "/emby/videos/688899/stream.mp4",
             "raw_path": b"/emby/videos/688899/stream.mp4",
             "query_string": b"", "headers": headers,
             "scheme": "http", "server": ("testserver", 80),
             "client": ("testclient", 50000), "root_path": ""}
    return Request(scope)


def _make_file(root, rel=REL, data=DATA):
    full = os.path.join(str(root), rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as f:
        f.write(data)
    return full


class _RecordingTimer:
    def __init__(self, real):
        self._real = real
        self.marks = []

    def mark(self, name):
        self.marks.append(name)
        self._real.mark(name)

    def done(self):
        self._real.done()


@pytest.fixture()
def pt_marks(monkeypatch):
    from backend.emby_server import play_timing
    timers = []
    real_new_timer = play_timing.new_timer

    def fake_new_timer(label):
        timer = _RecordingTimer(real_new_timer(label))
        timers.append(timer)
        return timer

    monkeypatch.setattr(play_timing, "new_timer", fake_new_timer)
    return lambda: [m for t in timers for m in t.marks]


def _stub_flow(monkeypatch, item):
    """门禁/鉴权桩掉（本文件测出流顺序，不测门禁本身）；
    存在性校验的凭据与网络层桩掉（与 test_stream_skip_drive_walk
    同口径），_ensure_drive_file_playable 本体走真实实现。"""
    monkeypatch.setattr(
        api, "_require_visible_item", lambda db, user, item_id: item)
    monkeypatch.setattr(api, "ensure_playback_allowed",
                        lambda db, user: None)
    monkeypatch.setattr(
        "backend.emby_server.playback_gate.ensure_binding_playback_allowed",
        lambda db, user, r=None: None)
    from backend.emby_server import drive_changes
    monkeypatch.setattr(
        drive_changes, "get_token_for_drive",
        lambda drive_id, force_refresh=False: "tok")
    monkeypatch.setattr(
        api, "_drive_exists_http_get",
        lambda url, params=None, headers=None, timeout=None:
        SimpleNamespace(status_code=200))


async def _collect(resp):
    return b"".join([chunk async for chunk in resp.body_iterator])


def test_whitelist_drive_item_served_by_mount_not_302(
        db, tmp_path, monkeypatch, pt_marks):
    """白名单 Drive 条目（生产开关组合）→ 挂载 serve_file 出流，
    不许被 fast302 截成 302。"""
    _make_file(tmp_path)
    _prod_config(db, ids="3", roots=f"3={tmp_path}")
    _stub_flow(monkeypatch, _item())

    resp = asyncio.run(api.video_stream(
        "688899", _request("bytes=0-99"), _user(), db))

    assert resp.status_code == 206, "挂载出流应是 serve_file 的 206，不是 302"
    assert resp.headers["content-range"] == f"bytes 0-99/{len(DATA)}"
    assert asyncio.run(_collect(resp)) == DATA[:100]
    marks = pt_marks()
    assert "mount_serving" in marks, f"必须有 mount_serving 打点，实际：{marks}"
    assert "drive_exists_check" not in marks, \
        "挂载已 engages 时不许再走 fast302 存在性校验"


def test_non_whitelist_drive_item_still_fast302(
        db, tmp_path, monkeypatch, pt_marks):
    """非白名单 Drive 条目 → 与旧行为一致：fast302 存在性校验后 302。"""
    _make_file(tmp_path)
    _prod_config(db, ids="2", roots=f"3={tmp_path}")
    _stub_flow(monkeypatch, _item())

    resp = asyncio.run(api.video_stream(
        "688899", _request(), _user(), db))

    assert resp.status_code == 302
    loc = resp.headers["location"]
    assert "drive.usercontent.google.com" in loc
    assert f"id={FID}" in loc
    marks = pt_marks()
    assert "drive_exists_check" in marks
    assert "mount_serving" not in marks


def test_whitelist_missing_file_503_no_fallback(
        db, tmp_path, monkeypatch, pt_marks):
    """白名单内但文件不在挂载点 → 503 明示，绝不回落 fast302 的 302。"""
    # 不建文件：挂载点是空的
    _prod_config(db, ids="3", roots=f"3={tmp_path}")
    _stub_flow(monkeypatch, _item())

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(api.video_stream(
            "688899", _request(), _user(), db))

    assert exc_info.value.status_code == 503
    assert "drive_exists_check" not in pt_marks(), \
        "503 后不许回落 fast302（drive_exists_check 打点不应出现）"


def test_whitelist_without_root_not_engaged(
        db, tmp_path, monkeypatch, pt_marks):
    """只配白名单没配根映射 ＝ 未接入 → 不介入，仍走 302 快路径。"""
    _make_file(tmp_path)
    _prod_config(db, ids="3", roots="")
    _stub_flow(monkeypatch, _item())

    resp = asyncio.run(api.video_stream(
        "688899", _request(), _user(), db))

    assert resp.status_code == 302
    assert "mount_serving" not in pt_marks()


def test_non_mount_path_not_engaged(db, monkeypatch, pt_marks):
    """非 mount 路径条目 → 挂载判定不介入，走原 _play_target 流程。"""
    _prod_config(db, ids="3", roots="3=/mnt/whatever")
    item = _item(path="/media/local/x.mp4", fid="")
    _stub_flow(monkeypatch, item)
    calls = []
    monkeypatch.setattr(
        api, "_play_target",
        lambda db_, it: calls.append(1) or PlayTarget(
            "url", "https://example.com/x.mp4", {}))
    monkeypatch.setattr(_strategy, "maybe_direct_url",
                        lambda db_, fid: None)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(api.video_stream("688899", _request(), _user(), db))

    assert calls == [1], "非 mount 条目必须走原 _play_target"
    assert exc_info.value.status_code == 410
    assert "mount_serving" not in pt_marks()
