"""302 Next F1：总闸与子开关（默认全关）。

现读 env，不在 import 时钉死；合法开值 1/true/yes/on（大小写不敏感）；
缺省、空串、0/false/no/off 都是关。子开关只在总闸开时才生效。
"""

import pytest

from backend.emby_server import next302_gate as gate


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (gate.MASTER_ENV, gate.DATASET_ENV, gate.SEARCH_ENV,
                gate.DATASET_INTERVAL_ENV):
        monkeypatch.delenv(key, raising=False)


def test_default_all_off(monkeypatch):
    """缺省 = 全关：总闸、数据集、搜索都是关。"""
    assert gate.next302_enabled() is False
    assert gate.dataset_enabled() is False
    assert gate.search_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "Yes", "on", "ON"])
def test_open_values(monkeypatch, value):
    monkeypatch.setenv(gate.MASTER_ENV, value)
    assert gate.next302_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "  ", "2", "enabled"])
def test_closed_values(monkeypatch, value):
    monkeypatch.setenv(gate.MASTER_ENV, value)
    assert gate.next302_enabled() is False


def test_sub_switch_requires_master(monkeypatch):
    """子开关自己开着、总闸关着 = 还是关（AND 语义）。"""
    monkeypatch.setenv(gate.DATASET_ENV, "1")
    monkeypatch.setenv(gate.SEARCH_ENV, "1")
    assert gate.dataset_enabled() is False
    assert gate.search_enabled() is False
    monkeypatch.setenv(gate.MASTER_ENV, "1")
    assert gate.dataset_enabled() is True
    assert gate.search_enabled() is True


def test_dataset_interval_default_and_floor(monkeypatch):
    assert gate.dataset_sync_interval_sec() == 86400
    monkeypatch.setenv(gate.DATASET_INTERVAL_ENV, "60")
    assert gate.dataset_sync_interval_sec() == 600
    monkeypatch.setenv(gate.DATASET_INTERVAL_ENV, "3600")
    assert gate.dataset_sync_interval_sec() == 3600
    monkeypatch.setenv(gate.DATASET_INTERVAL_ENV, "not-a-number")
    assert gate.dataset_sync_interval_sec() == 86400
