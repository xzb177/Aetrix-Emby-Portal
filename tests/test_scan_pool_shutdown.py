"""扫描线程池关闭竞态：部署重启不该让整库扫描失败。

## 事故

部署时容器收到 SIGTERM，进程级扫描线程池被关闭，正在跑的扫描继续往旧池
提交任务，抛：

    RuntimeError: cannot schedule new futures after shutdown

整库扫描被标记 failed。生产实测：「动漫」库整轮扫描失败（11422 集探测随之失败）。

## 修法

``_scan_pool()`` 原本只在**取池那一刻**检查 ``_shutdown``，从取到提交之间
存在窗口。新增 ``_submit()`` 捕获该异常、换新池重试一次。

**只吞这一种异常**：其它 RuntimeError 与函数自身的异常照常抛出，不会被误吞。
"""
import importlib
import os
import pytest
import tempfile
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmp = tempfile.mkstemp(suffix=".db")
os.close(_fd)

from backend import database as _dbmod  # noqa: E402
from sqlalchemy import create_engine as _ce  # noqa: E402
from sqlalchemy.orm import sessionmaker as _sm  # noqa: E402


from backend.database import init_db  # noqa: E402



@pytest.fixture(scope="module", autouse=True)
def _hygiene_module_engine():
    """测试卫生：本文件独占临时库在模块夹具内换绑、模块结束原样还原。

    导入期顶层换绑会让全局 engine/SessionLocal 在 collection 后停在本文件
    的临时库上，后面的文件/用例撞 no such table 或读错库（#1668 同族）；
    换绑只在本模块用例期间生效。
    """
    _hyg_saved_engine = _dbmod.engine
    _hyg_saved_factory = _dbmod.SessionLocal._factory
    _hyg_saved_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}"
    _dbmod.engine = _ce(os.environ["DATABASE_URL"])
    _dbmod.configure_session_local(_sm(bind=_dbmod.engine))
    init_db()
    _hyg_engine = _dbmod.engine
    try:
        yield
    finally:
        _dbmod.engine = _hyg_saved_engine
        _dbmod.configure_session_local(_hyg_saved_factory)
        if _hyg_saved_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = _hyg_saved_url
        _hyg_engine.dispose()

def _scanner():
    # 运行期导入：顶层 import 会抢在其它测试建库之前绑定 SessionLocal
    return importlib.import_module("backend.emby_server.scanner")


def test_submit_works_on_live_pool():
    sc = _scanner()
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        fut = sc._submit(pool, lambda: 42)
        assert fut.result() == 42
    finally:
        pool.shutdown()


def test_submit_recovers_after_shutdown():
    """核心回归：池已关闭时提交，必须换新池成功，而不是抛 RuntimeError"""
    sc = _scanner()
    dead = ThreadPoolExecutor(max_workers=1)
    dead.shutdown()  # 模拟部署重启

    fut = sc._submit(dead, lambda: "ok")
    assert fut.result() == "ok", "关闭的池应换新池重试"


def test_submit_does_not_swallow_other_errors():
    """其它 RuntimeError 必须照常抛出，不能被误吞"""
    sc = _scanner()
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        def _boom():
            raise RuntimeError("别的错误")
        fut = sc._submit(pool, _boom)
        try:
            fut.result()
            raise AssertionError("函数内的异常应向上抛")
        except RuntimeError as exc:
            assert "别的错误" in str(exc)
    finally:
        pool.shutdown()


def test_scan_pool_returns_usable_pool():
    sc = _scanner()
    pool = sc._scan_pool()
    assert pool is not None
    assert sc._submit(pool, lambda: 1).result() == 1
