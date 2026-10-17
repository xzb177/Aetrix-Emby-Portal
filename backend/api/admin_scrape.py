"""管理后台 · 元数据与刮削

TMDB API Key 后台填写（SystemConfig 落库，保存即热生效，无需重启）与
手动元数据刮削（条目级立即重刮 / 库级触发重新刮削扫描）。

前端入口在 EmbyAdmin.vue 的「元数据与刮削」分组（用户布局要求：与元数据 /
刮削设置放一起，不放在通用系统设置页）。路由挂在 admin_emby_router 上
（/api/admin/emby/scrape/*），成功写操作的审计由 emby_server/audit.py
中间件统一记录，本模块不再手写 _audit。
"""
from __future__ import annotations

import logging
import os
import posixpath
import re
import threading
import time
from dataclasses import replace
from datetime import datetime
from typing import Optional

import httpx
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend import models as base_models
from backend.database import SessionLocal, get_db
from backend.emby_server import models as em
from backend.emby_server import mounts as mount_lib
from backend.emby_server import nfo as nfo_lib
from backend.emby_server import nodes as node_lib
from backend.emby_server import auto_scan
from backend.emby_server import change_watcher

from backend.emby_server import scan_queue
from backend.emby_server.portal import admin_emby_router, require_staff
from backend.emby_server.tmdb import (
    TMDB_API_DEFAULT,
    TMDB_IMAGE_DEFAULT,
    TMDB_API_BASE_CONFIG_KEY,
    TMDB_IMAGE_BASE_CONFIG_KEY,
    TMDB_KEY_COOLDOWN_CONFIG_KEY,
    TMDB_KEY_INVALID_COOLDOWN_CONFIG_KEY,
    TMDB_KEYS_CONFIG_KEY,
    TMDB_LANGUAGE_OPTIONS,
    TMDB_POSTER_LANGUAGE_CONFIG_KEY,
    TMDB_POSTER_LANGUAGE_DEFAULT,
    TMDB_POSTER_LANGUAGE_OPTIONS,
    TMDB_PREFERRED_LANGUAGE_CONFIG_KEY,
    TMDB_PREFERRED_LANGUAGE_DEFAULT,
    DEFAULT_KEY_COOLDOWN_SEC,
    DEFAULT_KEY_INVALID_COOLDOWN_SEC,
    TmdbTransientError,
    _db_keys,
    _env_keys,
    _split_keys,
    invalidate_language,
    invalidate_settings,
    poster_language,
    preferred_language,
    prewarm_images,
    settings as tmdb_settings,
    tmdb_client,
    validate_base,
)
from backend.integrations import store

logger = logging.getLogger(__name__)


# ==================== TMDB API Keys ====================

class TmdbKeysSaveRequest(BaseModel):
    keys: str = Field(default="", description="多 key：逗号 / 换行 / 空白分隔")


class TmdbTestRequest(BaseModel):
    keys: str = Field(default="", description="为空则测当前生效的 key，否则测这批候选 key")


@admin_emby_router.get("/scrape/tmdb-keys")
def get_tmdb_keys(
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """当前 TMDB Key 状态与限速（只返回掩码与数量，不返回原文）

    顺带给出**实际在打**的请求速率：配置里的 ENRICH_TMDB_PER_SEC 只是上限，
    撞 429 会自适应减半、连续成功后再慢慢加回来（见 tmdb._RequestLimiter）。
    """
    meta = tmdb_client.stats()
    cfg = tmdb_settings(db)
    return {
        "configured": tmdb_client.configured,
        "source": tmdb_client.key_source,  # env | db | none
        "count": len(tmdb_client.api_keys),
        "masked": tmdb_client.masked_keys(),
        "env_present": bool(_env_keys()),  # 环境变量有值时后台填写暂不生效
        "rate": meta["rate"],              # 实际请求/秒（自适应后）
        "rate_ceiling": meta["ceiling"],   # 配置上限
        "throttled": meta["throttled"],    # 进程内累计撞 429 次数
        # 密钥池逐把状态：哪把在用、哪把在冷却（还要 xx 秒）、为什么
        "pool": tmdb_client.key_pool(db),
        "keys_cooling": meta.get("keys_cooling", 0),
        # 镜像：空值 = 用官方地址，环境变量优先
        "api_base": cfg["api_base"],
        "image_base": cfg["image_base"],
        "api_base_default": TMDB_API_DEFAULT,
        "image_base_default": TMDB_IMAGE_DEFAULT,
        "api_base_from_env": cfg["api_base_from_env"],
        "image_base_from_env": cfg["image_base_from_env"],
    }


@admin_emby_router.put("/scrape/tmdb-keys")
def save_tmdb_keys(
    req: TmdbKeysSaveRequest,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """保存 TMDB API Keys：SystemConfig 落库后立即热生效，无需重启"""
    keys = _split_keys(req.keys)
    store.write_values(
        db,
        {TMDB_KEYS_CONFIG_KEY: ",".join(keys)},
        {TMDB_KEYS_CONFIG_KEY: "TMDB API Keys（元数据与刮削，多 key 逗号分隔）"},
    )
    db.commit()
    info = tmdb_client.refresh_keys(db)
    return {"success": True, "saved": len(keys), **info}


def _test_one_key(key: str) -> tuple[bool, str]:
    """测单个 key：调 TMDB /configuration（不记录原文）

    走**当前生效的镜像地址**：境内配了反代时，直连官方可能测不通，但反代本身是好的。
    """
    from backend.emby_server import tmdb as tmdb_mod

    try:
        resp = httpx.get(f"{tmdb_mod.api_base()}/configuration",
                         params={"api_key": key}, timeout=10)
    except Exception as e:  # noqa: BLE001 — 网络异常直接当测试失败
        return False, f"网络异常：{type(e).__name__}"
    if resp.status_code == 200:
        return True, "有效"
    if resp.status_code == 401:
        return False, "无效（401 未授权）"
    return False, f"HTTP {resp.status_code}"


@admin_emby_router.post("/scrape/tmdb-test")
def test_tmdb_keys(
    req: TmdbTestRequest,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """一键测试全部密钥：逐个 key 调 TMDB，只返回序号 / 掩码 / 有效性

    测的是**当前生效的池**（除非传了 ``keys``）。测出 401 / 429 的 key 直接
    放进冷却：测都过不了的 key 没必要继续拿去打真实请求。
    """
    candidates = _split_keys(req.keys) or list(tmdb_client.api_keys)
    testing_pool = not req.keys
    results = []
    for i, key in enumerate(candidates):
        ok, message = _test_one_key(key)
        if testing_pool and key in tmdb_client.api_keys and not ok:
            if "401" in message:
                tmdb_client.note_key_invalid(key)
            else:
                tmdb_client.note_key_throttled(key)
        results.append({
            "index": i + 1,
            "masked": f"****{key[-4:]}" if len(key) > 4 else "****",
            "ok": ok,
            "message": message,
        })
    return {"results": results, "pool": tmdb_client.key_pool(db)}


class TmdbKeyAddRequest(BaseModel):
    key: str = Field(..., min_length=8, max_length=200, description="一把新的 TMDB API Key")


def _locked_db_keys(db: Session) -> list[str]:
    """行锁 TMDB 密钥池配置行后读取（F-P1-9 修复）。

    密钥增删是读-改-写：不加锁时两请求读到同一旧列表会互相覆盖新增/删除。
    调用方必须在同一事务内随后调 _save_pool（它会 commit），锁直到提交才释放。
    用 ttl=0 直查 DB，绕过 TTL 缓存，确保读到刚加锁行的最新提交值。

    已知残留窗口（P3-4）：`with_for_update().first()` 只对已存在的行加锁。
    全新安装且从未用过"批量保存 TMDB Keys"时配置行不存在，此时两并发
    add_tmdb_key 可互相覆盖。窗口极窄（首次写入前一刻的两并发），且 _save_pool
    的 upsert 本身是单行写覆盖，覆盖后果也只丢一把 key（界面会提示密钥数异常，
    可重新添加）；不引入 advisory lock/insert-retry 等侵入修法，留注释说明。
    """
    db.query(base_models.SystemConfig).filter(
        base_models.SystemConfig.key == TMDB_KEYS_CONFIG_KEY
    ).with_for_update().first()
    raw = store.get_value(db, TMDB_KEYS_CONFIG_KEY, "", ttl=0)
    return _split_keys(raw)


def _lock_metasource_config_rows(db: Session) -> None:
    """行锁多源元数据配置的所有配置行（F-P1-9 修复）。

    密钥增删是"读快照→改 keys→整体写回"：不加锁时并发的两边会互相覆盖，
    连带把对方的开关/顺序/限速改动也洗掉。按 key 排序加锁，防死锁。
    调用方必须在同一事务内随后调 write_config（它会 commit）。
    """
    from backend.emby_server.metasources import config as ms_config
    from backend.emby_server.metasources import sources as ms_sources
    keys = [ms_config.CONFIG_ENABLED, ms_config.CONFIG_PREFER_CN, ms_config.CONFIG_ORDER]
    for spec in ms_sources.SPECS:
        keys.append(ms_config.CONFIG_SOURCE_ENABLED.format(id=spec.id))
        keys.append(ms_config.CONFIG_SOURCE_KEYS.format(id=spec.id))
        keys.append(ms_config.CONFIG_SOURCE_RATE.format(id=spec.id))
    db.query(base_models.SystemConfig).filter(
        base_models.SystemConfig.key.in_(keys)
    ).order_by(base_models.SystemConfig.key).with_for_update().all()


def _save_pool(db: Session, keys: list[str]) -> int:
    """把密钥池写回 SystemConfig 并热生效（返回写入数量）"""
    count = store.write_values(
        db,
        {TMDB_KEYS_CONFIG_KEY: ",".join(keys)},
        {TMDB_KEYS_CONFIG_KEY: "TMDB API Keys（元数据来源，多 key 逗号分隔）"},
    )
    db.commit()
    tmdb_client.refresh_keys(db)
    return count


@admin_emby_router.post("/scrape/tmdb-keys/add")
def add_tmdb_key(
    req: TmdbKeyAddRequest,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """逐把增：往密钥池里追加一把（去重，不覆盖已有）

    环境变量里有 key 时（``TMDB_API_KEYS``）写入仍然成功，但**不会生效**——
    返回里的 ``effective=false`` 就是给界面提示用的，与密钥来源优先级一致。
    """
    key = req.key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="密钥不能为空")
    # F-P1-9：行锁后读，防并发增删互相覆盖
    existing = _locked_db_keys(db)
    if key in existing:
        raise HTTPException(status_code=409, detail="这把密钥已经在池子里了")
    _save_pool(db, existing + [key])
    info = tmdb_client.refresh_keys(db)
    return {
        "success": True,
        "count": len(tmdb_client.api_keys),
        "masked": tmdb_client.masked_keys(),
        "effective": tmdb_client.key_source == "db",
        "note": "" if tmdb_client.key_source == "db"
                else "环境变量 TMDB_API_KEYS 优先，后台添加的这把暂不生效",
        **info,
    }


@admin_emby_router.delete("/scrape/tmdb-keys/{index}")
def delete_tmdb_key(
    index: int,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """逐把删：按**序号**（从 1 开始，与界面展示一致）删掉一把"""
    # F-P1-9：行锁后读，防并发增删互相覆盖
    keys = _locked_db_keys(db)
    if index < 1 or index > len(keys):
        raise HTTPException(
            status_code=400,
            detail=f"序号超出范围：密钥池里只有 {len(keys)} 把（删的是后台填写的那部分）",
        )
    removed = keys.pop(index - 1)
    _save_pool(db, keys)
    return {
        "success": True,
        "removed": f"****{removed[-4:]}" if len(removed) > 4 else "****",
        "count": len(tmdb_client.api_keys),
        "masked": tmdb_client.masked_keys(),
    }


@admin_emby_router.post("/scrape/tmdb-keys/cooldown/reset")
def reset_tmdb_key_cooldown(
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """清除全部密钥的冷却（换完 key / 网络恢复后手动重来一次）"""
    cleared = tmdb_client.clear_cooldowns()
    return {"success": True, "cleared": cleared, "pool": tmdb_client.key_pool(db)}


class TmdbMirrorRequest(BaseModel):
    api_base: str = Field(default="", description="API 镜像/反代地址（留空 = 官方）")
    image_base: str = Field(default="", description="图片 CDN 镜像地址（留空 = 官方）")


@admin_emby_router.put("/scrape/tmdb-mirror")
def save_tmdb_mirror(
    req: TmdbMirrorRequest,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """保存 TMDB 镜像地址（API 与图片分开；保存即热生效）

    空值 = 回到官方地址；写进 SystemConfig，跨进程靠短 TTL 兜底。
    """
    try:
        api = validate_base(req.api_base, "API 镜像地址")
        image = validate_base(req.image_base, "图片 CDN 镜像地址")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    store.write_values(
        db,
        {TMDB_API_BASE_CONFIG_KEY: api, TMDB_IMAGE_BASE_CONFIG_KEY: image},
        {
            TMDB_API_BASE_CONFIG_KEY: "TMDB API 镜像/反代地址（留空 = 官方地址）",
            TMDB_IMAGE_BASE_CONFIG_KEY: "TMDB 图片 CDN 镜像地址（留空 = 官方地址）",
        },
    )
    db.commit()
    invalidate_settings()
    cfg = tmdb_settings(db, refresh=True)
    return {
        "success": True,
        "api_base": cfg["api_base"],
        "image_base": cfg["image_base"],
        "defaults": {"api_base": TMDB_API_DEFAULT, "image_base": TMDB_IMAGE_DEFAULT},
    }


@admin_emby_router.get("/scrape/tmdb-mirror")
def get_tmdb_mirror(
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """当前生效的镜像地址 + 官方默认值（界面上的“恢复默认”要比对得出差异）"""
    cfg = tmdb_settings(db, refresh=True)
    return {
        "api_base": cfg["api_base"],
        "image_base": cfg["image_base"],
        "api_base_from_env": cfg["api_base_from_env"],
        "image_base_from_env": cfg["image_base_from_env"],
        "defaults": {"api_base": TMDB_API_DEFAULT, "image_base": TMDB_IMAGE_DEFAULT},
        "cooldown_sec": cfg["cooldown_sec"],
        "invalid_cooldown_sec": cfg["invalid_cooldown_sec"],
        "cooldown_config_keys": [TMDB_KEY_COOLDOWN_CONFIG_KEY, TMDB_KEY_INVALID_COOLDOWN_CONFIG_KEY],
    }


class TmdbLanguageSaveRequest(BaseModel):
    language: str = Field(default=TMDB_PREFERRED_LANGUAGE_DEFAULT,
                          description="TMDB 首选语言：简介/标题/别名返回哪种语言")


@admin_emby_router.get("/scrape/tmdb-language")
def get_tmdb_language(
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """TMDB 首选语言：当前生效值 + 可选列表。

    from_env 为真时环境变量 TMDB_LANGUAGE 覆盖了后台配置（下拉改了也不生效）。

    refresh=True 直查 DB（绕过 30s 进程缓存 + 60s store TTL）：管理后台
    刚保存后立刻 GET 必须看到新值，不能等 TTL 过期。
    """
    return {
        "language": preferred_language(db, refresh=True),
        "options": list(TMDB_LANGUAGE_OPTIONS),
        "default": TMDB_PREFERRED_LANGUAGE_DEFAULT,
        "from_env": bool((os.getenv("TMDB_LANGUAGE") or "").strip()),
    }


@admin_emby_router.put("/scrape/tmdb-language")
def save_tmdb_language(
    req: TmdbLanguageSaveRequest,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """保存 TMDB 首选语言（写进 SystemConfig，保存即热生效，无需重启）"""
    lang = (req.language or "").strip()
    if lang not in TMDB_LANGUAGE_OPTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的语言：{lang}（可选：{'、'.join(TMDB_LANGUAGE_OPTIONS)}）",
        )
    store.write_values(
        db,
        {TMDB_PREFERRED_LANGUAGE_CONFIG_KEY: lang},
        {TMDB_PREFERRED_LANGUAGE_CONFIG_KEY: "TMDB 首选语言（简介/标题/别名返回语言）"},
    )
    db.commit()
    # commit 后立即过期会话级缓存：防止同一 Session 的 identity map 读到旧值
    #（保存后同一 db 读必须看到新值）
    db.expire_all()
    invalidate_language()
    # 直接用刚保存的值计算生效值，不经过 preferred_language(db) 的 DB 回读：
    # 回读要走 _LANGUAGE_CACHE + store._ttl_cache 两层缓存，任何一层残留旧值都会
    # 被重新缓存并污染后续的 get_tmdb_language（保存后读到旧值的根因）。
    # lang 已校验合法；环境变量优先口径与 preferred_language 一致。
    env_lang = (os.getenv("TMDB_LANGUAGE") or "").strip()
    effective = env_lang if env_lang in TMDB_LANGUAGE_OPTIONS else lang
    return {"success": True, "language": effective}


class TmdbPosterLanguageSaveRequest(BaseModel):
    language: str = Field(default=TMDB_POSTER_LANGUAGE_DEFAULT,
                          description="海报语言偏好：system（跟随系统）/ original（原语言）/ zh-CN")


@admin_emby_router.get("/scrape/tmdb-poster-language")
def get_tmdb_poster_language(
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """TMDB 海报语言偏好：当前值 + 可选列表（对标 StrmAssistant #10 原语言海报）。"""
    return {
        "language": poster_language(db),
        "options": list(TMDB_POSTER_LANGUAGE_OPTIONS),
        "default": TMDB_POSTER_LANGUAGE_DEFAULT,
    }


@admin_emby_router.put("/scrape/tmdb-poster-language")
def save_tmdb_poster_language(
    req: TmdbPosterLanguageSaveRequest,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """保存 TMDB 海报语言偏好（写进 SystemConfig，保存即生效，无需重启）"""
    lang = (req.language or "").strip()
    if lang not in TMDB_POSTER_LANGUAGE_OPTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的语言：{lang}（可选：{'、'.join(TMDB_POSTER_LANGUAGE_OPTIONS)}）",
        )
    store.write_values(
        db,
        {TMDB_POSTER_LANGUAGE_CONFIG_KEY: lang},
        {TMDB_POSTER_LANGUAGE_CONFIG_KEY: "TMDB 海报语言偏好（system 跟随系统 / original 原语言 / zh-CN）"},
    )
    db.commit()
    db.expire_all()
    return {"success": True, "language": lang}


class TmdbKeyCooldownsSaveRequest(BaseModel):
    cooldown_sec: float = Field(default=DEFAULT_KEY_COOLDOWN_SEC,
                                description="密钥限流冷却（秒），≥1，默认 900")
    invalid_cooldown_sec: float = Field(default=DEFAULT_KEY_INVALID_COOLDOWN_SEC,
                                        description="密钥 401 失效冷却（秒），≥1，默认 21600")


@admin_emby_router.get("/scrape/tmdb-key-cooldowns")
def get_tmdb_key_cooldowns(
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """TMDB 密钥冷却时长：当前生效值 + 默认值 + 环境变量覆盖标记。

    配置瘦身第 3c 批：429 限流冷却与 401 失效冷却此前无后台写入口。
    TMDB_KEY_COOLDOWN_SEC / TMDB_KEY_INVALID_COOLDOWN_SEC 环境变量优先。
    """
    cfg = tmdb_settings(db, refresh=True)
    return {
        "cooldown_sec": cfg["cooldown_sec"],
        "invalid_cooldown_sec": cfg["invalid_cooldown_sec"],
        "from_env": {
            "cooldown_sec": bool((os.getenv("TMDB_KEY_COOLDOWN_SEC") or "").strip()),
            "invalid_cooldown_sec": bool((os.getenv("TMDB_KEY_INVALID_COOLDOWN_SEC") or "").strip()),
        },
        "defaults": {
            "cooldown_sec": float(DEFAULT_KEY_COOLDOWN_SEC),
            "invalid_cooldown_sec": float(DEFAULT_KEY_INVALID_COOLDOWN_SEC),
        },
    }


@admin_emby_router.put("/scrape/tmdb-key-cooldowns")
def save_tmdb_key_cooldowns(
    req: TmdbKeyCooldownsSaveRequest,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """保存 TMDB 密钥冷却时长（写 SystemConfig，保存即热生效，无需重启）。"""
    try:
        cd = float(req.cooldown_sec)
        icd = float(req.invalid_cooldown_sec)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="冷却时长必须是数字")
    if cd < 1 or icd < 1:
        raise HTTPException(status_code=400, detail="冷却时长需 ≥ 1 秒")
    store.write_values(
        db,
        {TMDB_KEY_COOLDOWN_CONFIG_KEY: str(cd),
         TMDB_KEY_INVALID_COOLDOWN_CONFIG_KEY: str(icd)},
        {TMDB_KEY_COOLDOWN_CONFIG_KEY: "TMDB 密钥限流（429）冷却秒数，默认 900",
         TMDB_KEY_INVALID_COOLDOWN_CONFIG_KEY: "TMDB 密钥 401 失效冷却秒数，默认 21600"},
    )
    db.commit()
    db.expire_all()
    invalidate_settings()
    return {"success": True, "cooldown_sec": cd, "invalid_cooldown_sec": icd}


# ==================== 条目级重刮 ====================

# 变更摘要里跟踪的字段（只读这些，不碰文件 / 播放相关字段）
_TRACKED_FIELDS = (
    "name", "original_title", "overview", "tagline", "production_year",
    "community_rating", "official_rating", "genres", "studios",
    "tmdb_id", "imdb_id", "aliases", "external_ids",
    "poster_path", "primary_image_url", "backdrop_path", "backdrop_image_url",
)


def _read_sibling_text(
    db: Session, file_path: Optional[str], candidates: list[str], parent_levels: int = 0
) -> Optional[str]:
    """读 file_path 同目录（或上 parent_levels 级目录）里的候选 NFO 文本。

    本机路径直接读文件；mount:// 路径经挂载 provider 读。读不到返回 None。
    """
    if not file_path or not candidates:
        return None
    parsed = mount_lib.parse_mount_path(file_path)
    if parsed:
        mount_id, rel = parsed
        try:
            provider = mount_lib.build_provider(mount_lib.load_mount(db, mount_id), db)
        except Exception:  # noqa: BLE001 — 挂载不可用就当没有 NFO
            return None
        d = posixpath.dirname(rel)
        for _ in range(parent_levels + 1):
            for cand in candidates:
                try:
                    return provider.read_text(posixpath.join(d, cand))
                except Exception:  # noqa: BLE001 — 单个候选读不到就试下一个
                    continue
            d = posixpath.dirname(d) or "/"
        return None
    d = os.path.dirname(file_path)
    for _ in range(parent_levels + 1):
        for cand in candidates:
            try:
                with open(os.path.join(d, cand), "r", encoding="utf-8-sig", errors="ignore") as f:
                    # F-P1-12：限制 NFO 读取大小（256KB），防异常大文件一次性进内存
                    data = f.read(256 * 1024)
                    if f.read(1):
                        logger.warning("NFO 文件过大已截断：%s（只读前 256KB）",
                                       os.path.join(d, cand))
                    return data
            except OSError:
                continue
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def _discover_nfo(db: Session, item: em.MediaItem) -> Optional[dict]:
    """给条目找 NFO（电影/剧集优先；季/集按 NFO 能力处理）"""
    kind = item.item_type
    if kind == "movie":
        stem = posixpath.splitext(posixpath.basename(item.file_path or ""))[0]
        cands = [f"{stem}.nfo", "movie.nfo"] if stem else ["movie.nfo"]
        text = _read_sibling_text(db, item.file_path, cands)
    elif kind == "series":
        # 用第一集定位剧集目录：tvshow.nfo 在剧集目录（或季目录的父级）
        ep = (
            db.query(em.MediaItem)
            .filter(em.MediaItem.series_id == item.id, em.MediaItem.item_type == "episode")
            .first()
        )
        text = _read_sibling_text(db, ep.file_path if ep else None, ["tvshow.nfo"], parent_levels=1)
    elif kind == "episode":
        stem = posixpath.splitext(posixpath.basename(item.file_path or ""))[0]
        text = _read_sibling_text(db, item.file_path, [f"{stem}.nfo"] if stem else [])
    elif kind == "season":
        ep = (
            db.query(em.MediaItem)
            .filter(em.MediaItem.parent_id == item.id, em.MediaItem.item_type == "episode")
            .first()
        )
        text = _read_sibling_text(db, ep.file_path if ep else None, ["season.nfo"])
    else:
        return None
    return nfo_lib.parse_nfo(text) if text else None


# ==================== 多源元数据（Phase 6b） ====================

class MetaSourcesSaveRequest(BaseModel):
    enabled: bool = Field(default=False, description="多源补全总开关")
    prefer_chinese: bool = Field(default=True, description="中文信息优先")
    order: list[str] = Field(default_factory=list, description="源顺序（源 id 数组）")
    toggles: dict[str, bool] = Field(default_factory=dict, description="逐源开关")
    rates: dict[str, float] = Field(default_factory=dict, description="逐源请求间隔（秒）")
    # Optional：没带这个字段 = 老版本前端/脚本在保存 → 不动该键（write_config 的 no-clobber 语义）；
    # 带了就按值写。兜底开关是单向棘轮，开了就不该被一次无意的保存关掉。
    # 链路规划 Step 2：该键降级为多源总开关的子项（统一兜底链启用时由总开关
    # 统一控制，保留键值用于回滚/兼容）；后台 UI 上收为二级开关。
    tmdb_miss_fallback: Optional[bool] = Field(default=None, description="TMDB-miss 时走多源兜底（多源总开关的子项）")


class MetaSourceProbeRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200, description="要试的片名")
    year: Optional[int] = Field(default=None, ge=1800, le=2200)
    kind: str = Field(default="series", description="series | movie")
    source: str = Field(default="", description="只试这一个源（留空 = 按顺序全试）")


def _meta_sources_view(db: Session, snapshot=None) -> dict:
    """把配置快照 + 运行时状态拼成一份界面直接可用的视图

    **密钥原文不出这个函数**：每把只给掩码、冷却剩余秒数与原因。
    """
    from backend.emby_server.metasources import config as ms_config
    from backend.emby_server.metasources import keypool as ms_keypool
    from backend.emby_server.metasources import sources as ms_sources

    snapshot = snapshot if snapshot is not None else ms_config.read_config(db, ms_sources.SPECS)
    order = [s.id for s in snapshot.sources]
    rows = []
    for position, source_cfg in enumerate(snapshot.sources, start=1):
        spec = ms_sources.SPEC_BY_ID.get(source_cfg.id)
        if source_cfg.id == "tmdb":
            # TMDB 的密钥池归 Phase 6a 的 TmdbClient 管（单一入口）。
            # 这里直接读它的快照，**不再建第二个池**——两处冷却状态各说各话，
            # 管理员会以为同一把钥匙在两个地方表现不一样。
            key_rows = tmdb_client.key_pool(db)
            cooling = len([r for r in key_rows if r.get("cooling")])
        else:
            pool = ms_keypool.pool_for(source_cfg.id, source_cfg.keys)
            key_rows = pool.status()
            cooling = pool.cooling_count()
        rows.append({
            "id": source_cfg.id,
            "label": source_cfg.label,
            "note": spec.note if spec else "",
            "position": position,
            "enabled": source_cfg.enabled,
            "requires_key": source_cfg.requires_key,
            "lang": source_cfg.lang,
            "rate": source_cfg.rate,
            "key_count": len(source_cfg.keys),
            "keys": key_rows,
            "cooling": cooling,
            # 「不参与」的两种原因分别说清楚：手动关掉 vs 缺密钥
            "skipped_reason": ("已在后台关闭" if not source_cfg.enabled
                               else ("需要密钥但一个都没配" if source_cfg.needs_key else "")),
            # key 去哪申请（界面直接给链接与说明，不用去搜索引擎里找）
            "apply_url": spec.apply_url if spec else "",
            "apply_hint": spec.apply_hint if spec else "",
            # 密钥存在哪个配置键：界面据此告诉用户「去哪填」
            "key_storage": source_cfg.key_storage or (spec.key_storage if spec else ""),
            # true = 已废弃的旧键里还有残留（启动自愈会合并，界面提醒看一眼）
            "keys_legacy": source_cfg.keys_legacy,
            # TMDB 的密钥池在 6a 那张卡片里（单一入口），这里不再重复给输入框
            "key_entry": "pool_card" if source_cfg.id == "tmdb" else "inline",
        })
    return {
        "enabled": snapshot.enabled,
        "prefer_chinese": snapshot.prefer_chinese,
        "tmdb_miss_fallback": snapshot.tmdb_miss_fallback,
        "order": order,
        "sources": rows,
        "default_order": list(ms_config.DEFAULT_ORDER),
        "max_sources": ms_config.MAX_SOURCES,
        "collect_timeout_sec": ms_config.COLLECT_TIMEOUT_SEC,
        "active_count": len([r for r in rows if r["enabled"] and not r["skipped_reason"]]),
    }


@admin_emby_router.get("/scrape/meta-sources")
def get_meta_sources(
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """多源配置 + 逐源运行时状态（密钥只给掩码）"""
    return _meta_sources_view(db)


@admin_emby_router.put("/scrape/meta-sources")
def save_meta_sources(
    req: MetaSourcesSaveRequest,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """保存总开关 / 中文优先 / TMDB-miss 兜底开关 / 顺序 / 逐源开关 / 逐源限速（**不动密钥池**）"""
    from backend.emby_server.metasources import config as ms_config
    from backend.emby_server.metasources import sources as ms_sources

    known = {spec.id for spec in ms_sources.SPECS}
    order = [sid for sid in (req.order or []) if sid in known]
    # 顺序里没提到的源追加到末尾（前端漏发不丢源）；重复的丢弃
    order += [sid for sid in ms_config.DEFAULT_ORDER if sid in known and sid not in order]
    toggles = {sid: bool(on) for sid, on in (req.toggles or {}).items() if sid in known}
    rates = {sid: rate for sid, rate in (req.rates or {}).items() if sid in known}
    ms_config.write_config(
        db, enabled=bool(req.enabled), prefer_chinese=bool(req.prefer_chinese),
        order=order, toggles=toggles, rates=rates,
        tmdb_miss_fallback=None if req.tmdb_miss_fallback is None
        else bool(req.tmdb_miss_fallback))
    return _meta_sources_view(db)


class MetaSourceKeyAddRequest(BaseModel):
    key: str = Field(..., min_length=6, max_length=200, description="一把新的密钥")


@admin_emby_router.post("/scrape/meta-sources/{source_id}/keys")
def add_meta_source_key(
    source_id: str,
    req: MetaSourceKeyAddRequest,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """逐源逐把增：追加一把密钥（去重，不覆盖已有）"""
    from backend.emby_server.metasources import config as ms_config
    from backend.emby_server.metasources import sources as ms_sources

    if source_id not in ms_sources.SPEC_BY_ID:
        raise HTTPException(status_code=404, detail=f"未知的数据源：{source_id}")
    key = req.key.strip()
    if not key:
        raise HTTPException(status_code=400, detail="密钥不能为空")
    # F-P1-9：行锁所有配置行后读快照，防并发增删/改开关互相覆盖
    _lock_metasource_config_rows(db)
    snapshot = ms_config.read_config(db, ms_sources.SPECS)
    existing = list((snapshot.by_id(source_id).keys if snapshot.by_id(source_id) else []))
    if key in existing:
        raise HTTPException(status_code=409, detail="这把密钥已经在池子里了")
    ms_config.write_config(db, enabled=snapshot.enabled,
                            prefer_chinese=snapshot.prefer_chinese,
                            order=[s.id for s in snapshot.sources],
                            toggles={s.id: s.enabled for s in snapshot.sources},
                            rates={s.id: s.rate for s in snapshot.sources},
                            # TMDB-miss 兜底开关原样带回去：逐源增删密钥不许动它
                            tmdb_miss_fallback=snapshot.tmdb_miss_fallback,
                            keys={source_id: existing + [key]})
    return _meta_sources_view(db)


@admin_emby_router.delete("/scrape/meta-sources/{source_id}/keys/{index}")
def delete_meta_source_key(
    source_id: str,
    index: int,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """逐源逐把删：按**序号**（从 1 开始，与界面展示一致）删一把"""
    from backend.emby_server.metasources import config as ms_config
    from backend.emby_server.metasources import sources as ms_sources

    if source_id not in ms_sources.SPEC_BY_ID:
        raise HTTPException(status_code=404, detail=f"未知的数据源：{source_id}")
    # F-P1-9：行锁所有配置行后读快照，防并发增删/改开关互相覆盖
    _lock_metasource_config_rows(db)
    snapshot = ms_config.read_config(db, ms_sources.SPECS)
    source_cfg = snapshot.by_id(source_id)
    if source_cfg is None:
        raise HTTPException(status_code=404, detail=f"未知的数据源：{source_id}")
    if index < 1 or index > len(source_cfg.keys):
        raise HTTPException(
            status_code=400,
            detail=f"序号超出范围：「{source_cfg.label}」的池子里只有 {len(source_cfg.keys)} 把",
        )
    remaining = [k for i, k in enumerate(source_cfg.keys, start=1) if i != index]
    ms_config.write_config(db, enabled=snapshot.enabled,
                            prefer_chinese=snapshot.prefer_chinese,
                            order=[s.id for s in snapshot.sources],
                            toggles={s.id: s.enabled for s in snapshot.sources},
                            rates={s.id: s.rate for s in snapshot.sources},
                            # TMDB-miss 兜底开关原样带回去：逐源增删密钥不许动它
                            tmdb_miss_fallback=snapshot.tmdb_miss_fallback,
                            keys={source_id: remaining})
    return _meta_sources_view(db)


@admin_emby_router.post("/scrape/meta-sources/{source_id}/keys/reset")
def reset_meta_source_cooldown(
    source_id: str,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """清除某个源全部密钥的冷却（换完密钥 / 网络恢复后手动重来一次）"""
    from backend.emby_server.metasources import config as ms_config
    from backend.emby_server.metasources import keypool as ms_keypool
    from backend.emby_server.metasources import sources as ms_sources

    if source_id not in ms_sources.SPEC_BY_ID:
        raise HTTPException(status_code=404, detail=f"未知的数据源：{source_id}")
    # F-P1-11：TMDB 的密钥池归 Phase 6a 的 TmdbClient 管（单一入口），
    # 重置冷却必须走 tmdb_client 的接口，不能操作 metasources 的池。
    if source_id == "tmdb":
        from backend.emby_server import tmdb as tmdb_module
        cleared = tmdb_module.tmdb_client.clear_cooldowns()
        return {"success": True, "cleared": cleared, "config": _meta_sources_view(db)}
    snapshot = ms_config.read_config(db, ms_sources.SPECS)
    source_cfg = snapshot.by_id(source_id)
    keys = list(source_cfg.keys) if source_cfg else []
    cleared = ms_keypool.pool_for(source_id, keys).clear()
    return {"success": True, "cleared": cleared, "config": _meta_sources_view(db)}


@admin_emby_router.post("/scrape/meta-sources/test")
def probe_meta_sources(
    req: MetaSourceProbeRequest,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """试采集：用一个片名跑一遍，**不改任何数据**，返回逐源命中与合并后的字段

    这是配置页最有用的一把尺子：开一个源之前先看它到底能不能搜到这部片。
    ``source`` 指定时只问那一个源（用来单独排查“是不是这个站在抽风”）。
    """
    from backend.emby_server.metasources import config as ms_config
    from backend.emby_server.metasources import engine as ms_engine
    from backend.emby_server.metasources import sources as ms_sources

    kind = "movie" if req.kind == "movie" else "series"
    if req.source and req.source not in ms_sources.SPEC_BY_ID:
        raise HTTPException(status_code=404, detail=f"未知的数据源：{req.source}")
    # 试采集要能测「被关掉的源」，所以不走过滤后的 ordered()
    result = ms_engine.collect(db, req.title.strip(), req.year, kind,
                               only=req.source or None, ignore_switch=True)
    snapshot = ms_config.read_config(db, ms_sources.SPECS)
    view = _meta_sources_view(db, snapshot)
    return {
        "success": True,
        "switch_on": snapshot.enabled,
        "probe": result.as_dict(),
        "note": "" if snapshot.enabled
                else "总开关现在是关的，这次是临时试采集（不会真的落库）",
        "sources": view["sources"],
    }


# 只有这些词出现时，才算「密钥自己的问题」。
# 写成一组词而不是逐个源判断，是因为各站的报错文案不一致（OMDb 说 Error: Invalid API key!，
# TheTVDB 说 token 过期，TMDB 直接回 401），而**网络类失败**长得很像（超时 / 拒绝连接 /
# 429 / 403），绝不能误伤——那会把好密钥白白晾起来。
_KEY_FAULT_WORDS = ("401", "403 认证", "密钥无效", "无效密钥", "api key", "apikey",
                    "invalid", "token", "unauthorized", "配额", "quota")


def _is_key_fault(message: str) -> bool:
    text = str(message or "").lower()
    return any(word in text for word in _KEY_FAULT_WORDS)


@admin_emby_router.post("/scrape/meta-sources/{source_id}/test")
def test_meta_source_keys(
    source_id: str,
    req: MetaSourceProbeRequest,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """一键测试某个源的密钥：逐把试一遍，测不通的直接进冷却

    与 TMDB 那套同一口径：测都过不了的密钥没必要继续拿去打真实请求。
    测不出来（非密钥类失败，如网络）**不进冷却**——那不是密钥的锅。
    """
    from backend.emby_server.metasources import config as ms_config
    from backend.emby_server.metasources import keypool as ms_keypool
    from backend.emby_server.metasources import sources as ms_sources

    spec = ms_sources.SPEC_BY_ID.get(source_id)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"未知的数据源：{source_id}")
    snapshot = ms_config.read_config(db, ms_sources.SPECS)
    source_cfg = snapshot.by_id(source_id)
    keys = list(source_cfg.keys) if source_cfg else []
    if not keys:
        raise HTTPException(status_code=400, detail=f"「{spec.label}」还没有配置密钥")
    pool = ms_keypool.pool_for(source_id, keys)
    kind = "movie" if req.kind == "movie" else "series"
    results = []
    for index, key in enumerate(keys, start=1):
        probe_pool = ms_keypool.KeyPool(source_id, [key])
        gate = ms_keypool.RateGate(source_id, 0.0)   # 测试不等限速
        began = time.monotonic()
        try:
            hit = spec.search(req.title.strip(), req.year, kind,
                              pool=probe_pool, gate=gate)
        except ms_sources.SourceError as exc:
            message = str(exc)
            # 只冷却**密钥自己的锅**（401 / 无效 / token 过期 / 配额耗尽）。
            # 网络超时、限流、反爬一律不冷却：那不是这把密钥的问题，
            # 把它晾起来只会在管理员网络恢复后平白少一把能用的密钥。
            if _is_key_fault(message):
                pool.note_invalid(key)
                message = f"{message}（已冷却，不再使用这把）"
            results.append({"index": index, "masked": ms_keypool.mask(key),
                            "ok": False, "message": message[:140]})
            continue
        results.append({
            "index": index, "masked": ms_keypool.mask(key), "ok": True,
            "hit": hit is not None,
            "message": (f"可用，搜到「{hit.title}」" if hit else "可用，但这片没搜到"),
            "elapsed_ms": int((time.monotonic() - began) * 1000),
        })
    return {"results": results, "config": _meta_sources_view(db)}


# ==================== 库级重刮 ====================

class LibraryRescrapeRequest(BaseModel):
    policy: str = Field(default="missing_only", description="missing_only | all")


def _run_library_rescrape(library_id: int, policy: str) -> None:
    """后台线程跑 scan_library_sync（与 scan_queue 工作线程同构：独立 Session）。

    不走 scan_queue.enqueue：policy 覆盖是本轮快照的一次性行为，不改库配置。
    """
    from backend.emby_server import scan_instrument
    from backend.emby_server.scanner import (
        LibrarySnapshot,
        ScanInProgress,
        normalize_scrape_policy,
        scan_library_sync,
    )

    db = SessionLocal()
    try:
        lib = db.query(em.Library).filter(em.Library.id == library_id).first()
        if not lib:
            logger.warning("重新刮削：媒体库 %s 不存在", library_id)
            return
        snap = LibrarySnapshot.of(lib)
        # F-P1-10：请求级 policy 恒覆盖快照，不限于 all。
        # 原逻辑只在 policy=="all" 时覆盖，库原配置为 all 而本次请求 missing_only
        # 时仍按 all 执行，与管理员当轮选择相悖。
        snap = replace(snap, scrape_policy=normalize_scrape_policy(policy))
        scan_instrument.install()  # 幂等：进度上报点
        scan_library_sync(db, lib, snap, trigger="rescrape")
    except ScanInProgress:
        logger.info("重新刮削：媒体库 %s 正在扫描中，本次触发跳过", library_id)
    except Exception:  # noqa: BLE001 — 后台线程异常只记日志，不影响接口已返回
        logger.exception("重新刮削：媒体库 %s 失败", library_id)
    finally:
        db.close()


@admin_emby_router.post("/scrape/libraries/{library_id}/rescrape")
def rescrape_library(
    library_id: int,
    req: LibraryRescrapeRequest,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """库级手动刮削：触发一次该库扫描，policy 只覆盖本轮快照，不改库配置"""
    lib = db.query(em.Library).filter(em.Library.id == library_id).first()
    if not lib:
        raise HTTPException(status_code=404, detail="媒体库不存在")
    policy = req.policy if req.policy in ("missing_only", "all") else "missing_only"
    owner = node_lib.library_owner(db, lib)
    if owner is not None and owner.id != node_lib.self_node_id(db):
        raise HTTPException(
            status_code=409,
            detail=f"该库归节点「{owner.name}」扫描，请在该节点面板操作",
        )
    if scan_queue.is_busy(library_id):
        return {
            "success": True,
            "started": False,
            "already": True,
            "message": "该媒体库正在扫描中，稍后再试",
        }
    threading.Thread(
        target=_run_library_rescrape,
        args=(library_id, policy),
        daemon=True,
        name=f"rescrape-lib-{library_id}",
    ).start()
    return {
        "success": True,
        "started": True,
        "library_id": library_id,
        "policy": policy,
        "message": f"已触发重新刮削（{policy}），后台运行中",
    }


# ==================== 定时扫描（用户可控的自动扫描计划） ====================

class AutoScanSaveRequest(BaseModel):
    enabled: bool = Field(default=False, description="总开关")
    time: str = Field(default="03:00", description="每天执行时间，HH:MM（24 小时制，服务器本地时间）")


class ChaseNewSaveRequest(BaseModel):
    enabled: bool = Field(default=False, description="追新总开关")
    interval: int = Field(default=10, description="轮询间隔（分钟），5-120")
    excluded: str = Field(default="",
                          description="**排除**监听的库 ID，逗号分隔，空 = 全部启用库都监听")
    # 旧字段（包含清单）：只为还在用老前端的部署保留，后端会换算成排除清单
    libraries: Optional[str] = Field(
        default=None, description="已废弃：旧版包含清单（空 = 全部监听）")


@admin_emby_router.get("/scrape/chase-new")
def get_chase_new(
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """追新当前配置（开关 / 间隔 / 监听库 / 上次检查）"""
    result = {"success": True, **change_watcher.get_config(db)}

    try:
        from backend.emby_server import drive_changes as dc_mod

        result["drive_changes"] = dc_mod.status()
    except Exception:
        logger.warning("drive_changes.status() failed", exc_info=True)
        result["drive_changes"] = {
            "running": False,
            "last_poll": None,
            "last_changes": 0,
            "last_libs_triggered": 0,
        }

    recent_runs: list = []
    alerts: list = []
    total_found = 0
    try:
        from sqlalchemy import func

        run_rows = (
            db.query(em.ChaseRun)
            .order_by(em.ChaseRun.id.desc())
            .limit(10)
            .all()
        )
        recent_runs = [
            {
                "id": r.id,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "source": r.source,
                "libs_checked": r.libs_checked,
                "files_listed": r.files_listed,
                "new_found": r.new_found,
                "scans_triggered": r.scans_triggered,
                "status": r.status,
                "error": r.error,
            }
            for r in run_rows
        ]

        alert_rows = (
            db.query(em.ChaseSourceState)
            .filter(em.ChaseSourceState.consec_failures >= 3)
            .all()
        )
        alerts = [
            {
                "source_key": a.source_key,
                "consec_failures": a.consec_failures,
                "last_error": a.last_error,
                "last_ok_at": a.last_ok_at.isoformat() if a.last_ok_at else None,
            }
            for a in alert_rows
        ]

        total = db.query(func.sum(em.ChaseRun.new_found)).scalar()
        total_found = int(total) if total is not None else 0
    except Exception:
        logger.warning("chase stats query failed", exc_info=True)
        recent_runs = []
        alerts = []
        total_found = 0

    result["recent_runs"] = recent_runs
    result["alerts"] = alerts
    result["total_found"] = total_found
    return result



@admin_emby_router.put("/scrape/chase-new")
def save_chase_new(
    req: ChaseNewSaveRequest,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """保存追新配置：立即生效，无需重启"""
    cfg = change_watcher.save_config(db, req.enabled, req.interval,
                                     excluded=req.excluded, libraries=req.libraries)
    return {"success": True, **cfg}


@admin_emby_router.get("/scrape/auto-scan")
def get_auto_scan(
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """定时扫描当前配置（开关 / 时间 / 上次执行日期）"""
    return {"success": True, **auto_scan.get_config(db)}


@admin_emby_router.post("/scrape/enrich/retry-unmatched")
def retry_unmatched_items(
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """重试未匹配项（处方 5）：把「搜过、没有」终态条目捞回队列重新补搜。

    TMDB 每天新增条目、本地别名（Tier 1.5）也在变好——终态化不等于永久放弃。
    捞回的条目置 enrich_priority=50（repair 之下、默认之上），排在队首。
    """
    from backend.emby_server import enrich_worker
    n = enrich_worker.retry_unmatched(db)
    return {"success": True, "requeued": n}


@admin_emby_router.post("/scrape/enrich/requeue-inherit-children")
def requeue_inherit_children(
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """空壳 inherit 单集补排（v2.58.0）：父级已有 tmdb_id、但子集没拿到单集级
    数据（done+inherit 空壳）的，翻回 pending 重跑。

    与常态联动（父级新拿 tmdb_id 自动翻）同一谓词、同一动作；部署后跑一次即可，
    日常由联动覆盖。返回翻回条数。
    """
    from backend.emby_server import enrich_worker
    n = enrich_worker.requeue_stuck_inherit_children(db)
    return {"success": True, "requeued": n}


@admin_emby_router.get("/scrape/enrich-none-stats")
def get_enrich_none_stats(
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """none 存量只读计数（链路规划 Step 4）：done + metadata_source='none' +
    无 tmdb_id 的条目，按 item_type 分组；reschedulable_now 是当前周期下
    janitor 下一轮可自动捞回的条数（剧/电影 + 超期口径）。只读，不改库。
    """
    from backend.emby_server import enrich_worker
    return {"success": True, **enrich_worker.none_stock_stats(db)}


@admin_emby_router.get("/scrape/enrich-field-stats")
def get_enrich_field_stats(
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """字段级缺失率只读统计（链路规划 Step 5）：已刮完条目（done + 实际刮到过
    数据）的标题/年份/海报/简介/类型/演员缺失数与缺失率 + 开关状态 + 进程内
    计数。只读，不改库。
    """
    from backend.emby_server import enrich_worker
    return {"success": True, **enrich_worker.field_missing_stats(db)}


@admin_emby_router.get("/scrape/enrich-progress")
def get_enrich_progress(
    staff: base_models.WebUser = Depends(require_staff),
):
    """补全 worker 进度：enrich 待处理/进行中/成功/失败/重试中 + probe 队列 + 线程数"""
    from backend.emby_server import enrich_worker
    return {"success": True, **enrich_worker.get_progress()}


class EnrichEpisodeNightSaveRequest(BaseModel):
    enabled: bool = Field(default=False, description="单集仅夜间刮削开关")
    night_start: str = Field(default="02:00", description="夜间窗口开始 HH:MM")
    night_end: str = Field(default="06:00", description="夜间窗口结束 HH:MM")
    night_tz: str | None = Field(default=None, description="夜间窗口时区（IANA 时区名），不传则沿用后台已存值")


@admin_emby_router.get("/scrape/enrich-episode-night")
def get_enrich_episode_night(
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """单集夜间刮削配置：当前生效值 + 默认值。

    from_env 为真时环境变量覆盖了后台配置（改这里不生效）。
    时间为服务器本地时间，窗口支持跨午夜（如 22:00-06:00）。
    """
    from backend.emby_server import enrich_worker
    cfg = enrich_worker.get_episode_night_config(db)
    return {
        "success": True,
        **cfg,
        "defaults": {
            "enabled": enrich_worker.NIGHT_ONLY_DEFAULT,
            "night_start": enrich_worker.NIGHT_START_DEFAULT,
            "night_end": enrich_worker.NIGHT_END_DEFAULT,
            "night_tz": enrich_worker.NIGHT_TZ_DEFAULT,
        },
        "tz_options": enrich_worker.NIGHT_TZ_OPTIONS,
    }


@admin_emby_router.put("/scrape/enrich-episode-night")
def save_enrich_episode_night(
    req: EnrichEpisodeNightSaveRequest,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """保存单集夜间刮削配置（写 SystemConfig，60 秒内热生效，无需重启）"""
    from backend.emby_server import enrich_worker
    cfg = enrich_worker.save_episode_night_config(
        db, req.enabled, req.night_start, req.night_end, req.night_tz)
    return {"success": True, **cfg}


# ==================== 单集分项开关（批3：刮削瘦身收官）====================

class EnrichEpisodeFieldsSaveRequest(BaseModel):
    title: bool = Field(default=True, description="单集标题开关（默认开）")
    overview: bool = Field(default=True, description="单集简介开关（默认开）")
    still: bool = Field(default=True, description="单集剧照开关（默认开）")


@admin_emby_router.get("/scrape/enrich-episode-fields")
def get_enrich_episode_fields(
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """单集分项开关：当前生效值 + 默认值。

    from_env 为真时环境变量覆盖了后台配置（改这里不生效）。
    三项默认全开 = 与现状行为完全一致。
    """
    from backend.emby_server import enrich_worker
    cfg = enrich_worker.get_episode_field_config(db)
    return {
        "success": True,
        **cfg,
        "defaults": {
            "title": True,
            "overview": True,
            "still": True,
        },
    }


@admin_emby_router.put("/scrape/enrich-episode-fields")
def save_enrich_episode_fields(
    req: EnrichEpisodeFieldsSaveRequest,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """保存单集分项开关（写 SystemConfig，60 秒内热生效，无需重启）"""
    from backend.emby_server import enrich_worker
    cfg = enrich_worker.save_episode_field_config(
        db, req.title, req.overview, req.still)
    return {"success": True, **cfg}


# ==================== 刮削性能参数（链路规划 Step 1：配置面收口）====================

class EnrichTuningSaveRequest(BaseModel):
    values: dict = Field(default_factory=dict, description="9 个性能参数的待保存值（key 必须在白名单内）")
    db_pool_size: Optional[str] = Field(default=None, description="DB 连接池大小（进程启动时解析，改完需重启进程）")


@admin_emby_router.get("/scrape/enrich-tuning")
def get_enrich_tuning(
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """刮削性能参数：当前生效值 + 默认值 + 中文说明 + env 接管状态。

    线程数（enrich_workers）与 DB 池大小（db_pool_size）改完需重启进程生效，
    其余保存后 60 秒内热生效。from_env 为真时环境变量覆盖了后台配置（改这里不生效）。
    episode_quota：单集认领配额制状态（链路规划 Step 3，只读：当天已用配额等）。
    """
    from backend import database as dbmod
    from backend.emby_server import enrich_worker
    from backend.integrations import store
    cfg = enrich_worker.get_enrich_tuning_config(db)
    pool = dbmod.db_pool_config()
    # 已保存但待重启的池大小（与当前生效值不同时提示）
    saved_pool = (store.read_value(db, dbmod.CFG_DB_POOL_SIZE, "") or "").strip()
    pool["saved_pool_size"] = saved_pool
    pool["pending_restart"] = bool(saved_pool) and saved_pool != str(pool["pool_size"])
    return {"success": True, **cfg, "db_pool": pool,
            "episode_quota": enrich_worker.enrich_episode_quota_status(db)}


@admin_emby_router.put("/scrape/enrich-tuning")
def save_enrich_tuning(
    req: EnrichTuningSaveRequest,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """保存刮削性能参数（写 SystemConfig；除线程数/DB 池外 60 秒内热生效）"""
    from backend import database as dbmod
    from backend.emby_server import enrich_worker
    from backend.integrations import store
    allowed = set(enrich_worker.get_enrich_tuning_config(db)["defaults"].keys())
    unknown = [k for k in (req.values or {}) if k not in allowed]
    if unknown:
        raise HTTPException(status_code=400, detail=f"未知参数: {', '.join(unknown)}")
    pool_size = None
    if req.db_pool_size is not None:
        try:
            pool_size = max(1, int(str(req.db_pool_size).strip()))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="db_pool_size 必须是 ≥1 的整数")
        store.write_values(
            db,
            {dbmod.CFG_DB_POOL_SIZE: str(pool_size)},
            {dbmod.CFG_DB_POOL_SIZE: "DB 连接池大小（进程启动/engine 创建时解析，改完需重启进程生效）"},
        )
        db.commit()
        db.expire_all()
    cfg = enrich_worker.save_enrich_tuning_config(db, req.values or {})
    pool = dbmod.db_pool_config()
    saved_pool = (store.read_value(db, dbmod.CFG_DB_POOL_SIZE, "") or "").strip()
    pool["saved_pool_size"] = saved_pool
    pool["pending_restart"] = bool(saved_pool) and saved_pool != str(pool["pool_size"])
    return {"success": True, **cfg, "db_pool": pool}


# ==================== 媒体信息探测 worker（v2.53）====================

class ProbeResetRequest(BaseModel):
    # stuck：probing → pending；failed：failed → pending（次数清零）；
    # retrying：退避中的立即可重试；all：以上全部
    scope: str = Field("stuck", pattern="^(stuck|failed|retrying|all)$")


@admin_emby_router.get("/scrape/probe-progress")
def get_probe_progress(
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """探测进度：各状态计数 / 重试中 / 过期抢单 / 常见错误 / 速率 / 熔断 / ETA"""
    from backend.emby_server import probe_worker
    return {"success": True, **probe_worker.status_snapshot(db)}


@admin_emby_router.post("/scrape/probe/reset")
def reset_probe_queue(
    req: ProbeResetRequest,
    request: Request,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """重置卡住 / 失败的探测条目（放回 pending），调度器立即接着做"""
    from backend.emby_server import probe_worker
    try:
        out = probe_worker.reset(db, req.scope)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # 富审计：scope 与影响行数挂到 request.state，写操作审计中间件会并进操作日志
    # （调用时间/调用方/IP 由中间件记，这里只补业务细节）
    request.state.audit_extra = {
        "scope": req.scope,
        "reset": out,
        "reset_total": sum(int(v) for v in out.values()),
    }
    return {"success": True, "scope": req.scope, "reset": out}


@admin_emby_router.post("/scrape/probe/pause")
def pause_probe_worker(
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """暂停后台探测（跨进程生效；按需入队照常记录，恢复后先处理）"""
    from backend.emby_server import probe_worker
    return {"success": True, "paused": probe_worker.set_paused(db, True)}


@admin_emby_router.post("/scrape/probe/resume")
def resume_probe_worker(
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """恢复后台探测"""
    from backend.emby_server import probe_worker
    return {"success": True, "paused": probe_worker.set_paused(db, False)}


@admin_emby_router.put("/scrape/auto-scan")
def save_auto_scan(
    req: AutoScanSaveRequest,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """保存定时扫描配置：立即生效，无需重启；时间格式非法返回 400"""
    try:
        cfg = auto_scan.save_config(db, req.enabled, req.time)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, **cfg}


# ==================== 手动绑定 TMDB ID ====================

@admin_emby_router.get("/items/search")
def search_items_for_bind(
    q: str = "",
    year: int | None = None,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """按剧名关键字（+可选年份）搜索条目，供「手动绑定 TMDB」选择条目用。

    只搜顶层条目（movie/series），不返回单集/季。最多返回 20 条。
    """
    keyword = (q or "").strip()
    if not keyword:
        raise HTTPException(status_code=400, detail="请填写搜索关键字")
    # LIKE 转义：把用户输入里的 % _ \ 当字面量处理
    escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    query = db.query(em.MediaItem).filter(
        em.MediaItem.item_type.in_(("movie", "series")),
        em.MediaItem.name.ilike(f"%{escaped}%", escape="\\"),
    )
    if year:
        query = query.filter(em.MediaItem.production_year == year)
    rows = query.order_by(em.MediaItem.id.desc()).limit(20).all()
    return {
        "items": [
            {
                "id": r.id,
                "name": r.name,
                "year": r.production_year,
                "item_type": r.item_type,
                "tmdb_id": r.tmdb_id,
                "metadata_locked": bool(r.metadata_locked),
            }
            for r in rows
        ]
    }


class TmdbBindRequest(BaseModel):
    tmdb_id: str = Field(default="", description="要绑定的 TMDB ID；留空表示解绑")
    verify: bool = Field(default=True, description="绑定前先向 TMDB 校验该 ID 确实存在")
    mode: str = Field(default="missing",
                      description="补全模式：missing=仅补缺失（默认），all=全量刷新")


# IMDb ID 形如 tt1234567（tt + 纯数字）；纯数字才是 TMDB ID
_IMDB_ID_RE = re.compile(r"^tt\d+$")

# 全量刷新时清掉的字段：只清 TMDB 来源的。
# NFO 的文字目前没有字段级来源标记，所以 name 不在这里——片名是管理员可能手改过的，
# 绑错一次 ID 就顺手把人家改好的名字洗掉，是不可逆事故；简介/评分/类型/别名/图片
# 全部来自刮削，可以按 TMDB 重写。
_TMDB_OWNED_FIELDS = (
    "overview",
    "community_rating",
    "genres",
    "aliases",
    "imdb_id",
    "primary_image_url",
    "backdrop_image_url",
    "poster_path",
    "backdrop_path",
)


def _clear_tmdb_fields(item: em.MediaItem) -> None:
    """全量刷新第一步：先把 TMDB 来源的字段清空，再按新 ID 重填"""
    for field in _TMDB_OWNED_FIELDS:
        if hasattr(item, field):
            setattr(item, field, None)


def _resolve_external_id(tid: str, kind: str) -> tuple[str, str | None]:
    """IMDb ID（tt 开头）→ TMDB ID；纯数字的 TMDB ID 原样返回。

    返回 (tmdb_id, imdb_id)：第二个值非空表示输入是 IMDb ID。
    TMDB 的 find 接口按条目类型分别返回 movie_results / tv_results：优先取与条目
    类型一致的那一侧，另一侧兜底（防止条目类型判错导致找不到）。
    """
    if _IMDB_ID_RE.match(tid):
        if not tmdb_client.configured:
            raise HTTPException(status_code=400, detail="未配置 TMDB Key，无法解析 IMDb ID")
        found = tmdb_client.find_by_imdb(tid)
        primary = "tv_results" if kind == "series" else "movie_results"
        secondary = "movie_results" if kind == "series" else "tv_results"
        hit = None
        for key in (primary, secondary):
            rows = (found or {}).get(key) or []
            if rows:
                hit = rows[0]
                break
        if not hit or not hit.get("id"):
            raise HTTPException(status_code=404,
                                detail=f"IMDb {tid} 在 TMDB 上找不到对应的条目")
        return str(hit["id"]), tid
    return tid, None


@admin_emby_router.get("/scrape/items/{item_id}/tmdb-preview")
def preview_tmdb_id(
    item_id: int,
    tmdb_id: str,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """输入 TMDB ID 时实时预览：管理员要先看清「这到底是哪部片」再决定绑不绑。

    预览只读，不写库。返回的 title/year 会显示在确认框里，避免手滑绑错。
    接受两种输入：纯数字的 TMDB ID，或 tt 开头的 IMDb ID（走 TMDB find 接口换算）。
    """
    item = db.query(em.MediaItem).filter(em.MediaItem.id == item_id).first()
    if item is None:
        raise HTTPException(status_code=404, detail="条目不存在")
    tid = (tmdb_id or "").strip()
    if not tid or not (tid.isdigit() or _IMDB_ID_RE.match(tid)):
        raise HTTPException(status_code=400, detail="TMDB ID 必须是数字，IMDb ID 以 tt 开头")
    if not tmdb_client.configured:
        raise HTTPException(status_code=400, detail="未配置 TMDB Key，无法校验")
    kind = "series" if item.item_type in ("series", "season", "episode") else "movie"
    tid, imdb_id = _resolve_external_id(tid, kind)
    try:
        data = tmdb_client.details(tid, kind)
    except TmdbTransientError as exc:
        logger.warning("TMDB 校验暂时不可用 id=%s: %s", item.id, exc)
        raise HTTPException(
            status_code=503,
            detail="TMDB 暂时不可用（网络/限流），请稍后再试") from exc
    if not data:
        raise HTTPException(status_code=404, detail=f"TMDB 上找不到 ID {tid}")
    title = data.get("name") or data.get("title") or ""
    year = (data.get("first_air_date") or data.get("release_date") or "")[:4]
    return {
        "tmdb_id": tid,
        "imdb_id": imdb_id,
        "title": title,
        "year": year or None,
        "poster": data.get("poster_path"),
        "current_name": item.name,
        "current_tmdb_id": item.tmdb_id,
        "matches_current": bool(title) and _norm_name(title) == _norm_name(item.name or ""),
    }


def _norm_name(s: str) -> str:
    """归一化片名用于「是否就是当前这条」的提示（不参与任何写库判断）"""
    import re as _re
    return _re.sub(r"[\s:：·\-—_、,，!！?？~～'\"“”‘’()（）]+", "", (s or "").lower())


@admin_emby_router.post("/scrape/items/{item_id}/bind-tmdb")
def bind_tmdb_id(
    item_id: int,
    req: TmdbBindRequest,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """手动指定 TMDB ID（或解绑）。

    背景：TMDB 对中文剧集/综艺收录偏少，少数条目自动刮削怎么搜都搜不到，
    反复重试也是白试。给管理员一个**手动出口**——比再接第四个数据源更治本。

    绑定后立刻取详情补全（图/简介/评分/别名），省得还要再点一次重刮。

    mode=all（全量刷新）：先把 TMDB 来源的字段清空（保留片名 name——那是管理员
    可能手改过的，不碰），再按新 ID 把简介/评分/类型/别名/图片全部重写一遍。
    """
    item = db.query(em.MediaItem).filter(em.MediaItem.id == item_id).first()
    if item is None:
        raise HTTPException(status_code=404, detail="条目不存在")
    if item.item_type not in ("movie", "series", "season", "episode"):
        raise HTTPException(status_code=400, detail=f"不支持的条目类型：{item.item_type}")

    tid = (req.tmdb_id or "").strip()
    notes: list[str] = []
    mode = (req.mode or "missing").strip()
    if mode not in ("missing", "all"):
        raise HTTPException(status_code=400, detail="mode 必须是 missing 或 all")

    if not tid:
        # 解绑
        before = item.tmdb_id
        item.tmdb_id = None
        item.last_scraped_at = None
        item.metadata_source = None
        item.enrich_status = "pending"
        item.enrich_attempts = 0
        item.enrich_next_retry_at = None
        # 解绑即解锁：绑定时加的锁随之解除，恢复自动刮削
        item.metadata_locked = False
        db.commit()
        return {
            "success": True, "unbound": True,
            "item": {"id": item.id, "name": item.name, "item_type": item.item_type},
            "notes": [f"已解绑（原 TMDB {before}）并重新排入补全队列"],
        }

    if not (tid.isdigit() or _IMDB_ID_RE.match(tid)):
        raise HTTPException(status_code=400, detail="TMDB ID 必须是数字，IMDb ID 以 tt 开头")

    kind = "series" if item.item_type in ("series", "season", "episode") else "movie"
    # IMDb ID 先换算成 TMDB ID；库里存的永远是 TMDB ID
    tid, imdb_id = _resolve_external_id(tid, kind)

    if mode == "all" and not tmdb_client.configured:
        raise HTTPException(status_code=400, detail="未配置 TMDB Key，全量刷新取不到数据")

    data = None
    if req.verify:
        if not tmdb_client.configured:
            raise HTTPException(status_code=400, detail="未配置 TMDB Key，无法校验")
        try:
            data = tmdb_client.details(tid, kind)
        except TmdbTransientError as exc:
            logger.warning("绑定 TMDB 校验暂时不可用 id=%s: %s", item.id, exc)
            raise HTTPException(
                status_code=503,
                detail="TMDB 暂时不可用（网络/限流），请稍后再试") from exc
        if not data:
            raise HTTPException(status_code=404, detail=f"TMDB 上找不到 ID {tid}，未绑定")
        notes.append(f"已校验：{data.get('name') or data.get('title')}")
    if imdb_id:
        notes.append(f"IMDb {imdb_id} → TMDB {tid}")

    item.tmdb_id = tid
    item.last_scraped_at = datetime.now()
    item.metadata_source = "tmdb"
    if tmdb_client.configured and (req.verify or mode == "all"):
        try:
            if data is None:
                data = tmdb_client.details(tid, kind)
        except TmdbTransientError as exc:
            logger.warning("绑定后补详情暂时不可用 id=%s: %s", item.id, exc)
            data = None
        if data:
            prewarm_images(data)
            if mode == "all":
                # 全量刷新：先清空 TMDB 来源字段（保留 name），再按新 ID 重填
                _clear_tmdb_fields(item)
                tmdb_client.apply_details(item, data)
                notes.append("全量刷新：简介/评分/类型/别名已按 TMDB 重写")
                if tmdb_client.apply_images(item, data):
                    notes.append("全量刷新：海报/背景图已按 TMDB 重写")
            else:
                # 仅补缺失（不覆盖 NFO 已提供的文字 / 已有图片）
                if not (item.imdb_id and item.aliases):
                    tmdb_client.apply_details(item, data)
                    notes.append("补齐 IMDb/别名")
                if not (item.poster_path or item.primary_image_url):
                    if tmdb_client.apply_images(item, data):
                        notes.append("补齐海报")
    item.enrich_status = "done"
    item.enrich_attempts = 0
    item.enrich_next_retry_at = None
    # 绑定成功即锁定：这是管理员的手动成果，自动刮削不再碰它
    #（enrich worker 抢单时跳过 metadata_locked=True 的条目）。
    # 解绑（unbind 分支）时解锁，恢复自动刮削。
    item.metadata_locked = True
    notes.append("已锁定：自动刮削不再覆盖（解绑可解锁）")
    db.commit()
    return {
        "success": True, "unbound": False,
        "item": {"id": item.id, "name": item.name, "item_type": item.item_type,
                 "tmdb_id": item.tmdb_id},
        "notes": notes,
    }


# ==================== 元数据锁定（P3，Emby 式手动识别） =============
def _item_lock_view(item: "em.MediaItem") -> dict:
    """条目锁定视图：名字/类型/TMDB ID/锁定态，供前端行按钮与详情展示"""
    return {
        "id": item.id,
        "name": item.name,
        "item_type": item.item_type,
        "tmdb_id": item.tmdb_id,
        "metadata_locked": bool(item.metadata_locked),
    }


def _lock_item_or_404(db: Session, item_id: int) -> "em.MediaItem":
    item = db.query(em.MediaItem).filter(em.MediaItem.id == item_id).first()
    if item is None:
        raise HTTPException(status_code=404, detail="条目不存在")
    return item


@admin_emby_router.get("/scrape/items/{item_id}")
def get_item_lock_status(
    item_id: int,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """条目详情（含 metadata_locked，供管理端展示锁定状态）。"""
    return {"success": True, "item": _item_lock_view(_lock_item_or_404(db, item_id))}


@admin_emby_router.post("/scrape/items/{item_id}/lock")
def lock_item_metadata(
    item_id: int,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """锁定条目元数据（Emby 式）：自动补全（enrich 抢单）不再碰它，
    防止自动刷新覆盖管理员手动整理/手动识别的成果。
    手动操作（手动绑定 bind-tmdb、手动重刮 rescrape）不受锁定影响——手动永远优先于锁定。
    """
    item = _lock_item_or_404(db, item_id)
    item.metadata_locked = True
    db.commit()
    return {"success": True, "item": _item_lock_view(item)}


@admin_emby_router.post("/scrape/items/{item_id}/unlock")
def unlock_item_metadata(
    item_id: int,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """解锁条目元数据：恢复自动补全资格。"""
    item = _lock_item_or_404(db, item_id)
    item.metadata_locked = False
    db.commit()
    return {"success": True, "item": _item_lock_view(item)}
def _tmdb_candidate_view(hit: dict, kind: str) -> dict:
    """把 TMDB 原始 hit 压成前端候选卡片要的形状（只读投影，不写库）。"""
    title = hit.get("name") or hit.get("title") or ""
    date = hit.get("first_air_date") or hit.get("release_date") or ""
    year = date[:4] if len(date) >= 4 and date[:4].isdigit() else None
    return {
        "tmdb_id": hit.get("id"),
        "title": title,
        "year": year,
        "overview": (hit.get("overview") or "")[:200],
        "poster_path": hit.get("poster_path"),
        "media_type": hit.get("media_type") or ("tv" if kind == "series" else "movie"),
    }


@admin_emby_router.get("/scrape/tmdb/search")
def search_tmdb_candidates(
    q: str,
    year: int | None = None,
    kind: str = "series",
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """Emby 式手动识别：搜剧名 → 返回 TMDB 候选列表 → 管理员点一下绑定。

    刮削要的是「唯一正确答案」（search() 会按置信度裁剪、宁缺毋滥），
    手动识别要的是「一组候选」：用户搜什么就列什么，选哪部由他决定。
    year 可选：给了就只留年份一致的候选（重名翻拍/同名剧就靠它区分）。
    失败一律返回空列表，不 500——TMDB 抖动或没配 key 都不该把搜索框打成报错。
    """
    keyword = (q or "").strip()
    search_kind = "movie" if kind == "movie" else "series"
    try:
        if not keyword:
            return {"candidates": []}
        hits = tmdb_client.search_candidates(keyword, search_kind, limit=10)
    except Exception:  # noqa: BLE001 — 与 search_candidates 同口径：失败不当成致命错误
        logger.warning("TMDB 候选搜索失败：q=%r kind=%s", keyword, search_kind, exc_info=True)
        return {"candidates": []}
    candidates = [_tmdb_candidate_view(h, search_kind) for h in hits if isinstance(h, dict)]
    if year:
        candidates = [c for c in candidates if c["year"] == str(year)]
    return {"candidates": candidates}


# ==================== 豆瓣（可选）：中文兜底数据源 ====================

@admin_emby_router.get("/scrape/douban-config")
def get_douban_config(
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """读取豆瓣配置：总开关（默认关）+ 限速（秒/次）"""
    from backend.emby_server import douban as _dbn
    return {
        "enabled": _dbn.enabled(db),
        "min_interval": _dbn.min_interval(db),
    }


class _DoubanConfigBody(BaseModel):
    enabled: bool = True
    min_interval: float = 1.0


@admin_emby_router.put("/scrape/douban-config")
def save_douban_config(
    body: _DoubanConfigBody,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """保存豆瓣配置（保存即热生效）"""
    from backend.emby_server import douban as _dbn
    from backend.integrations import store
    enabled = body.enabled
    iv = max(0.0, float(body.min_interval or 0))
    store.write_values(
        db,
        {
            _dbn.DOUBAN_ENABLED_KEY: "1" if enabled else "0",
            _dbn.DOUBAN_RATE_KEY: str(iv),
        },
        {
            _dbn.DOUBAN_ENABLED_KEY: "豆瓣（可选）总开关（中文标题 TMDB 未命中时走豆瓣兜底）",
            _dbn.DOUBAN_RATE_KEY: "豆瓣请求最小间隔秒数",
        },
    )
    db.commit()
    return {"success": True, "enabled": enabled, "min_interval": iv}


# ==================== 备用链路限速（配置瘦身第 3 批） ====================
# altmeta 是豆瓣主链路封禁/降级后的备用链路（豆瓣 subject_suggest + Bangumi 兜底），
# 它的两个最小间隔秒数（altmeta_douban_rate / altmeta_bangumi_rate）此前无后台入口。


@admin_emby_router.get("/scrape/altmeta-rates")
def get_altmeta_rates(
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """读取备用链路限速：豆瓣备用 / Bangumi 兜底的最小间隔秒数（0=不限速）"""
    from backend.emby_server import altmeta as _am
    return {
        "douban_rate": _am.min_interval(db),
        "bangumi_rate": _am.min_interval_bangumi(db),
    }


class _AltmetaRatesBody(BaseModel):
    douban_rate: float = 1.0
    bangumi_rate: float = 1.0


@admin_emby_router.put("/scrape/altmeta-rates")
def save_altmeta_rates(
    body: _AltmetaRatesBody,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """保存备用链路限速（保存即热生效）；0=不限速，上限 60 秒"""
    from backend.emby_server import altmeta as _am
    from backend.integrations import store
    iv_d = max(0.0, min(60.0, float(body.douban_rate or 0)))
    iv_b = max(0.0, min(60.0, float(body.bangumi_rate or 0)))
    store.write_values(
        db,
        {
            _am.CONFIG_RATE: str(iv_d),
            _am.CONFIG_BANGUMI_RATE: str(iv_b),
        },
        {
            _am.CONFIG_RATE: "备用链路（豆瓣 subject_suggest）请求最小间隔秒数",
            _am.CONFIG_BANGUMI_RATE: "备用链路（Bangumi 兜底）请求最小间隔秒数",
        },
    )
    db.commit()
    return {"success": True, "douban_rate": iv_d, "bangumi_rate": iv_b}


# ==================== 豆瓣封禁降级（链路规划 Step 0.5） ====================

class _DoubanBanDowngradeBody(BaseModel):
    enabled: bool = False


@admin_emby_router.get("/scrape/douban-ban-downgrade")
def get_douban_ban_downgrade(
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """读取豆瓣封禁降级开关 + 当前封禁状态（只读，秒级新鲜）。"""
    from backend.emby_server import enrich_worker
    return {"success": True, **enrich_worker.get_douban_ban_downgrade_config(db)}


@admin_emby_router.put("/scrape/douban-ban-downgrade")
def save_douban_ban_downgrade(
    body: _DoubanBanDowngradeBody,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """保存豆瓣封禁降级开关（写 SystemConfig，60 秒内热生效，无需重启）。"""
    from backend.emby_server import enrich_worker
    cfg = enrich_worker.save_douban_ban_downgrade_config(db, body.enabled)
    return {"success": True, **cfg}


# ==================== 兜底链路统一（链路规划 Step 2） ====================

class _UnifiedFallbackBody(BaseModel):
    enabled: bool = False


@admin_emby_router.get("/scrape/unified-fallback")
def get_unified_fallback(
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """读取兜底链路统一开关（只读，秒级新鲜）。"""
    from backend.emby_server import enrich_worker
    return {"success": True, **enrich_worker.get_unified_fallback_config(db)}


@admin_emby_router.put("/scrape/unified-fallback")
def save_unified_fallback(
    body: _UnifiedFallbackBody,
    staff: base_models.WebUser = Depends(require_staff),
    db: Session = Depends(get_db),
):
    """保存兜底链路统一开关（写 SystemConfig，60 秒内热生效，无需重启）。

    开 = 一个豆瓣入口（豆瓣 rich → 多源引擎含豆瓣源）；
    关 = 回滚到 Step 1 部署后的原行为。
    """
    from backend.emby_server import enrich_worker
    cfg = enrich_worker.save_unified_fallback_config(db, body.enabled)
    return {"success": True, **cfg}


@admin_emby_router.get("/scrape/scheduler/status")
def get_scheduler_status(
    staff: base_models.WebUser = Depends(require_staff),
):
    """统一任务调度器状态（v2.54.1）：一次看到扫描队列 + 刮削积压 + 让路/熔断状态。

    给管理后台"后台任务"统一视图用。
    """
    from backend.emby_server import task_scheduler as _ts

    return {"success": True, "status": _ts.get_unified_status()}
