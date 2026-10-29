"""BYOS_EXTRA_BINDING_PROVIDERS：无对应挂载时也开放绑定入口。"""
from backend.emby_server import user_bindings as ub


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


def test_extra_pan115_without_mount(monkeypatch):
    monkeypatch.setenv("BYOS_EXTRA_BINDING_PROVIDERS", "pan115")
    assert ub.list_binding_providers(_DB([("drive",)])) == ["drive", "pan115"]


def test_extra_ignores_invalid_and_dedups(monkeypatch):
    monkeypatch.setenv("BYOS_EXTRA_BINDING_PROVIDERS", " PAN115 , bogus,drive,pan115 ")
    assert ub.list_binding_providers(_DB([])) == ["drive", "pan115"]


def test_extra_empty_is_noop(monkeypatch):
    monkeypatch.setenv("BYOS_EXTRA_BINDING_PROVIDERS", "")
    assert ub.list_binding_providers(_DB([("115",)])) == ["pan115"]
