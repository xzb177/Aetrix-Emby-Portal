"""清单可信度守卫（FMBY 扫描域，清单第 12 项）。

口径钉死：
- 某目录直系文件类条目本轮确认缺失（未见 + 其他来源也读不到）比例
  严格大于阈值、且目录规模达到下限时，先怀疑上游抖动：本轮不删该
  目录条目、不计缺失轮次，只记警告；
- 小规模正常缺失仍走缺失宽限（默认 3 轮），顺序与口径不动；
- 守卫关闭时行为与现行逐字一致；
- 无路径行不属于任何目录，不进守卫统计（硬归一处会误拦）；
- 阈值 0.5（沿用整库预算先例）与最小规模 10 均为我方起步值，
  非 FMBY 实测常量。

测试用 mount://9/... 路径：9 号挂载不存在，media_exists 恒为 False，
即“确认缺失”无需 mock；已见行用 seen_guids 传入。
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


def _lib(db, guid="g-cred"):
    row = em.Library(guid=guid, name="电影库", collection_type="movies",
                     paths="", storage_backends="")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _item(db, lib, guid, path):
    row = em.MediaItem(guid=guid, library_id=lib.id, item_type="movie",
                       name=guid, file_path=path)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _seed_dir(db, lib, dirname, n, prefix):
    """往一个目录里放 n 条，返回 guid 列表（顺序即 id 顺序）。"""
    guids = []
    for i in range(n):
        row = _item(db, lib, f"{prefix}{i:02d}", f"{dirname}/f{i:02d}.mkv")
        guids.append(row.guid)
        db.expunge(row)
    return guids


def _visible_count(db, lib):
    return db.execute(text(
        "SELECT COUNT(*) FROM emby_items WHERE library_id = :l "
        "AND deleted_at IS NULL"), {"l": lib.id}).scalar()


def _missing_counts(db, lib):
    return {r[0]: r[1] for r in db.execute(text(
        "SELECT guid, missing_count FROM emby_items "
        "WHERE library_id = :l AND deleted_at IS NULL"), {"l": lib.id}).all()}


def test_mass_disappearance_triggers_guard_zero_deletion(db, monkeypatch):
    """20 条里 12 条凭空消失（0.6 > 0.5）：零删除、零累计、只记警告"""
    monkeypatch.setattr(scanner, "MISSING_GRACE_SCANS", 1)  # 隔离宽限：没守卫就当轮删
    lib = _lib(db)
    guids = _seed_dir(db, lib, "mount://9/D", 20, "m")
    seen = set(guids[12:])  # 前 12 条消失

    removed = scanner._remove_missing_items(db, lib, seen)

    assert removed == 0
    assert _visible_count(db, lib) == 20
    assert all(v == 0 for v in _missing_counts(db, lib).values()), "不计入缺失轮次"
    assert scanner._CLEANUP_LAST["credibility_guarded"] == ["mount://9/D"]


def test_guard_off_mass_disappearance_deletes(db, monkeypatch):
    """守卫关闭时行为与现行逐字一致：同样的 12 条当轮下架"""
    monkeypatch.setattr(scanner, "MISSING_GRACE_SCANS", 1)
    monkeypatch.setattr(scanner, "CREDIBILITY_GUARD_ENABLED", 0)
    lib = _lib(db)
    guids = _seed_dir(db, lib, "mount://9/D", 20, "m")
    seen = set(guids[12:])

    removed = scanner._remove_missing_items(db, lib, seen)

    assert removed == 12
    assert _visible_count(db, lib) == 8
    assert scanner._CLEANUP_LAST["credibility_guarded"] == []


def test_small_loss_goes_through_grace(db):
    """小规模缺失（4/20 = 0.2）不触发守卫，仍走宽限累计"""
    lib = _lib(db)
    guids = _seed_dir(db, lib, "mount://9/D", 20, "m")
    seen = set(guids[4:])

    assert scanner._remove_missing_items(db, lib, seen) == 0
    counts = _missing_counts(db, lib)
    assert [counts[g] for g in guids[:4]] == [1, 1, 1, 1]
    assert scanner._CLEANUP_LAST["credibility_guarded"] == []

    assert scanner._remove_missing_items(db, lib, seen) == 0
    counts = _missing_counts(db, lib)
    assert [counts[g] for g in guids[:4]] == [2, 2, 2, 2], "宽限继续累计"


def test_threshold_boundary(db, monkeypatch):
    """边界：恰好 0.5 不拦（严格大于才拦），0.55 拦"""
    monkeypatch.setattr(scanner, "MISSING_GRACE_SCANS", 1)
    lib = _lib(db)
    guids = _seed_dir(db, lib, "mount://9/D", 20, "m")
    seen = set(guids[10:])  # 恰好 10/20 = 0.5

    assert scanner._remove_missing_items(db, lib, seen) == 10
    assert scanner._CLEANUP_LAST["credibility_guarded"] == []

    lib2 = _lib(db, guid="g-cred-2")
    guids2 = _seed_dir(db, lib2, "mount://9/E", 20, "n")
    seen2 = set(guids2[11:])  # 11/20 = 0.55

    assert scanner._remove_missing_items(db, lib2, seen2) == 0
    assert scanner._CLEANUP_LAST["credibility_guarded"] == ["mount://9/E"]


def test_small_dir_never_guarded(db, monkeypatch):
    """小目录（9 条 < 下限 10）即使 8 条消失也不拦，走正常清理"""
    monkeypatch.setattr(scanner, "MISSING_GRACE_SCANS", 1)
    lib = _lib(db)
    guids = _seed_dir(db, lib, "mount://9/S", 9, "s")
    seen = {guids[0]}

    assert scanner._remove_missing_items(db, lib, seen) == 8
    assert scanner._CLEANUP_LAST["credibility_guarded"] == []


def test_pathless_rows_excluded(db, monkeypatch):
    """无路径行不属于任何目录：20 条里 12 条消失也不触发守卫"""
    monkeypatch.setattr(scanner, "MISSING_GRACE_SCANS", 1)
    lib = _lib(db)
    guids = []
    for i in range(20):
        row = _item(db, lib, f"p{i:02d}", None)
        guids.append(row.guid)
        db.expunge(row)
    seen = set(guids[12:])

    assert scanner._remove_missing_items(db, lib, seen) == 12
    assert scanner._CLEANUP_LAST["credibility_guarded"] == []


def test_guarded_dir_seen_rows_still_reset(db, monkeypatch):
    """被拦目录里本轮真实列到的行照常清零；未见行既不累计也不删"""
    monkeypatch.setattr(scanner, "MISSING_GRACE_SCANS", 1)
    lib = _lib(db)
    guids = _seed_dir(db, lib, "mount://9/D", 20, "m")
    db.query(em.MediaItem).filter(
        em.MediaItem.library_id == lib.id,
        em.MediaItem.guid.in_(guids[12:]),
    ).update({"missing_count": 1}, synchronize_session=False)
    db.commit()
    seen = set(guids[12:])  # 后 8 条列到了，前 12 条消失（0.6 > 0.5）

    assert scanner._remove_missing_items(db, lib, seen) == 0
    counts = _missing_counts(db, lib)
    assert [counts[g] for g in guids[12:]] == [0] * 8, "已见行清零"
    assert [counts[g] for g in guids[:12]] == [0] * 12, "未见行不累计"


def test_guard_scopes_to_one_directory(db, monkeypatch):
    """只拦超阈目录：A 目录 12/20 消失被拦，B 目录 2/20 正常清理"""
    monkeypatch.setattr(scanner, "MISSING_GRACE_SCANS", 1)
    lib = _lib(db)
    guids_a = _seed_dir(db, lib, "mount://9/A", 20, "a")
    guids_b = _seed_dir(db, lib, "mount://9/B", 20, "b")
    seen = set(guids_a[12:]) | set(guids_b[2:])

    assert scanner._remove_missing_items(db, lib, seen) == 2
    counts = _missing_counts(db, lib)
    assert all(counts[g] == 0 for g in guids_a), "A 目录零变更"
    assert scanner._CLEANUP_LAST["credibility_guarded"] == ["mount://9/A"]
