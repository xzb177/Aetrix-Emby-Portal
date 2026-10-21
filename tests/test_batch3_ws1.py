"""WS1（系统清点 Batch 3 + 残留两处计数）回归测试。

- item1：compose 不再硬编码三键 env（面板接管）
- item2：B3 绑定门条件放宽（_tmdb_text_needs_fill）
- item3：T1 failed 回捞 + enrich_last_error 落库
- item4/5：显式可见口径（deleted_at + merged_into_id 双过滤）

全部在隔离临时 SQLite 里跑，不连生产库；不测真实 TMDB 网络。
"""
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmppath}"
from backend import database as _dbmod
from sqlalchemy import create_engine as _ce
from sqlalchemy.orm import sessionmaker as _sm

_dbmod.engine = _ce(os.environ["DATABASE_URL"])
# 必须走 configure_session_local：直接赋值会把 SessionLocal 代理顶掉
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

import uuid

import pytest

from backend import models as core_models
from backend.api.admin_scrape import _tmdb_text_needs_fill
from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import enrich_worker
from backend.emby_server import soft_delete

init_db()
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        s.query(em.MediaStream).delete()
        s.query(em.MediaItem).delete()
        s.query(core_models.SystemConfig).delete()
        s.commit()
        yield s
    finally:
        s.close()


def _make_item(db, **kw):
    it = em.MediaItem(
        guid=uuid.uuid4().hex,
        library_id=kw.pop("library_id", 1),
        item_type=kw.pop("item_type", "movie"),
        name=kw.pop("name", "测试"),
        file_path=kw.pop("file_path", "/tmp/x.mp4"),
        enrich_status=kw.pop("enrich_status", "pending"),
        **kw,
    )
    db.add(it)
    db.commit()
    return it


# ================= item1：compose 不再硬编码三键 =================

def _active_env_lines(text):
    """只取真正生效的 environment 行（跳过注释）"""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


@pytest.mark.parametrize("compose", ["docker-compose.yml", "docker-compose.prod.yml"])
def test_compose_no_hardcoded_tuning_env(compose):
    """三键改由管理面板接管：compose 里不许再出现这三个 env（注释除外）"""
    text = (ROOT / compose).read_text(encoding="utf-8")
    for line in _active_env_lines(text):
        assert not line.startswith("ENRICH_WORKERS:"), f"{compose}: {line}"
        assert not line.startswith("ENRICH_BATCH:"), f"{compose}: {line}"
        assert not line.startswith("DB_POOL_SIZE:"), f"{compose}: {line}"


def test_compose_keeps_overflow_env():
    """DB_MAX_OVERFLOW 仍走 env（本次不动），别误删"""
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "DB_MAX_OVERFLOW:" in text


def test_compose_ws1_comment_present():
    """移除处留注释，说明三键去哪了"""
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "改由管理面板接管" in text


# ================= item2：B3 门条件 =================

def _full_item(**kw):
    base = dict(
        imdb_id="tt1234567",
        aliases="别名1,别名2",
        overview="简介",
        community_rating=8.5,
        genres="剧情",
        production_year=2024,
        premiere_date=datetime(2024, 1, 1),
        metadata_runtime_ticks=36000000000,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_gate_all_filled_no_fill():
    assert _tmdb_text_needs_fill(_full_item()) is False


@pytest.mark.parametrize("field", [
    "imdb_id", "aliases", "overview", "community_rating",
    "genres", "production_year", "premiere_date", "metadata_runtime_ticks",
])
def test_gate_any_field_missing_triggers(field):
    """任一应补字段缺失即调 apply_details"""
    assert _tmdb_text_needs_fill(_full_item(**{field: None})) is True


def test_gate_old_condition_would_miss():
    """旧门条件（只看 imdb+aliases）会漏掉简介缺失 —— 这正是 B3 的 bug"""
    item = _full_item(overview=None)
    assert bool(item.imdb_id and item.aliases) is True  # 旧门：不触发
    assert _tmdb_text_needs_fill(item) is True  # 新门：触发


# ================= item3：T1 failed 回捞 + last_error =================

def test_models_have_new_columns():
    assert hasattr(em.MediaItem, "enrich_last_error")
    assert hasattr(em.MediaItem, "enrich_failed_reschedules")


def test_migration_list_has_new_columns():
    import inspect as _inspect
    src = _inspect.getsource(_dbmod._auto_migrate)
    assert '("enrich_last_error", "VARCHAR(255)", "NULL")' in src
    assert '("enrich_failed_reschedules", "INTEGER", "0")' in src


def test_failed_tuning_specs_registered(db):
    specs = {k: (default, kind) for k, _, default, kind, _, _, _
             in enrich_worker._ENRICH_TUNING_SPECS}
    # 2026-10-21 A 类摘面板现值对齐：生产现值为开，spec 缺省由 "0" 翻 "1"
    assert specs[enrich_worker.CFG_ENRICH_FAILED_AUTO_RESCHEDULE] == ("1", "flag1")
    assert specs[enrich_worker.CFG_ENRICH_FAILED_RESCHEDULE_DAYS] == ("30", "int")
    assert specs[enrich_worker.CFG_ENRICH_FAILED_MAX_RESCHEDULES] == ("3", "int")
    cfg = enrich_worker.get_enrich_tuning_config(db)["values"]
    assert cfg[enrich_worker.CFG_ENRICH_FAILED_AUTO_RESCHEDULE] is True
    assert cfg[enrich_worker.CFG_ENRICH_FAILED_RESCHEDULE_DAYS] == 30
    assert cfg[enrich_worker.CFG_ENRICH_FAILED_MAX_RESCHEDULES] == 3


def test_failed_reschedule_switch_off_is_noop(db):
    """开关关掉后直接返回 0，不碰任何行。缺省是开，本例经保存路径显式关掉。"""
    enrich_worker.save_enrich_tuning_config(db, {
        enrich_worker.CFG_ENRICH_FAILED_AUTO_RESCHEDULE: False,
    })
    try:
        it = _make_item(db, enrich_status="failed", enrich_attempts=5)
        n = enrich_worker.enrich_failed_auto_reschedule(db)
        assert n == 0
        db.refresh(it)
        assert it.enrich_status == "failed"
    finally:
        from backend import models as _models
        from backend.integrations import store as _store
        db.query(_models.SystemConfig).filter(
            _models.SystemConfig.key == enrich_worker.CFG_ENRICH_FAILED_AUTO_RESCHEDULE
        ).delete(synchronize_session=False)
        db.commit()
        _store.invalidate(enrich_worker.CFG_ENRICH_FAILED_AUTO_RESCHEDULE)
        enrich_worker._enrich_tuning_cache["at"] = 0.0
        enrich_worker._enrich_tuning_cache["cfg"] = None


def _enable_failed_reschedule(db):
    enrich_worker.save_enrich_tuning_config(db, {
        enrich_worker.CFG_ENRICH_FAILED_AUTO_RESCHEDULE: "1",
        enrich_worker.CFG_ENRICH_FAILED_RESCHEDULE_DAYS: "30",
        enrich_worker.CFG_ENRICH_FAILED_MAX_RESCHEDULES: "3",
    })


def test_failed_reclaim_round_trip(db):
    """开开关后：旧 failed → pending；达上限/合并副本/done 行不动；last_error 保留"""
    _enable_failed_reschedule(db)
    old = _make_item(db, enrich_status="failed", enrich_attempts=5,
                     enrich_next_retry_at=None, enrich_failed_reschedules=0,
                     enrich_last_error="boom")
    capped = _make_item(db, enrich_status="failed", enrich_attempts=5,
                        enrich_next_retry_at=None, enrich_failed_reschedules=3)
    merged = _make_item(db, enrich_status="failed", enrich_attempts=5,
                        enrich_next_retry_at=None, enrich_failed_reschedules=0,
                        merged_into_id=999)
    done = _make_item(db, enrich_status="done", enrich_attempts=0)

    n = enrich_worker.enrich_failed_auto_reschedule(db)
    assert n == 1

    db.refresh(old)
    assert old.enrich_status == "pending"
    assert old.enrich_attempts == 0
    assert old.enrich_failed_reschedules == 1
    assert old.enrich_next_retry_at is not None
    assert old.enrich_last_error == "boom"  # 保留不清，供排障

    for it, want in ((capped, "failed"), (merged, "failed"), (done, "done")):
        db.refresh(it)
        assert it.enrich_status == want, it.id


def test_failed_reclaim_batch_cap(db):
    """单轮硬上限 2000：超量时捞满即停"""
    _enable_failed_reschedule(db)
    for _ in range(5):
        _make_item(db, enrich_status="failed", enrich_attempts=5,
                   enrich_next_retry_at=None, enrich_failed_reschedules=0)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(enrich_worker, "ENRICH_FAILED_AUTO_RESCHEDULE_BATCH", 3)
        n = enrich_worker.enrich_failed_auto_reschedule(db)
    assert n == 3
    left = db.query(em.MediaItem).filter(
        em.MediaItem.enrich_status == "failed").count()
    assert left == 2


def test_mark_failed_writes_last_error(db):
    """_mark_failed 超限转 failed 时错误原文落库（截断 250）"""
    it = _make_item(db, enrich_status="enriching", enrich_attempts=4)
    err = "x" * 500
    out = enrich_worker._mark_failed(db, it.id, 4, err, claim_token=None)
    assert out == "failed"
    db.refresh(it)
    assert it.enrich_status == "failed"
    assert it.enrich_last_error == "x" * 250


def test_mark_failed_retry_path_keeps_pending(db):
    """未超限仍是 pending + 退避，不写 last_error"""
    it = _make_item(db, enrich_status="enriching", enrich_attempts=1)
    out = enrich_worker._mark_failed(db, it.id, 1, "boom", claim_token=None)
    assert out == "retry"
    db.refresh(it)
    assert it.enrich_status == "pending"
    assert it.enrich_last_error is None


# ================= item4/5：显式可见口径 =================

def test_count_visible_explicit_double_filter(db):
    """portal/scanner 两处调用点的口径：deleted_at + merged_into_id 双显式过滤"""
    _make_item(db, item_type="movie", name="可见")
    _make_item(db, item_type="movie", name="合并副本", merged_into_id=1)
    _make_item(db, item_type="movie", name="软删",
               deleted_at=datetime.now() - timedelta(days=1))
    _make_item(db, item_type="episode", name="单集不计入")
    n = soft_delete.count_visible(
        db,
        em.MediaItem.library_id == 1,
        em.MediaItem.item_type.in_(["movie", "series"]),
        em.MediaItem.deleted_at.is_(None),
        em.MediaItem.merged_into_id.is_(None),
    )
    assert n == 1
