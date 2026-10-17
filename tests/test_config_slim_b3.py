# -*- coding: utf-8 -*-
"""配置瘦身第 3 批：4 个补入口键的后端语义与新/改端点。

- altmeta 两个限速键：GET/PUT /scrape/altmeta-rates 读写一致、0=不限速、钳制 [0, 60]
- strm_gen 两个键：jitter_ratio 解析钳制 [0, 0.5]、subtree_listing 开关默认开；
  StrmGenConfigSave 接受新字段；_read_config 含新键。
全部离线：store 用内存桩，不做真实网络与数据库请求。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from types import SimpleNamespace

import pytest

from backend.emby_server import altmeta as _am
from backend.emby_server import strm_gen as _gen
from backend.integrations import store as _store


class _MemStore:
    """store.get_value / write_values 的内存桩"""

    def __init__(self):
        self.rows = {}

    def get_value(self, db, key, default=""):
        return self.rows.get(key, default)

    def write_values(self, db, values, descriptions=None):
        self.rows.update(values)


@pytest.fixture()
def memstore(monkeypatch):
    ms = _MemStore()
    monkeypatch.setattr(_store, "get_value", ms.get_value)
    monkeypatch.setattr(_store, "write_values", ms.write_values)
    return ms


def _staff():
    return SimpleNamespace(id=1, is_superuser=True, username="admin")


def _db():
    db = SimpleNamespace()
    db.committed = 0

    def _commit():
        db.committed += 1

    db.commit = _commit
    return db


# ---------- altmeta 限速端点 ----------

def test_altmeta_rates_defaults(memstore):
    from backend.api import admin_scrape
    got = admin_scrape.get_altmeta_rates(staff=_staff(), db=_db())
    assert got == {"douban_rate": 1.0, "bangumi_rate": 1.0}


def test_altmeta_rates_roundtrip(memstore):
    from backend.api import admin_scrape
    body = admin_scrape._AltmetaRatesBody(douban_rate=2.5, bangumi_rate=0)
    out = admin_scrape.save_altmeta_rates(body=body, staff=_staff(), db=_db())
    assert out["success"] is True
    assert out["douban_rate"] == 2.5 and out["bangumi_rate"] == 0
    # 落的是 altmeta 的两个键，且 0=不限速能读回
    assert memstore.rows[_am.CONFIG_RATE] == "2.5"
    assert memstore.rows[_am.CONFIG_BANGUMI_RATE] == "0.0"  # str(float(0))，读回 float 一致即可
    got = admin_scrape.get_altmeta_rates(staff=_staff(), db=_db())
    assert got == {"douban_rate": 2.5, "bangumi_rate": 0}


def test_altmeta_rates_clamped(memstore):
    from backend.api import admin_scrape
    body = admin_scrape._AltmetaRatesBody(douban_rate=-3, bangumi_rate=999)
    out = admin_scrape.save_altmeta_rates(body=body, staff=_staff(), db=_db())
    assert out["douban_rate"] == 0
    assert out["bangumi_rate"] == 60


# ---------- strm_gen jitter / subtree ----------

def test_jitter_ratio_default_and_clamp(memstore):
    assert _gen.jitter_ratio(object()) == 0.2
    memstore.rows[_gen.CONFIG_JITTER_RATIO] = "0.35"
    assert _gen.jitter_ratio(object()) == 0.35
    memstore.rows[_gen.CONFIG_JITTER_RATIO] = "0.9"
    assert _gen.jitter_ratio(object()) == 0.5
    memstore.rows[_gen.CONFIG_JITTER_RATIO] = "-1"
    assert _gen.jitter_ratio(object()) == 0.0
    memstore.rows[_gen.CONFIG_JITTER_RATIO] = "0"
    assert _gen.jitter_ratio(object()) == 0.0
    memstore.rows[_gen.CONFIG_JITTER_RATIO] = "不是数字"
    assert _gen.jitter_ratio(object()) == 0.2


def test_subtree_listing_default_and_toggle(memstore):
    assert _gen.subtree_listing_enabled(object()) is True
    memstore.rows[_gen.CONFIG_SUBTREE_LISTING] = "0"
    assert _gen.subtree_listing_enabled(object()) is False
    memstore.rows[_gen.CONFIG_SUBTREE_LISTING] = "false"
    assert _gen.subtree_listing_enabled(object()) is False
    memstore.rows[_gen.CONFIG_SUBTREE_LISTING] = "1"
    assert _gen.subtree_listing_enabled(object()) is True


def test_strm_gen_save_schema_accepts_new_fields():
    from backend.api.admin_strm_gen import StrmGenConfigSave
    body = StrmGenConfigSave(jitter_ratio=0.3, subtree_listing=False)
    assert body.jitter_ratio == 0.3
    assert body.subtree_listing is False
    # 不传=不改
    body2 = StrmGenConfigSave()
    assert body2.jitter_ratio is None
    assert body2.subtree_listing is None


def test_strm_gen_jitter_validation_range():
    """端点内的校验口径：[0, 0.5]，与 strm_gen.jitter_ratio 钳制一致"""
    from backend.api import admin_strm_gen
    src = open(admin_strm_gen.__file__, encoding="utf-8").read()
    assert "CONFIG_JITTER_RATIO" in src
    assert "CONFIG_SUBTREE_LISTING" in src
    assert '"jitter_ratio": _gen.jitter_ratio(db)' in src
    assert '"subtree_listing": _gen.subtree_listing_enabled(db)' in src
    assert "0.5" in src  # 校验上界
