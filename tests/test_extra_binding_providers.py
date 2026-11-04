"""BYOS_EXTRA_BINDING_PROVIDERS：无对应挂载时也开放绑定入口。"""
import pytest

from backend.emby_server import user_bindings as ub


@pytest.fixture(autouse=True)
def _binding_switch_on(monkeypatch):
    # 本套件用 _DB 桩测 env 额外名单口径，写于 Drive 绑定开关
    # （2026-11-04）之前：把开关读取桩成常开，开关关/开两态由
    # test_drive_binding_switch.py 专门覆盖。
    monkeypatch.setattr(ub, "drive_binding_enabled", lambda db: True)


class _Q:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def distinct(self):
        return self

    def all(self):
        return self._rows


class _DB:
    def __init__(self, rows):
        self._rows = rows

    def query(self, *a, **k):
        return _Q(self._rows)


def test_default_only_enabled_mounts(monkeypatch):
    monkeypatch.delenv("BYOS_EXTRA_BINDING_PROVIDERS", raising=False)
    assert ub.list_binding_providers(_DB([("drive",), ("rclone",)])) == ["drive"]


def test_extra_pan115_removed_not_opened(monkeypatch):
    # 已下线 provider 写进额外名单也不再开放（2026-11-02 删除单）。
    monkeypatch.setenv("BYOS_EXTRA_BINDING_PROVIDERS", "pan115")
    assert ub.list_binding_providers(_DB([("drive",)])) == ["drive"]


def test_extra_ignores_invalid_and_dedups(monkeypatch):
    monkeypatch.setenv("BYOS_EXTRA_BINDING_PROVIDERS", " PAN115 , bogus,drive,pan115 ")
    assert ub.list_binding_providers(_DB([])) == ["drive"]


def test_extra_empty_is_noop(monkeypatch):
    monkeypatch.setenv("BYOS_EXTRA_BINDING_PROVIDERS", "")
    assert ub.list_binding_providers(_DB([("115",)])) == []
