"""开箱即用播放优化的回归测试。

覆盖 backend/emby_server/playback_tune.py 的三个能力：
1. rclone 挂载参数默认值
2. SA 自动轮换
3. 单文件并发 Range 限流
"""
import configparser
import threading
import time
from pathlib import Path

import pytest

from backend.emby_server import playback_tune as pt


# ---------------------------------------------------------------- 1. 挂载参数
def test_default_mount_args_contain_perf_tuning():
    args = pt.build_rclone_mount_args()
    joined = " ".join(args)
    for flag, val in [
        ("--vfs-read-chunk-size", "32M"),
        ("--vfs-read-chunk-size-limit", "256M"),
        ("--vfs-read-ahead", "128M"),
        ("--buffer-size", "32M"),
    ]:
        assert flag in args, f"缺默认参数 {flag}"
        idx = args.index(flag)
        assert args[idx + 1] == val, f"{flag} 默认值应为 {val}"
    assert "--vfs-cache-mode" in args


def test_user_args_override_defaults():
    args = pt.build_rclone_mount_args(["--vfs-read-chunk-size", "64M"])
    idx = args.index("--vfs-read-chunk-size")
    assert args[idx + 1] == "64M"
    # 只出现一次，不重复
    assert args.count("--vfs-read-chunk-size") == 1


def test_build_mount_cmd():
    cmd = pt.build_rclone_mount_cmd("MP:", "/mnt/mp", config="/x/rclone.conf")
    assert cmd[:3] == ["rclone", "mount", "MP:"]
    assert "/mnt/mp" in cmd
    assert "--config" in cmd
    assert "--vfs-read-chunk-size" in cmd


# ---------------------------------------------------------------- 2. SA 轮换
def _write_conf(path: Path, body: str):
    path.write_text(body, encoding="utf-8")


def test_sa_rotation_jittered_step(tmp_path, monkeypatch):
    # 应用层轮换：步长由 _random.randint(1, n) 决定，这里固定为 2（反风控）
    monkeypatch.setattr(pt._random, "randint", lambda a, b: 2)
    sa_dir = tmp_path / "sa"
    sa_dir.mkdir()
    (sa_dir / "a.json").write_text("{}")
    (sa_dir / "b.json").write_text("{}")
    (sa_dir / "c.json").write_text("{}")
    conf = tmp_path / "rclone.conf"
    _write_conf(conf, "[MP]\ntype = drive\nservice_account_file = /old/a.json\n")
    state = tmp_path / "state.json"

    r1 = pt.ensure_sa_rotation(conf, sa_dir, state_path=state)
    assert r1["mode"] == "rotated"
    assert r1["changed"] is True
    assert r1["sa_file"].endswith("a.json")

    r2 = pt.ensure_sa_rotation(conf, sa_dir, state_path=state)
    assert r2["changed"] is True  # 轮换推进，不是幂等
    assert r2["sa_file"].endswith("c.json")  # 0 + 2 = 2

    r3 = pt.ensure_sa_rotation(conf, sa_dir, state_path=state)
    assert r3["sa_file"].endswith("b.json")  # 2 + 2 = 4 mod 3 = 1

    r4 = pt.ensure_sa_rotation(conf, sa_dir, state_path=state)
    assert r4["sa_file"].endswith("a.json")  # 1 + 2 = 3 mod 3 = 0，回绕

    # 配置里写的是单文件模式（rclone 通用）
    cfg = configparser.ConfigParser()
    cfg.read(conf, encoding="utf-8")
    assert cfg.has_option("MP", "service_account_file")
    assert not cfg.has_option("MP", "service_account_file_path")


def test_sa_rotation_jitter_not_predictable(tmp_path):
    # 不 mock 随机数：验证轮换具备覆盖性，且步长不恒为 1（非严格 +1 循环）
    sa_dir = tmp_path / "sa"
    sa_dir.mkdir()
    names = ["a.json", "b.json", "c.json"]
    for name in names:
        (sa_dir / name).write_text("{}")
    conf = tmp_path / "rclone.conf"
    _write_conf(conf, "[MP]\ntype = drive\nservice_account_file = /old/a.json\n")
    state = tmp_path / "state.json"

    picked = []
    for _ in range(60):
        r = pt.ensure_sa_rotation(conf, sa_dir, state_path=state)
        picked.append(r["sa_file"])

    # ① 覆盖：3 个文件都被选中过
    for name in names:
        assert any(p.endswith(name) for p in picked)

    # ② 相邻两次选中的下标差（mod n）不全等于 1
    name_to_idx = {name: i for i, name in enumerate(names)}

    def _which(path):
        for name in names:
            if path.endswith(name):
                return name_to_idx[name]
        raise AssertionError("unexpected sa_file: %r" % (path,))

    seq = [_which(p) for p in picked]
    n = len(names)
    steps = [(seq[i + 1] - seq[i]) % n for i in range(len(seq) - 1)]
    assert any(step != 1 for step in steps)


def test_sa_rotation_cleans_deprecated_dir_mode(tmp_path):
    # 清理废弃的 service_account_file_path（rclone 已移除该选项）
    sa_dir = tmp_path / "sa"
    sa_dir.mkdir()
    (sa_dir / "a.json").write_text("{}")
    (sa_dir / "b.json").write_text("{}")
    conf = tmp_path / "rclone.conf"
    _write_conf(conf, "[MP]\ntype = drive\nservice_account_file_path = /opt/rclone-sa/\n")
    state = tmp_path / "state.json"
    r = pt.ensure_sa_rotation(conf, sa_dir, state_path=state)
    assert r["changed"] is True
    assert r["warning"] and "service_account_file_path" in r["warning"]
    cfg = configparser.ConfigParser()
    cfg.read(conf, encoding="utf-8")
    assert not cfg.has_option("MP", "service_account_file_path")
    assert cfg.has_option("MP", "service_account_file")


def test_sa_rotation_single_sa(tmp_path):
    # 只有一个 SA 文件时 mode=single，不轮换
    sa_dir = tmp_path / "sa"
    sa_dir.mkdir()
    (sa_dir / "only.json").write_text("{}")
    conf = tmp_path / "rclone.conf"
    _write_conf(conf, "[MP]\ntype = drive\nservice_account_file = /old/a.json\n")
    state = tmp_path / "state.json"
    r = pt.ensure_sa_rotation(conf, sa_dir, state_path=state)
    assert r["mode"] == "single"
    cfg = configparser.ConfigParser()
    cfg.read(conf, encoding="utf-8")
    assert cfg.get("MP", "service_account_file").endswith("only.json")


def test_sa_rotation_empty_dir(tmp_path):
    sa_dir = tmp_path / "sa"
    sa_dir.mkdir()
    conf = tmp_path / "rclone.conf"
    _write_conf(conf, "[MP]\ntype = drive\nservice_account_file = /old/a.json\n")
    r = pt.ensure_sa_rotation(conf, sa_dir)
    assert r["changed"] is False
    assert r["warning"]


def test_sa_rotation_missing_conf(tmp_path):
    sa_dir = tmp_path / "sa"
    sa_dir.mkdir()
    (sa_dir / "a.json").write_text("{}")
    r = pt.ensure_sa_rotation(tmp_path / "nope.conf", sa_dir)
    assert r["changed"] is False
    assert r["warning"]


def test_sa_rotation_state_corrupted_recovers(tmp_path):
    # 状态文件损坏时从头开始，不抛异常
    sa_dir = tmp_path / "sa"
    sa_dir.mkdir()
    (sa_dir / "a.json").write_text("{}")
    (sa_dir / "b.json").write_text("{}")
    conf = tmp_path / "rclone.conf"
    _write_conf(conf, "[MP]\ntype = drive\nservice_account_file = /old/a.json\n")
    state = tmp_path / "state.json"
    state.write_text("not-json{{{", encoding="utf-8")
    r = pt.ensure_sa_rotation(conf, sa_dir, state_path=state)
    assert r["changed"] is True
    assert r["sa_file"].endswith("a.json")


# ---------------------------------------------------------------- 2b. 缓存配置
def test_build_cache_args_defaults():
    args = pt.build_rclone_cache_args(cache_dir="/tmp", max_size="5G")
    joined = " ".join(args)
    assert "--cache-dir" in args
    assert args[args.index("--cache-dir") + 1] == "/tmp"
    assert "--vfs-cache-max-size" in args
    assert args[args.index("--vfs-cache-max-size") + 1] == "5G"
    assert "--vfs-cache-max-age" in args
    assert "--vfs-cache-min-free-space" in args
    assert "--dir-cache-time" in args


def test_build_cache_args_auto_size(tmp_path):
    # max_size=None 时自动按磁盘计算
    args = pt.build_rclone_cache_args(cache_dir=str(tmp_path))
    idx = args.index("--vfs-cache-max-size")
    assert args[idx + 1].endswith("G")


def test_mount_args_with_cache():
    cache = pt.build_rclone_cache_args(cache_dir="/tmp", max_size="5G")
    args = pt.build_rclone_mount_args(cache_args=cache)
    assert "--vfs-cache-max-size" in args
    assert args[args.index("--vfs-cache-max-size") + 1] == "5G"
    # 默认 VFS 参数仍在
    assert "--vfs-read-chunk-size" in args


def test_mount_args_cache_user_override():
    # 用户 extra 可覆盖缓存参数
    cache = pt.build_rclone_cache_args(cache_dir="/tmp", max_size="5G")
    args = pt.build_rclone_mount_args(
        extra=["--vfs-cache-max-size", "10G"], cache_args=cache)
    assert args.count("--vfs-cache-max-size") == 1
    assert args[args.index("--vfs-cache-max-size") + 1] == "10G"


def test_check_disk_pressure_ok(tmp_path):
    r = pt.check_disk_pressure(cache_dir=str(tmp_path), min_free_gb=0.001)
    assert r["ok"] is True
    assert r["total_gb"] > 0


def test_check_disk_pressure_low():
    # 阈值设得极高，必然触发告警
    import tempfile
    r = pt.check_disk_pressure(cache_dir=tempfile.gettempdir(),
                               min_free_gb=999999999.0)
    assert r["ok"] is False
    assert r["warning"]


# ---------------------------------------------------------------- 3. 并发限流
def test_range_limiter_allows_max_concurrent():
    lim = pt.RangeConcurrencyLimiter(max_concurrent=2, acquire_timeout=1)
    l1 = lim.acquire("/x/a.mkv")
    assert l1 is not None
    l2 = lim.acquire("/x/a.mkv")
    assert l2 is not None
    assert l1 != l2  # 租约 id 唯一
    # 第三个拿不到（1 秒超时）
    t0 = time.monotonic()
    assert lim.acquire("/x/a.mkv") is None
    assert time.monotonic() - t0 < 5
    lim.release("/x/a.mkv", l1)
    l3 = lim.acquire("/x/a.mkv")
    assert l3 is not None
    lim.release("/x/a.mkv", l2)
    lim.release("/x/a.mkv", l3)


def test_range_limiter_try_acquire_nonblocking():
    lim = pt.RangeConcurrencyLimiter(max_concurrent=1, acquire_timeout=30)
    l1 = lim.try_acquire("/x/c.mkv")
    assert l1 is not None
    # 非阻塞：拿不到立即返回 None，不等 timeout
    t0 = time.monotonic()
    assert lim.try_acquire("/x/c.mkv") is None
    assert time.monotonic() - t0 < 1.0
    lim.release("/x/c.mkv", l1)
    l2 = lim.try_acquire("/x/c.mkv")
    assert l2 is not None
    lim.release("/x/c.mkv", l2)


def test_range_limiter_per_file_isolation():
    lim = pt.RangeConcurrencyLimiter(max_concurrent=1, acquire_timeout=1)
    la = lim.acquire("/x/a.mkv")
    assert la is not None
    # 不同文件互不影响
    lb = lim.acquire("/x/b.mkv")
    assert lb is not None
    lim.release("/x/a.mkv", la)
    lim.release("/x/b.mkv", lb)


def test_range_limiter_threaded():
    lim = pt.RangeConcurrencyLimiter(max_concurrent=3, acquire_timeout=10)
    entered = []
    lock = threading.Lock()

    def worker():
        lease = lim.acquire("/x/big.mkv")
        if lease is not None:
            with lock:
                entered.append(1)
            time.sleep(0.2)
            lim.release("/x/big.mkv", lease)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 6 个线程，每个都拿到了名额（排队而非失败），总耗时约 2 轮
    assert len(entered) == 6
    assert time.monotonic() - t0 >= 0.35


def test_range_limiter_late_release_after_sweep_ignored():
    # D-P1-4 ABA 回归：租约被 _sweep 强制回收后，原持有者的迟到 release
    # 必须被忽略，不能弹掉别人的有效租约（否则计数永久偏小）。
    lim = pt.RangeConcurrencyLimiter(max_concurrent=1, acquire_timeout=1, lease_ttl=0.05)
    l1 = lim.try_acquire("/x/aba.mkv")
    assert l1 is not None
    time.sleep(0.1)  # 等租约过期
    l2 = lim.try_acquire("/x/aba.mkv")  # 触发 sweep 回收 l1，拿到新租约
    assert l2 is not None
    assert l2 != l1
    lim.release("/x/aba.mkv", l1)  # 迟到释放：必须被忽略
    # l2 的租约必须还在——再拿应该拿满（max=1，l2 占着）
    assert lim.try_acquire("/x/aba.mkv") is None
    lim.release("/x/aba.mkv", l2)
    assert lim.try_acquire("/x/aba.mkv") is not None



def test_sa_rotation_finds_sa_subdir(tmp_path):
    # 生产 /opt/rclone-sa 布局是 sa/*.json（drive_changes 同口径）
    root = tmp_path / "rclone-sa"
    (root / "sa").mkdir(parents=True)
    (root / "sa" / "a.json").write_text("{}")
    conf = tmp_path / "rclone.conf"
    _write_conf(conf, "[MP]\ntype = drive\nservice_account_file = /old/x.json\n")
    r = pt.ensure_sa_rotation(conf, root, state_path=tmp_path / "s.json")
    assert r["sa_file"] and r["sa_file"].endswith("a.json")


def test_admin_sa_rotate_endpoint_passes_sa_dir():
    """/gdrive/sa-rotate 以前漏传 sa_dir → 每次 TypeError 500。"""
    from unittest import mock
    from backend.api import admin as admin_api
    with mock.patch.object(pt, "ensure_sa_rotation", autospec=True,
                           return_value={"sa_file": "/x/a.json", "mode": "single"}) as m, \
            mock.patch("pathlib.Path.exists", return_value=True), \
            mock.patch.object(admin_api, "_audit"):
        out = admin_api.gdrive_sa_rotate(current_admin=mock.MagicMock(),
                                         db=mock.MagicMock())
    assert out["success"] is True and out["sa_file"] == "/x/a.json"
    assert str(m.call_args[0][1]) == "/opt/rclone-sa"
