"""FMBY 批三 #23（阶段 2）provider 五能力位声明骨架。

骨架阶段的唯一承诺：声明如实、零行为变化。本文件只钉声明本身；
协议拆分（resolve_play_target / open_range / read_sidecar_text /
refresh_credentials）的测试在同一文件的第二部分，随拆分提交一起加。
"""
import ast
import json
import os
from pathlib import Path

import pytest
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from backend.emby_server import mounts as mnt
from backend.emby_server.mount_drive import DriveMount
from backend.emby_server.mount_rclone import RcloneMount
from backend.emby_server.mounts import (
    LocalMount,
    MountProvider,
    Pan115Mount,
    ProviderCapabilities,
)


def _mount(**kw):
    base = {"mount_type": "", "config": "{}", "path": "", "name": "test"}
    base.update(kw)
    return SimpleNamespace(**base)


def test_base_capabilities_default_all_false():
    prov = MountProvider(_mount())
    assert prov.capabilities() == ProviderCapabilities(
        can_list=False, can_random_read=False, can_read_sidecar=False,
        can_generate_play_target=False, can_refresh_credentials=False,
    )


def test_capabilities_as_dict_roundtrip():
    caps = ProviderCapabilities(can_list=True, can_refresh_credentials=True)
    d = caps.as_dict()
    assert d == {
        "can_list": True, "can_random_read": False, "can_read_sidecar": False,
        "can_generate_play_target": False, "can_refresh_credentials": True,
    }


def test_local_capabilities(tmp_path):
    prov = LocalMount(_mount(mount_type="local", path=str(tmp_path)))
    assert prov.capabilities() == ProviderCapabilities(
        can_list=True, can_random_read=True, can_read_sidecar=True,
        can_generate_play_target=True, can_refresh_credentials=False,
    )


def test_pan115_capabilities():
    # 115 全仓唯一确定的否定声明：Cookie 静态、不可刷新。
    prov = Pan115Mount(_mount(
        mount_type="pan115", config=json.dumps({"cid": "0"})))
    assert prov.capabilities() == ProviderCapabilities(
        can_list=True, can_random_read=True, can_read_sidecar=True,
        can_generate_play_target=True, can_refresh_credentials=False,
    )


def test_drive_capabilities():
    prov = DriveMount(_mount(
        mount_type="drive", path="drive:0AERUVu4kfWEfUk9PVA"))
    assert prov.capabilities() == ProviderCapabilities(
        can_list=True, can_random_read=True, can_read_sidecar=True,
        can_generate_play_target=True, can_refresh_credentials=True,
    )


def test_rclone_capabilities_both_modes():
    for mode in ("rc", "cli"):
        prov = RcloneMount(_mount(
            mount_type="rclone", path="rclone:gdrive/Movies",
            config=json.dumps({"mode": mode})))
        assert prov.mode == mode
        assert prov.capabilities() == ProviderCapabilities(
            can_list=True, can_random_read=True, can_read_sidecar=True,
            can_generate_play_target=True, can_refresh_credentials=False,
        )


# ---------------- 拆分（第二步） ----------------
# 协议四口：resolve_play_target（换链）/ open_range（搬字节）/
# read_sidecar_text（文本旁挂）/ refresh_credentials（凭据刷新）。
# 钉死点：resolve_final 的 .strm 耦合显式走旁挂口，不靠默认兜底；
# 清单第 2 项：旧 resolve 已删，换链一律走 resolve_play_target；
# 旧 read_text 名保留，duck-type 旁挂不被静默打断。


class _SpyProvider(MountProvider):
    """MountProvider 子类桩：记录换链与旁挂各被调了几次。"""

    def __init__(self):
        self.resolve_calls = []
        self.sidecar_calls = []

    def resolve_play_target(self, rel):
        self.resolve_calls.append(rel)
        return mnt.PlayTarget("url", "http://x/f", {})

    def read_text(self, rel):
        self.sidecar_calls.append(rel)
        return "http://x/target.mkv"


def test_resolve_final_strm_uses_sidecar_not_resolve():
    prov = _SpyProvider()
    target = prov.resolve_final("a.strm")
    assert target.value == "http://x/target.mkv"
    assert prov.sidecar_calls == ["a.strm"]
    assert prov.resolve_calls == []


def test_resolve_final_non_strm_uses_chain_not_sidecar():
    prov = _SpyProvider()
    target = prov.resolve_play_target("a.mkv")
    assert target.value == "http://x/f"
    assert prov.resolve_calls == ["a.mkv"]
    assert prov.sidecar_calls == []


def test_resolve_play_target_is_chain_only():
    """换链口不读旁挂：旁挂桩被调就炸，证明两口已分开。"""

    class _ChainOnly(_SpyProvider):
        def read_sidecar_text(self, rel):
            raise AssertionError("换链口不许读旁挂")

    target = _ChainOnly().resolve_play_target("a.mkv")
    assert target.value == "http://x/f"


def test_read_sidecar_text_defaults_to_read_text():
    prov = _SpyProvider()
    assert prov.read_sidecar_text("a.nfo") == "http://x/target.mkv"
    assert prov.sidecar_calls == ["a.nfo"]


class _DuckProvider:
    """只实现旧 read_text 的 duck-type 提供者（非 MountProvider 子类）。"""

    def __init__(self):
        self.read_text_calls = []

    def read_text(self, rel):
        self.read_text_calls.append(rel)
        return "duck-text"


def test_sidecar_transition_falls_back_for_duck_provider():
    duck = _DuckProvider()
    assert mnt.read_sidecar_text_from(duck, "a.nfo") == "duck-text"
    assert duck.read_text_calls == ["a.nfo"]


def test_sidecar_transition_prefers_new_mouth():
    prov = _SpyProvider()
    assert mnt.read_sidecar_text_from(prov, "a.nfo") == "http://x/target.mkv"
    assert prov.sidecar_calls == ["a.nfo"]


def test_open_range_local_seek(tmp_path):
    data = b"0123456789"
    (tmp_path / "f.mkv").write_bytes(data)
    prov = LocalMount(_mount(mount_type="local", path=str(tmp_path)))
    assert prov.open_range("f.mkv", 2, 5) == b"234"
    assert prov.open_range("f.mkv", 8) == b"89"
    with pytest.raises(mnt.MountError):
        prov.open_range("f.mkv", 5, 5)
    with pytest.raises(mnt.MountError):
        prov.open_range("f.mkv", -1)


def test_open_range_local_strm_reads_file_itself(tmp_path):
    """open_range 读的是 .strm 文件本身，不解析其目标。"""
    (tmp_path / "a.strm").write_text("http://x/target.mkv")
    prov = LocalMount(_mount(mount_type="local", path=str(tmp_path)))
    assert prov.open_range("a.strm", 0, 4) == b"http"


class _FakeRemote(mnt.RemoteMount):
    def __init__(self, status, body, headers_seen):
        self._status = status
        self._body = body
        self._headers_seen = headers_seen

    def resolve_play_target(self, rel):
        return mnt.PlayTarget("url", "http://x/f", {})

    def _request(self, method, url, headers=None, **kw):
        self._headers_seen.append(headers or {})
        return SimpleNamespace(status_code=self._status, content=self._body)


def test_open_range_remote_206_passthrough():
    seen = []
    prov = _FakeRemote(206, b"bc", seen)
    assert prov.open_range("f.mkv", 1, 3) == b"bc"
    assert seen[0]["Range"] == "bytes=1-2"


def test_open_range_remote_200_sliced_locally():
    """上游无视 Range 回 200：本地切片兜底，调用方仍拿到正确字节。"""
    seen = []
    prov = _FakeRemote(200, b"0123456789", seen)
    assert prov.open_range("f.mkv", 2, 5) == b"234"
    assert seen[0]["Range"] == "bytes=2-4"


def test_open_range_remote_error_raises():
    prov = _FakeRemote(404, b"", [])
    with pytest.raises(mnt.MountError):
        prov.open_range("f.mkv", 0, 4)


def test_open_range_pan115(monkeypatch):

    class _FakeClient:
        def __init__(self, seen):
            self._seen = seen

        def get(self, url, headers=None):
            self._seen.append(headers or {})
            return SimpleNamespace(status_code=206, content=b"xy")

    class _Pan115(Pan115Mount):
        def __init__(self):
            pass

        def resolve_play_target(self, rel):
            return mnt.PlayTarget("url", "http://x/115", {})

    seen = []
    monkeypatch.setattr(mnt, "_shared_http_client", lambda name: _FakeClient(seen))
    prov = _Pan115()
    assert prov.open_range("f.mkv", 0, 2) == b"xy"
    assert seen[0]["Range"] == "bytes=0-1"
    # 115 凭据不经 open_range 下发以外的路：头里没有 Cookie 钥匙以外的
    # 凭据字段（resolve 桩只给空头，本断言钉住“不额外拼凭据”）。
    assert "Cookie" not in seen[0]


def test_refresh_credentials_matrix(monkeypatch):

    base = MountProvider(_mount())
    assert base.refresh_credentials() is False
    local = LocalMount(_mount(mount_type="local", path="/tmp"))
    assert local.refresh_credentials() is False
    pan = Pan115Mount(_mount(
        mount_type="pan115", config=json.dumps({"cid": "0"})))
    assert pan.refresh_credentials() is False
    rclone = RcloneMount(_mount(
        mount_type="rclone", path="rclone:gdrive/Movies",
        config=json.dumps({"mode": "cli"})))
    assert rclone.refresh_credentials() is False

    drive = DriveMount(_mount(
        mount_type="drive", path="drive:0AERUVu4kfWEfUk9PVA"))
    monkeypatch.setattr(drive, "_token", lambda force_refresh=False: "tok")
    assert drive.refresh_credentials() is True

    def _boom(force_refresh=False):
        raise mnt.MountAuthError("no token")

    monkeypatch.setattr(drive, "_token", _boom)
    with pytest.raises(mnt.MountAuthError):
        drive.refresh_credentials()


def test_capabilities_callers_are_only_the_gates():
    """清单第 2 项：能力声明不再是死代码——调用方只许是门控点。

    骨架阶段（af2769a2）的“零调用方”断言已被本阶段取代：上层开始按
    能力编排时，本测试按原文要求改成“调用方清单”断言（不是悄悄删）。
    清单外新增调用方必须先更新本断言（说明新门控点），不许悄悄加。
    本断言只扫 backend/emby_server（生产代码），不扫 tests/。
    """
    root = Path(__file__).resolve().parents[1] / "backend" / "emby_server"
    callers = []
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        func_stack: list[str] = []

        class _Visitor(ast.NodeVisitor):
            def visit_FunctionDef(self, node):
                func_stack.append(node.name)
                self.generic_visit(node)
                func_stack.pop()

            def visit_AsyncFunctionDef(self, node):
                func_stack.append(node.name)
                self.generic_visit(node)
                func_stack.pop()

            def visit_Attribute(self, node):
                if node.attr == "capabilities":
                    # 取最外层函数名：门控闭包 _mount_source 算进
                    # build_library_sources，不单列。
                    callers.append((path.name, func_stack[0] if func_stack else "<module>"))
                self.generic_visit(node)

        _Visitor().visit(tree)
    assert sorted(callers) == [
        ("mounts.py", "build_library_sources"),
        ("mounts.py", "refresh_credentials_if_supported"),
    ], callers
