"""r48 遗留 ⑨：成就定义管理端点 + pet stages 校验收紧。"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.api import panel as panel_api
from backend.panel_achievements import seed_achievement_defs


@pytest.fixture
def db():
    # 内存库：不依赖 /tmp（512M tmpfs，全量跑时会被其他测试的 sqlite 文件塞满）
    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine)
    s = Session()
    try:
        seed_achievement_defs(s)
        yield s
    finally:
        s.close()


_ADMIN = SimpleNamespace(id=1)


def test_achievements_get_returns_all_sorted(db):
    out = panel_api.admin_achievements_get(_ADMIN, db)
    keys = [a["key"] for a in out["achievements"]]
    assert len(keys) == 13
    sorts = [a["sort"] for a in out["achievements"]]
    assert sorts == sorted(sorts)
    assert keys[0] == "first_night"


def test_achievements_put_updates_display_fields_only(db):
    body = panel_api.AchievementDefIn(
        name="回头客", desc="隔月再看", icon="🌀", sort=5, enabled=False)
    out = panel_api.admin_achievements_put("revisit", body, _ADMIN, db)
    assert out["achievement"]["key"] == "revisit"
    assert out["achievement"]["name"] == "回头客"
    assert out["achievement"]["enabled"] is False

    row = db.query(models.PanelAchievementDef).filter_by(key="revisit").one()
    assert row.name == "回头客" and row.enabled is False


def test_achievements_put_unknown_key_404(db):
    body = panel_api.AchievementDefIn(
        name="x", desc="x", icon="🏅", sort=0, enabled=True)
    with pytest.raises(HTTPException) as exc_info:
        panel_api.admin_achievements_put("no_such_key", body, _ADMIN, db)
    assert exc_info.value.status_code == 404


def test_achievements_put_rejects_empty_name():
    with pytest.raises(ValidationError):
        panel_api.AchievementDefIn(name="", desc="x", icon="🏅", sort=0, enabled=True)


def test_pet_stages_rejects_nonzero_first_stage():
    with pytest.raises(HTTPException) as exc_info:
        panel_api._validate_pet_stages([
            {"name": "蛋", "icon": "🥚", "hours": 10},
            {"name": "鸟", "icon": "🦉", "hours": 50},
        ])
    assert exc_info.value.status_code == 400
    assert "stages[0].hours" in exc_info.value.detail


def test_pet_stages_rejects_decreasing_hours():
    with pytest.raises(HTTPException) as exc_info:
        panel_api._validate_pet_stages([
            {"name": "蛋", "icon": "🥚", "hours": 0},
            {"name": "鸟", "icon": "🦉", "hours": 50},
            {"name": "龙", "icon": "🐉", "hours": 40},
        ])
    assert exc_info.value.status_code == 400


def test_pet_stages_accepts_valid():
    stages = panel_api._validate_pet_stages([
        {"name": "蛋", "icon": "🥚", "hours": 0},
        {"name": "鸟", "icon": "🦉", "hours": 50},
        {"name": "龙", "icon": "🐉", "hours": 50},
    ])
    assert [s["hours"] for s in stages] == [0, 50, 50]
