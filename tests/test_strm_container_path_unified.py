""".strm 容器内路径单一事实源回归测试。

现场：docker-compose 写死 `:/strm:ro`，maintenance 的签名刷新读环境变量
STRM_CONTAINER_PATH（默认 /strm），而扫描/判定读后台「容器内挂载点」配置——
后台改了挂载点后刷新任务仍扫 /strm，新目录里的签名永远不刷新。

口径：
- 唯一事实源 strm_config.container_path(db)：后台配置 > 环境变量 STRM_CONTAINER_PATH > /strm；
- 签名刷新任务走 container_path(db)；
- 后台保存「容器内挂载点」时目录在容器内不存在 → 400，提示改 .env STRM_CONTAINER_PATH 并重启；
- docker-compose 三个后端服务的容器侧挂载点也由 STRM_CONTAINER_PATH 决定并传给后端。
"""

import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def db():
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


def test_container_path_falls_back_to_env(db, monkeypatch):
    from backend.emby_server import strm_config
    monkeypatch.setenv("STRM_CONTAINER_PATH", "/data/strm/")
    assert strm_config.container_path(db) == "/data/strm"
    # 后台配置优先于环境变量
    strm_config.write_config(db, enabled=True, host_dir="/opt/strm", container_path="/ui/strm")
    assert strm_config.container_path(db) == "/ui/strm"


def test_container_path_env_invalid_falls_back_default(db, monkeypatch):
    from backend.emby_server import strm_config
    monkeypatch.setenv("STRM_CONTAINER_PATH", "relative/x")
    assert strm_config.container_path(db) == "/strm"


def test_sig_refresh_uses_configured_container_path(db, monkeypatch, tmp_path):
    from backend.emby_server import maintenance, strm_config, strm_sign
    strm_root = tmp_path / "mystrm"
    (strm_root / "a").mkdir(parents=True)
    f = strm_root / "a" / "x.strm"
    f.write_text("https://drive.example.com/uc?id=ABC\n", encoding="utf-8")
    strm_config.write_config(db, enabled=True, host_dir="/opt/strm", container_path=str(strm_root))
    monkeypatch.delenv("STRM_CONTAINER_PATH", raising=False)
    monkeypatch.setattr(maintenance, "STRM_REFRESH_LAST_FILE", str(tmp_path / "last"))
    result = maintenance.strm_sig_refresh_tick(db=db)
    assert result.get("refreshed") == 1, result
    assert strm_sign.verify_strm_url(f.read_text(encoding="utf-8"))[1] == "ok"


def test_save_rejects_missing_container_dir(db, tmp_path):
    from fastapi import HTTPException
    from backend.emby_server.portal_mount_routes import StrmConfigRequest, update_strm_config
    from backend.emby_server import strm_config
    missing = str(tmp_path / "nope")
    with pytest.raises(HTTPException) as ei:
        update_strm_config(StrmConfigRequest(enabled=True, host_dir="/opt/strm",
                                             container_path=missing), staff=None, db=db)
    assert ei.value.status_code == 400
    assert "STRM_CONTAINER_PATH" in str(ei.value.detail)
    assert "重启" in str(ei.value.detail)
    # 不落半个配置
    assert strm_config.container_path(db) == "/strm"
    ok = update_strm_config(StrmConfigRequest(enabled=True, host_dir="/opt/strm",
                                              container_path=str(tmp_path)), staff=None, db=db)
    assert ok["strm"]["container_path"] == str(tmp_path)


def test_save_allows_missing_dir_when_disabled(db, tmp_path):
    """总开关关闭时不要求目录存在（关掉功能不该被挂载卡住）。"""
    from backend.emby_server.portal_mount_routes import StrmConfigRequest, update_strm_config
    res = update_strm_config(StrmConfigRequest(enabled=False, host_dir="/opt/strm",
                                               container_path=str(tmp_path / "nope")), staff=None, db=db)
    assert res["strm"]["enabled"] is False


def test_compose_container_side_configurable():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert ":/strm:ro" not in text
    # aetrix-api 需要写入 /strm（.strm 生成器跑在 api 进程内）；
    # aetrix-worker 需要写入 /strm（strm_sig_refresh 签名刷新跑在 worker 的 janitor 线程）；
    # aetrix-ea 保持只读
    assert text.count("${STRM_MOUNT_DIR:-/opt/strm}:${STRM_CONTAINER_PATH:-/strm}:ro") == 1
    assert text.count("${STRM_MOUNT_DIR:-/opt/strm}:${STRM_CONTAINER_PATH:-/strm}:rw") == 2
    assert text.count("STRM_CONTAINER_PATH: ${STRM_CONTAINER_PATH:-/strm}") == 3


def test_no_hardcoded_strm_env_read_in_backend():
    hits = []
    for p in (ROOT / "backend").rglob("*.py"):
        if 'getenv("STRM_CONTAINER_PATH"' in p.read_text(encoding="utf-8", errors="ignore") \
                and p.name != "strm_config.py":
            hits.append(str(p))
    assert hits == []
