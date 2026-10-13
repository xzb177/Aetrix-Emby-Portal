"""302 直连下线（2026-10）的契约测试。

## 为什么下线

调研 Alist / RClone / Cloudreve 三家后发现，Google Drive 的「302 真直链」
根本走不通，三家全是服务端代理：

1. Drive 的 ``alt=media`` 要 ``Authorization`` 头，而 302 是重定向——客户端
   不会把本服务请求上的请求头带到新地址；
2. 唯一能塞进 URL 的 ``access_token`` 会进客户端日志 / Referer，且 Google 对
   「URL 带 token」的请求有更严的限流。

## 这里锁住什么

- 老用户库里存着的 ``direct`` 一律被折成 ``relay``（读、写两个方向），用户
  不会看到一个点了没变化的死选项；
- 播放路径不再产生 302 到 Drive（连导入都不留）；
- ``direct_link`` 连同直链生成逻辑**已彻底删除**（代码不再读取它）；库里已存的配置值
  原样留着，不读、不报错、不报错迁移；
- 用户端类型与面板里不再出现 direct 选项。
"""
# 必须**在任何业务模块之前**设好：backend.database 在 import 期就按
# DATABASE_TYPE 建好 engine（本机没有 PG 服务）。与仓库其它需要库的测试一致。
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import asyncio
import ast
import inspect
import re
import textwrap

import pytest

from backend.emby_server import api, mount_rclone, play_line, portal
from backend.emby_server.play_line import LINE_DIRECT, LINE_RELAY


@pytest.fixture()
def db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from backend import models

    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _user(db, username="u"):
    from backend import models

    u = models.WebUser(username=username, password_hash="x")
    db.add(u)
    db.commit()
    return u


def _store_raw_pref(db, user_id, line):
    """直接写一行偏好（模拟升级前的老数据）"""
    from backend import models

    db.add(models.UserPlayLine(user_id=user_id, line=line))
    db.commit()


# ---------------- 偏好迁移 ----------------


def test_legacy_pref_reads_back_as_relay(db):
    user = _user(db)
    _store_raw_pref(db, user.id, LINE_DIRECT)
    assert portal.get_play_line_pref(user, db)["line"] == LINE_RELAY


def test_put_any_line_returns_relay(db):
    """线路选择已下线：任何值都返回 relay，不报错"""
    user = _user(db)
    _store_raw_pref(db, user.id, LINE_DIRECT)
    for line in ["direct", "cdn", "cache", "relay", "bogus"]:
        out = portal.set_play_line_pref({"line": line}, user, db)
        assert out == {"line": LINE_RELAY}
    assert play_line.get_play_line(db, user.id) == LINE_RELAY


def test_direct_is_not_a_selectable_line():
    assert LINE_DIRECT not in play_line.PLAY_LINES
    assert play_line.DEFAULT_LINE == LINE_RELAY
    assert play_line.normalize(LINE_DIRECT) == LINE_RELAY


# ---------------- 播放路径不再 302 ----------------


def test_video_stream_source_has_no_google_direct_call():
    """结构性护栏：video_stream 里不能再出现直链入口，也不再有 302 响应

    用 AST 取**真实代码**（注释里可以继续解释为什么下线）。
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(api.video_stream)))
    code = ast.unparse(ast.Module(body=tree.body, type_ignores=[]))
    assert "try_google_direct_url" not in code
    assert "target.direct" not in code
    assert "status_code=302" not in code, "播放路径不再对客户端 302"


def test_module_no_longer_imports_direct_url():
    assert not hasattr(api, "try_google_direct_url"), (
        "已下线的直链入口不应再被播放模块导入")


def test_relay_line_never_redirects_without_explicit_param(monkeypatch):
    """默认线路（relay）下，即便目标是公开直链也不会自己 302

    db 传 ``object()``：``get_play_line`` 读失败会回默认 relay，正是这里要的。
    """
    from types import SimpleNamespace

    from starlette.requests import Request

    from backend.emby_server.mounts import PlayTarget

    target = PlayTarget("url", "https://cdn.example/movie.mkv", {})
    monkeypatch.setattr(api, "_require_item", lambda db_, item_id: SimpleNamespace(container="mp4"))
    # 假 db/user 读不到可见范围；S12 起读失败 fail-closed，这里显式按「不过滤」桩掉
    monkeypatch.setattr(api, "_library_scope", lambda db_, user_: None)
    monkeypatch.setattr(api, "ensure_playback_allowed", lambda *a, **k: None)
    monkeypatch.setattr(api.playback_policy, "ensure_client_allowed", lambda *a, **k: None)
    monkeypatch.setattr(api, "_play_target", lambda db_, item: target)
    seen = []

    async def fake_proxy(url, request, headers, media_type, cache_control=None, **kwargs):
        seen.append(url)
        return "proxied"

    monkeypatch.setattr(api, "serve_remote_async", fake_proxy)
    request = Request({"type": "http", "method": "GET", "path": "/v/1/stream",
                       "raw_path": b"/v/1/stream", "query_string": b"",
                       "headers": [], "scheme": "http",
                       "server": ("t", 80), "client": ("1.1.1.1", 1)})
    resp = asyncio.run(api.video_stream("item", request, SimpleNamespace(id=1), object()))
    assert resp == "proxied"
    assert seen == [target.value], "不该出现任何 302，带不带凭据都一样"


def test_rclone_target_with_credentials_never_redirects(monkeypatch):
    """远程目标（带凭据 / 签名）一律代理转发：连 ?direct=true 也不 302"""
    from types import SimpleNamespace

    from starlette.requests import Request

    from backend.emby_server.mounts import PlayTarget

    target = PlayTarget("url", "http://127.0.0.1:5572/%5Bgdrive:%5D/a.mkv",
                        {"Authorization": "Basic dXNlcjpwYXNz"})
    monkeypatch.setattr(api, "_require_item", lambda db_, item_id: SimpleNamespace(container="mp4"))
    # 假 db/user 读不到可见范围；S12 起读失败 fail-closed，这里显式按「不过滤」桩掉
    monkeypatch.setattr(api, "_library_scope", lambda db_, user_: None)
    monkeypatch.setattr(api, "ensure_playback_allowed", lambda *a, **k: None)
    monkeypatch.setattr(api.playback_policy, "ensure_client_allowed", lambda *a, **k: None)
    monkeypatch.setattr(api, "_play_target", lambda db_, item: target)
    seen = []

    async def fake_proxy(url, request, headers, media_type, cache_control=None, **kwargs):
        seen.append(url)
        return "proxied"

    monkeypatch.setattr(api, "serve_remote_async", fake_proxy)
    request = Request({"type": "http", "method": "GET", "path": "/v/1/stream",
                       "raw_path": b"/v/1/stream", "query_string": b"direct=true",
                       "headers": [], "scheme": "http",
                       "server": ("t", 80), "client": ("1.1.1.1", 1)})
    resp = asyncio.run(api.video_stream("item", request, SimpleNamespace(id=1), object()))
    assert resp == "proxied"
    assert seen == [target.value], "不该出现任何 302，带不带凭据都一样"


# ---------------- rclone.conf 由用户自理 ----------------


def test_no_server_side_rclone_conf_generation():
    """面板不再代管 rclone.conf：生成/写出的那两个入口整个模块都没了"""
    import importlib

    for gone in ("rclone_admin", "rclone_manager"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(f"backend.emby_server.{gone}")
    assert not hasattr(mount_rclone, "generate_rclone_conf")
    assert not hasattr(mount_rclone, "write_conf_from_db")


def test_play_target_has_no_direct_field():
    from backend.emby_server.mounts import PlayTarget

    assert not hasattr(PlayTarget("url", "https://x/f.mp4"), "direct")


# ---------------- 前端契约 ----------------


def _src(relative: str) -> str:
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / relative
    assert path.is_file(), f"找不到前端文件: {relative}"
    return path.read_text(encoding="utf-8")


def test_user_frontend_play_line_type_has_no_direct():
    source = _src("user_frontend/src/api/user.ts")
    union = re.search(r"export type PlayLine =([^\n]+)", source)
    assert union, "PlayLine 类型声明没找到"
    assert "'direct'" not in union.group(1)
    assert "'relay'" in union.group(1)


def test_user_frontend_play_line_is_deprecated():
    """用户端线路选择已删除：user.ts 只剩废弃兼容
    （2026-10-13 旧页面清理：ProfileView.vue 已删，P0 功能迁入 /game/account 账号设置设施，
    新设施里同样没有线路选择器）"""
    from pathlib import Path

    source = _src("user_frontend/src/api/user.ts")
    assert "已废弃" in source or "deprecated" in source.lower()
    profile = Path(__file__).resolve().parent.parent / "user_frontend/src/views/ProfileView.vue"
    assert not profile.exists(), "ProfileView.vue 应已删除（改密码/订阅/设备迁入 AccountSettingsView）"
    vue = _src("user_frontend/src/views/game/AccountSettingsView.vue")
    assert "pickLine(" not in vue, "线路选择器已删除"