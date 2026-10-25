"""可见性治理流水线 L7 单测。

硬约束（缺一不可）：
① 开关关时行为与现行逐字一致；
② 开关开时露出结果与案 A 完全一致，只补审计链——复核动作不改变露出。
另证：事件/决策表只追加（模块源码无 update/delete 调用、事件模型无
updated_at 列）；复核队列准入/驳回留痕；决策行即使被手工写反也不影响露出。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pathlib
import uuid
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models  # noqa: F401  注册全部表
from backend.database import Base
from backend.emby_server import identity_gate
from backend.emby_server import models as em
from backend.emby_server import visibility_governance as vg


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/vg.db")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    try:
        yield s
    finally:
        s.close()


def _mk(db, lib, item_type, name, **kw):
    it = em.MediaItem(
        guid=uuid.uuid4().hex, library_id=lib.id, item_type=item_type,
        name=name, sort_name=name, file_path=f"/m/{uuid.uuid4().hex}.mkv",
        **kw)
    db.add(it)
    db.flush()
    return it


@pytest.fixture()
def scene(db):
    lib = em.Library(guid=uuid.uuid4().hex, name="库",
                     collection_type="mixed", is_enabled=True)
    db.add(lib)
    db.flush()
    s = {}
    s["bound"] = _mk(db, lib, "movie", "已绑定", tmdb_id="123")
    s["unbound"] = _mk(db, lib, "movie", "未绑定")
    s["douban_only"] = _mk(db, lib, "movie", "只有豆瓣",
                           external_ids='{"douban": "1234567"}')
    series = _mk(db, lib, "series", "已绑定剧", tmdb_id="999")
    s["ep_follow"] = _mk(db, lib, "episode", "集跟随已绑定剧",
                         series_id=series.id, season_number=1,
                         episode_number=1)
    s["other"] = _mk(db, lib, "person", "人物不在治理内")
    return s


def test_switch_off_matches_current_behavior(db, scene, monkeypatch):
    """硬约束①：开关关时 = 现行口径（IDENTITY_SHELF_GATE 关全露、开则案 A）。"""
    monkeypatch.setattr(vg, "VISIBILITY_GOVERNANCE_ENABLED", 0)
    monkeypatch.setattr(identity_gate, "IDENTITY_SHELF_GATE", 0)
    assert vg.resolve_visibility(db, scene["unbound"], "shelf") is True
    monkeypatch.setattr(identity_gate, "IDENTITY_SHELF_GATE", 1)
    assert vg.resolve_visibility(db, scene["bound"], "shelf") is True
    assert vg.resolve_visibility(db, scene["unbound"], "shelf") is False
    assert vg.resolve_visibility(db, scene["douban_only"], "shelf") is False
    assert vg.resolve_visibility(db, scene["ep_follow"], "shelf") is True
    assert vg.resolve_visibility(db, scene["unbound"], "search") is True
    assert vg.resolve_visibility(db, scene["unbound"], "direct") is True


def test_switch_on_matches_case_a_exactly(db, scene, monkeypatch):
    """硬约束②：开关开时露出 = 案 A（与 IDENTITY_SHELF_GATE 本身状态无关）。"""
    monkeypatch.setattr(vg, "VISIBILITY_GOVERNANCE_ENABLED", 1)
    monkeypatch.setattr(identity_gate, "IDENTITY_SHELF_GATE", 0)
    assert vg.resolve_visibility(db, scene["bound"], "shelf") is True
    assert vg.resolve_visibility(db, scene["unbound"], "shelf") is False
    assert vg.resolve_visibility(db, scene["douban_only"], "shelf") is False
    assert vg.resolve_visibility(db, scene["ep_follow"], "shelf") is True
    assert vg.resolve_visibility(db, scene["other"], "shelf") is True
    assert vg.resolve_visibility(db, scene["unbound"], "search") is True
    assert vg.resolve_visibility(db, scene["unbound"], "direct") is True


def test_review_actions_do_not_change_exposure(db, scene, monkeypatch):
    """复核动作不改变露出：处置前后 resolve 恒等，且事件/决策只追加。"""
    monkeypatch.setattr(vg, "VISIBILITY_GOVERNANCE_ENABLED", 1)
    item = scene["unbound"]
    before = {s: vg.resolve_visibility(db, item, s)
              for s in ("shelf", "search", "direct")}
    task = vg.create_task(db, item, "shelf", reason="人工复核", actor=7)
    db.commit()
    assert task.status == "pending"
    vg.review_task(db, task.id, True, actor=7, note="准入留痕")
    db.commit()
    after = {s: vg.resolve_visibility(db, item, s)
             for s in ("shelf", "search", "direct")}
    assert after == before
    assert before["shelf"] is False  # 未绑定仍不露：准入没把露出改成 True
    rows, total = vg.list_events(db, item_id=item.id)
    assert total == 3  # task_created + task_approved + decision_recorded
    assert {r.event_type for r in rows} == {
        "task_created", "task_approved", "decision_recorded"}
    decisions = vg.list_decisions(db, item.id)
    assert len(decisions) == 1
    assert decisions[0].visible is False  # 审计快照 = 案 A 结果，不是开关
    assert decisions[0].rule_key == "case_a_unbound"
    # 重复处置被拒
    with pytest.raises(ValueError):
        vg.review_task(db, task.id, False, actor=7)


def test_decision_rows_never_drive_exposure(db, scene, monkeypatch):
    """决策行即使被手工写反（visible=True 给未绑定条目），露出仍是案 A。"""
    monkeypatch.setattr(vg, "VISIBILITY_GOVERNANCE_ENABLED", 1)
    item = scene["unbound"]
    db.add(em.ItemVisibilityDecision(item_id=item.id, surface="shelf",
                                     visible=True, rule_key="case_a_bound",
                                     task_id=None))
    db.commit()
    assert vg.resolve_visibility(db, item, "shelf") is False


def test_events_append_only_by_construction():
    """事件只追加不改：模块 AST 无 update/delete 调用；事件模型无 updated_at 列。"""
    import ast
    tree = ast.parse(pathlib.Path(vg.__file__).read_text(encoding="utf-8"))
    calls = [n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
    assert "update" not in calls
    assert "delete" not in calls
    assert "updated_at" not in {c.name for c in em.VisibilityEvent.__table__.columns}
    assert "updated_at" not in {c.name for c in em.ItemVisibilityDecision.__table__.columns}
    # 治理模块不给事件/决策表留更新/删除函数
    for name in dir(vg):
        assert not name.startswith("update_")
        assert not name.startswith("delete_")


def test_invalid_surface_rejected(db, scene):
    with pytest.raises(ValueError):
        vg.case_a_visible(db, scene["bound"], "nope")
    with pytest.raises(ValueError):
        vg.create_task(db, scene["bound"], "nope")
