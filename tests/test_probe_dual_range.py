"""远程探测第二跳：moov 在文件尾的 MP4/MOV 用**并行**双 Range 读取。

背景（v2.42.16）：第一跳只注入 ``Range: bytes=0-1MiB``。时长写在 EBML 头部的 MKV
一次就拿到，但 moov 原子在**文件尾**的 mp4/mov 前 1 MiB 里根本没有时长 → format 为空。
旧回退是「去掉 Range 再探一次」，ffprobe 会把整个文件从头拉到尾——8 GB 的片子就是
8 GB 流量，2.6 万条文件撞上它是一场灾难。

这里钉住五件事：
1. 只对 mp4/mov 走第二跳（mkv 头里就有时长，给它加尾部请求纯属白烧流量）；
2. 两个 Range **并行**发出（用 barrier 逼出串行实现），窗口固定为头 1 MiB + 尾 2 MiB；
3. 拼出来的是「头部 + moov」的小文件，并把 mdat 长度改写到恰好停在 moov 前——
   不改写的话 demuxer 跳完 mdat 就 EOF，实测报 ``moov atom not found``；
4. 双 Range 失手时仍然回退到旧的「自由 seek」路径，行为不倒退；
5. 容器级的码率 / 大小要按真实文件还原（合成文件算出来的值是假的）。
"""
import os
import struct
import threading
from typing import Optional

import pytest

from backend.emby_server import scanner


MP4_URL = "https://rclone:5572/[drv:]/movies/Big Movie (2021).mp4?sig=abc"
TOTAL = 8 * 1024 ** 3  # 8 GiB：仅用于「远大于两个窗口」的守卫类断言

#: 拿来跑真实抓取流程的 fixture：比两个窗口之和略大一点，两个窗口才真的错开。
LOCAL_SIZE = scanner.PROBE_REMOTE_RANGE_BYTES + scanner.PROBE_REMOTE_TAIL_BYTES + 8192


def _box(typ: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + typ + payload


def _fake_mp4(total: int = LOCAL_SIZE, moov_body: bytes = b"M" * 1024) -> bytes:
    """一个最小可解析的 mp4：ftyp + free + mdat + moov（moov 在尾）。

    mdat 体故意撑满中间：这正是真实文件的样子（mdat 声明长度 = 整个文件大小），
    也是「必须用 allow_overrun 遍历才能定位 mdat」的原因。
    """
    moov = _box(b"moov", moov_body)
    mdat_body = b"D" * (total - 32 - 8 - 8 - len(moov))
    return (_box(b"ftyp", b"isom" + b"\x00" * 20) + _box(b"free", b"")
            + _box(b"mdat", mdat_body) + moov)


def _split_window(blob: bytes, head: int = 4096, tail: int = 2048):
    """模拟「取头部窗口 + 尾窗口」两段字节。"""
    return blob[:head], blob[max(0, len(blob) - tail):]


def _sliced(blob: bytes, seen: Optional[list] = None):
    """假的服务端：按 Range 返回 blob 对应片段（越界截到末尾，与真实服务器一致）。"""
    def fetch(url, headers, start, end):
        if seen is not None:
            seen.append((start, end))
        return blob[min(start, len(blob)):min(end + 1, len(blob))]
    return fetch


# ---------- 1. 容器白名单：只 mp4/mov 系要尾部再读一次 ----------

@pytest.mark.parametrize("name", ["mp4", "m4v", "m4a", "mov", "qt", "3gp", "MP4"])
def test_tail_moov_containers_match(name):
    assert scanner._tail_moov_container("http://x/a.mp4", name) is True


@pytest.mark.parametrize("name", ["mkv", "webm", "avi", "ts", "flv", "", "mpeg"])
def test_non_tail_moov_containers_skipped(name):
    """MKV 的时长在 EBML 头部：给它加尾部请求纯属白烧网盘流量"""
    assert scanner._tail_moov_container("http://x/a.mkv", name) is False


def test_container_falls_back_to_url_suffix_without_query():
    """直链常带 ?token=…，不能把查询串当扩展名"""
    assert scanner._tail_moov_container(MP4_URL) is True
    assert scanner._tail_moov_container("http://x/a.mkv?sig=1") is False


def test_db_container_wins_over_url_suffix():
    """扩展名可能骗人（.mkv 里装 mp4）：库里记的 container 优先"""
    assert scanner._tail_moov_container("http://x/a.mkv", "mp4") is True
    assert scanner._tail_moov_container("http://x/a.mp4", "mkv") is False


# ---------- 2. 两个 Range 必须并行 ----------

def test_dual_range_fetches_are_parallel(monkeypatch):
    """串行实现会在这里死锁：barrier 要两个请求同时在飞才放行。"""
    barrier = threading.Barrier(2, timeout=5)
    seen = []
    blob = _fake_mp4()

    def fake_fetch(url, headers, start, end):
        seen.append((start, end))
        barrier.wait()  # 第二个请求没并行发起 → BrokenBarrierError
        return blob[min(start, len(blob)):min(end + 1, len(blob))]

    monkeypatch.setattr(scanner, "_http_fetch_range", fake_fetch)
    tmp = scanner._dual_range_temp_file(MP4_URL, {}, len(blob))
    try:
        assert tmp is not None, "两个请求应并行取回并拼出临时文件"
        assert len(seen) == 2
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def test_dual_range_requests_head_and_tail(monkeypatch):
    """窗口固定：头 1 MiB + 尾 2 MiB，尾窗口贴着文件末尾，与文件多大无关"""
    seen = []
    blob = _fake_mp4()
    monkeypatch.setattr(scanner, "_http_fetch_range", _sliced(blob, seen))
    tmp = scanner._dual_range_temp_file(MP4_URL, {}, len(blob))
    try:
        assert tmp is not None
        assert sorted(seen) == sorted([
            (0, scanner.PROBE_REMOTE_RANGE_BYTES - 1),
            (len(blob) - scanner.PROBE_REMOTE_TAIL_BYTES, len(blob) - 1),
        ])
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


# ---------- 3. 拼出来的必须是「头部 + moov」的小文件，且 mdat 要改写 ----------

def test_probe_bytes_are_head_plus_moov():
    """拼装结果 = 头部原样 + moov 原样跟上（只有 mdat 长度字段被改写），与原文件大小无关"""
    blob = _fake_mp4()
    head, tail = _split_window(blob)
    moov = _box(b"moov", b"M" * 1024)
    out = scanner._mp4_probe_bytes(head, tail)
    assert out is not None
    assert out[-len(moov):] == moov, "moov 必须原样跟上"
    assert out[:40] == head[:40], "ftyp / free 原样保留"
    assert out[48:len(head)] == head[48:], "mdat 载荷原样保留"
    assert len(out) < len(blob), "落盘必须远小于原文件，否则等于把整个文件搬下来"


def test_mdat_is_rewritten_to_end_at_moov():
    """不改写 mdat 的话 demuxer 跳完 mdat 就 EOF，压根读不到 moov（实测报 moov atom not found）"""
    blob = _fake_mp4()
    head, tail = _split_window(blob)
    out = scanner._mp4_probe_bytes(head, tail)
    assert out is not None
    boxes = {typ: (start, end) for typ, start, end, _h, _s in scanner._mp4_boxes(out)}
    mdat_start, mdat_end = boxes[b"mdat"]
    moov_start, _ = boxes[b"moov"]
    assert mdat_end == moov_start, "mdat 必须恰好结束在 moov 之前"
    assert struct.unpack(">I", out[mdat_start:mdat_start + 4])[0] == mdat_end - mdat_start


def test_64bit_mdat_is_rewritten():
    """大文件的 mdat 用 64 位长度（size==1），改写要跟着换格式，否则长度对不上"""
    moov = _box(b"moov", b"M" * 64)
    mdat = struct.pack(">I", 1) + b"mdat" + struct.pack(">Q", 1 << 40) + b"D" * 32
    blob = _box(b"ftyp", b"isom" + b"\x00" * 20) + mdat + moov
    out = scanner._mp4_probe_bytes(blob[:200], blob[-128:])
    assert out is not None
    boxes = {typ: (start, end) for typ, start, end, _h, _s in scanner._mp4_boxes(out)}
    mdat_start, mdat_end = boxes[b"mdat"]
    moov_start, _ = boxes[b"moov"]
    assert struct.unpack(">I", out[mdat_start:mdat_start + 4])[0] == 1, "要继续用 64 位长度"
    assert struct.unpack(">Q", out[mdat_start + 8:mdat_start + 16])[0] == mdat_end - mdat_start
    assert mdat_end == moov_start


def test_probe_bytes_reject_missing_moov():
    """尾窗口里没有完整 moov（例如 moov 比窗口还大）→ 返回 None，交给上层回退"""
    blob = _fake_mp4()
    assert scanner._mp4_probe_bytes(blob[:64], blob[:128]) is None


def test_probe_bytes_reject_file_without_top_level_mdat():
    """分片 mp4（moof/mdat 结构）拼不对，硬拼只会产出一个坏文件"""
    blob = _box(b"ftyp", b"isom" + b"\x00" * 20) + _box(b"moof", b"O" * 64) + _box(b"moov", b"M" * 64)
    assert scanner._mp4_probe_bytes(blob[:200], blob[-128:]) is None


def test_find_moov_ignores_bogus_length_field():
    """搜到 moov 四字节码还要校验长度字段，否则会把 mdat 里碰巧出现的字节当成盒子"""
    bogus = b"\x00" * 4 + struct.pack(">I", 4096) + b"moov" + b"Z" * 8
    assert scanner._find_moov(bogus) is None
    good = b"\x00" * 4 + struct.pack(">I", 12) + b"moov" + b"Z" * 8
    assert scanner._find_moov(good) == (4, 16)


def test_mp4_boxes_strict_mode_stops_at_overrunning_box():
    """mdat 声明长度超出缓冲区：严格模式必须停下（否则会把乱码当盒子）"""
    blob = _fake_mp4()
    strict = [t for t, _s, _e, _h, _z in scanner._mp4_boxes(blob[:4096])]
    assert strict == [b"ftyp", b"free"]
    lenient = [t for t, _s, _e, _h, _z in scanner._mp4_boxes(blob[:4096], allow_overrun=True)]
    assert lenient == [b"ftyp", b"free", b"mdat"], "定位 mdat 头要能越过长度越界"


def test_mp4_boxes_stops_on_truncated_tail():
    """头部窗口被截断时不能抛异常，也不能返回半个盒子"""
    blob = _fake_mp4()[:40]
    assert all(end <= len(blob) for _t, _s, end, _h, _sz in scanner._mp4_boxes(blob))


# ---------- 4. 临时文件与清理 ----------

def test_dual_range_skipped_when_file_fits_head_window(monkeypatch):
    """文件整个都在头部窗口里：拼出来就是原文件，没必要"""
    monkeypatch.setattr(scanner, "_http_fetch_range",
                        lambda *a, **k: pytest.fail("不该发起尾部请求"))
    assert scanner._dual_range_temp_file(MP4_URL, {}, 4096) is None


def test_dual_range_skipped_when_windows_overlap(monkeypatch):
    """文件比两个窗口加起来还小：拼出来等于整个文件，不如直接回退"""
    size = scanner.PROBE_REMOTE_RANGE_BYTES + scanner.PROBE_REMOTE_TAIL_BYTES
    monkeypatch.setattr(scanner, "_http_fetch_range",
                        lambda *a, **k: pytest.fail("不该发起请求"))
    assert scanner._dual_range_temp_file(MP4_URL, {}, size) is None


def test_dual_range_returns_none_when_fetch_fails(monkeypatch):
    """一段没拿到就整个放弃，交给上层回退——不能拿半截文件去探"""
    monkeypatch.setattr(scanner, "_http_fetch_range",
                        lambda url, headers, start, end: None if start == 0 else b"z" * 8)
    assert scanner._dual_range_temp_file(MP4_URL, {}, TOTAL) is None


def test_dual_range_surfaces_http_failure(monkeypatch):
    """403/404 要能冒到上层（配额熔断器、永久失败判定都靠它）"""

    def boom(url, headers, start, end):
        raise RuntimeError("HTTP 403")

    monkeypatch.setattr(scanner, "_http_fetch_range", boom)
    assert scanner._dual_range_temp_file(MP4_URL, {}, TOTAL) is None


def test_dual_probe_cleans_up_temp_file(monkeypatch):
    """临时文件必须删掉：一次探测建 2.6 万个就是几万个 inode"""
    blob = _fake_mp4()
    monkeypatch.setattr(scanner, "_http_fetch_range", _sliced(blob))
    monkeypatch.setattr(scanner, "shutil_which", lambda cmd: "/usr/bin/ffprobe")
    seen = {}

    def fake_ffprobe(path, headers=None, size=0, ranged=True, local_path=""):
        seen["local_path"] = local_path
        seen["exists"] = os.path.exists(local_path) if local_path else None
        return {"format": {"duration": "7200.0"}, "streams": []}

    monkeypatch.setattr(scanner, "_ffprobe", fake_ffprobe)
    scanner._dual_range_probe(MP4_URL, {}, len(blob), "mp4")
    assert seen["local_path"], "第二跳走本地临时文件入口"
    assert seen["exists"] is True
    assert not os.path.exists(seen["local_path"]), "探测完必须删掉临时文件"


def test_dual_probe_skips_mkv(monkeypatch):
    """MKV 不进第二跳"""
    monkeypatch.setattr(scanner, "_http_fetch_range",
                        lambda *a, **k: pytest.fail("MKV 不该发尾部请求"))
    assert scanner._dual_range_probe("http://x/a.mkv", {}, TOTAL, "mkv") is None


def test_dual_probe_respects_caller_range_header(monkeypatch):
    """调用方自带 Range 说明它对这条直链另有安排，别插手"""
    monkeypatch.setattr(scanner, "_http_fetch_range",
                        lambda *a, **k: pytest.fail("不该覆盖调用方的 Range"))
    assert scanner._dual_range_probe(MP4_URL, {"Range": "bytes=0-10"}, TOTAL, "mp4") is None


# ---------- 5. 码率/大小还原（合成文件算出来的容器级数值是假的） ----------

def test_container_bitrate_is_rescaled_to_real_file():
    """实测：224 MiB 的文件被合成文件算出 19 kb/s（真值 3134），不还原就写进库了"""
    real_size, synth_size = 235078363, 1433383
    data = {"format": {"duration": "600.0", "bit_rate": "19000", "size": "1433383"}}
    scanner._rescale_synthetic_format(data, real_size=real_size, synth_size=synth_size)
    fmt = data["format"]
    assert fmt["bit_rate"] == int(19000 * real_size / synth_size), "码率要按大小比例还原"
    assert fmt["size"] == str(real_size), "大小也不能是合成文件的大小"


def test_rescale_ignores_missing_bitrate():
    data = {"format": {"duration": "600.0"}}
    scanner._rescale_synthetic_format(data, real_size=235078363, synth_size=1433383)
    assert "bit_rate" not in data["format"]


def test_rescale_keeps_size_even_when_synth_is_bigger():
    """极端情况下合成文件比原文件还大（不该发生），也不能把 size 弄错"""
    data = {"format": {"bit_rate": "1000", "size": "9"}}
    scanner._rescale_synthetic_format(data, real_size=100, synth_size=900)
    assert data["format"]["size"] == "100"
    assert data["format"]["bit_rate"] == "1000"


# ---------- 6. probe_metadata 接线：第二跳优先，失手才回退旧路径 ----------

def test_mp4_second_hop_beats_slow_seek(monkeypatch):
    """mp4：第一跳没时长 → 双 Range 拿到 format，就**不该**再跑慢速回退"""
    blob = _fake_mp4()
    calls = []

    def fake_ffprobe(path, headers=None, size=0, ranged=True, local_path=""):
        calls.append("local" if local_path else "ranged")
        if local_path:
            return {"format": {"duration": "5400.0", "bit_rate": "9000000"},
                    "streams": [{"codec_type": "video", "codec_name": "h264"}]}
        return {}

    monkeypatch.setattr(scanner, "_ffprobe", fake_ffprobe)
    monkeypatch.setattr(scanner, "_http_fetch_range", _sliced(blob))
    info = scanner.probe_metadata(MP4_URL, size=len(blob))
    assert calls == ["ranged", "local"]
    assert info["duration_ticks"] == 5400 * 10_000_000
    assert not info.get("_degraded")


def test_mkv_never_takes_second_hop(monkeypatch):
    """MKV 第一跳失败直接走旧的自由 seek，第二跳对它完全不触发"""
    calls = []

    def fake_ffprobe(path, headers=None, size=0, ranged=True, local_path=""):
        calls.append("local" if local_path else ranged)
        if local_path:
            return pytest.fail("MKV 不该走双 Range")
        if ranged:
            return {}
        return {"format": {"duration": "1800.0"}, "streams": []}

    monkeypatch.setattr(scanner, "_ffprobe", fake_ffprobe)
    monkeypatch.setattr(scanner, "_http_fetch_range",
                        lambda *a, **k: pytest.fail("MKV 不该发尾部请求"))
    # 本例验的是回退机制本身：关掉整文件流量闸（TOTAL=8GiB 默认会被闸拦下，
    # 闸的行为由 test_playback_readfix 单独覆盖）
    monkeypatch.setattr(scanner, "PROBE_WHOLE_FILE_MAX_BYTES", 0)
    info = scanner.probe_metadata("http://x/a.mkv", size=TOTAL)
    assert calls == [True, False]
    assert info["duration_ticks"] == 1800 * 10_000_000


def test_falls_back_to_slow_seek_when_dual_range_fails(monkeypatch):
    """双 Range 失手不能把结果搞坏：仍能回退到旧的自由 seek 路径"""
    calls = []

    def fake_ffprobe(path, headers=None, size=0, ranged=True, local_path=""):
        if local_path:
            return {}  # 拼出来的文件也没读出 moov
        calls.append(ranged)
        if ranged:
            return {}
        return {"format": {"duration": "600.0"}, "streams": []}

    monkeypatch.setattr(scanner, "_ffprobe", fake_ffprobe)
    monkeypatch.setattr(scanner, "_http_fetch_range", lambda *a, **k: None)
    # 同上：关掉整文件流量闸，只验双 Range 落空后的回退
    monkeypatch.setattr(scanner, "PROBE_WHOLE_FILE_MAX_BYTES", 0)
    info = scanner.probe_metadata(MP4_URL, size=TOTAL)
    assert calls == [True, False], "双 Range 落空后仍要试一次自由 seek"
    assert info["duration_ticks"] == 600 * 10_000_000


def test_http_error_still_skips_second_hop(monkeypatch):
    """403 重试没有意义：配额熔断器已经拿到信号了"""
    monkeypatch.setattr(scanner, "_ffprobe", lambda *a, **k: {
        "_http_code": 403, "_error": "quota", "_error_detail": "远端配额/权限受限 (HTTP 403)"})
    monkeypatch.setattr(scanner, "_http_fetch_range",
                        lambda *a, **k: pytest.fail("HTTP 错误不该再发请求"))
    info = scanner.probe_metadata(MP4_URL, size=TOTAL)
    assert info["_error"] == "quota" and info["_http_code"] == 403


def test_local_file_never_takes_second_hop(monkeypatch):
    """本机文件不需要：ffprobe 直接就能 seek 到 moov"""
    monkeypatch.setattr(scanner, "_ffprobe", lambda *a, **k: {
        "format": {"duration": "120.0"}, "streams": []})
    monkeypatch.setattr(scanner, "_http_fetch_range",
                        lambda *a, **k: pytest.fail("本机文件不该发 HTTP 请求"))
    assert scanner.probe_metadata("/tmp/a.mp4", size=TOTAL)["duration_ticks"] == 120 * 10_000_000