"""TMDB 磁盘缓存（v2.42.9 第 7 批）：搜索/详情落盘 + TTL + 容量控制

进程内缓存（``TmdbClient._cache``）只有 300 秒：重启即失、跨进程不通，
而重试退避（60/120/240/480/960 秒）必然跨过这个窗口——「注定搜不到」的名字
每一轮重试都把 4~5 个候选重新打一遍（生产测算约 7 万次白打请求）。

本模块把 TMDB 的 search / details 响应落成本地 JSON 文件（跨进程、跨重启）：

- **命中 = 不发请求**：同一部片在多个库/多个条目/多次重试/进程重启之间只打一次；
- **TTL**：search 命中 7 天、search 阴性 24 小时、details 30 天（env 可调）。
  阴性给短 TTL：TMDB 每天都在新增条目，「搜不到」比「搜到了」更容易过期；
- **阴性结果也落盘**（§7.3④：性价比最高的单点改动）——退避重试不再重复打网络；
- **容量上限** ``EMBY_TMDB_CACHE_MB``（默认 256 MB）：巡检时删过期文件，
  超限按 mtime 从旧到新淘汰到 80%；巡检限频（写满 32 MB 或距上次 ≥5 分钟），
  启动维护与 janitor 周期也会跑（见 maintenance）；
- **只缓存成功的响应**：``_get`` 返回 None（网络失败/429/限流）不落盘，
  让这类条目照旧走重试队列——缓存绝不能把「暂时失败」固化成「没有」；
- **磁盘不可用自动降级**：目录建不出来（只读 FS）就禁用，只告警一次；
  写失败计数并保留 L1，**绝不影响刮削主流程**。

配置（目录约定与图片/字幕缓存一致，缺省面向小机器）：

==========================  ==================================================
``EMBY_TMDB_CACHE``          ``0`` 关闭（默认开启）
``EMBY_TMDB_CACHE_DIR``      缓存目录（默认 ``<EMBY_TRANSCODE_DIR>/tmdb``）
``EMBY_TMDB_CACHE_MB``       容量上限，默认 256 MB；0 = 不限
``EMBY_TMDB_TTL_SEARCH``     search 命中 TTL 秒，默认 7 天
``EMBY_TMDB_TTL_NEGATIVE``   search 阴性 TTL 秒，默认 24 小时
``EMBY_TMDB_TTL_DETAILS``    details TTL 秒，默认 30 天
==========================  ==================================================
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Optional
from backend.emby_server.env_util import env_int

logger = logging.getLogger(__name__)


def _env_flag(name: str, default: str = "1") -> bool:
    return (os.getenv(name, default) or default).strip().lower() not in {
        "0", "false", "no", "off",
    }


# 配置全部**动态**读 env：测试可以用 monkeypatch.setenv 逐例改，无需重新导入；
# 代价只是每次多几个 os.getenv（相对一次文件 IO 可忽略）。
def _cfg_dir() -> str:
    return (os.getenv("EMBY_TMDB_CACHE_DIR", "").strip()
            or os.path.join(os.getenv("EMBY_TRANSCODE_DIR", "/tmp/emby_transcode"),
                            "tmdb"))


def _cfg_enabled() -> bool:
    return _env_flag("EMBY_TMDB_CACHE", "1")


def _cfg_max_bytes() -> int:
    return max(0, env_int("EMBY_TMDB_CACHE_MB", 256)) * 1024 * 1024  # 0 = 不限


def _cfg_ttl_search() -> int:
    return max(60, env_int("EMBY_TMDB_TTL_SEARCH", 7 * 86400))


def _cfg_ttl_negative() -> int:
    return max(60, env_int("EMBY_TMDB_TTL_NEGATIVE", 86400))


def _cfg_ttl_details() -> int:
    return max(60, env_int("EMBY_TMDB_TTL_DETAILS", 30 * 86400))


# 写入量累计到 32MB 或距上次巡检 ≥5 分钟就顺手巡检一次（删过期 + 容量淘汰）
_PRUNE_TRIGGER_BYTES = 32 * 1024 * 1024
_PRUNE_MIN_INTERVAL = 300.0
_KEEP_RATIO = 0.8                  # 淘汰到容量的 80%，给新写入留余量（沿 _CACHE_KEEP_RATIO 思路）

_stats: dict = {"hits": 0, "misses": 0, "writes": 0, "expired": 0,
                "evicted": 0, "waits": 0, "errors": 0}
_stats_lock = threading.Lock()
_io_lock = threading.Lock()
_written_since_prune = 0
_last_prune = 0.0
_prune_guard = threading.Lock()    # 同进程同时只跑一个巡检
_disabled = False                  # 运行期降级开关（目录建不出来时置位）
_warned = False


def enabled() -> bool:
    return _cfg_enabled() and not _disabled


def _bump(key: str) -> None:
    with _stats_lock:
        _stats[key] += 1


def _warn_once(reason: str) -> None:
    global _warned
    if not _warned:
        _warned = True
        logger.warning("TMDB 磁盘缓存不可用（不影响刮削，只是每次都真的请求）: %s", reason)


def _key_path(endpoint: str, ident: str, year: int, lang: str = "") -> str:
    """缓存键 → 分片文件路径：sha1(endpoint|归一化查询|年份|语言)，前 2 位做目录

    查询串由调用方用 ``_norm_text`` 归一化（大小写/全半角标点/空白不敏感）——
    这是让「跨条目/跨库去重」可靠而非碰运气的前提（§7.3③）。文件名不落原文，
    任何字符串都能安全落地。

    语言是缓存维度（v2.49.0 起）：TMDB 返回的简介/标题是按 language 本地化的，
    切了首选语言后旧语言的条目不再命中、各语言互不污染。老版本写下的无语言键
    （lang=""）自然过期淘汰（prune 按 TTL），升级后最多触发一轮重新拉取。
    """
    digest = hashlib.sha1(
        f"{endpoint}|{ident}|{int(year)}|{str(lang or '')}".encode("utf-8")
    ).hexdigest()
    return os.path.join(_cfg_dir(), digest[:2], f"{digest}.json")


def _ensure_dir(path: str) -> bool:
    global _disabled
    if _disabled or not _cfg_enabled():
        return False
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return True
    except OSError as exc:  # 只读 FS / 权限：这是持续性问题，整个禁用
        _disabled = True
        _warn_once(f"缓存目录建不出来（{exc}）")
        return False


def _load(path: str, ttl_for: Callable[[Any], int]):
    """读一个缓存文件。返回 (hit, payload)；过期视为未命中并顺手删除。"""
    if not _ensure_dir(path):
        return False, None
    try:
        with open(path, "r", encoding="utf-8") as f:
            envelope = json.load(f)
    except FileNotFoundError:
        with _stats_lock:
            _stats["misses"] += 1
        return False, None
    except Exception:  # noqa: BLE001 — 半截/坏文件当未命中，删掉别占地方
        _bump("errors")
        _unlink(path)
        return False, None
    age = time.time() - float(envelope.get("at") or 0)
    if age > ttl_for(envelope.get("v")):
        _unlink(path)
        _bump("expired")
        with _stats_lock:
            _stats["misses"] += 1
        return False, None
    _bump("hits")
    return True, envelope.get("v")


def _unlink(path: str) -> bool:
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def _save(path: str, payload) -> bool:
    """原子写一个缓存文件（临时文件 + os.replace），并按需触发巡检。"""
    global _written_since_prune
    if not _ensure_dir(path):
        return False
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        blob = json.dumps({"at": time.time(), "v": payload},
                          ensure_ascii=False, separators=(",", ":"))
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(blob)
        os.replace(tmp, path)      # 原子替换：并发读永远不会看到半截 JSON
    except OSError as exc:
        _bump("errors")
        _warn_once(f"缓存写入失败（{exc}）")
        _unlink(tmp)
        return False
    with _io_lock:
        _stats["writes"] += 1
        _written_since_prune += len(blob)
        due = (_written_since_prune >= _PRUNE_TRIGGER_BYTES
               or time.time() - _last_prune >= _PRUNE_MIN_INTERVAL)
    if due:
        prune()
    return True


# ---------- 公开 API（TmdbClient 调） ----------

def load_search(endpoint: str, norm_query: str, year: int, lang: str = ""):
    """search 缓存。返回 (hit, results)——hit=True 时 results 可能是 []（阴性命中）。"""
    return _load(_key_path(endpoint, norm_query, int(year or 0), lang),
                 lambda v: _cfg_ttl_negative() if not v else _cfg_ttl_search())


def save_search(endpoint: str, norm_query: str, year: int, results, lang: str = "") -> bool:
    """search 结果落盘；空列表（阴性）同样落盘，读时用更短的 TTL。"""
    return _save(_key_path(endpoint, norm_query, int(year or 0), lang),
                 list(results or []))


def load_details(endpoint: str, tmdb_id: str, lang: str = ""):
    """details 缓存。返回 (hit, data)。"""
    return _load(_key_path(endpoint, str(tmdb_id), 0, lang), lambda v: _cfg_ttl_details())


def save_details(endpoint: str, tmdb_id: str, data, lang: str = "") -> bool:
    """details 落盘。data 为空 = 请求失败（不是「没数据」），不落盘。"""
    if not data:
        return False
    return _save(_key_path(endpoint, str(tmdb_id), 0, lang), data)


def invalidate_search(name: str, year: Optional[int], kind: str, lang: str = "",
                    langs: Optional[list] = None) -> int:
    """把一部片名的搜索缓存删掉（管理端「重试未匹配项」用）。

    按与 ``TmdbClient.search`` 完全相同的候选逻辑重算缓存键，逐一删除——
    否则点「重试」只会命中旧的阴性缓存、一个请求都不发，重试就成了摆设。
    返回删除的文件数（尽力而为：文件本来就不在时不算数也不报错）。

    langs：要清理的语言列表；None 则按 language_fallback_chain() 全链清理
    （fallback 上线后一次 search 会写下多种语言的键，只清首选语言会让
    fallback 链名存实亡——其余语言仍命中旧阴性缓存）。
    """
    if not _cfg_enabled():
        return 0
    from backend.emby_server.tmdb import _norm_text, _search_candidates, language_fallback_chain

    endpoint = "tv" if kind == "series" else "movie"
    years = {int(year or 0), 0}   # year 维度两种取值都清（键里 year or 0）
    # 语言维度也要对齐：fallback 全链 + 老版本无语言键（lang=""）都清。
    # 后者覆盖升级前写下的缓存文件，以及测试/调用方用默认 lang="" 预置的情形；
    # 不清的话重试仍会命中旧阴性缓存、一个请求都不发。
    if langs is None:
        try:
            langs = list(language_fallback_chain())
        except Exception:
            langs = []
    langs = {*(langs or []), lang or "", ""}
    removed = 0
    for query, _fuzzy in _search_candidates(name or ""):
        norm = _norm_text(query)
        if not norm:
            continue
        for y in years:
            for lg in langs:
                path = _key_path(endpoint, norm, y, lg)
                if os.path.exists(path):
                    _unlink(path)
                    removed += 1
    return removed


# ---------- 单飞（照抄 mounts._single_flight_lock 的形状） ----------

_flight_locks: dict = {}
_flight_guard = threading.Lock()


@contextmanager
def single_flight(key):
    """同一缓存键的请求单飞：拿锁的线程去发请求，其余线程等它写完缓存。

    拿到锁后**必须二次检查缓存**（等锁期间别人可能已经填好）——这个约定由
    调用方负责。锁用完即弃（不随 20 万键无界增长）。
    """
    with _flight_guard:
        lock = _flight_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _flight_locks[key] = lock
        else:
            _bump("waits")
    try:
        with lock:
            yield
    finally:
        with _flight_guard:
            if _flight_locks.get(key) is lock:
                _flight_locks.pop(key, None)


# ---------- 巡检：过期清理 + 容量淘汰 ----------

def prune(force: bool = False) -> dict:
    """巡检缓存目录：删过期文件 + 超容量按 mtime 从旧到新淘汰。

    默认限频（写满触发字节 / 距上次 ≥5 分钟）；``force=True`` 由维护线程
    或测试显式触发，绕过限频。并发调用由 ``_prune_guard`` 串行化。
    """
    global _last_prune, _written_since_prune
    root = _cfg_dir()
    if not enabled() or not os.path.isdir(root):
        return {}
    with _prune_guard:
        if not force:
            with _io_lock:
                if time.time() - _last_prune < _PRUNE_MIN_INTERVAL:
                    return {}
                _last_prune = time.time()
                _written_since_prune = 0
        now = time.time()
        max_ttl = max(_cfg_ttl_search(), _cfg_ttl_negative(), _cfg_ttl_details())
        kept: list[tuple[str, int, float]] = []
        total = 0
        expired = 0
        for dirpath, _dirs, names in os.walk(root):
            for name in names:
                path = os.path.join(dirpath, name)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                if not name.endswith(".json"):
                    # 残留的写临时文件：超过 1 小时就是孤儿，清掉
                    if now - st.st_mtime > 3600:
                        _unlink(path)
                    continue
                if now - st.st_mtime > max_ttl:
                    if _unlink(path):
                        expired += 1
                    continue
                kept.append((path, st.st_size, st.st_mtime))
                total += st.st_size
        evicted = 0
        freed = 0
        cap = _cfg_max_bytes()
        if cap and total > cap:
            target = int(cap * _KEEP_RATIO)
            for path, size, _mtime in sorted(kept, key=lambda f: f[2]):
                if total <= target:
                    break
                if not _unlink(path):
                    continue
                total -= size
                evicted += 1
                freed += size
        result = {"files": len(kept) - evicted, "bytes": total,
                  "expired_removed": expired, "evicted": evicted,
                  "freed_bytes": freed}
        if expired or evicted:
            logger.info("TMDB 磁盘缓存巡检：剩 %d 个文件 / %.1f MB"
                        "（删过期 %d，超限淘汰最旧 %d 个，释放 %.1f MB）",
                        result["files"], total / 1024 / 1024,
                        expired, evicted, freed / 1024 / 1024)
        return result


def stats() -> dict:
    """缓存状态（健康检查 / 排查用）：配置口径 + 计数器 + 当前占用。"""
    root = _cfg_dir()
    files = 0
    size = 0
    if os.path.isdir(root):
        for dirpath, _dirs, names in os.walk(root):
            for name in names:
                if not name.endswith(".json"):
                    continue
                files += 1
                try:
                    size += os.stat(os.path.join(dirpath, name)).st_size
                except OSError:
                    pass
    with _stats_lock:
        counters = dict(_stats)
    return {
        "enabled": enabled(), "dir": root, "files": files, "bytes": size,
        "max_mb": _cfg_max_bytes() // (1024 * 1024),
        "ttl_search": _cfg_ttl_search(), "ttl_negative": _cfg_ttl_negative(),
        "ttl_details": _cfg_ttl_details(),
        **counters,
    }
