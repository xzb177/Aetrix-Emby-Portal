# -*- coding: utf-8 -*-
"""发言奖励管理后台可自定义（feat/chat-points-admin-config）。

查后结论：points_chat_daily_cap 早已走标准口径（WELFARE_CONFIG_KEYS 默认 "20"、
WELFARE_CONFIG_SCHEMA ("int", 0, _BIG)、管理端 WelfarePoints.vue 已有字段）；
缺的只有 points_chat_per_msg 一键：此前只能改库，不能在后台改。
本文件锁定补齐后的标准口径：键注册＋校验＋计分语义未变。
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import config_schema, models
from backend.api import welfare_admin


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _set_config(db, key, value):
    cfg = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    if cfg:
        cfg.value = str(value)
    else:
        db.add(models.SystemConfig(key=key, value=str(value)))
    db.commit()


def _make_user(db, **kw):
    u = models.WebUser(username=kw.pop("username", "u1"), password_hash="x", **kw)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def test_per_msg_registered_with_default_1():
    assert welfare_admin.WELFARE_CONFIG_KEYS["points_chat_per_msg"] == "1"
    assert config_schema.WELFARE_CONFIG_SCHEMA["points_chat_per_msg"] == (
        "int",
        0,
        config_schema._BIG,
    )


def test_daily_cap_registration_untouched():
    assert welfare_admin.WELFARE_CONFIG_KEYS["points_chat_daily_cap"] == "20"
    assert config_schema.WELFARE_CONFIG_SCHEMA["points_chat_daily_cap"] == (
        "int",
        0,
        config_schema._BIG,
    )


def test_per_msg_normalize_valid_values():
    spec = config_schema.WELFARE_CONFIG_SCHEMA["points_chat_per_msg"]
    assert config_schema.normalize("points_chat_per_msg", spec, 2) == "2"
    assert config_schema.normalize("points_chat_per_msg", spec, "3") == "3"
    assert config_schema.normalize("points_chat_per_msg", spec, 0) == "0"
    # 空串/None = 未设置（删行，回退默认值），与 PUT 端点口径一致
    assert config_schema.normalize("points_chat_per_msg", spec, "") is None
    assert config_schema.normalize("points_chat_per_msg", spec, None) is None


def test_per_msg_normalize_rejects_invalid():
    spec = config_schema.WELFARE_CONFIG_SCHEMA["points_chat_per_msg"]
    for bad in (-1, "abc", 1.5, True):
        with pytest.raises(config_schema.ConfigValueError):
            config_schema.normalize("points_chat_per_msg", spec, bad)


def test_custom_per_msg_and_cap_take_effect(db):
    from backend import points

    u = _make_user(db, username="chatter", points=0)
    _set_config(db, "points_chat_per_msg", 2)
    _set_config(db, "points_chat_daily_cap", 5)
    assert points.award_chat_points(db, u.id) == 2
    assert points.award_chat_points(db, u.id) == 2
    # 已拿 4 分，剩 1 分额度：单次奖励不得超过上限
    assert points.award_chat_points(db, u.id) == 1
    assert points.award_chat_points(db, u.id) == 0
    db.refresh(u)
    assert u.points == 5


def test_per_msg_zero_disables_reward(db):
    from backend import points

    u = _make_user(db, username="chatter", points=0)
    _set_config(db, "points_chat_per_msg", 0)
    _set_config(db, "points_chat_daily_cap", 20)
    assert points.award_chat_points(db, u.id) == 0
    db.refresh(u)
    assert u.points == 0


def test_defaults_without_rows(db):
    """未配行时走默认 1／20（与生产现状一致）。"""
    from backend import points

    u = _make_user(db, username="chatter", points=0)
    assert points.award_chat_points(db, u.id) == 1
    db.refresh(u)
    assert u.points == 1
