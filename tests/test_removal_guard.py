"""删除保护：挂载异常时不得批量删除媒体记录（§四 / §十四 验收 5、6）

事故形状：**挂载断开 → 底层空目录还在 → 本轮扫描看到 0 个文件 → 清理阶段把整库删了**。

``failed_roots`` 挡不住它——目录还在，只是空的，所以 ``os.path.isdir`` 与列目录都“正常”。
这里钉住补上的两道闸：

1. **零结果**：一个文件都没看到而库里有条目 → 一条都不删
2. **数量阈值**：预计要删的量超过「比例 / 绝对数」任一上限 → 一条都不删

以及「不该拦的时候别拦」：正常删几条（删了一部下架的电影）仍然要真删，否则这个功能
就变成了“清理永远不生效”。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.emby_server import models as em
from backend.emby_server import scanner
from backend.emby_server import soft_delete
from backend.integrations import store


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    yield session
    store.invalidate()
    session.close()


def _lib(db, paths=""):
    row = em.Library(guid="g1", name="电影库", collection_type="movies",
                     paths=paths, storage_backends="")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _item(db, lib, guid, item_type="movie", path=None):
    row = em.MediaItem(guid=guid, library_id=lib.id, item_type=item_type,
                       name=guid, file_path=path)
    db.add(row)
    db.commit()
    return row


# ==================== 零结果：事故主体 ====================

def test_zero_files_seen_blocks_all_removal(db, tmp_path):
    """挂载断了、目录还在但空的 → 本轮看到 0 个文件 → 库里 30 条全部保住"""
    lib = _lib(db, str(tmp_path))
    for i in range(30):
        _item(db, lib, f"m{i:02d}")

    removed = scanner._remove_missing_items(db, lib, set())

    assert removed == 0
    left = db.execute(text(
        "SELECT COUNT(*) FROM emby_items WHERE library_id = :l"), {"l": lib.id}).scalar()
    assert left == 30, "零结果时不允许删任何条目"
    assert scanner._CLEANUP_LAST["skipped"], "必须留下拦截原因供排障"


def test_zero_seen_blocks_even_a_tiny_library(db, tmp_path):
    """小库也要拦：库里就 1 条、这轮看到 0 个 —— 那一条也不能删"""
    lib = _lib(db, str(tmp_path))
    _item(db, lib, "only")

    assert scanner._remove_missing_items(db, lib, set()) == 0
    left = db.execute(text(
        "SELECT COUNT(*) FROM emby_items WHERE library_id = :l"), {"l": lib.id}).scalar()
    assert left == 1


# ==================== 数量阈值 ====================

def test_mass_loss_beyond_ratio_is_blocked(db, tmp_path):
    """看到 0 条不行；看到 5 条、库里有 100 条（要删 95 条）也不行"""
    lib = _lib(db, str(tmp_path))
    for i in range(100):
        _item(db, lib, f"m{i:03d}")

    seen = {f"m{i:03d}" for i in range(5)}
    removed = scanner._remove_missing_items(db, lib, seen)

    assert removed == 0
    left = db.execute(text(
        "SELECT COUNT(*) FROM emby_items WHERE library_id = :l"), {"l": lib.id}).scalar()
    assert left == 100
    assert "阈值" in scanner._CLEANUP_LAST["skipped"]


def test_absolute_floor_protects_small_libraries(db, tmp_path, monkeypatch):
    """库里就 5 条、看到 1 条（要删 4 条）：比例放到最松也拦得住

    单独把绝对下限调到 3（默认 20 是给大库用的），验证两道闸是**取大**而不是二选一。
    """
    monkeypatch.setattr(scanner, "REMOVAL_MAX_RATIO", 0.99)   # 比例放到最松
    monkeypatch.setattr(scanner, "REMOVAL_MAX_ABSOLUTE", 3)   # 绝对下限收紧
    lib = _lib(db, str(tmp_path))
    for i in range(5):
        _item(db, lib, f"m{i}")

    removed = scanner._remove_missing_items(db, lib, {"m0"})

    assert removed == 0
    left = db.execute(text(
        "SELECT COUNT(*) FROM emby_items WHERE library_id = :l"), {"l": lib.id}).scalar()
    assert left == 5


def test_absolute_floor_defaults_are_safe_for_small_libraries(db, tmp_path, monkeypatch):
    """默认参数下，小库删一大半确实会被比例阈拦住（不靠绝对值）"""
    # 缺失宽限（批一 #6）默认 3 轮才下架；本文件测的是删除保护本身，
    # 把宽限压到 1 轮隔离变量（宽限语义在 test_missing_grace.py 里钉）
    monkeypatch.setattr(scanner, "MISSING_GRACE_SCANS", 1)
    lib = _lib(db, str(tmp_path))
    for i in range(10):
        _item(db, lib, f"m{i}")

    # 看到 4 条 → 要删 6 条；比例上限 max(20, int(10*0.5)=5) = 20 → 6 < 20 不拦
    # （默认绝对值 20 是给“一次性下架很多片”的正常场景留的口子）
    removed = scanner._remove_missing_items(db, lib, {f"m{i}" for i in range(4)})
    assert removed == 6, "默认参数下这种小规模清理应当正常执行"


# ==================== 不能“一刀切地不删” ====================

def test_normal_small_cleanup_still_deletes(db, tmp_path, monkeypatch):
    """正常场景要真删：100 条里删 1 条（一部片下架了），必须下架它

    没有这一条的话，前面的保护会把清理功能整个废掉。

    v2.48.0 起「删掉」默认是软删除：行还在、只是对所有人隐藏（见 test_soft_delete.py）。
    这里同时钉住两条：默认隐藏、``MEDIA_SOFT_DELETE=0`` 时真的物理删除。
    """
    # 同上：隔离缺失宽限变量（批一 #6），单轮下架口径测删除主体
    monkeypatch.setattr(scanner, "MISSING_GRACE_SCANS", 1)
    lib = _lib(db, str(tmp_path))
    gone = _item(db, lib, "gone")
    for i in range(100):
        _item(db, lib, f"m{i:03d}")
    seen = {f"m{i:03d}" for i in range(100)}

    removed = scanner._remove_missing_items(db, lib, seen)

    assert removed == 1
    assert db.query(em.MediaItem).filter(em.MediaItem.guid == "gone").first() is None
    with soft_delete.include_deleted():
        hidden = db.query(em.MediaItem).filter(em.MediaItem.guid == "gone").one()
    assert hidden.deleted_at is not None, "默认是软删除：行保留、标记下架"
    assert not scanner._CLEANUP_LAST.get("skipped")


def test_normal_small_cleanup_physically_deletes_when_soft_delete_is_off(db, tmp_path,
                                                                        monkeypatch):
    """关掉软删除就是回到硬删：那条目**真的**没了（不是标记）"""
    monkeypatch.setattr(scanner, "MISSING_GRACE_SCANS", 1)
    monkeypatch.setenv("MEDIA_SOFT_DELETE", "0")
    lib = _lib(db, str(tmp_path))
    gone = _item(db, lib, "gone")
    for i in range(100):
        _item(db, lib, f"m{i:03d}")
    seen = {f"m{i:03d}" for i in range(100)}

    removed = scanner._remove_missing_items(db, lib, seen)

    assert removed == 1
    with soft_delete.include_deleted():
        assert db.query(em.MediaItem).filter(em.MediaItem.guid == "gone").first() is None


def test_empty_library_is_not_blocked(db, tmp_path):
    """库里本来就没有文件类条目 → 预算函数直接放行，不该报错也不该拦"""
    lib = _lib(db, str(tmp_path))
    allowed, why, total = scanner._removal_budget(db, lib, set())
    assert allowed is True and total == 0 and why == ""


# ==================== 阈值可通过环境变量调 ====================

def test_thresholds_are_env_tunable(db, tmp_path, monkeypatch):
    """运维要能在不改代码的前提下放宽/收紧阈值"""
    monkeypatch.setenv("SCAN_REMOVAL_MAX_ABSOLUTE", "2")
    assert max(1, int(os.environ["SCAN_REMOVAL_MAX_ABSOLUTE"])) == 2
    # 非法值不能把功能打挂：回落到默认
    monkeypatch.setenv("SCAN_REMOVAL_MAX_RATIO", "")
    assert float(os.environ["SCAN_REMOVAL_MAX_RATIO"] or 0.5) == 0.5