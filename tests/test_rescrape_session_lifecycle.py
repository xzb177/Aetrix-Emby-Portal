"""rescrape 会话生命周期回归测试。

## 事故（2026-10-17 生产，run 4811 / 4814）
日韩剧（库 45）与综艺（库 48）的两条 rescrape 在同一 API 进程里并发跑了十几分钟：
- 4811（partial）：19 个 Drive 子目录列举失败，清一色
  ``This session is in 'committed' state; no further SQL can be emitted within this transaction.``
- 4814（failed）：
  ``InvalidRequestError: This session is provisioning a new connection; concurrent operations are not permitted``

## 根因
``_run_library_rescrape``（backend/api/admin_scrape.py）自己 ``SessionLocal()``
开会话跑 ``scan_library_sync``，却漏了 ``scan_queue._run_task`` 在 2026-09-29
事故后加上的 ``db.expire_on_commit = False``
（见 tests/test_scan_session_concurrency.py）。
扫描是"一批一提交"的长事务；默认 ``expire_on_commit=True`` 时每次批次提交都把
ctx 里缓存的 series/season ORM 对象标过期，进程级 ``_SCAN_POOL`` 的 IO 线程随后
碰到这些对象就会触发懒加载，在共享 Session 上发 SQL → 上面两种崩溃。

## 修复
rescrape 建会话后同样置 ``db.expire_on_commit = False``，与 ``_run_task`` 同口径。
"""
import os
import tempfile

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models as base_models
from backend.database import configure_session_local
from backend.emby_server import models as em


def _make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}")
    base_models.Base.metadata.create_all(engine)
    return engine, path


def test_rescrape_session_disables_expire_on_commit():
    """_run_library_rescrape 建的 Session 必须 expire_on_commit=False。

    修复前：SessionLocal() 默认 True → 本用例红（与生产 4811/4814 同构）；
    修复后：置 False → 绿。
    """
    from backend.api import admin_scrape
    from backend.emby_server import scanner as scan_lib

    engine, path = _make_db()
    try:
        # 最小媒体库行（本机路径，不牵扯挂载）
        setup = sessionmaker(bind=engine)()
        lib = em.Library(guid="test-rescrape-lib", name="测试库",
                         collection_type="movies", paths="/tmp",
                         scrape_policy="missing_only", mount_ids="")
        setup.add(lib)
        setup.commit()
        lib_id = lib.id
        setup.close()

        # 测试专用通道切换 SessionLocal 目标（默认 expire_on_commit=True，
        # 与生产一致；不碰单例实例属性）
        configure_session_local(sessionmaker(bind=engine))

        captured = {}

        def fake_scan_library_sync(db, library, snapshot, trigger="manual"):
            captured["db"] = db
            captured["trigger"] = trigger
            captured["library_id"] = library.id
            return {"added": 0}

        # _run_library_rescrape 在函数内 from scanner import scan_library_sync，
        # 打模块属性即可拦截（类属性口径，不打实例）
        with mock.patch.object(scan_lib, "scan_library_sync",
                               side_effect=fake_scan_library_sync):
            admin_scrape._run_library_rescrape(lib_id, "missing_only")

        assert captured.get("trigger") == "rescrape"
        assert captured.get("library_id") == lib_id
        db = captured.get("db")
        assert db is not None
        assert db.expire_on_commit is False, (
            "rescrape 会话必须 expire_on_commit=False"
            "（与 scan_queue._run_task 同口径），否则批次提交过期 ORM 对象后，"
            "IO 线程懒加载会撞上 committed state / concurrent operations"
            "（2026-10-17 run 4811/4814）"
        )
    finally:
        configure_session_local()
        engine.dispose()
        os.unlink(path)
