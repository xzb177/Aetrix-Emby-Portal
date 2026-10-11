""".strm 签名刷新节奏必须跟着 TTL 走。

现场：刷新任务写死每 50 分钟跑一次、剩余 < 600 秒才重签，janitor 每 600 秒一拍；
后台把 strm_sig_ttl_seconds 调到 1200 以下（甚至 1800）时，两次刷新之间链接就过期，
全库间歇性拒播。另外刷新时用默认 TTL 重签，后台配的 TTL 根本不生效。

口径：
- 重签阈值 = max(TTL/3, 2×tick)；扫盘节流间隔 ≤ 阈值 − tick（保证过期前至少扫到一次）；
- 重签用后台配置的 TTL；
- 后台保存 TTL 下限 = 2×tick（默认 1200 秒），低于此 400 并说明原因；
- 按 tick 模拟 3×TTL 时长，任何时刻文件签名都不能失效。
"""

import pytest


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


def _set(db, key, value):
    from backend import models
    from backend.integrations import store
    db.query(models.SystemConfig).filter_by(key=key).delete()
    db.add(models.SystemConfig(key=key, value=value))
    db.commit()
    store.invalidate()


def test_threshold_derives_from_ttl():
    from backend.emby_server import strm_sign
    assert strm_sign.refresh_threshold_seconds(3600, 600) == 1200
    assert strm_sign.refresh_threshold_seconds(86400, 600) == 28800
    assert strm_sign.refresh_threshold_seconds(1200, 600) == 1200


@pytest.mark.parametrize("ttl", [1200, 1800, 3600, 7200, 86400])
def test_simulated_ticks_never_expire(db, monkeypatch, tmp_path, ttl):
    from backend.emby_server import maintenance, play_sign, strm_config, strm_sign
    root = tmp_path / "s"
    root.mkdir()
    f = root / "x.strm"
    f.write_text("https://drive.example.com/uc?id=ABC\n", encoding="utf-8")
    strm_config.write_config(db, enabled=True, host_dir="/opt/strm", container_path=str(root))
    _set(db, play_sign.CONFIG_STRM_SIG_TTL, str(ttl))
    monkeypatch.setattr(maintenance, "STRM_REFRESH_LAST_FILE", str(tmp_path / "last"))
    monkeypatch.setattr(maintenance, "MAINTENANCE_INTERVAL", 600)
    clock = {"t": 1_800_000_000.0}
    monkeypatch.setattr(maintenance.time, "time", lambda: clock["t"])  # 同一个 time 模块
    tick = 600
    for _ in range(int(3 * ttl / tick) + 2):
        maintenance.strm_sig_refresh_tick(db=db)
        status = strm_sign.verify_strm_url(f.read_text(encoding="utf-8"))[1]
        assert status == "ok", (ttl, clock["t"])
        # 下一拍前一刻（含 30 秒抖动）也必须仍有效
        clock["t"] += tick + 30
        status = strm_sign.verify_strm_url(f.read_text(encoding="utf-8"))[1]
        assert status == "ok", ("expired between ticks", ttl, clock["t"])
        clock["t"] -= 30


def test_refresh_signs_with_configured_ttl(db, monkeypatch, tmp_path):
    from urllib.parse import parse_qs, urlsplit
    from backend.emby_server import maintenance, play_sign, strm_config
    root = tmp_path / "s"
    root.mkdir()
    f = root / "x.strm"
    f.write_text("https://drive.example.com/uc?id=ABC\n", encoding="utf-8")
    strm_config.write_config(db, enabled=True, host_dir="/opt/strm", container_path=str(root))
    _set(db, play_sign.CONFIG_STRM_SIG_TTL, "7200")
    monkeypatch.setattr(maintenance, "STRM_REFRESH_LAST_FILE", str(tmp_path / "last"))
    clock = {"t": 1_800_000_000.0}
    monkeypatch.setattr(maintenance.time, "time", lambda: clock["t"])
    maintenance.strm_sig_refresh_tick(db=db)
    exp = int(parse_qs(urlsplit(f.read_text(encoding="utf-8").strip()).query)["aexp"][0])
    assert exp - int(clock["t"]) == 7200


def test_write_config_rejects_ttl_below_two_ticks(db, monkeypatch):
    from backend.emby_server import maintenance, play_sign
    monkeypatch.setattr(maintenance, "MAINTENANCE_INTERVAL", 600)
    with pytest.raises(ValueError) as ei:
        play_sign.write_config(db, enabled=True, play_sign_ttl=900, strm_sig_ttl=900)
    assert "1200" in str(ei.value)
    payload = play_sign.write_config(db, enabled=True, play_sign_ttl=900, strm_sig_ttl=1200)
    assert payload["strm_sig_ttl"] == 1200
    assert payload["strm_sig_ttl_min"] == 1200


def test_refresh_errors_emit_warning(db, monkeypatch, tmp_path, caplog):
    """刷新失败数 > 0 时必须记 warning（不能只在 info 汇总里当数字）。"""
    import logging
    import os as _os
    from backend.emby_server import maintenance, strm_config

    root = tmp_path / "s"
    root.mkdir()
    f = root / "x.strm"
    f.write_text("https://drive.example.com/uc?id=ABC\n", encoding="utf-8")
    strm_config.write_config(db, enabled=True, host_dir="/opt/strm", container_path=str(root))
    monkeypatch.setattr(maintenance, "STRM_REFRESH_LAST_FILE", str(tmp_path / "last"))
    clock = {"t": 1_800_000_000.0}
    monkeypatch.setattr(maintenance.time, "time", lambda: clock["t"])
    # 模拟只读文件系统：os.replace 抛 OSError（生产事故现场）
    real_replace = _os.replace
    def fake_replace(src, dst):
        raise OSError(30, "Read-only file system")
    monkeypatch.setattr(maintenance.os, "replace", fake_replace)

    with caplog.at_level(logging.WARNING, logger="backend.emby_server.maintenance"):
        result = maintenance.strm_sig_refresh_tick(db=db)

    assert result["errors"] >= 1
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("刷新失败" in r.getMessage() for r in warnings), \
        f"expected warning about refresh failures, got: {[r.getMessage() for r in warnings]}"



def test_refresh_cas_skips_file_rewritten_mid_tick(db, monkeypatch, tmp_path):
    """.strm 生成器（api 进程）若在本轮处理期间重写了同一文件，刷新必须跳过，
    不能用过期读出的旧 URL 覆盖生成器的新文件（否则新 file_id 的签名被旧签名覆盖，
    播放断流直到下次重签阈值）。"""
    import os as _os
    from backend.emby_server import maintenance, strm_config
    from backend.emby_server import strm_sign as strm_sign_mod

    root = tmp_path / "s"
    root.mkdir()
    f = root / "x.strm"
    old_url = "https://drive.example.com/uc?id=OLD\n"
    f.write_text(old_url, encoding="utf-8")
    strm_config.write_config(db, enabled=True, host_dir="/opt/strm", container_path=str(root))
    monkeypatch.setattr(maintenance, "STRM_REFRESH_LAST_FILE", str(tmp_path / "last"))

    new_url = "https://drive.example.com/uc?id=NEW\n"
    real_sign = strm_sign_mod.sign_strm_url

    def sign_and_rewrite(url, ttl_seconds):
        # 确定性模拟并发：签名计算时生成器重写了该文件（Drive 上文件被替换，file_id 变化）
        f.write_text(new_url, encoding="utf-8")
        return real_sign(url, ttl_seconds)

    # maintenance 在函数内延迟 import，直接 patch 源模块属性
    monkeypatch.setattr(strm_sign_mod, "sign_strm_url", sign_and_rewrite)

    result = maintenance.strm_sig_refresh_tick(db=db)

    # 修复前：refreshed==1 且文件被旧签名覆盖（测试失败）；
    # 修复后：检测到改写→跳过，生成器的新内容原样保留。
    assert result["refreshed"] == 0, f"expected CAS skip, got refreshed={result['refreshed']}"
    assert f.read_text(encoding="utf-8") == new_url


def test_refresh_tmp_name_unique_and_cleaned(db, monkeypatch, tmp_path):
    """tmp 文件名必须带 pid+线程 id（api 生成器与 worker 刷新跨进程写同一文件时，
    确定性的 "<file>.tmp" 会交错写入撕裂内容）；replace 失败后自己的 tmp 必须被清理。"""
    import os as _os
    from backend.emby_server import maintenance, strm_config

    root = tmp_path / "s"
    root.mkdir()
    f = root / "x.strm"
    f.write_text("https://drive.example.com/uc?id=ABC\n", encoding="utf-8")
    strm_config.write_config(db, enabled=True, host_dir="/opt/strm", container_path=str(root))
    monkeypatch.setattr(maintenance, "STRM_REFRESH_LAST_FILE", str(tmp_path / "last"))

    seen_tmps = []
    real_replace = _os.replace

    def fake_replace(src, dst):
        seen_tmps.append(src)
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(maintenance.os, "replace", fake_replace)

    result = maintenance.strm_sig_refresh_tick(db=db)

    assert result["errors"] >= 1
    assert seen_tmps, "expected at least one tmp write attempt"
    for t in seen_tmps:
        assert t != str(f) + ".tmp", "tmp 仍是确定性命名，跨进程会撞车"
        assert f".tmp.{_os.getpid()}." in t, f"tmp 缺少 pid 后缀: {t}"
        assert not _os.path.exists(t), f"tmp 残留未清理: {t}"
    leftovers = [p for p in root.iterdir() if ".tmp." in p.name]
    assert leftovers == [], f"tmp litter: {leftovers}"

