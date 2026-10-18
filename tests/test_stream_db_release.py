"""S1：流式响应不得在整个播放期间占着 DB 连接。

FastAPI ≥0.118 的 yield 依赖在「响应发完之后」才清理；流式端点必须在返回响应前
自己把会话关掉（``release_db_before_response``）。这里两层护栏：

1. 真连接池：响应体迭代期间 ``engine.pool.checkedout() == 0``（对照组不装饰时为 1，
   证明测试本身能抓到问题）。
2. 真实端点 ``api.video_stream``：会话在第一块数据发出之前已关闭。
   （2026-10-17 转码删除：video_hls 已不存在，本文件不再覆盖它。）
"""
import asyncio
import os
import types

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from fastapi import Depends, FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.emby_server.async_db import release_db_before_response, run_db


def _app_with_engine(tmp_path, decorate: bool):
    engine = create_engine(f"sqlite:///{tmp_path / 'pool.db'}")
    Local = sessionmaker(bind=engine)

    def get_db():
        db = Local()
        try:
            yield db
        finally:
            db.close()

    seen = []
    app = FastAPI()

    async def endpoint(db=Depends(get_db)):
        await run_db(lambda: db.execute(text("SELECT 1")).scalar())

        async def gen():
            for _ in range(3):
                seen.append(engine.pool.checkedout())
                yield b"x" * 16

        return StreamingResponse(gen())

    app.get("/s")(release_db_before_response(endpoint) if decorate else endpoint)
    return app, engine, seen


def test_pool_connection_released_while_streaming(tmp_path):
    app, engine, seen = _app_with_engine(tmp_path, decorate=True)
    r = TestClient(app).get("/s")
    assert r.status_code == 200 and len(r.content) == 48
    assert seen == [0, 0, 0]
    assert engine.pool.checkedout() == 0
    engine.dispose()


def test_control_without_release_holds_connection(tmp_path):
    """对照组：不装饰时迭代期间连接被占着（FastAPI ≥0.118 行为）。"""
    app, engine, seen = _app_with_engine(tmp_path, decorate=False)
    TestClient(app).get("/s")
    assert seen and all(n == 1 for n in seen)
    engine.dispose()


def test_sync_endpoint_released(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'pool2.db'}")
    Local = sessionmaker(bind=engine)

    def get_db():
        db = Local()
        try:
            yield db
        finally:
            db.close()

    seen = []
    app = FastAPI()

    @app.get("/f")
    @release_db_before_response
    def endpoint(db=Depends(get_db)):
        db.execute(text("SELECT 1"))

        def gen():
            seen.append(engine.pool.checkedout())
            yield b"y"

        return StreamingResponse(gen())

    assert TestClient(app).get("/f").content == b"y"
    assert seen == [0]
    engine.dispose()


class _FakeDb:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _patch_common(monkeypatch, api):
    item = types.SimpleNamespace(guid="g1", container="mkv", height=1080,
                                 file_fingerprint=None)
    monkeypatch.setattr(api, "_require_visible_item", lambda db, user, item_id: item)
    monkeypatch.setattr(api, "ensure_playback_allowed", lambda db, user: None)
    monkeypatch.setattr(api.play_sign, "check_referer", lambda *a, **k: None)
    return item


def test_video_stream_closes_session_before_body(monkeypatch, tmp_path):
    from backend.emby_server import api
    from backend.emby_server import mounts as mount_lib

    _patch_common(monkeypatch, api)
    monkeypatch.setattr(api, "_play_target",
                        lambda db, item: mount_lib.PlayTarget("local", str(tmp_path / "a.mkv"), {}))
    db = _FakeDb()
    states = []

    def fake_serve_file(path, request, media_type, cache_control=None):
        def gen():
            states.append(db.closed)
            yield b"data"
        return StreamingResponse(gen())

    monkeypatch.setattr(api, "serve_file", fake_serve_file)
    req = types.SimpleNamespace(headers={}, url=types.SimpleNamespace(path="/emby/Videos/g1/stream"))
    user = types.SimpleNamespace(id=1)

    async def go():
        resp = await api.video_stream("g1", req, user, db)
        assert db.closed  # 响应对象拿到手时会话已关
        async for _ in resp.body_iterator:
            pass

    asyncio.run(go())
    assert states == [True]


def test_streaming_endpoints_are_decorated():
    from backend.emby_server import api, media_routes, mount_routes, stream_routes

    # 2026-10-17 转码删除：api.video_hls 已不存在，从护栏名单移除
    for fn in (api.video_stream, media_routes.download_item,
               media_routes.item_thumbnail, stream_routes.item_file,
               mount_routes.mounted_item_file):
        assert getattr(fn, "__wrapped__", None) is not None, fn
