"""
统一数据库配置
整合用户端、管理后台和主项目数据到单一数据库
支持 PostgreSQL/MySQL + Redis 缓存
"""
import os
import threading
from sqlalchemy import create_engine, event, text, Column, Integer, String, Boolean, BigInteger, DateTime, Text, Numeric, ForeignKey, Index, JSON, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship
from datetime import datetime
import redis
from typing import Optional
from backend.emby_server.env_util import env_float, env_int

# ==================== 数据库配置 ====================
# 支持环境变量切换数据库类型
# v2.42.0 起**默认 PostgreSQL**：SQLite 只在单文件、写锁模型下工作，多进程
# 并发（扫描 / 播放上报 / 订单）容易撞 "database is locked"，且备份只能靠
# 文件级快照。老部署只要没设 DATABASE_TYPE，需要显式设 DATABASE_TYPE=sqlite
# 才能继续用 SQLite 路径（见下方 fallback 提示）。
DATABASE_TYPE = os.getenv("DATABASE_TYPE", "postgresql")  # postgresql(默认), sqlite, mysql

# SQLite 默认库文件名（v2.30.0 品牌统一）。
SQLITE_DB_FILENAME = "aetrix_unified.db"
LEGACY_SQLITE_DB_FILENAME = "royalbot_unified.db"  # brand-scan: allow — 老部署的库文件名，只用于兼容


def default_sqlite_url(cwd: str = ".") -> str:
    """没有显式 ``DATABASE_URL`` 时的 SQLite 默认连接串

    改品牌**不该让任何人「换了个文件名就丢整站数据」**：只有新文件名不存在、
    老文件名还在时才继续沿用老文件名。两者都在（或都不在）时用新名——
    也就是说，一个正常升级上来的部署会一直用老库，直到有人真的把新名建出来。
    显式设了 ``DATABASE_URL`` 的部署完全不受这段逻辑影响。
    """
    base = os.path.abspath(cwd or ".")
    if not os.path.exists(os.path.join(base, SQLITE_DB_FILENAME)) and os.path.exists(
        os.path.join(base, LEGACY_SQLITE_DB_FILENAME)
    ):
        return f"sqlite:///./{LEGACY_SQLITE_DB_FILENAME}"
    return f"sqlite:///./{SQLITE_DB_FILENAME}"


if DATABASE_TYPE == "postgresql":
    DATABASE_URL = os.getenv("DATABASE_URL") or "postgresql://aetrix:password@localhost:5432/aetrix"
    # SQLAlchemy 2.x 的 postgresql:// 默认找 psycopg(v3) 驱动；仓库依赖是
    # psycopg2-binary，没装 psycopg 时显式指定 psycopg2，否则 import 期直接炸。
    # 装了 psycopg3 的环境不受影响（走更快的 v3）。
    if DATABASE_URL.startswith("postgresql://"):
        try:
            import psycopg  # noqa: F401
        except ImportError:
            DATABASE_URL = "postgresql+psycopg2://" + DATABASE_URL[len("postgresql://"):]
    # 两个驱动都没装时**不能直接炸**：默认库是 PG，但本地开发 / 一次性脚本 /
    # 契约检查常常一个驱动都不装。降级到 SQLite 并明确告警——否则默认改 PG 之后，
    # 「不设环境变量直接跑个脚本」会从「能跑」变成「ModuleNotFoundError」。
    # 显式设了 DATABASE_TYPE=postgresql 的部署不会走到这里（那是有意的强制）。
    if os.getenv("DATABASE_TYPE", "").strip() == "":
        try:
            __import__("psycopg2")
        except ImportError:
            try:
                __import__("psycopg")
            except ImportError:
                import warnings as _warnings
                _warnings.warn(
                    "未安装 PostgreSQL 驱动（psycopg2-binary / psycopg[3]），"
                    "且未显式设置 DATABASE_TYPE，本次降级使用 SQLite。"
                    "生产部署请安装驱动或用 Docker Compose（镜像内已装）。",
                    RuntimeWarning, stacklevel=2,
                )
                DATABASE_TYPE = "sqlite"
                DATABASE_URL = os.getenv("DATABASE_URL") or default_sqlite_url()
elif DATABASE_TYPE == "mysql":
    DATABASE_URL = os.getenv("DATABASE_URL") or "mysql+pymysql://aetrix:password@localhost:3306/aetrix"
else:
    # SQLite 默认路径（相对工作目录，可通过 DATABASE_URL 覆盖）
    DATABASE_URL = os.getenv("DATABASE_URL") or default_sqlite_url()

# Redis 配置
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
REDIS_ENABLED = os.getenv("REDIS_ENABLED", "false").lower() == "true"

# ==================== 数据库引擎 ====================
engine_config = {
    "echo": os.getenv("DB_ECHO", "false").lower() == "true",
    "pool_pre_ping": True,
    "pool_recycle": 3600,
}

if DATABASE_TYPE == "sqlite":
    # busy timeout：等待写锁而不是立刻报 "database is locked"（媒体扫描/播放上报并发场景）
    engine_config["connect_args"] = {"check_same_thread": False, "timeout": 30}
# 每进程连接上限 = _POOL_SIZE + _MAX_OVERFLOW；api / worker / EA 三个进程各自持有独立 engine，
# 峰值 3×(25+10)=105；请保证 PG max_connections 留足富余（默认 100 不够，需同步调高），
# 避免高峰期随机 "too many clients already" 导致全站 500。
# ---- DB 连接池大小（Batch 2 开关清理：env-only 单源化） ----
# DB_POOL_SIZE：只读环境变量（默认 25 = 生产现值）；管理后台不再可写，
# SystemConfig 里即便有旧行也会被忽略。
# engine 创建时解析一次（进程启动时），改完需重启进程生效。
DB_POOL_SIZE_ENV = "DB_POOL_SIZE"
DB_POOL_SIZE_DEFAULT = 25


def _resolve_db_pool_size() -> int:
    """DB 连接池大小：env-only（默认 25 = 生产现值）；改完需重启进程生效。"""
    return env_int(DB_POOL_SIZE_ENV, DB_POOL_SIZE_DEFAULT, 1)


_POOL_SIZE = _resolve_db_pool_size()
_DB_POOL_SIZE_FROM_ENV = bool((os.getenv(DB_POOL_SIZE_ENV) or "").strip())
_MAX_OVERFLOW = env_int("DB_MAX_OVERFLOW", 10, 0)
if DATABASE_TYPE == "postgresql":
    engine_config["pool_size"] = _POOL_SIZE
    engine_config["max_overflow"] = _MAX_OVERFLOW
elif DATABASE_TYPE == "mysql":
    engine_config["pool_size"] = _POOL_SIZE
    engine_config["max_overflow"] = _MAX_OVERFLOW
    engine_config["pool_recycle"] = 7200

engine = create_engine(DATABASE_URL, **engine_config)


def db_pool_config() -> dict:
    """管理后台用：DB 连接池当前生效配置（进程启动/engine 创建时解析，改完需重启）。"""
    return {
        "pool_size": _POOL_SIZE,
        "max_overflow": _MAX_OVERFLOW,
        "from_env": _DB_POOL_SIZE_FROM_ENV,
        "env_name": DB_POOL_SIZE_ENV,
        "default": DB_POOL_SIZE_DEFAULT,
        "database_type": DATABASE_TYPE,
        "restart_required": True,
    }

if DATABASE_TYPE == "sqlite":
    @event.listens_for(engine, "connect")
    def _sqlite_pragma(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        # WAL 模式：读写不互斥，显著降低高并发下的写锁冲突；
        # busy_timeout：写锁被占时等待而不是立刻报 "database is locked"
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        # WAL 下 synchronous=NORMAL 是安全的：事务提交不再等 fsync 落盘，
        # 只在 checkpoint 时同步。默认的 FULL 会让每次提交（播放进度上报、
        # token 更新这类高频写）都付一次 fsync。
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

_real_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class _SessionLocalProxy:
    """``SessionLocal`` 本体：一个永远在**调用时刻**解析目标的转发器。

    ## 为什么需要它

    仓库里 20+ 个模块写的是 ``from backend.database import SessionLocal``，
    那是 **import 期就把引用复制一份**到本模块（等价于值传递）。此后
    ``database.SessionLocal`` 换掉，它们手里的旧引用**不会跟着变**。

    正常运行时 ``SessionLocal`` 只在 ``database.py`` 赋值一次、无人改它，所以
    无害。但测试为隔离库会重设它，于是踩坑::

        import backend.database as dbmod
        from backend.emby_server import enrich_worker   # 此刻绑定旧 SessionLocal
        dbmod.SessionLocal = sessionmaker(...)          # 重建后 enrich_worker 不跟着变
        # enrich_worker.get_progress() 于是查旧库 → 读到空表 → assert 0 == 2

    是否踩中取决于测试文件的字母序，失败时隐时现，极难定位。

    ## 设计要点

    这个代理**对象本身永不更换**——所有模块 import 到的都是同一个实例。
    切换只发生在它内部指向的目标上，所以无论 import 顺序如何、各模块何时
    import，调用时看到的都是当前生效的那个。

    全仓库 ``SessionLocal`` 只被调用（``SessionLocal()``），没有任何属性访问，
    因此把它做成可调用对象是安全的。
    """

    def __init__(self, factory):
        self._factory = factory

    def __call__(self, *args, **kwargs):
        return self._factory(*args, **kwargs)

    def __repr__(self):
        return f"<SessionLocal -> {self._factory!r}>"


# 全局唯一的代理实例。configure_session_local 只改它的目标，不换它。
SessionLocal = _SessionLocalProxy(_real_session_local)


def configure_session_local(factory=None) -> None:
    """切换 SessionLocal 代理指向的目标（**测试专用**）。

    请务必用这个函数，不要写 ``database.SessionLocal = ...``：
    直接赋值会把代理换成一个被冻结的真 factory，此后各模块 import 到的就不再是
    代理，转发能力随之失效——正是本类要解决的陷阱换个形式复发。

    ``factory=None`` 表示恢复默认（数据库模块的 engine）。

    传入代理自身会被忽略并回退到默认：调用方若想"保存再还原"，很容易把
    ``dbmod.SessionLocal``（代理本身）存下来再传回来，那会让代理指向自己、
    每次调用无限递归。这里挡掉这种误用。
    """
    if factory is None or isinstance(factory, _SessionLocalProxy):
        factory = _real_session_local
    SessionLocal._factory = factory


Base = declarative_base()

# ==================== Redis 连接 ====================
#
# S4（稳定性审查）：Redis 运行期故障不能把事件循环 / 线程池卡死。
#
# - socket 超时从 5s（且 retry_on_timeout=True，最坏 10s+）降到默认 1s、不重试；
# - 熔断器：连续 REDIS_BREAKER_FAILURES 次失败后 REDIS_BREAKER_SECONDS 秒内不再碰
#   Redis（缓存走内存、限流走进程内计数），到期后半开放行一次探测，成功即恢复；
# - 启动时 Redis 不可达不再「永久降级」：后台线程定期重连，连上后自动切回。
import threading as _threading
import time as _time


REDIS_SOCKET_TIMEOUT = env_float("REDIS_SOCKET_TIMEOUT", 1.0, 0.1, 30.0)
REDIS_CONNECT_TIMEOUT = env_float("REDIS_CONNECT_TIMEOUT", 1.0, 0.1, 30.0)
REDIS_BREAKER_FAILURES = env_int("REDIS_BREAKER_FAILURES", 3, 1, 100)
REDIS_BREAKER_SECONDS = env_float("REDIS_BREAKER_SECONDS", 30.0, 1.0, 3600.0)


class RedisBreaker:
    """极简熔断器：closed →（连续 N 次失败）→ open（冷却期内直接拒绝）→ 半开（放行一次探测）"""

    def __init__(self, failures: int = REDIS_BREAKER_FAILURES,
                 cooldown: float = REDIS_BREAKER_SECONDS):
        self.failures = failures
        self.cooldown = cooldown
        self._lock = _threading.Lock()
        self._consecutive = 0
        self._open_until = 0.0
        self._probing = False

    @property
    def state(self) -> str:
        with self._lock:
            if self._open_until == 0.0:
                return "closed"
            return "open" if _time.monotonic() < self._open_until else "half-open"

    def allow(self) -> bool:
        with self._lock:
            if self._open_until == 0.0:
                return True
            if _time.monotonic() < self._open_until:
                return False
            if self._probing:
                return False  # 半开：同一时刻只放一个探测请求
            self._probing = True
            return True

    def record_success(self) -> None:
        with self._lock:
            if self._open_until:
                print("✅ Redis 已恢复，退出降级")
            self._consecutive = 0
            self._open_until = 0.0
            self._probing = False

    def record_failure(self) -> None:
        with self._lock:
            self._consecutive += 1
            self._probing = False
            if self._consecutive >= self.failures:
                if not self._open_until or _time.monotonic() >= self._open_until:
                    print(f"⚠️ Redis 连续 {self._consecutive} 次失败，"
                          f"{self.cooldown:.0f}s 内改用内存缓存 / 进程内限流")
                self._open_until = _time.monotonic() + self.cooldown

    def reset(self) -> None:
        with self._lock:
            self._consecutive = 0
            self._open_until = 0.0
            self._probing = False


redis_breaker = RedisBreaker()
redis_client: Optional[redis.Redis] = None


def _make_redis_client() -> "redis.Redis":
    return redis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=REDIS_CONNECT_TIMEOUT,
        socket_timeout=REDIS_SOCKET_TIMEOUT,
        retry_on_timeout=False,
        health_check_interval=30,
    )


_reconnect_started = False
_reconnect_lock = _threading.Lock()


def _start_redis_reconnect() -> None:
    """启动时没连上 Redis：后台每 REDIS_BREAKER_SECONDS 秒重试，连上后切回 Redis"""
    global _reconnect_started
    with _reconnect_lock:
        if _reconnect_started:
            return
        _reconnect_started = True

    def _loop():
        global redis_client, _reconnect_started
        while redis_client is None:
            _time.sleep(REDIS_BREAKER_SECONDS)
            try:
                client = _make_redis_client()
                client.ping()
            except Exception:  # noqa: BLE001 - 继续等
                continue
            redis_client = client
            redis_breaker.reset()
            print("✅ Redis 重连成功，切回 Redis 缓存 / 限流")
        _reconnect_started = False

    _threading.Thread(target=_loop, name="redis-reconnect", daemon=True).start()


if REDIS_ENABLED:
    try:
        redis_client = _make_redis_client()
        # 测试连接
        redis_client.ping()
        print("✅ Redis 连接成功")
    except Exception as e:
        print(f"⚠️ Redis 连接失败: {e}，将使用内存缓存（后台每 {REDIS_BREAKER_SECONDS:.0f}s 重连）")
        redis_client = None
        _start_redis_reconnect()


def get_redis() -> Optional["redis.Redis"]:
    """取可用的 Redis 客户端：未配置 / 未连上 / 熔断中 → None（调用方走降级路径）

    拿到客户端后调用方应在成功 / 失败时回报 ``redis_breaker.record_success()`` /
    ``record_failure()``（或直接用 :func:`redis_call`）。
    """
    client = redis_client
    if client is None or not redis_breaker.allow():
        return None
    return client


_MISSING = object()


def redis_call(fn, default=None):
    """``fn(client)`` 走熔断器：不可用或出错时返回 ``default``（并记一次失败）"""
    client = get_redis()
    if client is None:
        return default
    try:
        result = fn(client)
    except Exception:  # noqa: BLE001 - Redis 故障一律降级
        redis_breaker.record_failure()
        return default
    redis_breaker.record_success()
    return result


# 进程内限流计数（Redis 降级期间用）：固定窗口，有界
_mem_counter_lock = _threading.Lock()
_mem_counters: dict = {}
_MEM_COUNTER_MAX = 20000


def memory_rate_incr(key: str, ttl: int = 70) -> int:
    """进程内 INCR + EXPIRE（Redis 降级期间的限流计数；多进程部署下各进程各算一份）"""
    now = _time.monotonic()
    with _mem_counter_lock:
        entry = _mem_counters.get(key)
        if entry is None or entry[1] <= now:
            if len(_mem_counters) >= _MEM_COUNTER_MAX:
                for k in [k for k, (_, exp) in _mem_counters.items() if exp <= now]:
                    _mem_counters.pop(k, None)
                while len(_mem_counters) >= _MEM_COUNTER_MAX:
                    _mem_counters.pop(next(iter(_mem_counters)))
            entry = [0, now + ttl]
            _mem_counters[key] = entry
        entry[0] += 1
        return entry[0]


def rate_limit_incr(key: str, ttl: int = 70) -> Optional[int]:
    """限流计数：Redis 正常走 Redis；配置了 Redis 但故障/熔断 → 进程内计数；
    未启用 Redis（REDIS_ENABLED=false）→ None（不限流，与升级前一致）"""
    client = get_redis()
    if client is not None:
        try:
            count = client.incr(key)
            if count == 1:
                client.expire(key, ttl)
        except Exception:  # noqa: BLE001
            redis_breaker.record_failure()
        else:
            redis_breaker.record_success()
            return int(count)
    if REDIS_ENABLED:
        return memory_rate_incr(key, ttl)
    return None


# ==================== 缓存管理 ====================
class CacheManager:
    """统一缓存管理器，支持 Redis 和内存缓存

    内存回退是有界的（借鉴 go-emby 的有界缓存思路）：条目上限
    ``_MEMORY_CACHE_MAX``，存 (value, expire_at)，读时校验过期、
    写满时先清过期条目再按 FIFO 淘汰最老的。Redis 故障时也不会
    无限增长吃掉内存。

    域 scope（FMBY 批一 #10，借鉴其 Redis 按域分片）：``get/set/delete/
    exists`` 可带 ``domain=``——带域写入的键落 ``rb:d:<域>:<原键>``
    命名空间，读取先查新键、未命中回退旧的无域键（老键不迁移、靠
    TTL 自然消亡）；不带 ``domain`` 的调用与从前逐字节一致，老调用方
    语义不变。整域失效走 :meth:`invalidate_domain`。
    """

    #: 起步四域：浏览 / 条目 / 播放 / 资源（域只是键命名空间，不改变值语义；
    #: 新域只要是合法 slug 即可用，不必改这张表——表是给人看的清单）
    DOMAINS = ("browse", "items", "playback", "assets")

    # 修复 E-P1-2：内存缓存被多线程共享，dict 与 FIFO order 必须原子更新
    _memory_cache: dict = {}
    _memory_cache_order: list = []  # FIFO 顺序，配合淘汰
    _MEMORY_CACHE_MAX = 2000
    _memory_cache_lock = threading.Lock()  # 保护 _memory_cache / _memory_cache_order 的并发读写

    @staticmethod
    def _memory_get(key: str) -> Optional[str]:
        # 热路径：命中且未过期时不加锁，避免读多写少场景下的锁竞争
        entry = CacheManager._memory_cache.get(key)
        if entry is None:
            return None
        value, expire_at = entry
        import time as _time
        if expire_at is not None and _time.time() > expire_at:
            # 修复 E-P1-2：过期删除需加锁，并同步从 FIFO order 中移除，防止 order 无界增长
            with CacheManager._memory_cache_lock:
                # 双重检查：期间可能已被其它线程刷新或删除
                entry = CacheManager._memory_cache.get(key)
                if entry is None:
                    return None
                if entry[1] is not None and _time.time() > entry[1]:
                    CacheManager._memory_cache.pop(key, None)
                    try:
                        CacheManager._memory_cache_order.remove(key)
                    except ValueError:
                        pass
                    return None
                # 已被其它线程写入新值且未过期，直接返回最新值
                return entry[0]
        return value

    @staticmethod
    def _memory_set(key: str, value: str, ttl: int = 300) -> None:
        import time as _time
        expire_at = _time.time() + ttl if ttl and ttl > 0 else None
        # 修复 E-P1-2：淘汰 + 写入全程持锁，保证 dict 与 order 一致；
        # 临界区内只有内存操作、无 IO，锁粒度可控
        with CacheManager._memory_cache_lock:
            cache = CacheManager._memory_cache
            order = CacheManager._memory_cache_order
            if key not in cache:
                if len(cache) >= CacheManager._MEMORY_CACHE_MAX:
                    now = _time.time()
                    # 1) 批量清理过期条目，并同步过滤 order，防止 order 只增不减
                    expired = [k for k, (_, exp) in cache.items()
                               if exp is not None and now > exp]
                    if expired:
                        for k in expired:
                            cache.pop(k, None)
                        order[:] = [k for k in order if k in cache]
                    # 2) 仍超限则按 FIFO 淘汰；跳过已不在 cache 中的失效 order 条目，
                    #    只有真正删掉一个 key 才继续，避免 order.pop(0) 空转忙等
                    while len(cache) >= CacheManager._MEMORY_CACHE_MAX and order:
                        oldest = order.pop(0)
                        if oldest in cache:
                            del cache[oldest]
                # 仅新增 key 时追加 order；更新已有 key 不重复追加
                order.append(key)
            cache[key] = (value, expire_at)

    # ---------------- 域 scope（#10） ----------------

    @staticmethod
    def _validate_domain(domain: Optional[str]) -> Optional[str]:
        """域 slug 校验：None 原样返回；非法 slug 直接抛错（防打错域静默失效）。"""
        if domain is None:
            return None
        d = str(domain).strip().lower()
        if not d or not d.isascii() or not all(
                c.isalnum() or c in "_-" for c in d):
            raise ValueError(f"非法缓存域: {domain!r}")
        return d

    @staticmethod
    def _scoped(key: str, domain: Optional[str]) -> str:
        """逻辑键：带域时 ``d:<域>:<原键>``（Redis 后缀与内存回退共用同一形）。"""
        return f"d:{domain}:{key}" if domain else key

    @staticmethod
    def _memory_delete(key: str) -> bool:
        with CacheManager._memory_cache_lock:
            if key not in CacheManager._memory_cache:
                return False
            del CacheManager._memory_cache[key]
            try:
                CacheManager._memory_cache_order.remove(key)
            except ValueError:
                pass
            return True

    @staticmethod
    def get(key: str, *, domain: Optional[str] = None) -> Optional[str]:
        """获取缓存（Redis 熔断 / 故障时走内存）

        带 ``domain`` 时先查域内新键，未命中回退旧无域键（迁移期双读，
        老键靠 TTL 自然消亡）；不带 domain 与从前一致。
        """
        domain = CacheManager._validate_domain(domain)
        if domain is None:
            value = redis_call(lambda r: r.get(f"rb:{key}"), _MISSING)
            if value is not _MISSING:
                return value
            return CacheManager._memory_get(key)
        scoped = CacheManager._scoped(key, domain)
        value = redis_call(lambda r: r.get(f"rb:{scoped}"), _MISSING)
        if value is not _MISSING:
            if value is not None:
                return value
            # Redis 可达、新键未命中 → 回退旧键
            legacy = redis_call(lambda r: r.get(f"rb:{key}"), _MISSING)
            return legacy if legacy is not _MISSING else None
        # Redis 不可用 → 内存回退：同样先新键后旧键
        hit = CacheManager._memory_get(scoped)
        if hit is not None:
            return hit
        return CacheManager._memory_get(key)

    @staticmethod
    def set(key: str, value: str, ttl: int = 300, *,
            domain: Optional[str] = None) -> bool:
        """设置缓存。带 ``domain`` 时只写入域命名空间（新键）。"""
        domain = CacheManager._validate_domain(domain)
        logical = CacheManager._scoped(key, domain)
        ok = redis_call(lambda r: r.setex(f"rb:{logical}", ttl, value), _MISSING)
        if ok is not _MISSING:
            return ok
        CacheManager._memory_set(logical, value, ttl)
        return True

    @staticmethod
    def delete(key: str, *, domain: Optional[str] = None) -> bool:
        """删除缓存。带 ``domain`` 时域内新键与旧无域键一并删（读路径
        双读，删除必须把两边都清掉，否则旧键会把已删的值又读回来）。"""
        domain = CacheManager._validate_domain(domain)
        if domain is None:
            res = redis_call(lambda r: r.delete(f"rb:{key}") > 0, _MISSING)
            if res is not _MISSING:
                return res
            if key in CacheManager._memory_cache:
                del CacheManager._memory_cache[key]
                try:
                    CacheManager._memory_cache_order.remove(key)
                except ValueError:
                    pass
            return True
        scoped = CacheManager._scoped(key, domain)
        res = redis_call(lambda r: r.delete(f"rb:{scoped}", f"rb:{key}") > 0,
                         _MISSING)
        if res is not _MISSING:
            return res
        CacheManager._memory_delete(scoped)
        CacheManager._memory_delete(key)
        return True

    @staticmethod
    def delete_pattern(pattern: str) -> int:
        """批量删除缓存"""
        def _del(r):
            keys = r.keys(f"rb:{pattern}")
            return r.delete(*keys) if keys else 0
        res = redis_call(_del, _MISSING)
        if res is not _MISSING:
            return res
        # 内存缓存不支持模式匹配
        return 0

    @staticmethod
    def exists(key: str, *, domain: Optional[str] = None) -> bool:
        """检查缓存是否存在（带 domain 时新键、旧键任一存在即 True）。"""
        domain = CacheManager._validate_domain(domain)
        if domain is None:
            res = redis_call(lambda r: r.exists(f"rb:{key}") > 0, _MISSING)
            if res is not _MISSING:
                return res
            return CacheManager._memory_get(key) is not None
        scoped = CacheManager._scoped(key, domain)
        res = redis_call(
            lambda r: (r.exists(f"rb:{scoped}") or r.exists(f"rb:{key}")) > 0,
            _MISSING)
        if res is not _MISSING:
            return res
        return (CacheManager._memory_get(scoped) is not None
                or CacheManager._memory_get(key) is not None)

    @staticmethod
    def invalidate_domain(domain: str) -> int:
        """按域失效：删掉该域全部带 scope 的键，返回删除数。

        只动 ``rb:d:<域>:*`` 命名空间——无域旧键与其它域不受影响。
        Redis 与内存回退都清（同一进程可能在 Redis 故障窗口攒下内存条目）；
        Redis 不可用时返回内存侧的删除数（best-effort，与 delete_pattern 同口径）。
        """
        domain = CacheManager._validate_domain(domain)
        if domain is None:
            raise ValueError("domain 不能为空")
        prefix = f"d:{domain}:"
        count = 0

        def _del(r):
            keys = r.keys(f"rb:{prefix}*")
            return r.delete(*keys) if keys else 0

        res = redis_call(_del, _MISSING)
        if res is not _MISSING:
            count += int(res or 0)
        with CacheManager._memory_cache_lock:
            doomed = [k for k in CacheManager._memory_cache
                      if k.startswith(prefix)]
            for k in doomed:
                del CacheManager._memory_cache[k]
                try:
                    CacheManager._memory_cache_order.remove(k)
                except ValueError:
                    pass
            count += len(doomed)
        return count


cache = CacheManager()


# ==================== 导入所有模型 ====================
# 这里将导入所有统一的模型，稍后创建


# ==================== 数据库会话 ====================
def get_db() -> Session:
    """获取数据库会话（依赖注入用）"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _dialect_col_spec(col_type: str, default: str) -> tuple[str, str]:
    """把迁移清单里的列类型/默认值转成当前方言合法的写法。

    清单里的 ``DATETIME`` / ``BOOLEAN ... DEFAULT 0`` 是 SQLite 口径：
    - PostgreSQL 没有 ``DATETIME`` 类型（叫 ``TIMESTAMP``）；
    - PG 不接受整数做布尔列的默认值（``DEFAULT 0`` 直接报错），必须写 ``TRUE``/``FALSE``。
    MySQL 两者都认，不用转。SQLite 保持原样。
    """
    dialect = engine.dialect.name
    if dialect == "postgresql":
        if col_type == "DATETIME":
            col_type = "TIMESTAMP"
        if col_type == "BOOLEAN":
            if default == "0":
                default = "FALSE"
            elif default == "1":
                default = "TRUE"
    return col_type, default


def _auto_migrate():
    """轻量自动迁移：为已有表补充新增列（SQLite/MySQL/PG 通用）

    create_all 只建新表不改旧表，这里用 ALTER TABLE ADD COLUMN 补齐新增字段。
    幂等：列已存在时跳过。

    **必须用列表，不能用 {表名: 列} 的字典**：同一张表在不同版本各自加过列
    （``registration_codes`` 有 v2.5.6 与 v2.6.20 两批），字典字面量里后一个键会
    静默覆盖前一个，历史列就永远补不上——升级上来的库会在查询时报
    ``no such column``（全站 500）。列表则按出现顺序逐条追加，重复表名不会互相吞掉。
    """
    from sqlalchemy import text, inspect

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    migrations: list[tuple[str, list[tuple[str, str, str]]]] = [
        ("web_users", [
            ("points", "INTEGER", "0"),
            # v2.26.0 管理员角色（super / operator / viewer）：老库补列后为 NULL，
            # 按 super 处理 → 升级前后权限完全一致（见 backend/admin_roles.py）
            ("admin_role", "VARCHAR(20)", "NULL"),
            # v2.44.0 注册渠道归因（admin/code/invitation/open）：老用户补列后为 NULL，
            # 按「未记录」显示——不硬猜成 open（见 backend/register_channel.py）
            ("register_channel", "VARCHAR(20)", "NULL"),
            # 用户级转码开关（Linger 借鉴）：默认允许，老用户补列后为 True
            ("enable_video_transcoding", "BOOLEAN", "1"),
            # v2.57.0 公益服模块1：公益身份与资格。老库补列后 is_welfare=0（非公益）、
            # 其余为 NULL（无公益资格），行为与升级前完全一致。
            ("is_welfare", "BOOLEAN", "0"),
            ("welfare_expires_at", "DATETIME", "NULL"),
            ("welfare_grant_channel", "VARCHAR(20)", "NULL"),
            ("welfare_granted_at", "DATETIME", "NULL"),
            # P1 统一货币体系：会员经验与缓存等级。老用户补列后 xp=0/level=1（初幕），
            # 与升级前行为一致（此前无等级概念）。
            ("member_xp", "INTEGER", "0"),
            ("member_level", "INTEGER", "1"),
            # P2/P3 审查修复：token 版本号（改密后旧 JWT 作废）。老库补列默认 0，
            # 存量 token 的 tv 也是 0，对得上，不会误杀。
            ("token_version", "INTEGER", "0"),
            # C1 活力值（竞品借鉴）：老库补列默认满值 14，升级后无人被误拦观影。
            ("vitality", "INTEGER", "14"),
            # 探险世界 · 时长榜隐私开关：老库补列默认上榜，与升级前行为一致。
            ("show_on_leaderboard", "BOOLEAN", "1"),
        ]),
        # v2.44.0 邀请码白名单（内测码 / 渠道码）：NULL / 空串都按「不限」处理，
        # 所以存量邀请码行为升级前后完全一致（见 backend/api/invitation.py）
        ("invitation_codes", [
            ("whitelist", "TEXT", "''"),
        ]),
        # v2.6.15 通知历史记录真实投递结果：邮件/TG 发送失败必须留下原因，
        # 而不是像以前那样一律写成 status="sent"
        ("notification_history", [
            ("error_message", "TEXT", "NULL"),
        ]),
        # v2.5.6 卡码体系：注册码 → 注册/续期/白名单/诱饵/指名
        # v2.6.20 多服运营：卡码归属到某个服（见 backend/realms.py）
        ("registration_codes", [
            ("code_type", "INTEGER", "1"),
            ("days", "INTEGER", "30"),
            # 诱饵码不建列：靠 HONEY- 前缀识别（见 backend/codes.is_honeypot），零表结构改动
            ("target_username", "VARCHAR(50)", "NULL"),
            ("source", "VARCHAR(20)", "'admin'"),
            ("realm_id", "INTEGER", "NULL"),
            # M1 统一码系统：并入兑换码的奖励类型
            ("reward_type", "VARCHAR(20)", "'subscription'"),
            ("points_value", "INTEGER", "0"),
            ("discount_pct", "INTEGER", "0"),
        ]),
        # v2.6.4 媒体库刮削策略、虚拟媒体库与搜索增强；v2.6.5 增加 115 账号绑定
        # v2.6.6 增加存储挂载绑定（storage_mounts）：媒体库的内容来源
        # v2.6.20 多节点：node_id 指定由哪台 EA 负责这个库（NULL = 所有节点可见）
        ("emby_libraries", [
            ("scrape_policy", "VARCHAR(20)", "'missing_only'"),
            ("is_virtual", "BOOLEAN", "0"),
            ("platform", "VARCHAR(30)", "NULL"),
            ("account_115_id", "INTEGER", "NULL"),
            ("mount_ids", "TEXT", "''"),
            # v2.43.0 路径与存储后端分离：与 paths 逐条对应的后端列（local/rclone/115）。
            # 老库补列后为 ''，界面按「挂载类型 + 路径前缀」现推，行为与升级前一致。
            ("storage_backends", "TEXT", "''"),
            ("node_id", "INTEGER", "NULL"),
            ("realm_id", "INTEGER", "NULL"),
            # 本轮扫描起始时刻：与 updated_at 分开，避免被进度刷盘顶掉
            # （见 models.Library.scan_started_at 的说明）
            ("scan_started_at", "DATETIME", "NULL"),
            # v2.22.0 最近一次扫描结果：老库补列后为 NULL，等价于「尚未扫描」，
            # 不改变原有「只看 is_scanning / last_scan_at」的行为。
            ("scan_status", "VARCHAR(20)", "NULL"),
            ("scan_stats", "TEXT", "NULL"),
            ("scan_error", "VARCHAR(500)", "NULL"),
            # v2.27.0 扫描进行中的进度快照：老库补列后为 NULL（= 当前没有进度可看），
            # 不改动任何既有行为；只有真正开始扫描时才会被写入，结束即清空。
            ("scan_progress", "TEXT", "NULL"),
            # 媒体库封面由管理员直传，扫描/刮削不得覆盖；只保存图片目录下的相对路径。
            ("cover_path", "VARCHAR(500)", "NULL"),
            # 封面自动生成样式与文字（老库补列后为 NULL = 未启用，仍用直传的 cover_path）。
            # template: poster（海报拼贴）/ visual（主视觉）/ filmstrip（胶片带）
            ("cover_template", "VARCHAR(20)", "NULL"),
            # 标题/副标题支持 {library} {type} {year} 三个变量，只渲染纯文本
            ("cover_title", "VARCHAR(100)", "NULL"),
            ("cover_subtitle", "VARCHAR(100)", "NULL"),
            # v2.43.1 新片入库后自动重生成封面；老库补列后为 0（关闭），行为与升级前一致
            ("cover_auto_regen", "BOOLEAN", "0"),
            # v2.44.0 每库扫描开关；老库补列后分别为 1（增量）与 1（实时监听），
            # 即与升级前行为一致：本来就是有增量、有追新的
            ("incremental_scan", "BOOLEAN", "1"),
            ("fs_watch", "BOOLEAN", "1"),
        ]),
        # v2.6.20 多节点：EA 用 node_key 认领自己那条服务器记录；服务器归属到某个服
        ("remote_servers", [
            ("node_key", "VARCHAR(60)", "NULL"),
            ("realm_id", "INTEGER", "NULL"),
        ]),
        # v2.7.0 公益服：一个服可以免费开放（access_mode=free，不需要订阅就能看），
        # 并带上自己的规则文案与下载策略。老库补列时默认 paid/允许下载 → 行为不变。
        ("server_realms", [
            ("access_mode", "VARCHAR(10)", "'paid'"),
            ("access_note", "VARCHAR(500)", "''"),
            ("allow_download", "BOOLEAN", "NULL"),
        ]),
        # v2.6.20 多服运营：订阅、套餐、卡码、求片、挂载都归属到某个服
        ("subscription_plans", [
            ("realm_id", "INTEGER", "NULL"),
            # P2 统一货币体系：套餐积分价。老库补列后为 NULL（不支持积分购买），与升级前行为一致。
            ("points_price", "NUMERIC(10, 2)", "NULL"),
        ]),
        # 订阅分档权益表（PR-1）：与上一条同表分开追加（本函数的列表纪律）。
        # 老库补列后为 NULL（未声明任何权益），全部权益回退现行全局配置，
        # 行为与升级前完全一致（判定单口见 backend/entitlements.py）。
        ("subscription_plans", [
            ("entitlements", "JSON", "NULL"),
        ]),
        # P4 会员等级订阅折扣：老库补列后为 0（无折扣），与升级前行为一致；
        # migrate_discount_pct 会按等级回填默认值。
        ("member_levels", [
            ("discount_pct", "INTEGER", "0"),
        ]),
        ("user_subscriptions", [
            ("realm_id", "INTEGER", "NULL"),
        ]),
        ("storage_mounts", [
            ("realm_id", "INTEGER", "NULL"),
            # FMBY 挂载模型地基（清单第 1 项）：五能力位声明列，老库补列
            # 后为 0（未声明），回填按 mount_type 派生；只声明、不执法。
            ("can_list", "BOOLEAN", "0"),
            ("can_random_read", "BOOLEAN", "0"),
            ("can_read_sidecar", "BOOLEAN", "0"),
            ("can_generate_play_target", "BOOLEAN", "0"),
            ("can_refresh_credentials", "BOOLEAN", "0"),
        ]),
        # v2.6.19 求片可以转交外部服务（MoviePilot 订阅 / qBittorrent 加种）：
        # 把「交给谁、成没成、为什么没成」落库，否则面板只能显示一句模糊的失败
        # v2.6.20 多服运营：求片也归属到某个服
        ("movie_requests", [
            ("realm_id", "INTEGER", "NULL"),
            ("push_target", "VARCHAR(20)", "NULL"),
            ("push_status", "VARCHAR(20)", "NULL"),
            ("push_message", "VARCHAR(300)", "NULL"),
            ("pushed_at", "DATETIME", "NULL"),
            # v2 求片附议数（新表 movie_request_votes 由 create_all 建）
            ("vote_count", "INTEGER", "0"),
        ]),
        # v2.10.0 优惠券：订单上快照「原价 / 优惠金额 / 核销记录」。
        # 退款与对账要能解释「当时到底按多少钱算的」——套餐改价之后不能按现价重算。
        # （新表 coupon_codes / coupon_usages 由 create_all 建，不在此列）
        ("recharge_orders", [
            ("list_price", "NUMERIC(10, 2)", "0"),
            ("discount_amount", "NUMERIC(10, 2)", "0"),
            ("coupon_usage_id", "INTEGER", "NULL"),
        ]),
        ("subscription_orders", [
            ("list_price", "NUMERIC(10, 2)", "0"),
            ("discount_amount", "NUMERIC(10, 2)", "0"),
            ("coupon_usage_id", "INTEGER", "NULL"),
        ]),
        ("emby_items", [
            ("imdb_id", "VARCHAR(20)", "NULL"),
            ("aliases", "TEXT", "''"),
            ("platforms", "TEXT", "''"),
            ("last_probed_at", "DATETIME", "NULL"),
            ("last_scraped_at", "DATETIME", "NULL"),
            ("repair_requested_at", "DATETIME", "NULL"),
            # v2.39.0 两阶段扫描：老库补列后 probe_status='pending'，但已有探测数据的
            # 条目会被后台 worker 按 needs_probe() 语义直接标 done，不会重复探测。
            ("probe_status", "VARCHAR(20)", "'pending'"),
            ("probe_priority", "INTEGER", "0"),
            ("probe_attempts", "INTEGER", "0"),
            ("probe_next_retry_at", "DATETIME", "NULL"),
            # v2.53 探测 worker 重构：抢单租约（stale reclaim）+ 最近失败原因。
            # 老库补列后为 NULL：历史 probing 行没有租约时刻，启动 reclaim 一律放回 pending。
            ("probe_claimed_at", "DATETIME", "NULL"),
            ("probe_last_error", "VARCHAR(255)", "NULL"),
            # v2.40.0 补全 worker 重试：老库补列后 enrich_attempts=0，
            # enrich_next_retry_at=NULL（可立即重试，由 worker 按退避调度）。
            ("enrich_attempts", "INTEGER", "0"),
            ("enrich_next_retry_at", "DATETIME", "NULL"),
            # v2.42.9 claim 租约：老库补列后为 NULL（历史 enriching 行没有抢单时刻，
            # janitor 用 date_modified 近似判定超时，不会误杀正在处理的条目）。
            ("enrich_claimed_at", "DATETIME", "NULL"),
            # v2.54 原子认领令牌：老库补列后为 NULL（历史行无令牌，不影响判定）。
            ("enrich_claim_token", "VARCHAR(32)", "NULL"),
            # WS1/T1：最近失败原因 + failed 自动回捞轮数。老库补列后为 NULL/0，
            # 历史 failed 行无错误原文、回捞轮数从 0 起算，行为与升级前一致。
            ("enrich_last_error", "VARCHAR(255)", "NULL"),
            ("enrich_failed_reschedules", "INTEGER", "0"),
            # Step 4 补强：none 自动回捞轮数（列模式仿 enrich_failed_reschedules）。
            # 老库补列后为 0，历史 none 行从 0 起算；达 enrich_none_max_reschedules
            # 上限后永久保留 done，不再被 janitor 自动捞回。
            ("enrich_none_reschedules", "INTEGER", "0"),
            # v2.42.9 处方 4 调度优先级：repair 置顶 100 / 重试未匹配 50，0=默认。
            ("enrich_priority", "INTEGER", "0"),
            # 元数据来源标记：老库补列后为 NULL（历史数据未标记），
            # 由刮削时重新写入；查"刮没刮干净"不再依赖 last_scraped_at 反推。
            ("metadata_source", "VARCHAR(20)", "NULL"),
            # v2.48.0 软删除：老库补列后全为 NULL = 全部可见，行为与以前完全一致。
            # 回滚：MEDIA_SOFT_DELETE=0 启动时会把已隐藏的行清空（见 _resurrect_soft_deleted）。
            ("deleted_at", "DATETIME", "NULL"),
            # v2.49.0 文件名元数据：扫描时从文件名解析分辨率/发行来源（零 Drive 调用）。
            # video_codec / audio_codec 列已存在（探测器时代留下），复用即可。
            ("video_resolution", "VARCHAR(10)", "NULL"),
            ("media_source", "VARCHAR(30)", "NULL"),
            # 手动识别 P3：元数据锁定（Emby 式）。老库补列后默认 0 = 未锁定，
            # 行为与升级前完全一致；锁定只挡 enrich 自动抢单，不挡手动绑定/重刮。
            ("metadata_locked", "BOOLEAN", "0"),
            # v2.51.0 演员刮削：TMDB details 的 origin_country / spoken_languages
            # 落库，供 EA 详情页 Countries / Languages 用。
            # 老库补列后为空串 = 以前的 []，行为与升级前一致。
            ("countries", "TEXT", "''"),
            ("languages", "TEXT", "''"),
        ]),
        # 媒体流逐流细节：客户端「媒体信息」页要显示帧率/动态范围/位深/采样率等，
        # 缺了详情页只剩编码与码率几行（对比其它 Emby 服务端就很空）。
        ("emby_items", [
            ("drive_file_id", "VARCHAR(64)", "NULL"),
            # 元数据片长（TMDB/豆瓣），探测前的 RunTimeTicks 回退；老库补列后为 0 = 未知
            ("metadata_runtime_ticks", "BIGINT", "0"),
            # StrmAssistant #4 多版本合并：被合并条目的 merged_into_id 指向主记录
            ("merged_into_id", "INTEGER", "NULL"),
        ]),
        ("emby_media_streams", [
            ("frame_rate", "VARCHAR(20)", "NULL"),
            ("video_range", "VARCHAR(20)", "NULL"),
            ("profile", "VARCHAR(30)", "NULL"),
            ("level", "VARCHAR(30)", "NULL"),
            ("pixel_format", "VARCHAR(30)", "NULL"),
            ("aspect_ratio", "VARCHAR(20)", "NULL"),
            ("bit_depth", "INTEGER", "NULL"),
            ("sample_rate", "INTEGER", "NULL"),
            ("channel_layout", "VARCHAR(30)", "NULL"),
            ("sample_format", "VARCHAR(20)", "NULL"),
        ]),
        # StrmAssistant #9 演职人员增强：person_tmdb_id 供刷新演员详情用
        ("emby_people", [
            ("person_tmdb_id", "VARCHAR(32)", "NULL"),
        ]),
        # P3 兑换码折扣类型：老库补 discount_pct 列（默认 0，老兑换码不受影响）；
        # 新表 exchange_discount_credits 由 create_all 建，不在此列
        ("exchange_codes", [
            ("discount_pct", "INTEGER", "0"),
        ]),
        # P1 审查修复：红包幂等键（防 TG poller 重放双花）。老库补列后为 NULL，
        # 唯一约束只对将来带键的新包生效，存量包行为不变。
        ("red_packets", [
            ("idempotency_key", "VARCHAR(64)", "NULL"),
        ]),
        # 系统告警（事件型规则）：AlertRule 表扩展事件类型/接收人/冷却列。
        # 老库补列后 event_type/recipients 为 NULL（= legacy 指标型规则，行为不变），
        # cooldown_sec 默认 3600（与模型 default 一致）。
        ("alert_rules", [
            ("event_type", "VARCHAR(50)", "NULL"),
            ("recipients", "TEXT", "NULL"),
            ("cooldown_sec", "INTEGER", "3600"),
        ]),
        # R16 P1-2/P2-3：告警记录加送达标记 + 确认时间（老库补列后
        # notified_ok=0（未确认送达），acknowledged_at=NULL，行为与升级前一致）
        ("alerts", [
            ("notified_ok", "BOOLEAN", "0"),
            ("acknowledged_at", "DATETIME", "NULL"),
        ]),
        # Q3 同剧治理：emby_items 加 tmdb_loose_repair_at（宽松重识别只跑一次，
        # 防无限重试）。老库补列后为 NULL（= 还没跑过），行为与升级前一致。
        ("emby_items", [
            ("tmdb_loose_repair_at", "DATETIME", "NULL"),
        ]),
        # 发现页实时动态流（2026-10-17）：panel_activity 加事件类型/库名/归属服。
        # 老库补列后 event_type='system'、realm_id=NULL（= 全局事件，所有服可见），
        # 行为与升级前（纯文案 feed）一致。
        ("panel_activity", [
            ("event_type", "VARCHAR(20)", "'system'"),
            ("library", "VARCHAR(200)", "NULL"),
            ("realm_id", "INTEGER", "NULL"),
        ]),
        # AI 配额跨午夜退款漂移修复（r51 观察项）：ai_refund_log 加 refunded 标记。
        # 老库补列后 DEFAULT TRUE：升级前的行都是在退款时写入的「已退」标记，
        # 语义与升级前完全一致（升级后重放旧 request_id 是空操作，不会再退一次）；
        # 升级后 consume 在扣费时写 refunded=0 的归属行，退款时原子认领翻成 1。
        ("ai_refund_log", [
            ("refunded", "BOOLEAN", "1"),
        ]),
        # Drive 链接分享自动补开（2026-10-19 播放事故治本；r57 补上 #1127 漏掉的迁移）：
        # emby_items 加 drive_share_ok / drive_share_checked_at。老库补列后为
        # NULL（= 尚未确认），enrich janitor 巡检会把未确认行捞起补开；
        # 此前无此功能，行为与升级前一致。
        ("emby_items", [
            ("drive_share_ok", "BOOLEAN", "NULL"),
            ("drive_share_checked_at", "DATETIME", "NULL"),
        ]),
        # 身份锚（清单第 8 项）：emby_items 加 source_id / provider_file_id。
        # 老库补列后为 NULL（= 尚未锚定，播放走现行 mount:// 解析并计回落）；
        # 新入库只写扫描时能确定的，行为与升级前一致。
        ("emby_items", [
            ("source_id", "INTEGER", "NULL"),
            ("provider_file_id", "VARCHAR(128)", "NULL"),
        ]),
        # 缺失宽限（2026-10-24，FMBY 学习批一 #6）：emby_items 加
        # missing_since / missing_count。老库补列后 missing_count=0
        # （= 从未缺失），与升级前行为衔接：下一轮清理起开始累计。
        ("emby_items", [
            ("missing_since", "DATETIME", "NULL"),
            ("missing_count", "INTEGER", "0"),
        ]),
        # 拼音搜索派生字段（2026-10-24，FMBY 学习批二 #16）：emby_items 加
        # pinyin_full / pinyin_initials（由 name 生成的全拼/首字母紧凑键）。
        # 老库补列后为 NULL：搜索拼音层对 NULL 天然不命中，行为与升级前
        # 一致；存量由 scripts/backfill_pinyin_search_fields.py 回填，新
        # 写入走 models 的落库钩子自动生成。
        ("emby_items", [
            ("pinyin_full", "TEXT", "NULL"),
            ("pinyin_initials", "TEXT", "NULL"),
        ]),
        # 扫描检查点专属落点（2026-10-24，FMBY 学习批一 #7）：emby_scan_runs
        # 加 state_json（定向扫描的续扫范围）。老库补列后为 NULL，
        # interrupted_resume_prefixes 读不到就回退 stats 里的 resume_scope
        # （老流水的既有落点），行为与升级前完全一致。
        ("emby_scan_runs", [
            ("state_json", "TEXT", "NULL"),
        ]),
        # 目录到期调度（2026-10-24，FMBY 学习批二 #12）：emby_scan_dirs 加
        # next_due_at / last_decision。老库补列后为 NULL（= 没有排期，
        # 按「已到期」处理：走完整核对后才开始排期），行为与升级前一致。
        ("emby_scan_dirs", [
            ("next_due_at", "DATETIME", "NULL"),
            ("last_decision", "VARCHAR(32)", "NULL"),
        ]),
        # 进度双记（2026-10-24，FMBY 学习批一 #4）：emby_user_media_data 加
        # max_position_ticks（历史最远进度，与 playback_position_ticks 并存）。
        # 老库补列后为 NULL（= 还没双记过），读/算回退 playback_position_ticks，
        # 行为与升级前一致；新上报经 _apply_play_progress 只增不减地维护。
        ("emby_user_media_data", [
            ("max_position_ticks", "BIGINT", "NULL"),
        ]),
        # 按变体取流（2026-10-24，FMBY 学习批二 #11 PR-2）：起播事件表加
        # variant（实际走的变体名）。老库补列后为 NULL（= 老事件/无变体
        # 调用），聚合读口仍按 provider × mount_id 分组，variant 只作
        # 筛选维度，行为与升级前一致。
        ("emby_play_start_events", [
            ("variant", "VARCHAR(30)", "NULL"),
        ]),
    ]

    _newly_added_columns: list[tuple[str, str]] = []
    for table, columns in migrations:
        if table not in existing_tables:
            continue
        existing_cols = {c["name"] for c in inspector.get_columns(table)}
        with engine.begin() as conn:
            for col_name, col_type, default in columns:
                if col_name not in existing_cols:
                    col_type, default = _dialect_col_spec(col_type, default)
                    conn.execute(text(
                        f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type} DEFAULT {default}"
                    ))
                    print(f"  🔧 已迁移: {table}.{col_name} ({col_type})")
                    _newly_added_columns.append((table, col_name))

    # R16 P2-2：alert_rules.event_type 唯一索引（老库 create_all 不回填已建的表，
    # 用 CREATE UNIQUE INDEX 补上；有重复行时记 warning 不崩启动，由下次播种的
    # IntegrityError 路径兜底）。PG/SQLite 的 UNIQUE 都允许多个 NULL，
    # legacy 指标型规则不受影响。
    if "alert_rules" in existing_tables:
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_alert_rules_event_type "
                    "ON alert_rules (event_type)"
                ))
            print("  🔧 已迁移: alert_rules 唯一索引 (event_type)")
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠️ alert_rules event_type 唯一索引创建跳过: {exc}")

    # R49：lottery_rounds 同群进行中（status='open'）唯一索引。
    # create_lottery_round 的"同群已有进行中则拒绝"此前只在端点层读-查-写，
    # 两管理员同群并发建活动可产生两个 open 轮次。部分唯一索引与
    # get_active_round（只查 status='open'）口径对齐；老库有重复行时记
    # warning 不崩启动（与上面 uq_alert_rules_event_type 同一口径），
    # 由 create 端的 IntegrityError 兜底路径拦截新的双建。
    if "lottery_rounds" in existing_tables:
        try:
            with engine.begin() as conn:
                conn.execute(text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_lottery_rounds_open_chat "
                    "ON lottery_rounds (chat_id) WHERE status = 'open'"
                ))
            print("  🔧 已迁移: lottery_rounds 同群进行中唯一索引 (chat_id WHERE open)")
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠️ lottery_rounds 同群进行中唯一索引创建跳过: {exc}")

    _widen_code_column(existing_tables, inspector)
    _widen_bitrate_column(existing_tables, inspector)
    _fix_station_message_from_user_fk(existing_tables, inspector)
    _widen_stream_title_columns(existing_tables, inspector)
    _backfill_orm_columns(existing_tables)
    # v2.49.0：本次刚补上 video_resolution 列的老库，用文件名解析回填已有条目
    # （纯字符串解析，零 Drive 调用；只跑一次，下次启动列已存在不会再触发）
    if ("emby_items", "video_resolution") in _newly_added_columns:
        _backfill_filename_meta()
    _ensure_legacy_indexes(existing_tables)
    _ensure_lottery_g1_tables(existing_tables)
    _migrate_redpacket_claim_unique(existing_tables)
    _migrate_redpacket_idempotency_unique(existing_tables)
    _migrate_scan_run_single_running(existing_tables)
    _migrate_panel_unseal_fk(existing_tables)
    _migrate_exchange_discount_credit_fk(existing_tables)
    _relax_mount_credential_mount_id(existing_tables)
    _resurrect_soft_deleted(existing_tables)
    _ensure_default_realm()
    _hash_plain_emby_tokens(existing_tables)


def _backfill_filename_meta() -> None:
    """v2.49.0：老库补上 video_resolution 列后，用文件名解析回填已有条目。

    纯字符串解析，零 Drive 调用。只处理 file_path 非空且 video_resolution
    为空的行；已有探测数据的字段（非空的 video_codec / 非零的 width 等）
    不覆盖——文件名解析只是回退，不如 ffprobe 精确。
    分批提交；幂等（跑过一次后触发条件不再成立）。
    """
    from backend.emby_server import models as emby_models
    from backend.emby_server.filename_meta import parse_filename, resolution_to_wh

    MI = emby_models.MediaItem
    session = Session(bind=engine)
    try:
        # 先一次性拿 ID（避免“解析不到的行保持 NULL 被反复选中”的死循环）
        all_ids = [
            row[0] for row in session.query(MI.id).filter(
                MI.video_resolution.is_(None), MI.file_path.isnot(None)
            ).all()
        ]
        BATCH = 1000
        total = 0
        for i in range(0, len(all_ids), BATCH):
            chunk = all_ids[i:i + BATCH]
            rows = (
                session.query(
                    MI.id, MI.file_path, MI.video_codec, MI.audio_codec,
                    MI.media_source, MI.width, MI.height,
                )
                .filter(MI.id.in_(chunk))
                .all()
            )
            mappings = []
            for (row_id, file_path, vcodec, acodec, msource, width, height) in rows:
                name = (file_path or "").rsplit("/", 1)[-1]
                meta = parse_filename(name)
                m = {"id": row_id, "video_resolution": meta.get("resolution")}
                if not vcodec and meta.get("video_codec"):
                    m["video_codec"] = meta["video_codec"]
                if not acodec and meta.get("audio_codec"):
                    m["audio_codec"] = meta["audio_codec"]
                if not msource and meta.get("source"):
                    m["media_source"] = meta["source"]
                w, h = resolution_to_wh(meta.get("resolution"))
                if not width and w:
                    m["width"] = w
                if not height and h:
                    m["height"] = h
                mappings.append(m)
            session.bulk_update_mappings(MI, mappings)
            session.commit()
            total += len(mappings)
        if total:
            print(f"  🔧 已回填: emby_items 文件名元数据 {total} 条")
    finally:
        session.close()


def _hash_plain_emby_tokens(existing_tables: set) -> None:
    """P1 安全修复：emby_api_tokens.token 改存 SHA256 哈希。

    存量明文 token（40 位 token_hex(20) 或其他遗留格式）一次性哈希化。
    幂等：只处理长度 != 64 的行，已哈希的（64 位 hex）跳过，跑两次不坏。
    注意这是单向的：回滚代码版本不会恢复明文，旧客户端会话需重新登录。
    """
    import hashlib

    if "emby_api_tokens" not in existing_tables:
        return
    from sqlalchemy import text

    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT id, token FROM emby_api_tokens WHERE LENGTH(token) != 64")
        ).fetchall()
        for rid, tok in rows:
            hashed = hashlib.sha256(tok.encode("utf-8")).hexdigest()
            conn.execute(
                text("UPDATE emby_api_tokens SET token = :h WHERE id = :i"),
                {"h": hashed, "i": rid},
            )
        if rows:
            print(f"  🔧 已迁移: emby_api_tokens.token 哈希化 {len(rows)} 条")


# 给老库补索引（幂等）。create_all 只在建新表时建索引；升级上来的库表已存在，
# 这里按 inspector 显式补上。每条：(表, 索引名, 列, 需要先存在的列 or None, 迁移日志)。
# 顺序即执行顺序（与拆分前各 _ensure_*_index 的调用顺序一致）。
# **可回滚**：任一索引 ``DROP INDEX <name>`` 即可，无数据影响。
_LEGACY_INDEXES: tuple[tuple[str, ...], ...] = (
    # 两阶段扫描（v2.39.0）：worker 取待探测条目时走索引
    ("emby_items", "idx_item_probe", "probe_status, probe_priority, id", None,
     "  🔧 已迁移: emby_items.idx_item_probe（探测队列索引）"),
    # v2.53：(probe_status, probe_next_retry_at)——抢单的「到期」判定、reclaim 与
    # 进度接口的「重试中」计数都按它走，28 万级积压下不再全表扫
    ("emby_items", "idx_item_probe_retry", "probe_status, probe_next_retry_at", None,
     "  🔧 已迁移: emby_items.idx_item_probe_retry（探测退避索引）"),
    # v2.48.0：补全队列 _claim_batch 抢单是 WHERE enrich_status='pending' AND
    # enrich_next_retry_at<=now 再按 enrich_priority 排序，单列索引帮不上忙
    ("emby_items", "idx_item_enrich", "enrich_status, enrich_next_retry_at, enrich_priority", None,
     "  🔧 已迁移: emby_items.idx_item_enrich（补全队列复合索引）"),
    # 追新日历：否则用户打开追新日历就是一次全表扫
    ("emby_items", "idx_item_added", "date_added, item_type", None,
     "  🔧 已迁移: emby_items.idx_item_added（追新日历索引）"),
    # v2.48.0 软删除可见性：deleted_at IS NULL 与几乎所有查询的 library_id = ? 绑在一起，
    # deleted_at 跟在 library_id 后面，已下架的行在索引里就被跳过
    ("emby_items", "idx_item_lib_deleted", "library_id, deleted_at", None,
     "  🔧 已迁移: emby_items.idx_item_lib_deleted（软删除可见性索引）"),
    # v2.52.0：改名/移动检测按 drive_file_id 批量查条目，没有索引就是全表扫
    ("emby_items", "idx_item_drive_file_id", "drive_file_id", None,
     "  已迁移: emby_items.idx_item_drive_file_id"),
    # StrmAssistant 打磨 R2（PR #406）：get_alternate_versions() 按 merged_into_id 查版本，
    # 详情页每次打开都触发；models.py 的 index=True 只对 create_all 新建表生效，
    # 老库升级上来没有索引就是全表扫（40 万行约 0.5-2 秒/次，拖慢播放链路）
    ("emby_items", "idx_item_merged_into_id", "merged_into_id", None,
     "  已迁移: emby_items.idx_item_merged_into_id"),
    # StrmAssistant 打磨 R2（PR #406）：refresh_person_worker 每天做全表
    # GROUP BY (item_id, person_tmdb_id) 去重，无复合索引时是分钟级全表扫 + hash 聚合。
    # 等价 DDL：CREATE INDEX idx_person_item_tmdb ON emby_people (item_id, person_tmdb_id)
    # person_tmdb_id 列由上面的列迁移先补上；列不存在时静默跳过（防御式）
    ("emby_people", "idx_person_item_tmdb", "item_id, person_tmdb_id", "person_tmdb_id",
     "  已迁移: emby_people.idx_person_item_tmdb"),
    # /emby/Items 默认浏览部分索引：item_type 等值/IN + ORDER BY (sort_name, id)。
    # 22 万行生产实测：翻页与 COUNT 都是 Parallel Seq Scan + Sort。
    # 部分谓词 WHERE is_hidden=false AND deleted_at IS NULL 与查询条件一致
    # （deleted_at 由全局 ORM 钩子 _hide_soft_deleted 自动拼上），规划器可用。
    # models.py 的 MediaItem.__table_args__ 已声明同名索引（新库 create_all 自带），
    # 这里给升级上来的老库补上。回滚：DROP INDEX idx_item_browse_sort。
    ("emby_items", "idx_item_browse_sort", "item_type, sort_name, id", None,
     "  已迁移: emby_items.idx_item_browse_sort（/emby/Items 默认浏览部分索引）",
     "is_hidden = false AND deleted_at IS NULL"),
    # P0 修复（2026-10-17 压测实锤）：Recursive=true 无类型过滤时，翻页 ORDER BY
    # (sort_name, id) 与 COUNT 都是 Parallel Seq Scan。部分索引去掉 item_type 首列，
    # 专供该形状。models.py 的 MediaItem.__table_args__ 已声明同名索引（新库自带），
    # 这里给升级上来的老库补上。回滚：DROP INDEX idx_item_browse_sort_all。
    # 注意：生产库用 CREATE INDEX CONCURRENTLY 手工建（见 PR 描述），避免阻塞去纸条化写负载；
    # 本条只给未来新库/老库兜底（幂等按名跳过）。
    ("emby_items", "idx_item_browse_sort_all", "sort_name, id", None,
     "  已迁移: emby_items.idx_item_browse_sort_all（/emby/Items 无类型过滤浏览排序部分索引）",
     "is_hidden = false AND deleted_at IS NULL"),
    # 搜索 trigram GIN 索引（r7）：/emby/Items?SearchTerm=、/emby/Search/Hints、
    # 求片中心的 ILIKE 预筛都走这四列。生产实测搜索是 Parallel Seq Scan（cost≈3万，
    # '%a%' 命中 3.6 万行），pg_trgm 扩展可用且应用用户为 superuser。
    # 全索引而非部分索引：三处调用的谓词各不相同，全索引复用度最高。
    # GIN 构建用普通 CREATE INDEX（CONCURRENTLY 不能在事务块里跑），
    # 24 万行一次性构建在启动迁移时完成。
    ("emby_items", "idx_item_name_trgm", "name gin_trgm_ops", "name",
     "  已迁移: emby_items.idx_item_name_trgm（搜索 trigram GIN 索引）",
     None, "gin"),
    ("emby_items", "idx_item_original_title_trgm", "original_title gin_trgm_ops",
     "original_title",
     "  已迁移: emby_items.idx_item_original_title_trgm（搜索 trigram GIN 索引）",
     None, "gin"),
    ("emby_items", "idx_item_sort_name_trgm", "sort_name gin_trgm_ops", "sort_name",
     "  已迁移: emby_items.idx_item_sort_name_trgm（搜索 trigram GIN 索引）",
     None, "gin"),
    ("emby_items", "idx_item_aliases_trgm", "aliases gin_trgm_ops", "aliases",
     "  已迁移: emby_items.idx_item_aliases_trgm（搜索 trigram GIN 索引）",
     None, "gin"),
    # 拼音搜索字段的 trigram 索引（2026-10-24，批二 #16）：与上面四列同一
    # 基建、同一幂等口径，索引名与 models.__table_args__ 声明同名。
    ("emby_items", "idx_item_pinyin_full_trgm", "pinyin_full gin_trgm_ops",
     "pinyin_full",
     "  已迁移: emby_items.idx_item_pinyin_full_trgm（搜索 trigram GIN 索引）",
     None, "gin"),
    ("emby_items", "idx_item_pinyin_initials_trgm",
     "pinyin_initials gin_trgm_ops", "pinyin_initials",
     "  已迁移: emby_items.idx_item_pinyin_initials_trgm（搜索 trigram GIN 索引）",
     None, "gin"),
    # 补全进度统计（2026-10-23 压测实锤）：进度快照按 enrich_claimed_at >= 今天
    # 逐类型计数，无索引时每条都是 Parallel Seq Scan（cost 约 5.5 万、每分钟
    # 数十次）。等价 DDL：CREATE INDEX idx_item_enrich_claimed
    # ON emby_items (enrich_claimed_at)。回滚：DROP INDEX idx_item_enrich_claimed。
    ("emby_items", "idx_item_enrich_claimed", "enrich_claimed_at", "enrich_claimed_at",
     "  已迁移: emby_items.idx_item_enrich_claimed（补全进度统计索引）"),
    # 抢单去重聚合（同日实锤）：_claim_batch 子查询 GROUP BY coalesce(series_id,
    # id) 原走单列状态索引位图扫 7.8 万行（含已软删/已合并死行）。部分索引只含
    # 活行（约 2.4 万），INCLUDE 三列供 index-only 聚合；非 PG 方言把 INCLUDE
    # 列并进键列（覆盖语义等价）。等价 DDL：CREATE INDEX
    # idx_item_enrich_claim_cover ON emby_items (enrich_status,
    # enrich_next_retry_at) INCLUDE (series_id, date_added, library_id)
    # WHERE deleted_at IS NULL AND merged_into_id IS NULL。
    # 回滚：DROP INDEX idx_item_enrich_claim_cover。
    ("emby_items", "idx_item_enrich_claim_cover", "enrich_status, enrich_next_retry_at",
     None,
     "  已迁移: emby_items.idx_item_enrich_claim_cover（抢单去重聚合部分覆盖索引）",
     "deleted_at IS NULL AND merged_into_id IS NULL", None,
     "series_id, date_added, library_id"),
)


def _ensure_lottery_g1_tables(existing_tables: set) -> None:
    """群抽奖 G1：PG/MySQL 上显式建表（幂等），SQLite 跳过。

    幂等：表已存在则跳过；create 时用 checkfirst=True 双保险。
    SQLite 由 init_db 的 create_all 建表，此处直接返回。
    """
    dialect = engine.dialect.name
    if dialect not in ("postgresql", "mysql"):
        return
    from backend import models
    tables = (
        models.LotteryRound.__table__,
        models.LotteryRoundPrize.__table__,
        models.LotteryRoundEntry.__table__,
        models.LotteryRoundWinner.__table__,
        # 三重门·资格门：黑名单表
        models.LotteryBlacklist.__table__,
    )
    for tbl in tables:
        if tbl.name not in existing_tables:
            tbl.create(bind=engine, checkfirst=True)
            print(f"  🔧 已迁移: 新建表 {tbl.name}（群抽奖）")

    # 三重门·公信门：lottery_rounds 补 drand 列（老库）
    from sqlalchemy import inspect, text
    inspector = inspect(engine)
    cols = {c["name"] for c in inspector.get_columns("lottery_rounds")}
    for col_name, col_def in (
        ("drand_round", "BIGINT"),
        ("drand_randomness", "VARCHAR(128)"),
    ):
        if col_name not in cols:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE lottery_rounds ADD COLUMN {col_name} {col_def}"))
            print(f"  🔧 已迁移: lottery_rounds 加列 {col_name}（三重门·公信门）")

    # 抽奖类型：lottery_rounds 补 lottery_type / password_keyword 列（老库）
    cols = {c["name"] for c in inspector.get_columns("lottery_rounds")}
    for col_name, col_def in (
        ("lottery_type", "VARCHAR(20) DEFAULT 'button'"),
        ("password_keyword", "VARCHAR(100)"),
    ):
        if col_name not in cols:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE lottery_rounds ADD COLUMN {col_name} {col_def}"))
            print(f"  🔧 已迁移: lottery_rounds 加列 {col_name}（抽奖类型）")
    # 老库可能未回填默认值，将 NULL 的 lottery_type 统一置为 'button'
    with engine.begin() as conn:
        conn.execute(text("UPDATE lottery_rounds SET lottery_type = 'button' WHERE lottery_type IS NULL"))

    # 僵尸 drawing 回收：lottery_rounds 补 drawing_at 列（老库）
    cols = {c["name"] for c in inspector.get_columns("lottery_rounds")}
    if "drawing_at" not in cols:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE lottery_rounds ADD COLUMN drawing_at TIMESTAMP"))
        print("  🔧 已迁移: lottery_rounds 加列 drawing_at（僵尸 drawing 回收）")


def _ensure_index(existing_tables: set, table: str, name: str, columns: str,
                  required_column: Optional[str], message: str,
                  where: Optional[str] = None,
                  using: Optional[str] = None,
                  include: Optional[str] = None) -> None:
    """给老库补一条索引（幂等）：表不存在 / 依赖列不存在 / 索引已在 → 什么都不做

    ``where`` 为 None 时 DDL 与以前逐字节一致；给出时建部分索引
    （``CREATE INDEX ... WHERE <where>``）。

    ``using`` 为 None 时行为不变；给出时建 ``USING <using>`` 索引，
    且仅 PostgreSQL 支持，非 PostgreSQL 方言直接返回。

    ``include`` 为 None 时行为不变；给出时 PG 建覆盖索引
    （``CREATE INDEX ... ({columns}) INCLUDE ({include})``），其余方言
    把 include 列并进键列，覆盖语义等价。"""
    from sqlalchemy import inspect, text

    if using is not None and engine.dialect.name != "postgresql":
        return
    if table not in existing_tables:
        return
    inspector = inspect(engine)
    if required_column is not None:
        cols = {c["name"] for c in inspector.get_columns(table)}
        if required_column not in cols:
            return
    names = {ix["name"] for ix in inspector.get_indexes(table)}
    if name in names:
        return
    with engine.begin() as conn:
        if using is None:
            ddl_columns = columns
            include_clause = ""
            if include is not None:
                if engine.dialect.name == "postgresql":
                    include_clause = f" INCLUDE ({include})"
                else:
                    ddl_columns = f"{columns}, {include}"
            ddl = f"CREATE INDEX {name} ON {table} ({ddl_columns}){include_clause}"
            if where is not None:
                ddl += f" WHERE {where}"
            conn.execute(text(ddl))
        else:
            if where is None:
                conn.execute(text(f"CREATE INDEX {name} ON {table} USING {using} ({columns})"))
            else:
                conn.execute(text(f"CREATE INDEX {name} ON {table} USING {using} ({columns}) WHERE {where}"))
        print(message)


def _ensure_legacy_indexes(existing_tables: set) -> None:
    """按 :data:`_LEGACY_INDEXES` 逐条补索引（含 idx_item_merged_into_id、idx_person_item_tmdb 等）"""
    from sqlalchemy import text
    if engine.dialect.name == "postgresql":
        # trigram GIN 索引依赖 pg_trgm 扩展；IF NOT EXISTS 幂等。
        # 生产库应用用户为 superuser（已验证）；pg_trgm 自 PG13 起是 trusted 扩展，
        # 普通库属主有 CREATE 权限也能装。装不上也不拦启动（搜索回退全表扫，
        # 与没加索引时行为一致），只打警告。
        try:
            with engine.begin() as conn:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠️ pg_trgm 扩展安装跳过: {exc}")
        # pg_stat_statements：慢查询归因（2026-10-23 压测后补）。扩展本体在
        # 这里幂等装；统计视图要等 postgres 以 shared_preload_libraries 加载
        # 它才可查——仓库 docker-compose.yml 的 postgres 启动参数已带，
        # 非 compose 部署需运维自行加上。装不上不拦启动（与 pg_trgm 同款）。
        try:
            with engine.begin() as conn:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_stat_statements"))
        except Exception as exc:  # noqa: BLE001
            print(f"  ⚠️ pg_stat_statements 扩展安装跳过: {exc}")
    for spec in _LEGACY_INDEXES:
        _ensure_index(existing_tables, *spec)


def _migrate_redpacket_claim_unique(existing_tables: set) -> None:
    """red_packet_claims 加唯一约束 (packet_id, user_id)（幂等）

    并发重复领取的数据库级兜底：同一用户对同一红包只能有一条领取记录。
    先去重（同一对只保留 id 最早的一条），再加约束；
    SQLite 由 create_all 建表时自带约束，此处跳过。
    """
    from sqlalchemy import inspect, text

    if "red_packet_claims" not in existing_tables:
        return
    inspector = inspect(engine)
    existing = {c["name"] for c in inspector.get_unique_constraints("red_packet_claims")}
    # 唯一索引与唯一约束同名也会冲突，一并检查
    existing |= {ix["name"] for ix in inspector.get_indexes("red_packet_claims")
                 if ix.get("unique")}
    if "uq_rpc_packet_user" in existing:
        return
    dialect = engine.dialect.name
    if dialect not in ("postgresql", "mysql"):
        return
    with engine.begin() as conn:
        # 去重：同一 (packet_id, user_id) 只保留最早一条（其余是历史竞态脏数据，
        # 积分已在 PointsLog 落账，删领取行不影响余额）
        if dialect == "postgresql":
            conn.execute(text(
                "DELETE FROM red_packet_claims a USING red_packet_claims b "
                "WHERE a.id > b.id AND a.packet_id = b.packet_id AND a.user_id = b.user_id"
            ))
        else:
            conn.execute(text(
                "DELETE a FROM red_packet_claims a "
                "JOIN red_packet_claims b ON a.packet_id = b.packet_id "
                "AND a.user_id = b.user_id AND a.id > b.id"
            ))
        conn.execute(text(
            "ALTER TABLE red_packet_claims ADD CONSTRAINT uq_rpc_packet_user "
            "UNIQUE (packet_id, user_id)"
        ))
    print("  🔧 已迁移: red_packet_claims 唯一约束 (packet_id, user_id)")


def _migrate_redpacket_idempotency_unique(existing_tables: set) -> None:
    """red_packets 加唯一约束 idempotency_key（幂等，防 TG 重放双花）

    列由 _auto_migrate 补齐后，这里补唯一约束。SQLite 由 create_all 建表时
    自带约束，此处跳过；PG/MySQL 显式加。NULL 值不参与唯一性（各方言一致），
    老包 idempotency_key 全 NULL 不会冲突。
    """
    from sqlalchemy import inspect, text

    if "red_packets" not in existing_tables:
        return
    inspector = inspect(engine)
    existing = {c["name"] for c in inspector.get_unique_constraints("red_packets")}
    existing |= {ix["name"] for ix in inspector.get_indexes("red_packets")
                 if ix.get("unique")}
    if "uq_redpacket_idempotency" in existing:
        return
    dialect = engine.dialect.name
    if dialect not in ("postgresql", "mysql"):
        return
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE red_packets ADD CONSTRAINT uq_redpacket_idempotency "
            "UNIQUE (idempotency_key)"
        ))
    print("  🔧 已迁移: red_packets 唯一约束 (idempotency_key)")


def _migrate_scan_run_single_running(existing_tables: set) -> None:
    """emby_scan_runs 同库只许一条 running 流水（跨进程防重，FMBY 学习批一 #7）

    worker 与 EA 是两个进程：进程内互斥（scanner._acquire_scan）挡不住跨
    进程同扫一个库，最后一道闸落在库层——部分唯一索引
    ``(library_id) WHERE status = 'running'``，begin_scan 撞上它抛
    ScanInProgress。新库由模型 ``__table_args__`` 的同名索引经 create_all
    直接建，这里只管存量库。

    **安全序不能反**：
    1. 先收尾存量僵尸——同库多条 running 只留最新一条（id 最大 = 最近
       开跑），其余收尾成 interrupted（被打断，不是失败；与
       maintenance.close_stale_scan_runs 的收尾口径一致），只动 running
       行，跑过一次后每库至多一条、这步自然空转（幂等）；
    2. 再建索引——反过来 CREATE UNIQUE INDEX 会被存量重复行直接顶失败。
    索引创建失败也只记 warning 不崩启动（与 uq_alert_rules_event_type
    同口径）：那时跨进程防重退回进程内防线，行为不比升级前差。
    MySQL 不支持部分索引，跳过（同上退回进程内防线）。
    """
    from sqlalchemy import inspect, text

    if "emby_scan_runs" not in existing_tables:
        return
    if engine.dialect.name not in ("sqlite", "postgresql"):
        print("  ⚠️ 当前方言不支持部分唯一索引，emby_scan_runs 跨进程防重跳过（保留进程内防线）")
        return
    inspector = inspect(engine)
    names = {ix["name"] for ix in inspector.get_indexes("emby_scan_runs")}
    if "uq_scan_runs_one_running" in names:
        return

    # 第一步：收尾同库重复的 running 流水（这一步在建索引之前，顺序不能反）
    from backend.emby_server import models as emby_models

    session = Session(bind=engine)
    closed = 0
    try:
        # 按 (库, id 倒序) 取行：每库先出现的是 id 最大 = 最近开跑的一条，留它；
        # 其余都是更早的僵尸，收尾成 interrupted
        rows = (
            session.query(emby_models.ScanRun)
            .filter(emby_models.ScanRun.status == "running")
            .order_by(emby_models.ScanRun.library_id, emby_models.ScanRun.id.desc())
            .all()
        )
        seen: set = set()
        now = datetime.now()
        for run in rows:
            if run.library_id in seen:
                run.status = "interrupted"
                run.finished_at = now
                run.error = (run.error or "迁移收尾：同库多条运行中流水，只保留最新一条")[:500]
                closed += 1
            else:
                seen.add(run.library_id)
        if closed:
            session.commit()
            print(f"  🔧 已迁移: emby_scan_runs 收尾 {closed} 条同库重复的运行中流水（只留最新一条）")
    except Exception as exc:  # noqa: BLE001 — 收尾失败不许连累启动（协调员补强）
        # 与下方建索引失败同口径：只告警、跳过——跨进程防重退回进程内
        # 防线，行为不比升级前差；异常时 session 里可能有未提交的收尾
        # 改动，close 前先 rollback，别把半截状态留给连接池里的下个用户。
        session.rollback()
        print(f"  ⚠️ emby_scan_runs 僵尸流水收尾跳过: {exc}")
    finally:
        session.close()

    # 第二步：建部分唯一索引
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE UNIQUE INDEX uq_scan_runs_one_running "
                "ON emby_scan_runs (library_id) WHERE status = 'running'"
            ))
        print("  🔧 已迁移: emby_scan_runs 同库单运行唯一索引 (library_id WHERE status='running')")
    except Exception as exc:  # noqa: BLE001
        print(f"  ⚠️ emby_scan_runs 同库单运行唯一索引创建跳过: {exc}")


def _migrate_panel_unseal_fk(existing_tables: set) -> None:
    """panel_unseals.item_id 加外键 → emby_items.id（幂等，r20 P3）

    模型侧已声明 ForeignKey('emby_items.id', ondelete='CASCADE')：新装库由
    create_all 直接建约束；老库（panel_unseals 已存在）走这里补。
    SQLite 跳过（create_all 建表时自带约束）；PG/MySQL 显式加。
    ondelete=CASCADE：条目被物理回收（purge_expired）时拆封记录一并消失——
    unseal 只按 user_id 查、只在条目存在时有意义，级联无副作用。
    补约束前先清孤儿行（item_id 在 emby_items 里不存在的），否则 ADD CONSTRAINT 失败。
    """
    from sqlalchemy import inspect, text

    if "panel_unseals" not in existing_tables or "emby_items" not in existing_tables:
        return
    inspector = inspect(engine)
    existing = {fk.get("name") for fk in inspector.get_foreign_keys("panel_unseals")}
    existing |= {fk.get("name") for fk in inspector.get_foreign_keys("panel_unseals")
                 if (fk.get("constrained_columns") or []) == ["item_id"]}
    if "fk_panel_unseal_item" in existing:
        return
    dialect = engine.dialect.name
    if dialect not in ("postgresql", "mysql"):
        return
    with engine.begin() as conn:
        # 清孤儿行：item_id 指向已不存在的条目
        orphan = conn.execute(text(
            "DELETE FROM panel_unseals WHERE item_id NOT IN (SELECT id FROM emby_items)"
        )).rowcount
        if orphan:
            print(f"  🧹 panel_unseals 孤儿行已清理: {orphan}")
        conn.execute(text(
            "ALTER TABLE panel_unseals ADD CONSTRAINT fk_panel_unseal_item "
            "FOREIGN KEY (item_id) REFERENCES emby_items(id) ON DELETE CASCADE"
        ))
    print("  🔧 已迁移: panel_unseals 外键 item_id → emby_items.id (CASCADE)")


def _migrate_exchange_discount_credit_fk(existing_tables: set) -> None:
    """R32-F1：exchange_discount_credits.exchange_code_id 去外键化。

    统一码系统（M1）的 discount 型卡码核销（codes.redeem_code）写
    ``exchange_code_id=0``（来源是 registration_codes，不是 exchange_codes）；
    PG 强校验外键 → flush 时 IntegrityError → 核销接口 500（本地 SQLite
    不强制 FK，测不出来）。该列全仓只写不读（消费端只按 user_id/status/
    expires_at 查，不读 .exchange_code_id），去外键 + 放宽 NOT NULL 无语义影响。
    生产 PG 实查 FK 名：exchange_discount_credits_exchange_code_id_fkey。

    幂等：指向 exchange_codes 的 FK 已不存在且列已可空时直接返回；
    SQLite 跳过（不强制 FK，0 本来就能写；NOT NULL 由 0 满足）。
    """
    from sqlalchemy import text

    if "exchange_discount_credits" not in existing_tables:
        return
    if engine.dialect.name != "postgresql":
        return

    with engine.begin() as conn:
        fks = conn.execute(text(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid='exchange_discount_credits'::regclass AND contype='f' "
            "AND confrelid='exchange_codes'::regclass "
            "AND 'exchange_code_id' = ANY (SELECT attname FROM pg_attribute "
            "WHERE attrelid=conrelid AND attnum = ANY (conkey))"
        )).fetchall()
        for (conname,) in fks:
            safe = conname.replace('"', '""')
            conn.execute(text(
                f'ALTER TABLE exchange_discount_credits DROP CONSTRAINT "{safe}"'
            ))
            print(f"  🔧 已迁移: exchange_discount_credits 去外键 {safe}")
        is_nullable = conn.execute(text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name='exchange_discount_credits' "
            "AND column_name='exchange_code_id'"
        )).scalar()
        if is_nullable == "NO":
            conn.execute(text(
                "ALTER TABLE exchange_discount_credits "
                "ALTER COLUMN exchange_code_id DROP NOT NULL"
            ))
            print("  🔧 已迁移: exchange_discount_credits.exchange_code_id 放宽为可空")


def _relax_mount_credential_mount_id(existing_tables: set) -> None:
    """BYOS P1：mount_credentials.mount_id 放宽为可空（仅 PG 老库）。

    #1230 建表时 mount_id 是 NOT NULL；BYOS 绑定凭据行不附属任何
    挂载（mount_id=NULL），否则同一用户第二个绑定即撞（mount_id,
    scope, owner_user_id）唯一约束。NULL 行在 PG 唯一约束里互不
    相等，故挂载附属行的约束语义逐字不变。

    幂等：列已可空时直接返回；SQLite 跳过（新鲜库由 create_all 按
    模型建表；SQLite 不支持 DROP NOT NULL，且开发库可重建）。
    """
    from sqlalchemy import text

    if "mount_credentials" not in existing_tables:
        return
    if engine.dialect.name != "postgresql":
        return
    with engine.begin() as conn:
        is_nullable = conn.execute(text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name='mount_credentials' "
            "AND column_name='mount_id'"
        )).scalar()
        if is_nullable == "NO":
            conn.execute(text(
                "ALTER TABLE mount_credentials "
                "ALTER COLUMN mount_id DROP NOT NULL"
            ))
            print("  🔧 已迁移: mount_credentials.mount_id 放宽为可空（BYOS 绑定行）")


def _resurrect_soft_deleted(existing_tables: set) -> None:
    """回滚软删除：``MEDIA_SOFT_DELETE=0`` 时把已隐藏的条目放回来（幂等）

    关掉开关意味着清理阶段回到硬删，也就是「条目会真的消失」。如果不把已隐藏的行
    放回来，它们就会变成既不显示、也永远删不掉的僵尸行——所以开关关掉的**那一刻**
    就必须恢复成软删除上线前的可见状态，之后的删除才是真的删除。
    """
    from sqlalchemy import text

    from backend.emby_server.soft_delete import soft_delete_enabled

    if soft_delete_enabled() or "emby_items" not in existing_tables:
        return
    with engine.begin() as conn:
        restored = conn.execute(
            text("UPDATE emby_items SET deleted_at = NULL WHERE deleted_at IS NOT NULL")
        ).rowcount
    if restored:
        print(f"  🔧 软删除已关闭：{restored} 条隐藏条目已恢复可见")


def _all_metadata():
    """所有 ORM 元数据（不止本模块的 Base）

    ``backend.emby_server.models`` 自建了一个 Base（两种部署形态要能分开建表），
    它不在 ``Base.metadata`` 里。以前这份对账只看 ``Base.metadata``，于是
    **emby_* 的表从来没被兼底过**：每次给 ``emby_libraries`` 加新列，都必须同步手写一条
    ``_MISSING_COLUMNS``；漏了就直接报 “table emby_libraries has no column named …”
    （建表走的是 emby 的 Base，而那条 ALTER 只对已经存在的表生效）。
    改成两边都以 ORM 为准，忘了写也能自愈。
    """
    from backend.emby_server import models as emby_models

    return (Base.metadata, emby_models.Base.metadata)


def _backfill_orm_columns(existing_tables: set) -> None:
    """兜底：ORM 声明了、库里却没有的列，在这里补上（幂等）

    上面的清单靠人维护，漏一条就会让某个页面在**升级过的库**上莫名其妙 500
    （历史上真发生过：``movie_requests.realm_id`` 被同名键覆盖，管理后台首页直接 500）。
    这里以 ``Base.metadata`` 为准做一次对账，只补列、不删列、不改类型：
    新库不受影响，老库不会再出现「模型有这个字段、库里没有」的错位。
    """
    from sqlalchemy import text, inspect

    # 每次重新 inspect：上面的清单已经改过表结构，缓存的列信息会过时
    inspector = inspect(engine)
    seen_tables: set = set()
    for meta in _all_metadata():
        for table in meta.sorted_tables:
            if table.name not in existing_tables or table.name in seen_tables:
                continue
            seen_tables.add(table.name)
            existing_cols = {c["name"] for c in inspector.get_columns(table.name)}
            missing = [c for c in table.columns
                       if c.name not in existing_cols and not c.primary_key]
            if not missing:
                continue
            with engine.begin() as conn:
                for col in missing:
                    conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {_column_ddl(col)}"))
                    print(f"  🔧 已迁移: {table.name}.{col.name}（按模型补齐）")


def _column_ddl(col) -> str:
    """把 ORM 列编译成 ALTER TABLE 片段：类型 + 字面量默认值（没有就留空 = 可空）"""
    ddl = f"{col.name} {col.type.compile(engine.dialect)}"
    arg = getattr(getattr(col, "default", None), "arg", None)
    if arg is None or callable(arg):
        return ddl
    if isinstance(arg, bool):
        # PG 不接受整数做布尔默认值（DEFAULT 1 直接报错），必须写 TRUE/FALSE；
        # SQLite/MySQL 用 1/0 也认。
        if engine.dialect.name == "postgresql":
            literal = "TRUE" if arg else "FALSE"
        else:
            literal = "1" if arg else "0"
    elif isinstance(arg, (int, float)):
        literal = str(arg)
    elif isinstance(arg, str):
        literal = "'" + arg.replace("'", "''") + "'"
    else:
        return ddl
    return f"{ddl} DEFAULT {literal}"


def _ensure_default_realm() -> None:
    """把「服」这套新结构补齐到可用状态（幂等，可反复执行）

    升级上来的单服部署不应该因为多了「多服运营」而行为变化，所以：

    1. 没有任何服时，建一个默认服（``slug='main'``）——它就是以前那套部署；
    2. 把所有 ``realm_id`` 为空的旧数据（套餐/订阅/卡码/求片/媒体库/挂载/服务器）
       回填到默认服；
    3. 默认服写进 ``SystemConfig['active_realm_id']``（面板顶部的「当前服」）。

    只在表已存在时执行；全新库由 create_all 建表，随后首次业务写入时自然落到默认服。
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "server_realms" not in tables:
        return

    realm_tables = {
        "subscription_plans": "realm_id",
        "user_subscriptions": "realm_id",
        "registration_codes": "realm_id",
        "movie_requests": "realm_id",
        "remote_servers": "realm_id",
        "emby_libraries": "realm_id",
        "storage_mounts": "realm_id",
    }
    with engine.begin() as conn:
        row = conn.execute(text("SELECT id, name FROM server_realms ORDER BY id LIMIT 1")).first()
        if row is None:
            conn.execute(text(
                "INSERT INTO server_realms (name, slug, url, description, is_active, sort_order, created_at) "
                "VALUES (:name, :slug, '', :desc, :active, 0, :now)"
            ), {
                "name": "默认服", "slug": "main",
                "desc": "升级自动创建：原有数据全部归到这个服，可改名或直接拆成多个服",
                "active": True, "now": datetime.now(),
            })
            row = conn.execute(text("SELECT id FROM server_realms ORDER BY id LIMIT 1")).first()
            print(f"  🏠 已创建默认服 server_realms.id={row[0]}（原有数据将归入该服）")
        default_id = row[0]
        for table, column in realm_tables.items():
            if table not in tables:
                continue
            cols = {c["name"] for c in inspector.get_columns(table)}
            if column not in cols:
                continue
            result = conn.execute(text(
                f"UPDATE {table} SET {column} = :rid WHERE {column} IS NULL"
            ), {"rid": default_id})
            if result.rowcount:
                print(f"  🔧 已回填: {table}.{column} → 服 #{default_id}（{result.rowcount} 行）")
        active = conn.execute(text(
            "SELECT value FROM system_configs WHERE key = 'active_realm_id'"
        )).first()
        if active is None:
            conn.execute(text(
                "INSERT INTO system_configs (key, value, description, updated_at) "
                "VALUES ('active_realm_id', :value, :desc, :now)"
            ), {"value": str(default_id), "desc": "面板当前操作的服（多服运营）", "now": datetime.now()})
            print(f"  🔧 已设置当前服: active_realm_id={default_id}")


def _widen_bitrate_column(existing_tables: set, inspector) -> None:
    """emby_items.bitrate 曾是 32 位 integer，遇到超大码率时探测 worker 整轮崩

    部分高码率原盘（4K/8K remux）报出的 bitrate 会超过 2**31-1，落库时抛
    ``NumericValueOutOfRange``，把 ``_probe_one`` 整轮打成异常、探测全停。
    这里加宽为 BIGINT（先钳位历史值再改类型），语句幂等，已是 BIGINT 直接跳过。
    SQLite 整数本身是 64 位，无需处理。
    """
    from sqlalchemy import text

    if "emby_items" not in existing_tables:
        return
    dialect = engine.dialect.name
    if dialect not in ("postgresql", "mysql"):
        return
    for col in inspector.get_columns("emby_items"):
        if col["name"] != "bitrate":
            continue
        if col["type"] is not None and "bigint" in str(col["type"]).lower():
            return
        with engine.begin() as conn:
            # 历史值理论上都写进来了（越界的当场就失败了），钳位仅作兜底
            conn.execute(text(
                "UPDATE emby_items SET bitrate = 2147483647 "
                "WHERE bitrate IS NOT NULL AND bitrate > 2147483647"
            ))
            if dialect == "postgresql":
                conn.execute(text("ALTER TABLE emby_items ALTER COLUMN bitrate TYPE BIGINT"))
            else:
                conn.execute(text("ALTER TABLE emby_items MODIFY COLUMN bitrate BIGINT"))
        print("  🔧 已迁移: emby_items.bitrate → BIGINT（修 32 位码率溢出）")


def _fix_station_message_from_user_fk(existing_tables: set, inspector) -> None:
    """站内消息发送者外键纠偏：from_user_id 原先指向已废弃的 admin_users(id)，
    实际写入的一直是管理员的 web_users.id；PG 强校验外键导致站内消息 INSERT
    失败且被调用方吞掉（管理后台"发送成功"但库里没有）。

    幂等：from_user_id 上已存在指向 web_users 的 FK 时直接返回；SQLite 跳过。
    """
    from sqlalchemy import text

    if "station_messages" not in existing_tables:
        return
    dialect = engine.dialect.name
    if dialect not in ("postgresql", "mysql"):
        return

    with engine.begin() as conn:
        if dialect == "postgresql":
            # 1) 已有指向 web_users 的 FK → 幂等返回
            already = conn.execute(text(
                "SELECT 1 FROM pg_constraint "
                "WHERE conrelid='station_messages'::regclass AND contype='f' "
                "AND confrelid='web_users'::regclass "
                "AND 'from_user_id' = ANY (SELECT attname FROM pg_attribute "
                "WHERE attrelid=conrelid AND attnum = ANY (conkey))"
            )).fetchone()
            if already:
                return
            # 2) 删掉 from_user_id 上所有指向非 web_users 表的旧 FK
            old = conn.execute(text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid='station_messages'::regclass AND contype='f' "
                "AND confrelid != 'web_users'::regclass "
                "AND 'from_user_id' = ANY (SELECT attname FROM pg_attribute "
                "WHERE attrelid=conrelid AND attnum = ANY (conkey))"
            )).fetchall()
            for (conname,) in old:
                safe = conname.replace('"', '""')
                conn.execute(text(
                    f'ALTER TABLE station_messages DROP CONSTRAINT "{safe}"'
                ))
        else:  # mysql
            # 1) 幂等检查
            already = conn.execute(text(
                "SELECT 1 FROM information_schema.KEY_COLUMN_USAGE "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'station_messages' "
                "AND COLUMN_NAME = 'from_user_id' AND REFERENCED_TABLE_NAME = 'web_users' "
                "LIMIT 1"
            )).fetchone()
            if already:
                return
            # 2) 删旧 FK
            old = conn.execute(text(
                "SELECT CONSTRAINT_NAME FROM information_schema.KEY_COLUMN_USAGE "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'station_messages' "
                "AND COLUMN_NAME = 'from_user_id' AND REFERENCED_TABLE_NAME IS NOT NULL "
                "AND REFERENCED_TABLE_NAME != 'web_users'"
            )).fetchall()
            for (conname,) in old:
                conn.execute(text(
                    f"ALTER TABLE station_messages DROP FOREIGN KEY `{conname}`"
                ))
        # 3) 孤儿值置空（旧约束下非空孤儿值本就写不进来，置空不丢有效数据；
        #    不清则 ADD CONSTRAINT 可能被脏数据卡住导致启动失败）
        conn.execute(text(
            "UPDATE station_messages SET from_user_id = NULL "
            "WHERE from_user_id IS NOT NULL "
            "AND from_user_id NOT IN (SELECT id FROM web_users)"
        ))
        # 4) 加新约束（名与 PG create_all 自动命名一致，新库/迁移库 schema 统一）
        conn.execute(text(
            "ALTER TABLE station_messages ADD CONSTRAINT station_messages_from_user_id_fkey "
            "FOREIGN KEY (from_user_id) REFERENCES web_users(id)"
        ))
    print("  🔧 已迁移: station_messages.from_user_id 外键 → web_users(id)（修管理后台发消息静默失败）")


def _widen_code_column(existing_tables: set, inspector) -> None:
    """卡码格式支持占位符后可能超过 20 字符，PG/MySQL 需要显式扩宽列宽

    SQLite 不校验 VARCHAR 长度，无需处理。语句幂等，已扩宽时直接跳过。
    """
    from sqlalchemy import text

    if "registration_codes" not in existing_tables:
        return
    dialect = engine.dialect.name
    if dialect not in ("postgresql", "mysql"):
        return
    for col in inspector.get_columns("registration_codes"):
        if col["name"] == "code" and (col.get("type") is None or getattr(col["type"], "length", 64) < 64):
            with engine.begin() as conn:
                if dialect == "postgresql":
                    conn.execute(text("ALTER TABLE registration_codes ALTER COLUMN code TYPE VARCHAR(64)"))
                else:
                    conn.execute(text("ALTER TABLE registration_codes MODIFY COLUMN code VARCHAR(64) NOT NULL"))
            print("  🔧 已迁移: registration_codes.code 宽度 → 64")


def _widen_stream_title_columns(existing_tables: set, inspector) -> None:
    """emby_media_streams.display_title/title 从 VARCHAR(200) 扩到 500

    生产日志：ffprobe 的 tags.title（流描述句，长度不受控）与外挂字幕文件名
    偶发超 200 字，PG 报 ``StringDataRightTruncation``，单条 INSERT 失败把
    整个写事务（同批扫描/探测/补全）全部带崩。模型已改为 String(500)，
    老库在这里幂等加宽；SQLite 不校验 VARCHAR 长度，无需处理。
    """
    from sqlalchemy import text

    if "emby_media_streams" not in existing_tables:
        return
    dialect = engine.dialect.name
    if dialect not in ("postgresql", "mysql"):
        return
    for col in inspector.get_columns("emby_media_streams"):
        if col["name"] not in ("display_title", "title"):
            continue
        length = getattr(col.get("type"), "length", None)
        if length is None or length >= 500:
            continue
        with engine.begin() as conn:
            if dialect == "postgresql":
                conn.execute(text(
                    f"ALTER TABLE emby_media_streams ALTER COLUMN {col['name']} "
                    "TYPE VARCHAR(500)"
                ))
            else:
                conn.execute(text(
                    f"ALTER TABLE emby_media_streams MODIFY COLUMN {col['name']} "
                    "VARCHAR(500)"
                ))
        print(f"  🔧 已迁移: emby_media_streams.{col['name']} 宽度 → 500"
              "（修 StringDataRightTruncation）")


_PG_LOCK_KEY = 72772620260929


def _sqlite_lock_path() -> str:
    """迁移锁文件：与 SQLite 库同目录（锁必须和数据在同一个文件系统上）。"""
    try:
        url = engine.url
    except Exception:  # noqa: BLE001 — engine 未就绪时退到 cwd
        return ".aetrix-migrate.lock"
    if url.drivername != "sqlite" or not url.database or url.database == ":memory:":
        return ".aetrix-migrate.lock"
    import os.path
    return os.path.join(os.path.dirname(os.path.abspath(url.database)) or ".",
                        ".aetrix-migrate.lock")


def _acquire_migrate_lock():
    """跨进程互斥地跑 schema 初始化/迁移。

    为什么必须有：init_db() 会被 api、worker、EA 三个进程**同时**调用，而建表/加列/
    建索引在 PG 与 SQLite 上都没有 IF NOT EXISTS。三个进程同时判定"表不存在"→ 全部
    执行 → 后到的抛 DuplicateTable / duplicate column name。而 main.py 与 worker.py
    都是 fail-closed 的 raise / return 1，restart: unless-stopped 下变成崩溃重启循环。

    切换到 PG（空库、首次建全表）时三条路径几乎必然同时进来，所以这是**切换的前置
    条件**，不是可选优化。

    两种方言各用各的原生手段：
    - PostgreSQL：pg_advisory_lock（会话级、跨进程、不占表）；
    - SQLite：fcntl.flock 文件锁。**不用数据库事务做锁**——试过 BEGIN IMMEDIATE 与
      "占一行不提交"，两种都会因为 create_all 走 engine 的其它连接、而 busy_timeout
      对该场景不生效而报 "database is locked"。flock 是操作系统级阻塞，语义明确。

    拿不到锁时**仍然继续**（fail-open）——加锁只是把"几乎必错"降为"几乎不错"，
    不能让一次锁故障把服务彻底挡住；原有"重复执行后自愈"的能力保留。
    """
    import contextlib

    @contextlib.contextmanager
    def _locked():
        if DATABASE_TYPE == "postgresql":
            conn = engine.connect()
            try:
                conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _PG_LOCK_KEY})
                try:
                    yield
                finally:
                    try:
                        conn.execute(text("SELECT pg_advisory_unlock(:k)"),
                                     {"k": _PG_LOCK_KEY})
                    except Exception:  # noqa: BLE001
                        import logging
                        logging.getLogger(__name__).error(
                            "pg_advisory_unlock 失败，丢弃连接以防 advisory lock 泄漏",
                            exc_info=True,
                        )
                        # 必须 invalidate：解锁失败说明该连接仍持有 advisory lock，
                        # 若直接归还连接池，下次迁移会在另一个连接上 pg_advisory_lock，
                        # 因锁被池中旧连接占用而永久阻塞（直到 pool_recycle 才回收）。
                        # invalidate 让连接池丢弃该连接并重建；其后的 finally: conn.close()
                        # 对已失效连接是安全的，不会把坏连接放回池中。
                        conn.invalidate()
            finally:
                conn.close()
            return

        import fcntl
        import os
        path = _sqlite_lock_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fh = open(path, "w")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)  # 阻塞直到拿到
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()

    return _locked()


def _ensure_admin_roles() -> None:
    """S1：空角色管理员的一次性迁移 + 「没有超管」时的防锁死自愈（见 backend/admin_roles.py）

    失败只记日志不阻断启动：失败的后果是空角色管理员按只读处理（fail closed），
    可以随时用 ``python scripts/create_admin.py <用户名>`` 把站长显式写成 super。
    """
    import logging

    from backend import admin_roles

    log = logging.getLogger(__name__)
    db = SessionLocal()
    try:
        result = admin_roles.ensure_legacy_admin_roles(db)
        if result.get("migrated"):
            log.warning("S1 迁移：空角色管理员 id=%s 已写为 super（最早的管理员）", result["migrated"])
        if result.get("left_viewer"):
            log.warning(
                "S1 迁移：空角色管理员 id=%s 现在按只读（viewer）处理，"
                "需要更高权限请由超级管理员在「管理员」页显式授予角色", result["left_viewer"])
        if result.get("healed"):
            log.warning("库里没有启用中的超级管理员：已把最早的管理员 id=%s 提为 super", result["healed"])
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("管理员角色迁移失败（空角色管理员将按只读处理）")
    finally:
        db.close()


def init_db():
    """初始化数据库，创建所有表并执行轻量自动迁移。

    迁移失败必须让调用方感知：继续启动会把“模型已升级、数据库仍是旧结构”的
    半可用服务暴露出去，首个业务请求才 500，且可能在不完整 schema 上继续写数据。
    部署探针 / lifespan 会据此 fail-closed；一次性 CLI 也能拿到真实异常。
    """
    from backend import models  # 导入所有模型
    from backend.emby_server import models as emby_models  # 自建 Emby 服务器模型
    with _acquire_migrate_lock():
        # trigram GIN 索引（r7）依赖 pg_trgm 扩展：create_all 会直接建模型里声明的
        # 四个 gin_trgm_ops 索引，扩展没装就报 UndefinedObject（CI 干净库实锤）。
        # IF NOT EXISTS 幂等；pg_trgm 自 PG13 起是 trusted 扩展，库属主有 CREATE
        # 权限就能装。装不上也不拦启动（与 _ensure_legacy_indexes 同一口径）。
        if engine.dialect.name == "postgresql":
            try:
                with engine.begin() as _conn:
                    _conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            except Exception as _exc:  # noqa: BLE001
                print(f"  ⚠️ pg_trgm 扩展安装跳过: {_exc}")
        Base.metadata.create_all(bind=engine)
        _auto_migrate()
        _ensure_admin_roles()
        # P1 统一货币体系：幂等播种默认 6 个会员等级（表空才插）
        try:
            from backend import member_level as _ml
            _s = SessionLocal()
            try:
                _ml.ensure_member_levels_seeded(_s)
            finally:
                _s.close()
        except Exception as e:  # noqa: BLE001 — 播种失败不阻止启动，用户端懒播种兜底
            print(f"⚠️ 会员等级种子写入失败（可忽略）: {e}")
        # v4 探险家日志面板：幂等播种 12 个成就定义（已存在的跳过）
        try:
            from backend import panel_achievements as _pa
            _s = SessionLocal()
            try:
                _added = _pa.seed_achievement_defs(_s)
                if _added:
                    print(f"✅ 面板成就定义已播种 {_added} 条")
            finally:
                _s.close()
        except Exception as e:  # noqa: BLE001 — 播种失败不阻止启动
            print(f"⚠️ 面板成就种子写入失败（可忽略）: {e}")
    print(f"✅ 数据库初始化完成 ({DATABASE_TYPE})")


# ==================== 导出 ====================
__all__ = [
    "Base",
    "engine",
    "SessionLocal",
    "get_db",
    "init_db",
    "redis_client",
    "cache",
    "DATABASE_TYPE",
    "DATABASE_URL",
]
