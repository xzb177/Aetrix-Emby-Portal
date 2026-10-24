"""目录清单指纹 + 到期调度（FMBY 学习批二 #12）

钉住的行为：
- 清单指纹 v2：本机/挂载都是对清单内容的哈希——清单变（改名/增删/远端
  ModTime 变）指纹必变，清单没变指纹必稳；只用列举元数据，不打开文件。
- 到期判定：first / changed / due / not_due 四路；到期层关掉或定向扫描
  时不判定（核对深度回到旧行为）。
- 未到期整段跳过：重扫时未到期目录不产生任何逐文件往返（probe/side
  计数为 0），但 seen_guids 完整、条目一条不少（清理不会误删）。
- 到期才重验：next_due_at 过期后走完整核对，核对完排期推进。
- 指纹变了只重扫变化目录：其余目录仍按未到期跳过。
- 与缺失宽限（批一 #6）的顺序不许反：清单不可信（整库看不到文件）时
  清理被守卫整轮拦下（missing 计数都不许动）；守卫放行时计数照常累计。
"""
import os
import tempfile
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmppath}"
from backend import database as _dbmod
from sqlalchemy import create_engine as _ce
from sqlalchemy.orm import sessionmaker as _sm
_dbmod.engine = _ce(os.environ["DATABASE_URL"])
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

import uuid
from types import SimpleNamespace

import pytest

from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import scanner

init_db()


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def _guid():
    return uuid.uuid4().hex


def _ctx(snap=None, stats=None):
    if snap is None:
        snap = SimpleNamespace(collection_type="movies", scrape_policy="smart",
                               targeted=False, force_full=False)
    return scanner._ScanContext(snap=snap, lib_id=1, stats=stats if stats is not None else {})


# ==================== 清单指纹 v2 ====================

def test_local_listing_fingerprint_v2_stable_and_name_sensitive(tmp_path):
    d = tmp_path / "dir"
    d.mkdir()
    (d / "A (2020).mkv").write_bytes(b"x" * 10)
    (d / "B (2021).mkv").write_bytes(b"y" * 10)
    sf = scanner.ScanFile(stored_path=str(d / "A (2020).mkv"), name="A (2020).mkv",
                          local_dir=str(d), size=10)
    scanner.clear_dir_cache()
    fp1 = scanner._dir_fingerprint(_ctx(), sf)
    assert fp1 and fp1.startswith("v2:")
    scanner.clear_dir_cache()
    assert scanner._dir_fingerprint(_ctx(), sf) == fp1      # 清单没变 → 指纹必稳
    (d / "B (2021).mkv").rename(d / "B-renamed (2021).mkv")  # 改名：项数/总大小都不变
    scanner.clear_dir_cache()
    assert scanner._dir_fingerprint(_ctx(), sf) != fp1      # 只用旧启发式会漏掉


def test_mount_listing_fingerprint_v2_detects_mod_change(monkeypatch):
    from backend.emby_server.mounts import MountEntry
    entries = [MountEntry(name="a.mkv", rel="/a.mkv", is_dir=False, size=100, mod_ts=1000.0),
               MountEntry(name="b.mkv", rel="/b.mkv", is_dir=False, size=200, mod_ts=2000.0)]
    monkeypatch.setattr(scanner, "_mount_names", lambda ctx, sf: entries)
    sf = scanner.ScanFile(stored_path="mount://9/a.mkv", name="a.mkv", local_dir=None,
                          mount_id=9, dir_rel="/", size=100)
    fp1 = scanner._dir_fingerprint(_ctx(), sf)
    assert fp1 and fp1.startswith("v2:")
    assert scanner._dir_fingerprint(_ctx(), sf) == fp1
    entries[0].mod_ts = 9999.0                                # 同尺寸替换，只变 ModTime
    assert scanner._dir_fingerprint(_ctx(), sf) != fp1


# ==================== 到期判定 ====================

def test_dir_decision_matrix(monkeypatch):
    monkeypatch.setenv("SCAN_DIR_DUE_SCHEDULING", "1")
    future = datetime.now() + timedelta(hours=1)
    past = datetime.now() - timedelta(hours=1)
    assert scanner._dir_decision(_ctx(), "/d1", "v2:a", None) == "first"
    assert scanner._dir_decision(_ctx(), "/d2", "v2:b", ("v2:a", future)) == "changed"
    assert scanner._dir_decision(_ctx(), "/d3", "v2:a", ("v2:a", future)) == "not_due"
    assert scanner._dir_decision(_ctx(), "/d4", "v2:a", ("v2:a", past)) == "due"
    assert scanner._dir_decision(_ctx(), "/d5", "v2:a", ("v2:a", None)) == "due"
    assert scanner._dir_decision(_ctx(), "/d6", None, ("v2:a", future)) is None


def test_dir_decision_counted_once_per_dir(monkeypatch):
    monkeypatch.setenv("SCAN_DIR_DUE_SCHEDULING", "1")
    ctx = _ctx()
    future = datetime.now() + timedelta(hours=1)
    assert scanner._dir_decision(ctx, "/d", "v2:a", ("v2:a", future)) == "not_due"
    assert scanner._dir_decision(ctx, "/d", "v2:a", ("v2:a", future)) == "not_due"
    assert ctx.stats.get("dirs_not_due") == 1


def test_dir_decision_off_and_targeted(monkeypatch):
    future = datetime.now() + timedelta(hours=1)
    monkeypatch.setenv("SCAN_DIR_DUE_SCHEDULING", "0")
    assert scanner._dir_decision(_ctx(), "/d", "v2:a", ("v2:a", future)) is None
    monkeypatch.setenv("SCAN_DIR_DUE_SCHEDULING", "1")
    targeted_snap = SimpleNamespace(collection_type="movies", scrape_policy="smart",
                                    targeted=True, force_full=False)
    ctx = _ctx(snap=targeted_snap)
    # 定向扫描（Drive Changes）不参与到期调度：不判定、不排期
    assert scanner._dir_decision(ctx, "/d", "v2:a", ("v2:a", future)) is None
    assert ctx.stats == {}


# ==================== 未到期只推迟「到期才做」的判定 ====================

def test_can_skip_file_defer_scrape(monkeypatch):
    # configured 是 TmdbClient 的 property：换成恒 True 的 property 替身
    monkeypatch.setattr(scanner._tmdb.tmdb_client.__class__, "configured",
                        property(lambda self: True))
    item = SimpleNamespace(
        deleted_at=None, size=100, duration_ticks=60_000_000,
        last_probed_at=datetime.now(), probe_status="ok",
        repair_requested_at=None, tmdb_id="123", imdb_id="tt123",
        aliases="x", last_scraped_at=datetime.now(),
    )
    sf = scanner.ScanFile(stored_path="/v/a.mkv", name="a.mkv", local_dir="/v",
                          size=100, mtime_ns=1)
    pending = scanner._Pending(scan_file=sf, guid="g", parsed={}, item_type="movie")
    snap = SimpleNamespace(collection_type="movies", scrape_policy="all",
                           targeted=False, force_full=False)
    ctx = _ctx(snap=snap)
    # 策略 all = 每次都要重刮：不推迟时不能跳过
    assert scanner._can_skip_file(ctx, item, pending, "v2:a", "v2:a") is False
    # 未到期推迟到期重刮：其余安全检查都过 → 可以跳过
    assert scanner._can_skip_file(ctx, item, pending, "v2:a", "v2:a",
                                  defer_scrape=True) is True
    # 推迟不是无条件放行：大小变了（needs_probe）照样拦
    sf2 = scanner.ScanFile(stored_path="/v/a.mkv", name="a.mkv", local_dir="/v",
                            size=200, mtime_ns=1)
    pending2 = scanner._Pending(scan_file=sf2, guid="g", parsed={}, item_type="movie")
    assert scanner._can_skip_file(ctx, item, pending2, "v2:a", "v2:a",
                                  defer_scrape=True) is False


# ==================== 状态落库往返 ====================

def test_store_and_load_dir_states_with_schedule(db):
    lib = em.Library(guid=_guid(), name="t", collection_type="movies")
    db.add(lib)
    db.flush()
    ctx = _ctx()
    ctx.lib_id = lib.id
    due = datetime.now() + timedelta(days=1)
    ctx.dirty_dirs["/d1"] = "v2:aaa"
    ctx.dirty_dir_schedule["/d1"] = (due, "first")
    scanner._store_dir_states(db, ctx)
    db.commit()
    states = scanner._load_dir_states(db, lib.id, ["/d1"])
    assert states["/d1"] == ("v2:aaa", due)
    row = db.query(em.ScanDirState).filter_by(library_id=lib.id, dir_key="/d1").one()
    assert row.last_decision == "first"
    # 只推进排期（指纹没变）：走 schedule-only 写回
    ctx2 = _ctx()
    ctx2.lib_id = lib.id
    due2 = due + timedelta(days=1)
    ctx2.dirty_dir_schedule["/d1"] = (due2, "due")
    scanner._store_dir_states(db, ctx2)
    db.commit()
    states = scanner._load_dir_states(db, lib.id, ["/d1"])
    assert states["/d1"] == ("v2:aaa", due2)
    row = db.query(em.ScanDirState).filter_by(library_id=lib.id, dir_key="/d1").one()
    assert row.last_decision == "due" and row.fingerprint == "v2:aaa"


# ==================== 整库集成：到期生命周期 ====================

DIRS, PER_DIR = 3, 4
TOTAL = DIRS * PER_DIR


def _build_library(tmp_path):
    root = tmp_path / "lib"
    dirs = []
    for d in range(DIRS):
        sub = root / f"Folder {d}"
        sub.mkdir(parents=True)
        dirs.append(sub)
        for i in range(PER_DIR):
            (sub / f"Movie {d}-{i} (2020).mkv").write_bytes(b"\x00" * 512)
    return root, dirs


def _fake_probe(path, headers=None, size=0, *a, **k):
    try:
        real = os.path.getsize(path)
    except OSError:
        real = size
    return {"duration_ticks": 60_000_000, "bitrate": 1500, "width": 1920,
            "height": 1080, "video_codec": "h264", "audio_codec": "aac",
            "audio_languages": "chi", "subtitle_languages": "", "streams": [],
            "size": real}


@pytest.fixture()
def scan_env(db, monkeypatch):
    counters = {"probe": 0, "side": 0}
    real_side = scanner._side_info

    def counting_side(ctx, scan_file):
        counters["side"] += 1
        return real_side(ctx, scan_file)

    def counting_probe(*a, **k):
        counters["probe"] += 1
        return _fake_probe(*a, **k)

    monkeypatch.setattr(scanner, "_side_info", counting_side)
    monkeypatch.setattr(scanner, "probe_metadata", counting_probe)
    monkeypatch.setenv("SCAN_DIR_DUE_SCHEDULING", "1")
    # 根因隔离（批二 #12 真红，见 test_scan_due_lifecycle）：
    # TMDB 客户端是进程级单例，全量单进程跑时前面用例往单例里塞过 key
    # 就会让它保持 configured=True。到期重扫走完整 _can_skip_file（不带
    # defer），此时缺 tmdb_id 的条目恒被 should_scrape 拦住跳过，side 照
    # 常全量跑 12 次——而未到期路径因 defer_scrape 绕过刮削判定才过。
    # 这不是到期语义错（到期本就该重评刮削），是本用例的前提被进程污染：
    # 它断言的是「什么都不用做时零往返」，前提就是 TMDB 未配置。
    # 这里用 monkeypatch 把单例 key 清空（用例结束自动恢复原值），
    # 断言数值本身一字不动；口径与 test_tmdb_text_fill_only 的快照/恢复一致。
    from backend.emby_server import tmdb as _tmdb_mod
    monkeypatch.setattr(_tmdb_mod.tmdb_client, "api_keys", [])
    monkeypatch.setattr(_tmdb_mod.tmdb_client, "api_key", "")
    return counters


def _mk_lib(db, root):
    lib = em.Library(guid=_guid(), name="到期库", collection_type="movies",
                     paths=str(root))
    db.add(lib)
    db.commit()
    return lib


def _dir_rows(db, lib_id):
    return db.query(em.ScanDirState).filter_by(library_id=lib_id).all()


def test_scan_due_lifecycle(db, scan_env, tmp_path):
    root, dirs = _build_library(tmp_path)
    lib = _mk_lib(db, root)

    cold = scanner.scan_library_sync(db, lib)
    assert cold["added"] == TOTAL
    assert cold.get("dirs_new") == DIRS
    rows = _dir_rows(db, lib.id)
    assert len(rows) == DIRS
    assert all(r.next_due_at and r.next_due_at > datetime.now() for r in rows)
    assert all(r.last_decision == "first" for r in rows)
    assert all((r.fingerprint or "").startswith("v2:") for r in rows)

    # 未到期重扫：零逐文件往返，条目一条不少
    scan_env.update(probe=0, side=0)
    again = scanner.scan_library_sync(db, lib)
    assert scan_env["probe"] == 0 and scan_env["side"] == 0
    assert again.get("dirs_not_due") == DIRS
    assert again["removed"] == 0
    assert db.query(em.MediaItem).filter_by(library_id=lib.id).count() == TOTAL
    rows = _dir_rows(db, lib.id)
    assert all(r.last_decision == "not_due" for r in rows)

    # 全部过期 → 到期重验：仍零逐文件往返（什么都没变），排期重新推进
    for r in rows:
        r.next_due_at = datetime.now() - timedelta(minutes=1)
    db.commit()
    scan_env.update(probe=0, side=0)
    due = scanner.scan_library_sync(db, lib)
    assert due.get("dirs_due") == DIRS
    assert scan_env["probe"] == 0 and scan_env["side"] == 0
    assert due["removed"] == 0
    rows = _dir_rows(db, lib.id)
    assert all(r.next_due_at > datetime.now() for r in rows)
    assert all(r.last_decision == "due" for r in rows)

    # 只改一个目录（加一个文件）：只有它算 changed，其余仍未到期跳过
    (dirs[1] / "Movie New (2021).mkv").write_bytes(b"\x00" * 512)
    scan_env.update(probe=0, side=0)
    changed = scanner.scan_library_sync(db, lib)
    assert changed["added"] == 1
    assert changed.get("dirs_changed") == 1
    assert changed.get("dirs_not_due") == DIRS - 1
    # 逐文件往返只发生在变化目录：新文件探+side 各 1 次；该目录旧文件按
    # 既有口径整目录重做 side 检查（指纹变了），但不重探（大小没变）；
    # 其余目录零往返——总数正好等于变化目录的文件数。
    assert scan_env["probe"] == 1 and scan_env["side"] == PER_DIR + 1
    assert changed["removed"] == 0


def test_scan_untrusted_listing_aborts_cleanup(db, scan_env, tmp_path):
    """清单不可信（来源还在、但绝大多数文件凭空消失）：可信度守卫在前——
    清理整轮中止，missing 计数一个都不许动（顺序反了会先累计再拦，
    条目会被误伤）。库要够大到越过删除预算阈值（max(20, 总数×0.5)）。"""
    root = tmp_path / "biglib"
    files = []
    for d in range(3):
        sub = root / f"Folder {d}"
        sub.mkdir(parents=True)
        for i in range(20):
            f = sub / f"Movie {d}-{i} (2020).mkv"
            f.write_bytes(b"\x00" * 512)
            files.append(f)
    lib = _mk_lib(db, root)
    scanner.scan_library_sync(db, lib)
    for f in files[1:]:                       # 只留 1 个：seen=1，总数 60
        f.unlink()
    stats = scanner.scan_library_sync(db, lib)
    assert stats["removed"] == 0
    assert stats.get("removal_guard")
    items = db.query(em.MediaItem).filter_by(library_id=lib.id).all()
    assert len(items) == 60
    assert all(it.deleted_at is None for it in items)
    assert all((it.missing_count or 0) == 0 for it in items)


def test_scan_missing_grace_still_counts_after_guard(db, scan_env, tmp_path):
    """守卫放行（只少一个文件）：missing 计数照常累计（宽限，不是立即删）。"""
    root, dirs = _build_library(tmp_path)
    lib = _mk_lib(db, root)
    scanner.scan_library_sync(db, lib)
    victim = dirs[0] / "Movie 0-0 (2020).mkv"
    victim.unlink()
    stats = scanner.scan_library_sync(db, lib)
    assert stats["removed"] == 0
    assert stats.get("dirs_changed") == 1     # 删文件的目录指纹已变
    item = (db.query(em.MediaItem).filter_by(library_id=lib.id)
            .filter(em.MediaItem.file_path == str(victim)).one())
    assert item.deleted_at is None
    assert item.missing_count == 1 and item.missing_since is not None


def test_scan_rescrape_deferred_until_dir_due(db, scan_env, tmp_path, monkeypatch):
    """到期重刮的有界推迟（批二 #12 新口径，与 smoke 8b 同一场景）：
    目录未到期时，到期重刮（3m 策略、条目已过期 200 天）先推迟；
    目录一到期，重验走完整判定，重刮照常发生——推迟有界，不饿死。"""
    from backend.emby_server import tmdb as tmdb_mod

    class FakeTmdb:
        configured = True

        def search(self, *a, **k):
            return None

        def details(self, *a, **k):
            return None

        def enrich(self, *a, **k):
            return None

        def refresh_images(self, *a, **k):
            return False

        def apply(self, *a, **k):
            return None

    root, dirs = _build_library(tmp_path)
    lib = _mk_lib(db, root)
    scanner.scan_library_sync(db, lib)                    # 冷扫（TMDB 未配）
    lib.scrape_policy = "3m"
    aged = (db.query(em.MediaItem).filter_by(library_id=lib.id)
            .filter(em.MediaItem.file_path == str(dirs[0] / "Movie 0-0 (2020).mkv"))
            .one())
    aged.tmdb_id = "12345"
    aged.imdb_id = "tt123"
    aged.aliases = "别名"
    aged.last_scraped_at = datetime.now() - timedelta(days=200)
    db.commit()
    monkeypatch.setattr(tmdb_mod, "tmdb_client", FakeTmdb())

    scan_env.update(probe=0, side=0)
    scanner.scan_library_sync(db, lib)
    assert scan_env["side"] == 0                          # 未到期：重刮被推迟
    db.expire_all()
    aged = db.query(em.MediaItem).filter_by(id=aged.id).one()
    assert aged.last_scraped_at < datetime.now() - timedelta(days=90)

    for r in _dir_rows(db, lib.id):
        r.next_due_at = datetime.now() - timedelta(minutes=1)
    db.commit()
    scan_env.update(probe=0, side=0)
    scanner.scan_library_sync(db, lib)
    assert scan_env["side"] >= 1                          # 到期：重刮照常发生
