"""多源元数据：配置（总开关 / 中文优先 / 源顺序 / 每源开关与密钥池）

## 为什么把配置单独拎出来

连哪几个外部站、谁优先、哪一站暂时不接 —— 这些是**运维的判断**，不是代码常量。
所以这里只做一件事：把 SystemConfig 里那几个键读成一份**快照**，并提供写回去的函数。
刮削时只读快照，不再查库。

| SystemConfig 键 | 含义 | 默认 |
|---|---|---|
| ``meta_sources_enabled`` | 总开关（关掉 = 完全走原来的 NFO → TMDB → 豆瓣/Bangumi 链路） | 0 |
| ``meta_sources_prefer_chinese`` | 中文信息优先 | 1 |
| ``meta_sources_tmdb_miss_fallback`` | TMDB-miss 时走多源兜底：多源总开关的子项（链路规划 Step 2 起，统一兜底链启用时由总开关统一控制，保留键值） | 0 |
| ``meta_sources_order`` | 源顺序（JSON 数组，**只有这里出现的源会被采集**） | DEFAULT_ORDER |
| ``meta_source_enabled_{id}`` | 单源开关 | 默认见 DEFAULT_ORDER（都开） |
| ``meta_source_keys_{id}`` | 该源的密钥池（逗号分隔，与 TMDB 那套同一个拆分口径） | 空 |
| ``meta_source_rate_{id}`` | 该源两次请求的最小间隔秒数（0 = 不限速） | 1.0 |

密钥与速率**逐源**而不是全局：Bangumi 有 UA 礼仪、TMDB 有配额、TVmaze 没限制，
用同一个间隔要么把快的源拖慢、要么把慢的源打挂。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from backend.emby_server.tmdb import _split_keys

CONFIG_ENABLED = "meta_sources_enabled"
CONFIG_PREFER_CN = "meta_sources_prefer_chinese"
CONFIG_ORDER = "meta_sources_order"
CONFIG_TMDB_MISS_FALLBACK = "meta_sources_tmdb_miss_fallback"
CONFIG_SOURCE_ENABLED = "meta_source_enabled_{id}"
CONFIG_SOURCE_KEYS = "meta_source_keys_{id}"
CONFIG_SOURCE_RATE = "meta_source_rate_{id}"

DEFAULT_RATE = 1.0

#: **TMDB 的密钥池只有一处存储**：复用 Phase 6a 的 ``tmdb_api_keys``（那才是 6a 的
#: 后台入口与密钥池实现的宿主）。6b 早期版本又开了一个 ``meta_source_keys_tmdb``，
#: 于是同一把钥匙存了两份、后台还显示两遍——这里把分叉收掉：
#:
#: - 读：只认 ``tmdb_api_keys``；
#: - 写：``meta_source_keys_tmdb`` **不再写**（函数层面堵死，不靠前端自觉）；
#: - 残留：旧键里已有的值由启动自愈合并过来（``config_self_heal``），本模块只
#:   标记「旧键里还有东西」让界面提醒，不当场当生效值用。
TMDB_KEY_STORAGE = "tmdb_api_keys"
LEGACY_KEY_CONFIG = {"tmdb": "meta_source_keys_tmdb"}

# 一次采集最多问几个源。7 个源全开 + 磁盘很大 = 每条 7 次网络请求，
# 这两个上限是为了让补全队列不被拖垮；宁可少问几个源，也不要把 worker 卡住。
MAX_SOURCES = 7
COLLECT_TIMEOUT_SEC = 40.0

# 默认顺序：**中文源在前**（治 TMDB 对中文剧集/综艺搜不准），
# TMDB 放最后兜底 —— 它字段最全，但中文命中率最低。
DEFAULT_ORDER = ["bangumi", "douban", "anilist", "tvmaze", "omdb", "tvdb", "tmdb"]


@dataclass
class SourceConfig:
    """一个源的运行配置（刮削时只读这份，不查库）"""

    id: str
    label: str
    enabled: bool
    requires_key: bool
    lang: str                       # zh / en / any：这个源天然给哪种语言的标题
    keys: list[str] = field(default_factory=list)
    rate: float = DEFAULT_RATE       # 两次请求最小间隔秒数，0 = 不限
    #: 已废弃的旧键里是否还有残留（启动自愈会合并；界面据此提醒去正式入口看一眼）
    keys_legacy: bool = False
    #: 密钥存在哪个 SystemConfig 键（界面告诉用户“去哪填”）
    key_storage: str = ""

    @property
    def needs_key(self) -> bool:
        """需要密钥但一个都没有 = 这一源这次不参与（不是报错，是配置问题）"""
        return self.requires_key and not self.keys


@dataclass
class Snapshot:
    """一次采集用的完整配置快照"""

    enabled: bool
    prefer_chinese: bool
    sources: list[SourceConfig]
    #: TMDB 搜过无命中时，中文标题的剧/电影是否走多源兜底（默认关）。
    #:
    #: 链路规划 Step 2 起降级为多源总开关的子项：统一兜底链
    #: （``enrich_unified_fallback=1``）启用时，总开关是唯一的门，本键保留
    #: （回滚/兼容）但不再独立门控；未启用时保持原语义（须与总开关同时开）。
    #: 后台「多源元数据补全」卡里把它收为总开关下的二级开关，不再与总开关平级。
    tmdb_miss_fallback: bool = False

    def ordered(self) -> list[SourceConfig]:
        """这一轮真正要采集的源（开关 + 密钥条件已过滤），保持配置顺序"""
        return [s for s in self.sources if s.enabled and not s.needs_key]

    def by_id(self, source_id: str) -> Optional[SourceConfig]:
        return next((s for s in self.sources if s.id == source_id), None)


def _truthy(raw: str, default: bool) -> bool:
    value = str(raw or "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


def _keys_for(db, cfg, source_id: str) -> tuple[list, bool, str]:
    """读某个源的密钥，返回 ``(生效的keys, 废弃键里是否还有残留, 存在哪个键)``

    TMDB 只认 Phase 6a 的 ``tmdb_api_keys``。废弃键里残留的旧值由启动自愈
    （``config_self_heal._migrate_legacy_meta_keys``）合并过去；这里**不当场生效**——
    引擎真正发请求的是 ``TmdbClient``，让它读一个它不读的键只会让面板显示“有 key”
    而实际一把都用不上，那比明说“没有”更糟。
    """
    from backend.emby_server.tmdb import TMDB_KEYS_CONFIG_KEY, _db_keys

    if source_id == "tmdb":
        keys = _db_keys(db) if db is not None else _split_keys(cfg(TMDB_KEYS_CONFIG_KEY, ""))
        legacy_left = bool(_split_keys(cfg(LEGACY_KEY_CONFIG["tmdb"], "")))
        return list(keys), legacy_left, TMDB_KEY_STORAGE
    return _split_keys(cfg(CONFIG_SOURCE_KEYS.format(id=source_id), "")), False, \
        CONFIG_SOURCE_KEYS.format(id=source_id)


def read_config(db, specs: list) -> Snapshot:
    """把 SystemConfig 读成快照（``specs`` 是各源的静态元信息，见 sources.SPECS）"""
    from backend.integrations import store

    def cfg(key: str, default: str = "") -> str:
        return store.get_value(db, key, default) if db is not None else default

    order = _parse_order(cfg(CONFIG_ORDER, ""), [s.id for s in specs])
    by_id = {s.id: s for s in specs}
    sources: list[SourceConfig] = []
    for source_id in order:
        spec = by_id.get(source_id)
        if spec is None:
            continue
        keys, keys_legacy, key_storage = _keys_for(db, cfg, source_id)
        sources.append(SourceConfig(
            id=spec.id,
            label=spec.label,
            enabled=_truthy(cfg(CONFIG_SOURCE_ENABLED.format(id=source_id), ""), True),
            requires_key=bool(spec.requires_key),
            lang=spec.lang,
            keys=keys,
            keys_legacy=keys_legacy,
            key_storage=key_storage,
            rate=_rate(cfg(CONFIG_SOURCE_RATE.format(id=source_id), "")),
        ))
    return Snapshot(
        enabled=_truthy(cfg(CONFIG_ENABLED, ""), False),
        prefer_chinese=_truthy(cfg(CONFIG_PREFER_CN, ""), True),
        sources=sources,
        tmdb_miss_fallback=_truthy(cfg(CONFIG_TMDB_MISS_FALLBACK, ""), False),
    )


def _parse_order(raw: str, known: list[str]) -> list[str]:
    """解析源顺序：旧的 / 坏的 / 少写的都能救回来，且**永远包含全部已知源**"""
    seen: list[str] = []
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                seen = [str(x) for x in parsed if str(x) in known]
        except (ValueError, TypeError):
            seen = []
    # 配置里没提到的源追加到末尾（新增源上线时老配置也不会把它弄丢）
    seen += [sid for sid in DEFAULT_ORDER if sid in known and sid not in seen]
    seen += [sid for sid in known if sid not in seen]
    return seen


def _rate(raw: str) -> float:
    try:
        return max(0.0, float(str(raw).strip()))
    except (TypeError, ValueError):
        return DEFAULT_RATE


def write_config(db, *, enabled: bool, prefer_chinese: bool,
                 order: list[str], toggles: dict[str, bool],
                 keys: Optional[dict] = None,
                 rates: Optional[dict] = None,
                 tmdb_miss_fallback: Optional[bool] = None) -> int:
    """保存配置（总开关 / 中文优先 / 顺序 / 每源开关 / 密钥池 / 限速 / TMDB-miss 兜底开关），立即热生效

    ``keys`` / ``rates`` 是可选的：**只在字典里出现的源才写**，这样「改一下开关」
    不会顺手把密钥池洗掉（密钥原文从不上前端，全靠这一层保留）。

    ``tmdb_miss_fallback`` 同理：**不传时不写该键**（保持库里原值），防止
    逐源改开关 / 增删密钥这类操作顺手把它关掉；传了就按值写。
    """
    from backend.integrations import store

    values = {
        CONFIG_ENABLED: "1" if enabled else "0",
        CONFIG_PREFER_CN: "1" if prefer_chinese else "0",
        CONFIG_ORDER: json.dumps(list(order), ensure_ascii=False),
    }
    descriptions = {
        CONFIG_ENABLED: "多源元数据补全总开关（关闭 = 只用原来的 NFO/TMDB/豆瓣链路）",
        CONFIG_PREFER_CN: "中文信息优先（同等条件下优先采用中文标题与简介）",
        CONFIG_ORDER: "元数据源采集顺序（JSON 数组，靠前的源先取，字段按序填充）",
    }
    for source_id, on in (toggles or {}).items():
        key = CONFIG_SOURCE_ENABLED.format(id=source_id)
        values[key] = "1" if on else "0"
        descriptions[key] = f"元数据源「{source_id}」开关"
    for source_id, pool in (keys or {}).items():
        # 接受两种形态：原始字符串（后台批量粘贴）或已拆好的列表（接口内部）
        items = list(pool) if isinstance(pool, (list, tuple)) else _split_keys(pool)
        items = [str(k).strip() for k in items if str(k).strip()]
        if source_id == "tmdb":
            # TMDB 只写 Phase 6a 那一个键（单一存储）；写完顺手刷新密钥池，
            # 否则多源这条路径写进去了、真正发请求的 TmdbClient 还拿着旧列表
            key = TMDB_KEY_STORAGE
            descriptions[key] = "TMDB API Keys（元数据来源，多 key 逗号分隔）"
        else:
            key = CONFIG_SOURCE_KEYS.format(id=source_id)
            descriptions[key] = f"元数据源「{source_id}」密钥池（逗号分隔，按顺序轮换）"
        values[key] = ",".join(items)
    for source_id in LEGACY_KEY_CONFIG:
        # 已废弃的分叉键：从「本轮要写的键」里拿掉，函数层面就不可能再写它
        values.pop(LEGACY_KEY_CONFIG[source_id], None)
    for source_id, raw_rate in (rates or {}).items():
        key = CONFIG_SOURCE_RATE.format(id=source_id)
        values[key] = str(_rate(raw_rate))
        descriptions[key] = f"元数据源「{source_id}」请求最小间隔秒数（0 = 不限速）"
    if tmdb_miss_fallback is not None:
        values[CONFIG_TMDB_MISS_FALLBACK] = "1" if tmdb_miss_fallback else "0"
        descriptions[CONFIG_TMDB_MISS_FALLBACK] = (
            "TMDB 搜过无命中时走多源兜底（多源总开关的子项：链路规划 Step 2 的"
            "统一兜底链启用时由总开关统一控制，保留键值用于回滚/兼容）"
        )
    count = store.write_values(db, values, descriptions)
    db.commit()
    if any(source_id == "tmdb" for source_id in (keys or {})):
        from backend.emby_server.tmdb import tmdb_client
        tmdb_client.refresh_keys(db)
    invalidate()
    return count


# 进程内快照缓存：刮削是高频读，配置是低频写
_SNAPSHOT_CACHE: dict = {"at": 0.0, "db_key": None, "snapshot": None}
SNAPSHOT_TTL_SEC = 30.0


def invalidate() -> None:
    """保存配置后立刻失效缓存（同进程即时生效，跨进程靠 TTL）"""
    _SNAPSHOT_CACHE.update({"at": 0.0, "snapshot": None, "db_key": None})


def snapshot(db, specs: list) -> Snapshot:
    """带 TTL 的快照读取（``db`` 为 None 时每次都真读，给后台接口用）"""
    if db is None:
        return read_config(None, specs)
    import time

    now = time.monotonic()
    cached = _SNAPSHOT_CACHE.get("snapshot")
    if cached is not None and (now - _SNAPSHOT_CACHE["at"]) < SNAPSHOT_TTL_SEC:
        return cached
    value = read_config(db, specs)
    _SNAPSHOT_CACHE.update({"at": now, "snapshot": value})
    return value


__all__ = [
    "COLLECT_TIMEOUT_SEC", "CONFIG_ENABLED", "CONFIG_ORDER", "CONFIG_PREFER_CN",
    "CONFIG_SOURCE_ENABLED", "CONFIG_SOURCE_KEYS", "CONFIG_SOURCE_RATE",
    "CONFIG_TMDB_MISS_FALLBACK",
    "DEFAULT_ORDER", "DEFAULT_RATE", "LEGACY_KEY_CONFIG", "MAX_SOURCES",
    "TMDB_KEY_STORAGE", "Snapshot", "SourceConfig",
    "invalidate", "read_config", "snapshot", "write_config",
]
