"""全测试进程内状态隔离。

enrich_worker 里有几个模块级 60s TTL 缓存（豆瓣/altmeta/夜间配置、单集等父级
等待计数）：生产里这是有意的进程内缓存，但 pytest 同一进程跑全量 tests/ 时，
前一个测试文件 mock 进去的配置值会泄漏到后一个文件，造成顺序相关的假失败
（完美审查 R17 C-P2-5 引入）。

每个测试前把这些缓存清零，保证用例之间相互独立。只影响测试，生产代码无改动。
"""
import os

import pytest

# ---------------------------------------------------------------------------
# pytest-xdist per-worker 隔离（容量三期，2026-11-07）
#
# 2026-11-02 容量二期实测过一轮 xdist 并行（PR #1771 第一轮）：计算本身
# 有效（pytest 步 1189 秒 → 615 秒），但同轮 34 failed + 17 errors 横跨
# 16 个文件后回滚。定案机理：4 个 worker 进程共用 job 级 env 的同一个
# DATABASE_URL 文件（./ci-smoke.db）与同一批固定路径（sidecar 的
# PROVIDER_RUNTIME_SOCK_DIR、MOUNT_RCLONE_CONF），串行时靠执行顺序与
# 各自清理维持的共享状态，并行下被别的 worker 的写入/删除踩掉。当时定
# 的重试前置条件就是先做 per-worker 隔离——即本段。
#
# 做法：worker 进程在 conftest 导入期（早于本进程内任何 backend 模块的
# 导入，backend.database 在导入期就按 DATABASE_URL 建 engine，晚了改
# 不动）按 PYTEST_XDIST_WORKER（gw0/gw1/...，xdist 在 worker 环境里
# 注入）改写三处 env，给每个 worker 一套自己的运行时沙箱：
# - DATABASE_URL：sqlite 文件名加 -<worker> 后缀（ci-smoke-gw0.db）；
#   未显式设置时同样落到 per-worker 文件，不再用共享默认名。
# - PROVIDER_RUNTIME_SOCK_DIR：下加 <worker> 子目录（socket 不撞名）。
# - MOUNT_RCLONE_CONF：文件名加 -<worker> 后缀（写配置不互踩）。
# 串行跑（无 PYTEST_XDIST_WORKER）整段不执行，行为与之前逐字一致。
_XDIST_WORKER = os.environ.get("PYTEST_XDIST_WORKER")

if _XDIST_WORKER:
    _db_url = os.environ.get("DATABASE_URL", "")
    _worker_db_path = None
    _shared_db_path = None
    if _db_url.startswith("sqlite:///") and ":memory:" not in _db_url:
        _shared_db_path = _db_url[len("sqlite:///"):]
        if _db_url.endswith(".db"):
            os.environ["DATABASE_URL"] = f"{_db_url[:-3]}-{_XDIST_WORKER}.db"
        else:
            os.environ["DATABASE_URL"] = f"{_db_url}-{_XDIST_WORKER}"
        _worker_db_path = os.environ["DATABASE_URL"][len("sqlite:///"):]
    elif not _db_url:
        os.environ["DATABASE_URL"] = f"sqlite:///./ci-smoke-{_XDIST_WORKER}.db"
        _worker_db_path = f"./ci-smoke-{_XDIST_WORKER}.db"
    _sock_dir = os.environ.get("PROVIDER_RUNTIME_SOCK_DIR")
    if _sock_dir:
        os.environ["PROVIDER_RUNTIME_SOCK_DIR"] = os.path.join(
            _sock_dir, _XDIST_WORKER)
    _rclone_conf = os.environ.get("MOUNT_RCLONE_CONF")
    if _rclone_conf:
        _conf_root, _conf_ext = os.path.splitext(_rclone_conf)
        os.environ["MOUNT_RCLONE_CONF"] = f"{_conf_root}-{_XDIST_WORKER}{_conf_ext}"

    # worker 自己的库必须在任何测试模块导入前就建好表并带上种子。
    #
    # 只靠 session 级 warmup fixture 预热不够：xdist worker 在跑第一个
    # 用例之前先要导入测试模块（收集期），而八十多个测试文件在导入期
    # 就以 `from backend.database import SessionLocal` 捕获当时的
    # sessionmaker、或干脆把全局 engine/SessionLocal 重绑到自己的临时
    # 库。等 warmup fixture 执行时，当前 engine 往往已是某个重绑文件
    # 的临时库——init_db 预热的是它，worker 自己的库（被早期导入捕获
    # 的原始 SessionLocal 正指着它）始终是空文件，于是这些用例在
    # setup 期整片撞 no such table。串行时这条链成立，是因为共享库
    # 在 pytest 开始前已被前面几十个冒烟脚本步建好表并播种。
    #
    # 故导入期就对齐串行的开局状态：先把共享库（冒烟步的种子）整库
    # 复制到 worker 独立文件（sqlite backup，含 WAL 内容），再导入
    # backend.database（此时 engine/SessionLocal 按改写后的
    # DATABASE_URL 建）并 init_db 补齐表结构。之后各测试文件在导入
    # 期重绑/捕获的都是已初始化的库，与串行同口径。本段失败不阻断
    # 收集：session warmup fixture 还会按原口径再预热一次兜底。
    if _worker_db_path:
        try:
            if (_shared_db_path and os.path.exists(_shared_db_path)
                    and not os.path.exists(_worker_db_path)):
                import sqlite3

                _src = sqlite3.connect(
                    f"file:{_shared_db_path}?mode=ro", uri=True)
                try:
                    _dst = sqlite3.connect(_worker_db_path)
                    try:
                        _src.backup(_dst)
                    finally:
                        _dst.close()
                finally:
                    _src.close()
            from backend import database as _dbmod

            _dbmod.init_db()
        except Exception as _exc:  # noqa: BLE001 — 兜底在 warmup fixture
            import warnings

            warnings.warn(f"xdist worker 库导入期预热失败: {_exc!r}")


@pytest.fixture(scope="session", autouse=True)
def _xdist_worker_db_warmup():
    """xdist 下每个 worker 开局先把自己的任务库 init_db 一次。

    串行全量时，任务库（ci-smoke.db）在 pytest 开始前就被前面几十个冒烟
    脚本步以同一 DATABASE_URL 起过服务、建好表并播种，跑在前面的测试文
    件也会 init_db；多数用例默认这套表与种子已经在场（2026-11-02 夹具
    隔离盘点定案的主要耦合面）。worker 的独立库开局是空文件，若不预热，
    「直接用全局 SessionLocal 又不自建表」的用例会在 setup 期撞
    no such table——那是隔离形态差异、不是用例本身的问题。故 session
    起始按串行同口径预热一次。只在 xdist 下生效，串行行为不变。
    """
    if _XDIST_WORKER:
        from backend import database as _database

        _database.init_db()
    yield


@pytest.fixture(autouse=True)
def _reset_enrich_worker_caches():
    try:
        from backend.emby_server import enrich_worker as ew
    except Exception:
        yield
        return
    for _name in ("_douban_cfg_cache", "_altmeta_cfg_cache", "_night_cfg_cache",
                  "_enrich_tuning_cache"):
        _c = getattr(ew, _name, None)
        if isinstance(_c, dict):
            _c.clear()
            _c.update({"at": 0.0, "cfg": None})
    _w = getattr(ew, "_parent_wait_counts", None)
    if isinstance(_w, dict):
        _w.clear()
    yield


@pytest.fixture(autouse=True)
def _isolate_transfer_stage_dir(tmp_path, monkeypatch):
    """跨 provider 源字节暂存是 worker 本地盘状态：测试间必须隔离。

    暂存目录默认挂在 tempfile.gettempdir() 下，同 pytest 进程内跨文件
    共享会导致「前一文件暂存了同内容、后一文件断言源读取次数」类
    顺序假失败（2026-10-31 第三单引入时实证：multipart 文件内前一
    用例暂存了同 key、后一用例哈希遍零次碰源）。每测一个独立目录，
    且清空进程内 stage 注册表。
    """
    monkeypatch.setenv("TRANSFER_STAGE_DIR", str(tmp_path / "transfer-stage"))
    try:
        from backend.emby_server import transfer_variant as tv
        with tv._STAGE_REGISTRY_LOCK:
            tv._STAGE_REGISTRY.clear()
    except Exception:
        pass
    yield
