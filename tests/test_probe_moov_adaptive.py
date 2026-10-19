"""自适应尾窗：moov 比固定尾窗大时的补救。

背景（2026-10-19）：生产 19 个大文件（1.3~3.4 GiB）的 moov 为 2.2~4.6 MiB，
大于固定的 PROBE_REMOTE_TAIL_BYTES（2 MiB）——moov 的四字节码根本不在尾窗口里，
``_find_moov`` 注定找不到，文件被 ``moov atom not found`` 判死。
修法：用头部 mdat 的声明长度算出 moov 起点（= 第一个 mdat 的终点），
在 PROBE_MOOV_MAX_BYTES 上限内补一次精确 Range。

这里钉住：
1. 大 moov 文件：恰好多 1 次请求，拼出「头部 + 完整 moov」；
2. 正常文件（moov 在窗内）：请求数不变（2 次），行为零变化；
3. moov 跨度超上限：不补取，返回 None（现行失败语义）；
4. largesize / mdat 后有 free 盒 / 损坏文件：不崩、语义正确。
"""
import os
import struct

import pytest

from backend.emby_server import scanner


MP4_URL = "https://rclone:5572/[drv:]/movies/Big Movie (2021).mp4?sig=abc"

HEAD_WIN = scanner.PROBE_REMOTE_RANGE_BYTES   # 1 MiB
TAIL_WIN = scanner.PROBE_REMOTE_TAIL_BYTES    # 2 MiB
MOOV_MAX = scanner.PROBE_MOOV_MAX_BYTES       # 8 MiB


def _box(typ: bytes, payload: bytes, largesize: bool = False) -> bytes:
    if largesize:
        return struct.pack(">I", 1) + typ + struct.pack(">Q", 16 + len(payload)) + payload
    return struct.pack(">I", 8 + len(payload)) + typ + payload


def _big_moov_file(moov_size: int, media_size: int = 1 << 20,
                   largesize_mdat: bool = False, free_after_mdat: bool = False,
                   moov_largesize: bool = False, corrupt_tail: bool = False) -> bytes:
    """ftyp + mdat + [free] + moov(moov_size) 的合成文件；moov 故意大于尾窗口。"""
    ftyp = _box(b"ftyp", b"isom" + b"\x00" * 20)
    media = b"D" * media_size
    if largesize_mdat:
        mdat = struct.pack(">I", 1) + b"mdat" + struct.pack(">Q", 16 + len(media)) + media
    else:
        mdat = _box(b"mdat", media)
    mid = _box(b"free", b"") if free_after_mdat else b""
    moov = _box(b"moov", b"M" * (moov_size - 16 if moov_largesize else moov_size - 8),
                largesize=moov_largesize)
    assert len(moov) == moov_size
    blob = ftyp + mdat + mid + moov
    if corrupt_tail:
        # 把 moov 区写成乱码：模拟「mdat 后根本没有 moov」的损坏文件
        blob = blob[:len(ftyp) + len(mdat)] + b"X" * (len(blob) - len(ftyp) - len(mdat))
    return blob


def _sliced(blob: bytes, seen=None):
    def fetch(url, headers, start, end):
        if seen is not None:
            seen.append((start, end))
        return blob[min(start, len(blob)):min(end + 1, len(blob))]
    return fetch


def _read_tmp(tmp):
    try:
        with open(tmp, "rb") as fh:
            return fh.read()
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def test_mdat_end_from_head():
    blob = _big_moov_file(3 << 20)
    head = blob[:HEAD_WIN]
    mdat_end = scanner._mdat_end_from_head(head)
    # ftyp(32) + mdat头(8) + media(1MiB)
    assert mdat_end == 32 + 8 + (1 << 20)


def test_mdat_end_from_head_none_without_mdat():
    assert scanner._mdat_end_from_head(b"ftyp...." + b"\x00" * 100) is None
    assert scanner._mdat_end_from_head(b"") is None


def test_big_moov_gets_one_extra_request(monkeypatch):
    moov_size = 3 << 20
    blob = _big_moov_file(moov_size)
    seen = []
    monkeypatch.setattr(scanner, "_http_fetch_range", _sliced(blob, seen))
    tmp = scanner._dual_range_temp_file(MP4_URL, {}, len(blob))
    assert tmp is not None
    # 头 + 尾 + 自适应补取 = 恰好 3 次
    assert len(seen) == 3, seen
    payload = _read_tmp(tmp)
    # 拼出来的 payload 里 moov 完整
    hit = payload.find(b"moov")
    assert hit > 0
    size = struct.unpack(">I", payload[hit - 4:hit])[0]
    assert size == moov_size
    assert len(payload) >= hit - 4 + moov_size
    # mdat 被改写成「恰好停在 moov 前」
    mdat_at = payload.find(b"mdat") - 4
    new_size = struct.unpack(">I", payload[mdat_at:mdat_at + 4])[0]
    assert new_size == (hit - 4) - mdat_at


def test_normal_moov_no_extra_request(monkeypatch):
    # 文件要大于头+尾两窗口之和（否则两窗口重叠、函数按设计直接返回 None）
    blob = _big_moov_file(1 << 20, media_size=3 << 20)  # moov 在尾窗口内
    seen = []
    monkeypatch.setattr(scanner, "_http_fetch_range", _sliced(blob, seen))
    tmp = scanner._dual_range_temp_file(MP4_URL, {}, len(blob))
    assert tmp is not None
    assert len(seen) == 2, seen  # 行为零变化：还是头+尾两次
    _read_tmp(tmp)


def test_moov_at_window_boundary_no_extra_request(monkeypatch):
    # moov 恰好等于尾窗口：起点正好落在窗口起点上，固定窗口能找到
    blob = _big_moov_file(TAIL_WIN)
    seen = []
    monkeypatch.setattr(scanner, "_http_fetch_range", _sliced(blob, seen))
    tmp = scanner._dual_range_temp_file(MP4_URL, {}, len(blob))
    assert tmp is not None
    assert len(seen) == 2, seen
    _read_tmp(tmp)


def test_moov_beyond_max_no_extra_request(monkeypatch):
    blob = _big_moov_file(MOOV_MAX + (1 << 20))  # 9 MiB > 上限
    seen = []
    monkeypatch.setattr(scanner, "_http_fetch_range", _sliced(blob, seen))
    assert scanner._dual_range_temp_file(MP4_URL, {}, len(blob)) is None
    assert len(seen) == 2, seen  # 超限不补取，现行失败语义


def test_largesize_mdat(monkeypatch):
    blob = _big_moov_file(3 << 20, largesize_mdat=True)
    seen = []
    monkeypatch.setattr(scanner, "_http_fetch_range", _sliced(blob, seen))
    tmp = scanner._dual_range_temp_file(MP4_URL, {}, len(blob))
    assert tmp is not None
    assert len(seen) == 3, seen
    payload = _read_tmp(tmp)
    hit = payload.find(b"moov")
    assert hit > 0
    assert struct.unpack(">I", payload[hit - 4:hit])[0] == 3 << 20


def test_largesize_moov(monkeypatch):
    blob = _big_moov_file(3 << 20, moov_largesize=True)
    seen = []
    monkeypatch.setattr(scanner, "_http_fetch_range", _sliced(blob, seen))
    tmp = scanner._dual_range_temp_file(MP4_URL, {}, len(blob))
    assert tmp is not None
    assert len(seen) == 3, seen
    payload = _read_tmp(tmp)
    hit = payload.find(b"moov")
    assert hit > 0
    assert struct.unpack(">I", payload[hit - 4:hit])[0] == 1
    assert struct.unpack(">Q", payload[hit + 4:hit + 12])[0] == 3 << 20


def test_free_box_between_mdat_and_moov(monkeypatch):
    blob = _big_moov_file(3 << 20, free_after_mdat=True)
    seen = []
    monkeypatch.setattr(scanner, "_http_fetch_range", _sliced(blob, seen))
    tmp = scanner._dual_range_temp_file(MP4_URL, {}, len(blob))
    assert tmp is not None
    assert len(seen) == 3, seen
    payload = _read_tmp(tmp)
    assert payload.find(b"moov") > 0


def test_corrupt_no_moov_returns_none(monkeypatch):
    blob = _big_moov_file(3 << 20, corrupt_tail=True)
    seen = []
    monkeypatch.setattr(scanner, "_http_fetch_range", _sliced(blob, seen))
    assert scanner._dual_range_temp_file(MP4_URL, {}, len(blob)) is None
    # 补取发生了一次（有界），但拼不出 moov → None，不崩
    assert len(seen) == 3, seen


def test_adaptive_fetch_failure_returns_none(monkeypatch):
    blob = _big_moov_file(3 << 20)
    calls = []

    def fetch(url, headers, start, end):
        calls.append((start, end))
        if len(calls) > 2:
            raise RuntimeError("boom")
        return blob[min(start, len(blob)):min(end + 1, len(blob))]

    monkeypatch.setattr(scanner, "_http_fetch_range", fetch)
    assert scanner._dual_range_temp_file(MP4_URL, {}, len(blob)) is None
