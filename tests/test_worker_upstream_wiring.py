# -*- coding: utf-8 -*-
"""worker 启动接线：上游探活守护 + 健康落库同步（只接线）。

钉死：sources_fn 只含 Emby/Jellyfin 型（CMS 型跳过）；api_key 只给
api_key 型源（password 型只做公开握手）；凭据解不开的行跳过、不降级
匿名；取源失败返回空列表不抛异常；main() 里两守护接线行真实存在。
全部离线：SessionLocal 与 enabled_sources/load_credential 均走替身。
"""
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import backend.database as dbmod
import backend.worker as worker_mod
from backend.emby_server import upstream_sources as ups


def _rows():
    return [
        SimpleNamespace(id=1, source_type="emby", base_url="https://a.example/emby",
                        auth_method="api_key"),
        SimpleNamespace(id=2, source_type="emby", base_url="https://b.example/emby",
                        auth_method="password"),
        SimpleNamespace(id=3, source_type="jellyfin", base_url="https://c.example/jf",
                        auth_method="none"),
        SimpleNamespace(id=4, source_type="applecms", base_url="https://d.example/cms",
                        auth_method="none"),
        SimpleNamespace(id=5, source_type="emby", base_url="https://e.example/emby",
                        auth_method="api_key"),
    ]


def _load_credential(row):
    if row.id == 5:
        raise RuntimeError("seal broken")
    if row.auth_method == "api_key":
        return {"auth_method": "api_key", "username": None, "secret": "tok-1"}
    return None


def test_probe_sources_adapter(monkeypatch):
    fake_db = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(dbmod, "SessionLocal", lambda: fake_db)
    monkeypatch.setattr(ups, "enabled_sources", lambda db: _rows())
    monkeypatch.setattr(ups, "load_credential", _load_credential)
    out = worker_mod._upstream_probe_sources()
    assert [s.key for s in out] == ["1", "2", "3"]
    assert [s.kind for s in out] == ["emby", "emby", "jellyfin"]
    assert out[0].api_key == "tok-1"  # api_key 型：token 只进请求头
    assert out[1].api_key is None    # password 型：只做公开握手
    assert out[2].api_key is None    # 无凭据：只做公开握手


def test_probe_sources_db_failure_returns_empty(monkeypatch):
    monkeypatch.setattr(dbmod, "SessionLocal",
                        lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(ups, "enabled_sources", lambda db: (_ for _ in ()).throw(
        RuntimeError("db down")))
    assert worker_mod._upstream_probe_sources() == []


def test_main_wires_both_daemons():
    import pathlib
    src = pathlib.Path(worker_mod.__file__).read_text(encoding="utf-8")
    assert "start_probe_daemon(" in src
    assert "start_health_sync_daemon(" in src
    assert "_upstream_probe_sources" in src
    assert "RedisHealthStore()" in src
