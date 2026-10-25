# -*- coding: utf-8 -*-
"""探测 worker 重构（v2.53）：抢单租约 / 回收 / 退避 / 超时整组杀 / 熔断 / 单体启动 / 排空。

生产事故：「探测 worker 启动了定时器，但实际探测没在跑，28.5 万条卡住，时不时卡死」。
复现与根因见 CHANGELOG 2.53.0 / 2.55.0；这里钉住修复后的每一条行为。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func

from backend.database import SessionLocal, init_db
from backend.emby_server import media_probe, probe_worker, proc_util, worker_registry
from backend.emby_server import models as em

init_db()

_ROOT = Path(__file__).resolve().parent.parent


def _guid():
    return uuid.uuid4().hex


@pytest.fixture(autouse=True, scope="module")
def _tables():
    """全量跑时前面的测试可能换了 SessionLocal 绑定的库（基线里 "no such table" 一族），
    这里对**当前**绑定建表，让本文件不受执行顺序影响。"""
    from backend.database import Base
    from backend import models as _base_models  # noqa: F401 — 注册全部模型
    db = SessionLocal()
    try:
        Base.metadata.create_all(bind=db.get_bind())
    finally:
        db.close()
    yield


@pytest.fixture()
def lib():
    db = SessionLocal()
    lib = em.Library(guid=_guid(), name=f"pw_{_guid()[:8]}", collection_type="mixed",
                     paths="/tmp/pw")
    db.add(lib)
    db.commit()
    db.refresh(lib)
    lib_id = lib.id
    db.close()
    yield lib_id
    db = SessionLocal()
    db.query(em.MediaItem).filter(em.MediaItem.library_id == lib_id).delete(
        synchronize_session=False)
    db.query(em.Library).filter(em.Library.id == lib_id).delete()
    db.commit()
    db.close()


def _add(db, lib_id, n=1, **kw):
    rows = []
    for _ in range(n):
        g = _guid()
        r = dict(guid=g, library_id=lib_id, name=f"i{g[:6]}", item_type="movie",
                 file_path=f"mount://8/m/{g}.mkv", probe_status="pending",
                 probe_priority=0, probe_attempts=0, date_added=datetime.now(),
                 size=10 ** 9)
        r.update(kw)
        if "{g}" in (r.get("file_path") or ""):
            r["file_path"] = r["file_path"].replace("{g}", g)
        rows.append(r)
    db.bulk_insert_mappings(em.MediaItem, rows)
    db.commit()
    return [i for (i,) in db.query(em.MediaItem.id).filter(
        em.MediaItem.guid.in_([r["guid"] for r in rows])).order_by(em.MediaItem.id).all()]


def _status(db, ids):
    db.expire_all()
    return {i: s for i, s in db.query(em.MediaItem.id, em.MediaItem.probe_status)
            .filter(em.MediaItem.id.in_(ids)).all()}


# ------------------------------------------------------------ 有上限的子进程
class TestBoundedRun:
    def test_timeout_kills_whole_process_group(self, tmp_path):
        """超时整组 SIGKILL：sh 包装下的孙进程也要死（旧 subprocess.run 只杀直接子进程）"""
        pidfile = tmp_path / "gc.pid"
        cmd = ["sh", "-c", f"sleep 30 & echo $! > {pidfile}; wait"]
        t0 = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            proc_util.bounded_run(cmd, timeout=0.5)
        assert time.monotonic() - t0 < 0.5 + proc_util.KILL_GRACE_SEC + 1
        gc = int(pidfile.read_text().strip())
        time.sleep(0.2)
        alive = True
        try:
            os.kill(gc, 0)
            # 可能是僵尸（父进程已死、等 init 收）：读状态
            with open(f"/proc/{gc}/stat") as f:
                alive = f.read().split()[2] != "Z"
        except (ProcessLookupError, FileNotFoundError):
            alive = False
        assert not alive, "孙进程没有被整组杀掉"

    def test_deadline_caps_timeout(self):
        t0 = time.monotonic()
        with proc_util.deadline(0.5):
            with pytest.raises(subprocess.TimeoutExpired):
                proc_util.bounded_run(["sleep", "10"], timeout=30)
        assert time.monotonic() - t0 < 3

    def test_success_returns_output(self):
        out = proc_util.bounded_run([sys.executable, "-c", "print('hi')"], timeout=10)
        assert out.returncode == 0 and out.stdout.strip() == "hi"

    def test_timeout_counter(self):
        before = proc_util.timeout_count()
        with pytest.raises(subprocess.TimeoutExpired):
            proc_util.bounded_run(["sleep", "5"], timeout=0.2)
        assert proc_util.timeout_count() == before + 1

    def test_call_with_timeout_abandons_hung_call(self):
        ev = threading.Event()
        t0 = time.monotonic()
        with pytest.raises(proc_util.CallTimeout):
            proc_util.call_with_timeout(ev.wait, 0.3)
        assert time.monotonic() - t0 < 1.5
        ev.set()

    def test_call_with_timeout_propagates_errors(self):
        def boom():
            raise ValueError("x")
        with pytest.raises(ValueError):
            proc_util.call_with_timeout(boom, 2)


# ------------------------------------------------------------ 抢单 / 回收 / 退避
class TestClaimReclaim:
    def test_claim_sets_lease_and_orders(self, lib):
        db = SessionLocal()
        try:
            a, b = _add(db, lib, 2, item_type="episode")
            hi = _add(db, lib, 1, probe_priority=1000)[0]
            skip_series = _add(db, lib, 1, item_type="series")[0]
            future = _add(db, lib, 1, probe_next_retry_at=datetime.now() + timedelta(hours=1))[0]
            rows = probe_worker._claim_rows(db, 10, library_id=lib)
            ids = [r[0] for r in rows]
            assert ids == [hi, b, a], "优先级高先，同优先级新入库（id 大）先"
            st = _status(db, ids + [skip_series, future])
            assert all(st[i] == "probing" for i in ids)
            assert st[skip_series] == "pending" and st[future] == "pending"
            claimed_at = db.query(em.MediaItem.probe_claimed_at).filter(
                em.MediaItem.id == hi).scalar()
            assert claimed_at is not None
        finally:
            db.close()

    def test_claim_excludes_open_breaker_prefix(self, lib):
        db = SessionLocal()
        try:
            dead = _add(db, lib, 2, file_path="mount://7/x/{g}.mkv")
            ok = _add(db, lib, 2)
            rows = probe_worker._claim_rows(db, 10, library_id=lib,
                                            exclude_prefixes=("mount://7/",))
            assert sorted(r[0] for r in rows) == sorted(ok)
            assert set(_status(db, dead).values()) == {"pending"}
        finally:
            db.close()

    def test_reclaim_stale_only(self, lib):
        db = SessionLocal()
        try:
            old = _add(db, lib, 1, probe_status="probing",
                       probe_claimed_at=datetime.now() - timedelta(hours=1))[0]
            legacy = _add(db, lib, 1, probe_status="probing", probe_claimed_at=None)[0]
            fresh = _add(db, lib, 1, probe_status="probing", probe_claimed_at=datetime.now())[0]
            n = probe_worker.reclaim_stale(db, ttl_sec=600)
            assert n >= 2
            st = _status(db, [old, legacy, fresh])
            assert st[old] == "pending" and st[legacy] == "pending"
            assert st[fresh] == "probing", "租约未过期的不能被抢走"
            probe_worker.reclaim_stale(db, ttl_sec=None)
            assert _status(db, [fresh])[fresh] == "pending"
        finally:
            db.close()

    def test_release_does_not_count_attempt(self, lib):
        db = SessionLocal()
        try:
            i = _add(db, lib, 1, probe_status="probing", probe_attempts=1)[0]
            until = datetime.now() + timedelta(minutes=5)
            assert probe_worker._release([i], retry_at=until) == 1
            it = db.query(em.MediaItem).get(i)
            assert it.probe_status == "pending" and it.probe_attempts == 1
            assert it.probe_next_retry_at is not None
        finally:
            db.close()

    def test_backoff_then_permanent_fail_with_error(self, lib, monkeypatch):
        monkeypatch.setattr(media_probe, "PROBE_MAX_ATTEMPTS", 3)
        monkeypatch.setattr(media_probe, "PROBE_RETRY_BASE_SEC", 60)
        monkeypatch.setattr(media_probe, "PROBE_RETRY_MAX_SEC", 100)
        db = SessionLocal()
        try:
            i = _add(db, lib, 1, probe_status="probing", probe_claimed_at=datetime.now())[0]
            it = db.query(em.MediaItem).get(i)
            t0 = datetime.now()
            media_probe.mark_retry(db, it, "超时 A")
            assert it.probe_status == "pending" and it.probe_attempts == 1
            # 基数 60s ±20% 抖动 → [48, 72]，留执行耗时余量
            assert 45 <= (it.probe_next_retry_at - t0).total_seconds() <= 75
            assert it.probe_last_error == "超时 A" and it.probe_claimed_at is None
            media_probe.mark_retry(db, it, "超时 B")
            # 120s 被封顶到 100s，再 ×[0.8, 1.2] 抖动 → [80, 120]
            assert (it.probe_next_retry_at - t0).total_seconds() <= 125
            media_probe.mark_retry(db, it, "超时 C")
            assert it.probe_status == "failed"
            assert "超时 C" in (it.probe_last_error or "")
        finally:
            db.close()


# ------------------------------------------------------------ 整理
class TestTriage:
    def test_triage_normalizes_statuses(self, lib):
        db = SessionLocal()
        try:
            series = _add(db, lib, 1, item_type="series", file_path=None)[0]
            season = _add(db, lib, 1, item_type="season", file_path=None)[0]
            has_info = _add(db, lib, 1, video_codec="h264", duration_ticks=10 ** 10)[0]
            fn_codec_no_dur = _add(db, lib, 1, video_codec="h264", duration_ticks=0)[0]
            ep = _add(db, lib, 1, item_type="episode")[0]
            from backend.emby_server.soft_delete import include_deleted
            with include_deleted():
                deleted = _add(db, lib, 1, deleted_at=datetime.now())[0]
            null_missing = _add(db, lib, 1, probe_status=None)[0]
            failed = _add(db, lib, 1, probe_status="failed")[0]
            with include_deleted():
                probe_worker.triage(db, chunk=7)
                st = _status(db, [series, season, has_info, fn_codec_no_dur, ep, deleted,
                                  null_missing, failed])
            assert st[series] == "skipped" and st[season] == "skipped"
            assert st[deleted] == "skipped"
            assert st[has_info] == "done"
            assert st[fn_codec_no_dur] == "pending", "文件名解析出的 codec 没有时长，仍需探测"
            assert st[ep] == "pending"
            assert st[null_missing] == "pending"
            assert st[failed] == "failed", "终态不复活"
        finally:
            db.close()

    def test_triage_boosts_recently_played(self, lib):
        db = SessionLocal()
        try:
            i = _add(db, lib, 1, item_type="episode")[0]
            from backend import models as base_models
            u = base_models.WebUser(username=f"pw{_guid()[:8]}", password_hash="x",
                                    email=f"{_guid()[:8]}@x.io")
            db.add(u)
            db.commit()
            db.add(em.UserMediaData(user_id=u.id, item_id=i, last_played_at=datetime.now()))
            db.commit()
            probe_worker.triage(db)
            prio = db.query(em.MediaItem.probe_priority).filter(em.MediaItem.id == i).scalar()
            assert prio == probe_worker.RECENT_PLAY_PRIORITY
            db.query(em.UserMediaData).filter(em.UserMediaData.item_id == i).delete()
            db.query(base_models.WebUser).filter(base_models.WebUser.id == u.id).delete()
            db.commit()
        finally:
            db.close()


# ------------------------------------------------------------ 熔断
class TestBreaker:
    def test_trips_after_threshold_and_resets(self):
        b = probe_worker.MountBreaker(threshold=3, cooldown=0.3)
        assert b.allow("mount://7/")
        assert not b.record_timeout("mount://7/")
        assert not b.record_timeout("mount://7/")
        assert b.record_timeout("mount://7/")
        assert not b.allow("mount://7/")
        assert b.open_keys() == ["mount://7/"]
        assert b.allow("mount://8/"), "熔断只影响出问题的挂载"
        time.sleep(0.35)
        assert b.allow("mount://7/"), "冷却到期半开"
        # 半开后再超时：立刻再熔断，冷却翻倍
        assert b.record_timeout("mount://7/")
        assert 0.3 < b.open_until("mount://7/") - time.time() <= 0.61
        b.record_ok("mount://7/")
        assert b.snapshot()["mount://7/"]["consecutive_timeouts"] == 0

    def test_mount_key(self):
        assert probe_worker.mount_key("mount://12/a/b.mkv") == "mount://12/"
        assert probe_worker.mount_key("/mnt/mp/tv/x.mkv") == "/mnt/mp/"
        assert probe_worker.mount_key("https://h.example/x/y.mp4") == "https://h.example/"


# ------------------------------------------------------------ 单条目：超时不吃线程
def _ok_probe(*a, **k):
    return {"video_codec": "h264", "audio_codec": "aac", "width": 1920, "height": 1080,
            "duration_ticks": 72_000_000_000, "bitrate": 8_000_000}


class TestProcessItem:
    @pytest.fixture(autouse=True)
    def _fast(self, monkeypatch):
        monkeypatch.setattr(probe_worker, "PROBE_RESOLVE_TIMEOUT_SEC", 0.5)
        monkeypatch.setattr(probe_worker, "PROBE_ITEM_TIMEOUT_SEC", 0.5)
        monkeypatch.setattr(probe_worker, "breaker", probe_worker.MountBreaker(3, 60))
        monkeypatch.setattr(media_probe.persist_lib, "deserialize", lambda db, item: False)
        monkeypatch.setattr(media_probe.persist_lib, "serialize", lambda db, item: False)

    def _claim(self, db, lib):
        i = _add(db, lib, 1, probe_status="probing", probe_claimed_at=datetime.now())[0]
        return i

    def test_hung_resolve_times_out(self, lib, monkeypatch):
        ev = threading.Event()
        def _hung_resolve(db, it):
            ev.wait()
            return (None, None)
        monkeypatch.setattr(media_probe, "resolve_probe_input", _hung_resolve)
        db = SessionLocal()
        try:
            i = self._claim(db, lib)
            t0 = time.monotonic()
            assert probe_worker.process_item(i) == "timeout"
            assert time.monotonic() - t0 < 3
            it = db.query(em.MediaItem).get(i)
            assert it.probe_status == "pending" and it.probe_attempts == 1
            assert "超时" in it.probe_last_error
        finally:
            ev.set()
            db.close()

    def test_hung_ffprobe_times_out(self, lib, monkeypatch):
        ev = threading.Event()
        monkeypatch.setattr(media_probe, "resolve_probe_input",
                            lambda db, it: (("http://x/a.mkv", {}, 1, "mkv"), None))
        monkeypatch.setattr(media_probe, "probe_metadata", lambda *a, **k: ev.wait())
        db = SessionLocal()
        try:
            i = self._claim(db, lib)
            t0 = time.monotonic()
            assert probe_worker.process_item(i) == "timeout"
            assert time.monotonic() - t0 < 0.5 + proc_util.KILL_GRACE_SEC + 4
            assert probe_worker.breaker.snapshot()["mount://8/"]["consecutive_timeouts"] == 1
        finally:
            ev.set()
            db.close()

    def test_success_writes_back(self, lib, monkeypatch):
        monkeypatch.setattr(media_probe, "resolve_probe_input",
                            lambda db, it: (("http://x/a.mkv", {}, 1, "mkv"), None))
        monkeypatch.setattr(media_probe, "probe_metadata", _ok_probe)
        db = SessionLocal()
        try:
            i = self._claim(db, lib)
            assert probe_worker.process_item(i) == "ok"
            it = db.query(em.MediaItem).get(i)
            assert it.probe_status == "done" and it.video_codec == "h264"
            assert it.duration_ticks > 0 and it.probe_claimed_at is None
        finally:
            db.close()

    def test_http_404_fails_permanently(self, lib, monkeypatch):
        monkeypatch.setattr(media_probe, "resolve_probe_input",
                            lambda db, it: (("http://x/a.mkv", {}, 1, "mkv"), None))
        monkeypatch.setattr(media_probe, "probe_metadata",
                            lambda *a, **k: {"_http_code": 404})
        db = SessionLocal()
        try:
            i = self._claim(db, lib)
            assert probe_worker.process_item(i) == "failed"
            assert db.query(em.MediaItem).get(i).probe_status == "failed"
        finally:
            db.close()


# ------------------------------------------------------------ 端到端：卡住的场景现在能排空
class TestDrain:
    def test_backlog_drains_despite_hanging_mount(self, lib, monkeypatch):
        """复现生产形态：单集 + 文件名 codec 电影 + 剧集骨架 + 一个挂死的挂载。

        旧实现：只抢 movie/series 缺 codec；挂死挂载吃光线程后每轮泄漏一批 probing。
        新实现：整理掉不可探的，按挂载熔断，健康挂载照常排空，无 probing 残留。
        """
        for k, v in {"PROBE_RESOLVE_TIMEOUT_SEC": 0.5, "PROBE_ITEM_TIMEOUT_SEC": 0.5,
                     "PROBE_MIN_INTERVAL_SEC": 0.0, "PROBE_IDLE_SLEEP_SEC": 0.2,
                     "PROBE_WORKERS": 4, "PROBE_REMOTE_CONCURRENCY": 4}.items():
            monkeypatch.setattr(probe_worker, k, v)
        monkeypatch.setattr(probe_worker, "breaker", probe_worker.MountBreaker(3, 600))
        monkeypatch.setattr(media_probe.persist_lib, "deserialize", lambda db, item: False)
        monkeypatch.setattr(media_probe.persist_lib, "serialize", lambda db, item: False)
        monkeypatch.setattr(probe_worker, "is_paused", lambda *a, **k: False)
        hang = threading.Event()
        calls = {"n": 0}

        def fake_resolve(db, item):
            return ((item.file_path.replace("mount://", "http://fake/"), {}, 1, "mkv"),
                    None)

        def fake_probe(path, headers=None, size=0, container=""):
            calls["n"] += 1
            if "/7/" in path:
                hang.wait()
            return _ok_probe()

        monkeypatch.setattr(media_probe, "resolve_probe_input", fake_resolve)
        monkeypatch.setattr(media_probe, "probe_metadata", fake_probe)
        db = SessionLocal()
        try:
            eps = _add(db, lib, 300, item_type="episode", file_path="mount://8/tv/{g}.mkv")
            fn_movies = _add(db, lib, 100, video_codec="h264", duration_ticks=0)
            series = _add(db, lib, 30, item_type="series", file_path=None)
            dead = _add(db, lib, 20, file_path="mount://7/d/{g}.mkv", probe_priority=100)
            healthy = eps + fn_movies
            probe_worker.start()
            # triage 必须有调度入口：PR #416 删除 preprobe 时把它删成了死代码（只剩定义、
            # 没有任何调用点），这里钉住 start() 真的注册了整理线程。
            assert "probe_triage" in worker_registry.snapshot(), "triage 调度未注册"
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                left = db.query(func.count(em.MediaItem.id)).filter(
                    em.MediaItem.id.in_(healthy),
                    em.MediaItem.probe_status != "done").scalar()
                db.rollback()
                if left == 0:
                    break
                time.sleep(0.5)
            probe_worker.stop()
            st = _status(db, healthy + series + dead)
            assert all(st[i] == "done" for i in healthy), \
                f"健康挂载未排空: {sum(1 for i in healthy if st[i] != 'done')} 条"
            assert all(st[i] == "skipped" for i in series)
            assert all(st[i] in ("pending", "failed") for i in dead), "挂死挂载不能留 probing"
            assert "mount://7/" in probe_worker.breaker.snapshot()
            # 熔断后挂死挂载最多被尝试 阈值 + 并发 次左右，不会被反复撞
            dead_calls = db.query(func.sum(em.MediaItem.probe_attempts)).filter(
                em.MediaItem.id.in_(dead)).scalar() or 0
            assert dead_calls <= 3 + 4
            assert not any(s == "probing" for s in st.values())
        finally:
            hang.set()
            probe_worker.stop()
            db.close()


# ------------------------------------------------------------ 启动：单体 + worker 同一入口，死了能重启
class TestStartup:
    def test_monolith_lifespan_starts_probe_worker(self):
        """M11：main.py 的单体分支必须启动探测 worker（以前只有 worker.py 起它）"""
        src = (_ROOT / "backend" / "main.py").read_text(encoding="utf-8")
        seg = src[src.index("async def lifespan"):src.index("yield")]
        assert "probe_worker.start()" in seg
        assert "if not _is_api_role:" in seg[:seg.index("probe_worker.start()")]
        wsrc = (_ROOT / "backend" / "worker.py").read_text(encoding="utf-8")
        assert "probe_worker.start()" in wsrc

    def test_start_registers_and_supervisor_restarts(self, monkeypatch):
        monkeypatch.setattr(probe_worker, "is_paused", lambda *a, **k: True)
        try:
            assert probe_worker.start() is True
            assert probe_worker.start() is True  # 幂等
            snap = worker_registry.snapshot()
            assert snap["probe_dispatcher"]["alive"]
            assert snap["probe_triage"]["alive"]
            # 模拟线程静默死亡：_stop_event 是 dispatcher 与 triage 共用的
            # 停止信号（两循环都以它为退出条件），所以两线程都会死；
            # 必须把两线程都 join 到已退出再跑监督，否则监督结果取决于
            # triage 线程是否刚好被调度到退出——原用例只 join dispatcher，
            # triage 活着时监督只回 dispatcher、退出了就回两项，全量
            # 冒烟（#1310）正是以后一种时序红的，不是监督本身坏了。
            # start() 本就把两线程都登记且都带 restart 回调（见
            # probe_worker._spawn_dispatcher/_spawn_triage），监督理应
            # 把两项都重启回来，这里钉住两项、不是只钉 dispatcher。
            probe_worker._stop_event.set()
            probe_worker._wake.set()
            probe_worker._dispatcher_thread.join(5)
            probe_worker._triage_thread.join(5)
            assert worker_registry.snapshot()["probe_dispatcher"]["status"] == "crashed"
            assert worker_registry.snapshot()["probe_triage"]["status"] == "crashed"
            probe_worker._stop_event.clear()
            assert worker_registry.supervise_once() == ["probe_dispatcher", "probe_triage"]
            assert probe_worker._dispatcher_thread.is_alive()
            assert probe_worker._triage_thread.is_alive()
            assert worker_registry.snapshot()["probe_dispatcher"]["restarts"] == 1
            assert worker_registry.snapshot()["probe_triage"]["restarts"] == 1
        finally:
            probe_worker.stop()

    def test_stop_releases_buffer_and_status_snapshot(self, lib):
        db = SessionLocal()
        try:
            _add(db, lib, 3, probe_status="probing", probe_claimed_at=None)
            snap = probe_worker.status_snapshot(db)
            assert snap["stale_probing"] >= 3
            assert "counts" in snap and "pending_ready" in snap
            out = probe_worker.reset(db, "stuck")
            assert out["stuck"] >= 3
            with pytest.raises(ValueError):
                probe_worker.reset(db, "nope")
        finally:
            db.close()

    def test_pause_resume_roundtrip(self):
        db = SessionLocal()
        try:
            assert probe_worker.set_paused(db, True) is True
            assert probe_worker.is_paused(db) is True
            assert probe_worker.set_paused(db, False) is False
            assert probe_worker.is_paused(db) is False
        finally:
            db.close()
