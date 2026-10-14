"""P0（2026-10-14）：全量轮无脑重写所有 .strm 导致 enrich 大翻转的修复。

根因：全量轮复用分支对每个已登记文件都调 _write_strm_file（drive_url 每次
都是无签名的新 URL，字节与磁盘上已签名版本不同）→ 约 20 万文件 mtime+size
齐变 → 扫描器 _compute_size_change 判 size_changed → enrich done→pending 翻转。

修复：重写前读已存在文件，解析 URL 签名剩余有效期；仍有效且剩余 > 30 分钟
→ 跳过重写（mtime/size 不动，generated 不计）；缺失/过期/将过期/file_id
不一致 → 照常重写（"文件必存在且可用"不变式不打破）。

不碰网络（Drive API 全部隔离）；TTL/verify/去重逻辑不动。
"""
import hashlib
import hmac
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.emby_server import strm_gen as sg
from backend.emby_server import strm_sign


def _local_fid(rel: str) -> str:
    return "local:" + hashlib.sha256(rel.encode()).hexdigest()[:32]


def _expired_signed_url(fid: str, expired_seconds_ago: int = 100) -> str:
    """手工构造一个过期但 HMAC 自洽的签名 URL（= _expired_but_authentic 口径）。"""
    from backend.security import SECRET_KEY

    canonical = sg.drive_url(fid)
    exp = int(time.time()) - expired_seconds_ago
    sig = hmac.new(SECRET_KEY.encode(), f"{canonical}:{exp}".encode(),
                   hashlib.sha256).hexdigest()[:32]
    return f"{canonical}&aexp={exp}&asig={sig}"


def _write(path: str, content: str, mtime: float | None = None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


class TestExistingStrmSigRemaining:
    """_existing_strm_sig_remaining 纯逻辑：只读不写，永不抛异常。"""

    def test_valid_sig_returns_remaining(self, tmp_path):
        fid = "FID_VALID"
        p = str(tmp_path / "a.strm")
        _write(p, strm_sign.sign_strm_url(sg.drive_url(fid), ttl_seconds=3600) + "\n")
        left = sg._existing_strm_sig_remaining(p, fid)
        assert left is not None
        assert left > sg._FULL_REUSE_SIG_MIN_REMAINING_S

    def test_expiring_soon_returns_small_remaining(self, tmp_path):
        fid = "FID_SOON"
        p = str(tmp_path / "b.strm")
        _write(p, strm_sign.sign_strm_url(sg.drive_url(fid), ttl_seconds=600) + "\n")
        left = sg._existing_strm_sig_remaining(p, fid)
        assert left is not None
        assert 0 < left < sg._FULL_REUSE_SIG_MIN_REMAINING_S

    def test_expired_sig_returns_none(self, tmp_path):
        fid = "FID_EXP"
        p = str(tmp_path / "c.strm")
        _write(p, _expired_signed_url(fid) + "\n")
        assert sg._existing_strm_sig_remaining(p, fid) is None

    def test_legacy_no_sig_returns_none(self, tmp_path):
        fid = "FID_LEGACY"
        p = str(tmp_path / "d.strm")
        _write(p, sg.drive_url(fid) + "\n")
        assert sg._existing_strm_sig_remaining(p, fid) is None

    def test_tampered_sig_returns_none(self, tmp_path):
        fid = "FID_TAMPER"
        p = str(tmp_path / "e.strm")
        good = strm_sign.sign_strm_url(sg.drive_url(fid), ttl_seconds=3600)
        bad = good[:-1] + ("0" if good[-1] != "0" else "1")
        _write(p, bad + "\n")
        assert strm_sign.verify_strm_url(bad + "\n")[1] == "bad"
        assert sg._existing_strm_sig_remaining(p, fid) is None

    def test_missing_file_returns_none(self, tmp_path):
        assert sg._existing_strm_sig_remaining(
            str(tmp_path / "nope.strm"), "FID_X") is None

    def test_empty_file_returns_none(self, tmp_path):
        p = str(tmp_path / "f.strm")
        _write(p, "\n")
        assert sg._existing_strm_sig_remaining(p, "FID_X") is None

    def test_fid_mismatch_returns_none(self, tmp_path):
        # 盘上已替换文件（file_id 变了）：必须重写，不能复用旧签名
        p = str(tmp_path / "g.strm")
        _write(p, strm_sign.sign_strm_url(sg.drive_url("FID_OLD"), ttl_seconds=3600) + "\n")
        assert sg._existing_strm_sig_remaining(p, "FID_NEW") is None


class TestFullRoundReuseSkip:
    """全量轮复用分支集成：local 模式跑 run_owned(full=True)。

    3 个已登记文件：2 个签名有效（>30min）→ mtime 不变、generated 不计；
    1 个签名过期 → 被重写、generated 计 1。
    """

    def _setup(self, tmp_path, files: dict[str, str]):
        media_root = str(tmp_path / "media")
        strm_root = str(tmp_path / "strm")
        did = "local:" + hashlib.sha256(
            os.path.abspath(media_root).encode()).hexdigest()[:16]

        loaded_state: dict = {}
        for rel, sig_content in files.items():
            full = os.path.join(media_root, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as f:
                f.write(b"\x00" * 1024)
            fid = _local_fid(rel)
            size = os.path.getsize(full)
            spath = os.path.splitext(rel)[0] + ".strm"
            loaded_state[(did, rel)] = (fid, size, spath)
            _write(os.path.join(strm_root, spath), sig_content + "\n",
                   mtime=time.time() - 7200)  # 2 小时前的旧 mtime
        return media_root, strm_root, loaded_state

    def _run(self, monkeypatch, media_root, strm_root, loaded_state):
        saved_rows: list = []
        monkeypatch.setattr(sg, "_load_state", lambda: dict(loaded_state))
        monkeypatch.setattr(sg, "_save_state_rows",
                            lambda rows: saved_rows.extend(rows))
        monkeypatch.setattr(sg, "_clear_gone_marks", lambda fids: None)
        monkeypatch.setattr(sg, "source_dirs", lambda db: [""])
        monkeypatch.setattr(sg, "source_type", lambda db: "local")
        monkeypatch.setattr(sg, "local_root", lambda db: media_root)
        monkeypatch.setattr(sg, "_container_strm_root", lambda db: strm_root)
        monkeypatch.setattr(sg, "dedupe_same_size_enabled", lambda db: False)
        monkeypatch.setattr(sg, "prune_enabled", lambda db: False)

        result = sg.run_owned(None, full=True)
        return result, saved_rows

    def _run_full(self, tmp_path, monkeypatch, files: dict[str, str]):
        media_root, strm_root, loaded_state = self._setup(tmp_path, files)
        result, saved_rows = self._run(monkeypatch, media_root, strm_root,
                                       loaded_state)
        return result, strm_root, loaded_state, saved_rows

    def test_valid_sigs_skipped_expired_rewritten(self, tmp_path, monkeypatch):
        fid_a = _local_fid("a.mkv")
        fid_b = _local_fid("b.mkv")
        fid_c = _local_fid("c.mkv")
        files = {
            "a.mkv": strm_sign.sign_strm_url(sg.drive_url(fid_a), ttl_seconds=3600),
            "b.mkv": strm_sign.sign_strm_url(sg.drive_url(fid_b), ttl_seconds=7200),
            "c.mkv": _expired_signed_url(fid_c),  # 过期 → 必须重写
        }
        result, strm_root, loaded_state, saved_rows = self._run_full(
            tmp_path, monkeypatch, files)

        assert result["ok"] is True
        stats = result["stats"]
        # 验收：有效签名的 2 个不计 generated，过期 1 个计 generated
        assert stats["generated"] == 1, stats
        assert stats["skipped"] == 2, stats
        assert stats["failed"] == 0, stats

        # 验收：有效签名的文件 mtime/内容纹丝不动
        for name in ("a.strm", "b.strm"):
            p = os.path.join(strm_root, name)
            st = os.stat(p)
            assert time.time() - st.st_mtime > 3600, f"{name} mtime 被改动了"
            with open(p, encoding="utf-8") as f:
                assert f.read().strip() == files[name.replace(".strm", ".mkv")]

        # 验收：过期的被重写为无签名 URL（内容变、mtime 更新）
        pc = os.path.join(strm_root, "c.strm")
        with open(pc, encoding="utf-8") as f:
            new_content = f.read().strip()
        assert new_content == sg.drive_url(fid_c)
        assert time.time() - os.stat(pc).st_mtime < 300

        # 状态行：跳过的 2 个不重存（行内容无变化），重写的 1 个落盘
        assert len(saved_rows) == 1
        assert saved_rows[0][1] == "c.mkv"

        # verify 完整性不受影响：3 个都在 state 里
        assert result["stats"]["verify"]["incomplete_series"] == 0

    def test_missing_file_rewritten(self, tmp_path, monkeypatch):
        # 不变式：文件缺失必须写（不能因为"签名有效"就跳过不存在的文件）
        fid_a = _local_fid("a.mkv")
        media_root, strm_root, loaded_state = self._setup(tmp_path, {
            "a.mkv": strm_sign.sign_strm_url(sg.drive_url(fid_a), ttl_seconds=3600)})
        os.unlink(os.path.join(strm_root, "a.strm"))  # 模拟文件被删
        result, _ = self._run(monkeypatch, media_root, strm_root, loaded_state)
        assert result["ok"] is True
        assert result["stats"]["generated"] == 1, result["stats"]
        assert result["stats"]["skipped"] == 0, result["stats"]
        assert os.path.exists(os.path.join(strm_root, "a.strm"))
