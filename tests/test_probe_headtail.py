"""探测正案第 1 步：头尾探测。

两次读（头部 PROBE_HEADTAIL_HEAD_BYTES + 尾部 PROBE_HEADTAIL_TAIL_BYTES），
写进同尺寸的稀疏临时文件（truncate(size) 后只填头尾），再对本地文件跑
``ffprobe -probesize 5M -analyzeduration 0``。50GB 的原盘和 500MB 的剧集
一样只读头尾两段，耗时与文件大小无关——不再让 ffprobe 直接在 FUSE 上乱 seek。

钉住六件事：
1. 小文件（size <= 头+尾）不走头尾探测，走现行流程；
2. 两个抓取**并行**发出；
3. 稀疏文件：逻辑大小 == 原文件大小，头在偏移 0、尾贴着文件末尾，中间是空洞；
4. 任一抓取缺段/短读 → 返回 None（fail-closed），不建残缺的临时文件；
5. probe_metadata 里拿到 format 就直接用，不再跑后面的三跳；拿不到则回退现行流程；
6. 临时文件探测完必须删掉。
"""
import os
import threading

import pytest

from backend.emby_server import scanner


URL = "https://rclone:5572/[drv:]/movies/Big Movie (2021).mkv?sig=abc"
HEAD = int(scanner.PROBE_HEADTAIL_HEAD_BYTES)
TAIL = int(scanner.PROBE_HEADTAIL_TAIL_BYTES)
TOTAL = HEAD + TAIL + 1024 * 1024  # 比两段窗口之和大 1 MiB，两段才真的错开


def _sliced(blob, seen=None):
    """假的服务端/本地读：按 Range 返回 blob 对应片段。"""
    def fetch(path, headers, start, end):
        if seen is not None:
            seen.append((start, end))
        return blob[min(start, len(blob)):min(end + 1, len(blob))]
    return fetch


# ---------- 1. 小文件 / 未知大小不走头尾探测 ----------

def test_headtail_skipped_when_size_unknown():
    assert scanner._headtail_sparse_temp(URL, {}, 0) is None


def test_headtail_skipped_when_file_fits_windows():
    """两段窗口就盖住全文件：走现行流程更直接"""
    assert scanner._headtail_sparse_temp(URL, {}, HEAD + TAIL) is None
    assert scanner._headtail_sparse_temp(URL, {}, HEAD + TAIL - 1) is None


def test_headtail_skipped_when_caller_brings_range(monkeypatch):
    """调用方自己带了 Range（另有安排），别插手"""
    blob = b"H" * TOTAL
    monkeypatch.setattr(scanner, "_http_fetch_range", _sliced(blob))
    assert scanner._headtail_sparse_temp(URL, {"Range": "bytes=0-99"}, TOTAL) is None


# ---------- 2. 并行 + 窗口位置 ----------

def test_headtail_fetches_are_parallel(monkeypatch):
    """串行实现会在这里死锁：barrier 要两个请求同时在飞才放行。"""
    barrier = threading.Barrier(2, timeout=5)
    seen = []
    blob = b"H" * TOTAL

    def fake_fetch(path, headers, start, end):
        seen.append((start, end))
        barrier.wait()  # 第二个请求没并行发起 → BrokenBarrierError
        return blob[min(start, len(blob)):min(end + 1, len(blob))]

    monkeypatch.setattr(scanner, "_http_fetch_range", fake_fetch)
    tmp = scanner._headtail_sparse_temp(URL, {}, TOTAL)
    try:
        assert tmp is not None, "两个请求应并行取回并建出稀疏文件"
        assert len(seen) == 2
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def test_headtail_requests_head_and_tail(monkeypatch):
    """窗口固定：头 HEAD + 尾 TAIL，尾窗口贴着文件末尾，与文件多大无关"""
    seen = []
    blob = b"H" * TOTAL
    monkeypatch.setattr(scanner, "_http_fetch_range", _sliced(blob, seen))
    tmp = scanner._headtail_sparse_temp(URL, {}, TOTAL)
    try:
        assert tmp is not None
        assert sorted(seen) == sorted([
            (0, HEAD - 1),
            (TOTAL - TAIL, TOTAL - 1),
        ])
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


# ---------- 3. 稀疏文件布局 ----------

def test_headtail_sparse_layout(monkeypatch, tmp_path):
    """逻辑大小 == 原文件；头在偏移 0、尾在 TOTAL-TAIL；中间是空洞。"""
    head = b"A" * HEAD
    tail = b"Z" * TAIL
    blob = head + b"\x00" * (TOTAL - HEAD - TAIL) + tail
    assert len(blob) == TOTAL
    monkeypatch.setattr(scanner, "_http_fetch_range", _sliced(blob))
    monkeypatch.setattr(scanner.tempfile, "mkstemp",
                        lambda prefix, suffix: (os.open(str(tmp_path / "ht.bin"),
                                                       os.O_CREAT | os.O_RDWR),
                                                str(tmp_path / "ht.bin")))
    tmp = scanner._headtail_sparse_temp(URL, {}, TOTAL)
    try:
        assert tmp is not None
        assert os.path.getsize(tmp) == TOTAL, "稀疏文件逻辑大小必须等于原文件"
        with open(tmp, "rb") as fh:
            assert fh.read(HEAD) == head, "头部必须落在偏移 0"
            fh.seek(TOTAL - TAIL)
            assert fh.read(TAIL) == tail, "尾部必须贴着文件末尾"
            fh.seek(HEAD)
            assert fh.read(16) == b"\x00" * 16, "中间必须是空洞（读零）"
        st = os.stat(tmp)
        assert st.st_blocks * 512 < TOTAL, "空洞不应占磁盘块（稀疏）"
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def test_headtail_local_path_uses_local_reads(monkeypatch, tmp_path):
    """本机/FUSE 路径走两次定长本地读，不走 HTTP。"""
    real = tmp_path / "movie.mkv"
    head = b"A" * HEAD
    tail = b"Z" * TAIL
    real.write_bytes(head + b"\x00" * (TOTAL - HEAD - TAIL) + tail)

    def boom(path, headers, start, end):
        raise AssertionError("本机路径不该走 HTTP Range")

    monkeypatch.setattr(scanner, "_http_fetch_range", boom)
    tmp = scanner._headtail_sparse_temp(str(real), {}, TOTAL)
    try:
        assert tmp is not None
        assert os.path.getsize(tmp) == TOTAL
        with open(tmp, "rb") as fh:
            assert fh.read(8) == b"A" * 8
            fh.seek(TOTAL - 8)
            assert fh.read(8) == b"Z" * 8
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def test_headtail_short_fetch_returns_none(monkeypatch):
    """短读（服务端截断）→ fail-closed：不建残缺的临时文件。"""
    blob = b"H" * (TOTAL - 100)  # 尾部少 100 字节

    def short_fetch(path, headers, start, end):
        return blob[min(start, len(blob)):min(end + 1, len(blob))]

    monkeypatch.setattr(scanner, "_http_fetch_range", short_fetch)
    assert scanner._headtail_sparse_temp(URL, {}, TOTAL) is None


def test_headtail_fetch_failure_returns_none(monkeypatch):
    monkeypatch.setattr(scanner, "_http_fetch_range",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert scanner._headtail_sparse_temp(URL, {}, TOTAL) is None


# ---------- 4. _ffprobe 的 probesize/analyzeduration 可覆盖 ----------

def test_ffprobe_probesize_override(monkeypatch):
    seen = {}

    class _Out:
        stdout = '{"format": {"duration": "1.0"}, "streams": []}'
        stderr = ""

    def fake_run(cmd, timeout):
        seen["cmd"] = cmd
        return _Out()

    monkeypatch.setattr(scanner, "shutil_which", lambda cmd: "/usr/bin/ffprobe")
    monkeypatch.setattr(scanner, "_run_probe_cmd", fake_run)
    scanner._ffprobe("/tmp/x.bin", local_path="/tmp/x.bin",
                     probesize=5 << 20, analyzeduration=0)
    cmd = seen["cmd"]
    assert "-probesize" in cmd and str(5 << 20) in cmd
    assert "-analyzeduration" in cmd and "0" in cmd


def test_ffprobe_defaults_unchanged(monkeypatch):
    """不传覆盖参数时保持原默认（FFPROBE_PROBESIZE/FFPROBE_ANALYZEDURATION）"""
    seen = {}

    class _Out:
        stdout = '{"format": {"duration": "1.0"}, "streams": []}'
        stderr = ""

    def fake_run(cmd, timeout):
        seen["cmd"] = cmd
        return _Out()

    monkeypatch.setattr(scanner, "shutil_which", lambda cmd: "/usr/bin/ffprobe")
    monkeypatch.setattr(scanner, "_run_probe_cmd", fake_run)
    scanner._ffprobe("/tmp/x.bin", local_path="/tmp/x.bin")
    cmd = seen["cmd"]
    assert str(scanner.FFPROBE_PROBESIZE) in cmd
    assert str(scanner.FFPROBE_ANALYZEDURATION) in cmd


# ---------- 5. probe_metadata 接线 ----------

def _fake_sparse(monkeypatch, blob, tmp_path):
    """让 _headtail_sparse_temp 走真实逻辑但落到 tmp_path（可断言删除）。"""
    monkeypatch.setattr(scanner, "_http_fetch_range", _sliced(blob))
    monkeypatch.setattr(scanner.tempfile, "mkstemp",
                        lambda prefix, suffix: (os.open(str(tmp_path / "ht.bin"),
                                                       os.O_CREAT | os.O_RDWR),
                                                str(tmp_path / "ht.bin")))


def test_probe_metadata_uses_headtail_when_format_found(monkeypatch, tmp_path):
    """第零跳拿到 format：直接用，不再跑后面的三跳；码率按真实大小还原。"""
    monkeypatch.setattr(scanner, "_remote_returns_login_page", lambda *a, **k: False)
    blob = b"H" * TOTAL
    _fake_sparse(monkeypatch, blob, tmp_path)
    calls = []

    def fake_ffprobe(path, headers=None, size=0, ranged=True, local_path="",
                     probesize=None, analyzeduration=None):
        calls.append((local_path, probesize, analyzeduration))
        assert local_path and os.path.exists(local_path), "第零跳走本地稀疏文件"
        assert probesize == scanner.PROBE_HEADTAIL_PROBESIZE
        assert analyzeduration == scanner.PROBE_HEADTAIL_ANALYZEDURATION
        return {"format": {"duration": "7200.0", "bit_rate": "1000",
                           "size": str(HEAD + TAIL)},
                "streams": []}

    monkeypatch.setattr(scanner, "_ffprobe", fake_ffprobe)
    info = scanner.probe_metadata(URL, {}, size=TOTAL, container="mkv")
    assert info["duration_ticks"] == 7200 * 10_000_000
    # 合成文件算出的 1000 bps 要按 真实大小/(头+尾) 还原
    assert info["bitrate"] == int(1000 * TOTAL / (HEAD + TAIL))
    assert info["_probe_backend"] == "headtail"
    assert len(calls) == 1, "第零跳成功后不再调 _ffprobe（跳过三跳）"
    assert not os.path.exists(tmp_path / "ht.bin"), "临时文件必须删掉"


def test_probe_metadata_falls_back_when_headtail_misses(monkeypatch):
    """第零跳没拼出来（返回 None）：走现行第一跳，行为不倒退。"""
    monkeypatch.setattr(scanner, "_remote_returns_login_page", lambda *a, **k: False)
    monkeypatch.setattr(scanner, "_headtail_sparse_temp", lambda *a, **k: None)
    calls = []

    def fake_ffprobe(path, headers=None, size=0, ranged=True, local_path="",
                     probesize=None, analyzeduration=None):
        calls.append((ranged, local_path))
        return {"format": {"duration": "60.0"}, "streams": []}

    monkeypatch.setattr(scanner, "_ffprobe", fake_ffprobe)
    info = scanner.probe_metadata(URL, {}, size=TOTAL, container="mkv")
    assert info["duration_ticks"] == 60 * 10_000_000
    assert any(ranged and not lp for ranged, lp in calls), "回退到现行第一跳"


def test_probe_metadata_headtail_respects_disabled_flag(monkeypatch):
    """PROBE_HEADTAIL_ENABLED=0：完全不走第零跳。"""
    monkeypatch.setattr(scanner, "_remote_returns_login_page", lambda *a, **k: False)
    monkeypatch.setattr(scanner, "PROBE_HEADTAIL_ENABLED", False)
    monkeypatch.setattr(scanner, "_headtail_sparse_temp",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("开关关闭时不应调用")))
    calls = []

    def fake_ffprobe(path, headers=None, size=0, ranged=True, local_path="",
                     probesize=None, analyzeduration=None):
        calls.append(True)
        return {"format": {"duration": "60.0"}, "streams": []}

    monkeypatch.setattr(scanner, "_ffprobe", fake_ffprobe)
    info = scanner.probe_metadata(URL, {}, size=TOTAL, container="mkv")
    assert info["duration_ticks"] == 60 * 10_000_000
    assert calls, "现行第一跳仍被调用"
