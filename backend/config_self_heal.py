"""配置自愈（借鉴 twilight-kotomi 的设计）

痛点：每次新增 env 配置项都要手动改 env.example 再同步到生产，经常漏；
SystemConfig 新增键同样靠人工补行。

做法：后端启动时（init_db 之后）跑一次 ``run_config_self_heal()``：

1. **env 自愈**：解析 ``env.example`` 里声明的所有 ``KEY=默认值``
   （生效的保持生效、注释掉的保持注释），``.env`` 里没有的 key 按原样
   追加到 ``.env`` 末尾。
   **例外**：会改变「数据落点 / 对外身份 / 暴露面」的 key 不自动补齐，
   只记一条 WARNING（见 ``_NEVER_AUTO_FILL``）——示例里的默认值只对
   「按示例新建的部署」成立，照抄到已经在跑的老部署上会连错库或把用户
   指到假域名；
2. **SystemConfig 自愈**：把代码各模块定义的 ``(key, 默认值)`` 注册表
   拿出来比对，数据库里没有的 key 插入默认值行。

纪律（与 AGENTS.md「横切能力只许一套」一致）：

- env 的注册表就是 ``env.example`` 本身，不另维护一份；
- SystemConfig 的默认值**引用**各模块已有的常量 / SPEC，
  不在注册表里手写第二份（默认值只许定义一次）；
- 读配置仍走原有入口（``os.getenv`` / ``integrations.store``），
  本模块只负责「补缺」，不提供新的读取方式。

幂等：只补缺失，已存在的 key 绝不覆盖；补了哪些 key 用 logger.info 记录。
任何异常内部消化、只记日志，绝不阻断启动。
"""
from __future__ import annotations

import importlib
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# backend/config_self_heal.py -> backend/ -> 项目根目录（与 backend/__init__.py 的算法一致）
ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = ROOT / "env.example"
ENV_FILE = ROOT / ".env"

# ---------------------------------------------------------------------------
# 绝不自动补齐的 key
# ---------------------------------------------------------------------------
# 自愈读的是 env.example 里**生效的默认行**。那些默认值对「按示例新建的部署」
# 成立，对「已经在跑的老部署」未必成立，而且写进 .env 之后会**永久生效**。
#
# 典型事故（本文件写这条护栏的直接原因）：
#   老部署把 DATABASE_URL 交给 docker-compose 的 environment 注入，.env 里没写它；
#   自愈把 env.example 的 postgresql://aetrix:aetrix@postgres:5432/aetrix 原样追加
#   之后，compose 的 `${DATABASE_URL:-...}` 改从 .env 取 → 用错密码连库 →
#   整站失去数据库。而且自愈跑在 lifespan 里（backend/database.py 早已 import
#   完），**本次启动照常、下次重启才炸**，是个延迟炸弹。
#
# 这些值没有「安全的默认」，只能由部署者按自己那台机器显式写进 .env。
# 注意：过滤只作用于**生效**的默认行；注释项原样追加只是文档，不改变行为。
_NEVER_AUTO_FILL = frozenset({
    # 数据落点：连错库是数据事故，不是配置问题
    "DATABASE_TYPE",
    "DATABASE_URL",
    "POSTGRES_PASSWORD",
    # 对外身份：用户端账号卡 / 播放器一键导入的地址、客户端认服务器的标识
    "EMBY_PUBLIC_URL",
    "EMBY_SERVER_ID",
    # 部署形态与暴露面
    "ENABLE_EMBY_GATEWAY",
    "EMBY_API_PUBLIC_URL",
    "EM_PANEL_URL",
    "EM_GATEWAY_REALM",
    "NODE_KEY",
    "REALM",
})

# 匹配 KEY=...（active）与 # KEY=...（注释掉的可选项）
_ACTIVE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
_COMMENTED_RE = re.compile(r"^\s*#\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _iter_example_items(path: Path = ENV_EXAMPLE):
    """解析 env.example，产出 (key, 原始行, 是否生效)；key 去重（首次出现为准）。"""
    items = []
    seen = set()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("[配置自愈] %s 不存在，跳过 env 自愈", path)
        return items
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\n")
        m = _ACTIVE_RE.match(line)
        if m and not line.lstrip().startswith("#"):
            key = m.group(1)
            active = True
        else:
            m = _COMMENTED_RE.match(line)
            if not m:
                continue
            key = m.group(1)
            active = False
        if key in seen:
            continue
        seen.add(key)
        items.append((key, line, active))
    return items


def _existing_env_keys(path: Path = ENV_FILE) -> set[str]:
    """读 .env 已有的 key（生效的和注释掉的都算「有」，注释掉视为管理员主动关闭）。"""
    keys: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return keys
    for line in text.splitlines():
        m = _ACTIVE_RE.match(line)
        if m and not line.lstrip().startswith("#"):
            keys.add(m.group(1))
            continue
        m = _COMMENTED_RE.match(line)
        if m:
            keys.add(m.group(1))
    return keys


def _value_of_active_line(line: str) -> str:
    """从 'KEY=value' 原始行里抠出 value（去掉首尾空白与成对引号）。"""
    value = line.split("=", 1)[1].strip() if "=" in line else ""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return value


def heal_env_file(example_path: Path = ENV_EXAMPLE, env_path: Path = ENV_FILE) -> list[str]:
    """env 自愈：.env 里缺的 key 按 env.example 的原样追加；返回补了的 key 列表。"""
    items = _iter_example_items(example_path)
    if not items:
        return []
    existing = _existing_env_keys(env_path)
    missing = [(k, line, active) for (k, line, active) in items if k not in existing]

    # 拦下发现在「生效」位置的敏感默认值：不写进 .env，只告警。
    # 只补注释项不影响行为，所以不拦。
    blocked = sorted(k for (k, _, active) in missing if active and k in _NEVER_AUTO_FILL)
    if blocked:
        logger.warning(
            "[配置自愈] 这些 key 在 .env 里缺失，但不自动补齐（示例默认值只对新部署成立，"
            "照抄到已有部署上会连错库 / 把用户指到假地址）：%s。"
            "需要哪个请按 env.example 的说明显式写进 .env。",
            "、".join(blocked),
        )
        missing = [item for item in missing if not (item[2] and item[0] in _NEVER_AUTO_FILL)]

    if not missing:
        return []

    created = not env_path.exists()
    active_lines = [line for (_, line, active) in missing if active]
    commented_lines = [line for (_, line, active) in missing if not active]

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    buf = []
    buf.append("")
    buf.append("# ===== 配置自愈（backend/config_self_heal.py）：env.example 里有、.env 里缺的项，按示例原样补齐 =====")
    buf.append(f"# 自愈时间：{stamp}；只补缺失，已存在的 key 不会被覆盖")
    buf.extend(active_lines)
    if commented_lines:
        buf.append("# ----- 以下为示例里默认注释的可选调优项（保持注释，需要时自行取消注释） -----")
        buf.extend(commented_lines)

    # 保证追加前文件以换行结尾
    if created:
        env_path.write_text("\n".join(buf).lstrip("\n") + "\n", encoding="utf-8")
        logger.info("[配置自愈] .env 不存在，已创建并写入 %d 个生效项、%d 个注释项",
                    len(active_lines), len(commented_lines))
    else:
        with env_path.open("a", encoding="utf-8") as f:
            text = env_path.read_text(encoding="utf-8")
            if text and not text.endswith("\n"):
                f.write("\n")
            f.write("\n".join(buf) + "\n")

    healed = []
    for key, line, active in missing:
        if active:
            # 让当前进程也与文件一致（语义同 load_dotenv(override=False)：只填真正的缺失）
            os.environ.setdefault(key, _value_of_active_line(line))
            logger.info("[配置自愈] .env 补齐 %s=%s", key, _value_of_active_line(line))
        else:
            logger.info("[配置自愈] .env 补齐注释项 %s（保持注释，不改变行为）", key)
        healed.append(key)
    return healed


# ---------------------------------------------------------------------------
# SystemConfig 自愈注册表
# ---------------------------------------------------------------------------
# 每个条目 (key, 默认值, 说明)。默认值的唯一来源是各模块自己的常量 / SPEC，
# 这里只做引用 —— 改默认值只改模块里的定义，不用动这里。


def _spec_defaults(slug: str) -> list[tuple[str, str, str]]:
    """能力中心 SPEC 字段的 (key, 默认值, 说明)。

    默认值算法与 backend/integrations/__init__.py::_defaults_for 完全一致：
    ``"" if f.get("default") is None else str(f["default"])``。
    """
    mod = importlib.import_module(f"backend.integrations.{slug}")
    spec = mod.SPEC
    title = spec.get("title") or slug
    out = []
    for f in spec.get("fields") or []:
        default = f.get("default")
        out.append((
            f["key"],
            "" if default is None else str(default),
            f"{title}：{f.get('label') or f['key']}",
        ))
    return out


def _spec_field_defaults(slug: str) -> list[tuple[str, str, str]]:
    """ai / branding / captcha 三个模块自带 _FIELD_DEFAULTS（算法是 (default or "")），直接引用。"""
    mod = importlib.import_module(f"backend.integrations.{slug}")
    spec = mod.SPEC
    title = spec.get("title") or slug
    labels = {f["key"]: f.get("label") or f["key"] for f in spec.get("fields") or []}
    return [(k, v, f"{title}：{labels.get(k, k)}") for k, v in mod._FIELD_DEFAULTS.items()]


def collect_system_config_defaults() -> list[tuple[str, str, str]]:
    """汇总代码里定义的所有 SystemConfig (key, 默认值, 说明)。"""
    items: list[tuple[str, str, str]] = []

    # 1. 能力中心（proxy / mail / telegram / geoip 走 SPEC；ai / branding / captcha 有 _FIELD_DEFAULTS）
    for slug in ("proxy", "mail", "telegram", "geoip"):
        items.extend(_spec_defaults(slug))
    for slug in ("ai", "branding", "captcha"):
        items.extend(_spec_field_defaults(slug))

    # 2. 定时扫描
    from backend.emby_server import auto_scan
    items.extend([
        (auto_scan.CONFIG_ENABLED, "0", "定时扫描开关（元数据与刮削）"),
        (auto_scan.CONFIG_TIME, auto_scan.DEFAULT_TIME, "定时扫描每天执行时间（HH:MM）"),
    ])

    # 3. 数据库定时备份
    from backend.emby_server import db_backup
    items.extend([
        (db_backup.CONFIG_ENABLED, "1" if db_backup.DEFAULT_ENABLED else "0", "数据库定时备份开关"),
        (db_backup.CONFIG_TIME, db_backup.DEFAULT_TIME, "数据库备份每天执行时间（HH:MM）"),
        (db_backup.CONFIG_KEEP_DAYS, str(db_backup.DEFAULT_KEEP_DAYS), "数据库备份保留天数"),
    ])

    # 4. 新片入库通知
    from backend.emby_server import new_media_notify
    items.extend([
        (new_media_notify.CONFIG_ENABLED,
         "1" if new_media_notify.DEFAULT_ENABLED else "0", "新片入库通知开关"),
        (new_media_notify.CONFIG_CHANNELS, new_media_notify.DEFAULT_CHANNELS, "新片入库通知渠道"),
    ])

    # 5. 追新（chase-new）：只收用户配置项，last_check / last_found 是运行时水位，job 自己会写
    from backend.emby_server import change_watcher
    items.extend([
        (change_watcher.CONFIG_ENABLED, "0", "追新开关（老剧出新集自动入库）"),
        (change_watcher.CONFIG_INTERVAL, str(change_watcher.DEFAULT_INTERVAL), "追新轮询间隔（分钟）"),
        (change_watcher.CONFIG_LIBRARIES, "", "追新库过滤（留空=全部库）"),
    ])

    # 6. TMDB API Key（管理员在后台填）+ 镜像地址 + 密钥冷却时长
    from backend.emby_server import tmdb
    items.extend([
        (tmdb.TMDB_KEYS_CONFIG_KEY, "", "TMDB API Key（多个换行分隔，后台填写）"),
        (tmdb.TMDB_API_BASE_CONFIG_KEY, "", "TMDB API 镜像/反代地址（留空 = 官方地址）"),
        (tmdb.TMDB_IMAGE_BASE_CONFIG_KEY, "", "TMDB 图片 CDN 镜像地址（留空 = 官方地址）"),
        (tmdb.TMDB_KEY_COOLDOWN_CONFIG_KEY, str(tmdb.DEFAULT_KEY_COOLDOWN_SEC),
         "单把 TMDB Key 被限流后的冷却秒数"),
        (tmdb.TMDB_KEY_INVALID_COOLDOWN_CONFIG_KEY, str(tmdb.DEFAULT_KEY_INVALID_COOLDOWN_SEC),
         "单把 TMDB Key 返回 401 后的冷却秒数"),
    ])

    # 6.5 多源元数据（Phase 6b）：总开关 + 中文优先 + 顺序 + 逐源开关/密钥池/限速
    from backend.emby_server.metasources import config as ms_config
    from backend.emby_server.metasources import sources as ms_sources
    items.extend([
        (ms_config.CONFIG_ENABLED, "0",
         "多源元数据补全总开关（关闭 = 只用原来的 NFO/TMDB/豆瓣链路）"),
        (ms_config.CONFIG_PREFER_CN, "1", "中文信息优先（标题与简介）"),
        (ms_config.CONFIG_ORDER,
         json.dumps(ms_config.DEFAULT_ORDER, ensure_ascii=False),
         "元数据源采集顺序（靠前的源先取，字段按序填充）"),
    ])
    for _spec in ms_sources.SPECS:
        items.extend([
            (ms_config.CONFIG_SOURCE_ENABLED.format(id=_spec.id), "1",
             f"元数据源「{_spec.label}」开关"),
            (ms_config.CONFIG_SOURCE_RATE.format(id=_spec.id),
             str(ms_config.DEFAULT_RATE), f"元数据源「{_spec.label}」请求最小间隔秒数"),
        ])
        # 密钥池键：TMDB 不在这里注册——它的密钥只有一处存储（tmdb_api_keys，
        # 见上面的 6a 段）。再注册一个 meta_source_keys_tmdb 就是两份密钥、
        # 两个入口，后台还会显示两遍。
        if _spec.id not in ms_config.LEGACY_KEY_CONFIG:
            items.append((
                ms_config.CONFIG_SOURCE_KEYS.format(id=_spec.id), "",
                f"元数据源「{_spec.label}」密钥池（逗号分隔，按顺序轮换）"))

    # 7. 播放策略
    from backend import playback_policy
    items.extend([
        (k, v, f"播放策略：{k}") for k, v in playback_policy.POLICY_DEFAULTS.items()
    ])

    # 7.4 活力值（C1 竞品借鉴）：默认开启/上限14/每日扣1/阈值3/1点=10积分
    from backend import vitality
    items.extend([
        (k, v, f"活力值：{k}") for k, v in vitality.VITALITY_DEFAULTS.items()
    ])



    # 7.7 求片中心：每日额度（规则与默认值都在 backend/media_seek.py）
    from backend import media_seek
    items.append((media_seek.CONFIG_DAILY_LIMIT, str(media_seek.DEFAULT_DAILY_LIMIT),
                  "用户每日求片上限（超过当天不能再提交）"))
    # 7.7b 求片中心：月度额度（公益/付费区分，与旧版公益服求片中心合并）
    items.append((media_seek.CONFIG_MONTHLY_WELFARE, str(media_seek.DEFAULT_MONTHLY_WELFARE),
                  "公益服用户每月求片上限"))
    items.append((media_seek.CONFIG_MONTHLY_PAID, str(media_seek.DEFAULT_MONTHLY_PAID),
                  "付费用户每月求片上限"))
    # v2 求片：总开关 / 附议 / 附议者通知（全部可配置，默认开）
    items.append((media_seek.CONFIG_ENABLED, "1",
                  "求片总开关（关闭后用户不能提交求片）"))
    items.append((media_seek.CONFIG_VOTE_ENABLED, "1",
                  "求片附议开关（用户可对他人的求片附议+1）"))
    items.append((media_seek.CONFIG_NOTIFY_VOTERS, "1",
                  "求片入库时是否通知附议者"))

    # 7.8 防共享（跨城市轨迹 + 同播检测）：两档都默认 off = 与升级前完全一致，
    # enforce（自动停用 / 自动拦截）只能由管理员显式选，绝不自动生效
    from backend import share_guard
    items.extend([
        (share_guard.CONFIG_TRAVEL_ACTION, share_guard.DEFAULTS[share_guard.CONFIG_TRAVEL_ACTION],
         "防共享·跨城市轨迹处置档位（off/record/alert/enforce，默认 off）"),
        (share_guard.CONFIG_TRAVEL_WINDOW,
         share_guard.DEFAULTS[share_guard.CONFIG_TRAVEL_WINDOW],
         "防共享·城市切换判定时间窗口（分钟）"),
        (share_guard.CONFIG_CONCURRENT_ACTION,
         share_guard.DEFAULTS[share_guard.CONFIG_CONCURRENT_ACTION],
         "防共享·同播检测处置档位（off/record/alert/enforce，默认 off）"),
        (share_guard.CONFIG_CONCURRENT_LIMIT,
         share_guard.DEFAULTS[share_guard.CONFIG_CONCURRENT_LIMIT],
         "防共享·同时播放数上限（超过即判定为同播）"),
        (share_guard.CONFIG_RETENTION_DAYS,
         share_guard.DEFAULTS[share_guard.CONFIG_RETENTION_DAYS],
         "防共享事件保留天数（0 = 不自动清理）"),
    ])

    # 7.9 媒体库可见范围：默认关闭 = 所有启用的库都可见（升级前行为）
    from backend import library_scope
    items.extend([
        (library_scope.CONFIG_DEFAULT_ENABLED,
         library_scope.DEFAULTS[library_scope.CONFIG_DEFAULT_ENABLED],
         "媒体库可见范围·服务器默认总开关（0 = 关闭，所有启用的库都可见）"),
        (library_scope.CONFIG_DEFAULT_LIBRARIES,
         library_scope.DEFAULTS[library_scope.CONFIG_DEFAULT_LIBRARIES],
         "媒体库可见范围·服务器默认可见的库 id（JSON 数组，总开关关闭时无效）"),
        (library_scope.CONFIG_USER_OVERRIDES,
         library_scope.DEFAULTS[library_scope.CONFIG_USER_OVERRIDES],
         "媒体库可见范围·逐用户覆盖（JSON，enabled=false = 跟随服务器默认）"),
    ])

    # 7.10 推广奖励（v2.44.0）：**默认关闭 + 0** —— 管理员不手动打开，
    # 邀请成功就不会多发任何东西（本期唯一的自动发奖动作，故默认关）
    from backend import promotion
    items.extend([
        (key, promotion.DEFAULTS[key], promotion.DESCRIPTIONS[key])
        for key in (promotion.CONFIG_ENABLED, promotion.CONFIG_TYPE,
                    promotion.CONFIG_AMOUNT, promotion.CONFIG_DAYS)
    ])

    # 7.11 访问拦截（UA 关键词 + IP 归属地）：两个开关**默认 false**
    # = 升级后行为与升级前完全一致，不会因为升级就有人打不开站；
    # 归属地规则还依赖地理能力，没配地理库时判定自动失效放行。
    from backend import access_guard
    items.extend([
        (key, access_guard.DEFAULTS[key], access_guard.DESCRIPTIONS[key])
        for key in (access_guard.CONFIG_UA_ENABLED, access_guard.CONFIG_UA_ALLOW,
                    access_guard.CONFIG_UA_DENY, access_guard.CONFIG_REGION_ENABLED,
                    access_guard.CONFIG_REGION_MODE,
                    access_guard.CONFIG_REGION_COUNTRIES,
                    access_guard.CONFIG_REGION_KEYWORDS)
    ])

    # 7.12 防盗链（播放签名，2026-10-17 起 .strm 签名已随 strm_gen 删除）：默认全部启用 = 与升级前一致
    from backend.emby_server import play_sign
    items.extend([
        (play_sign.CONFIG_HOTLINK_ENABLED, "true", play_sign.DESCRIPTIONS[play_sign.CONFIG_HOTLINK_ENABLED]),
        (play_sign.CONFIG_PLAY_SIGN_TTL, str(play_sign.PLAY_TTL_DEFAULT),
         play_sign.DESCRIPTIONS[play_sign.CONFIG_PLAY_SIGN_TTL]),
    ])

    # 8. 公益服查看权限价格
    from backend.emby_server import portal
    items.extend([
        (portal.VIEW_UNLOCK_POINTS_KEY, str(portal.VIEW_UNLOCK_POINTS_DEFAULT), "公益服查看权限积分价格"),
        (portal.VIEW_UNLOCK_DAYS_KEY, str(portal.VIEW_UNLOCK_DAYS_DEFAULT), "公益服查看权限有效期（天，0=永久）"),
    ])

    # 去重（key 首次出现为准）
    seen: set[str] = set()
    uniq = []
    for key, default, desc in items:
        if key in seen:
            continue
        seen.add(key)
        uniq.append((key, default, desc))
    return uniq


def _insert_missing(db, items: list[tuple[str, str, str]]) -> list[str]:
    """查出缺失的 key 并 INSERT；返回实际插入的 key 列表。"""
    from backend import models

    keys = [k for k, _, _ in items]
    existing = {
        row[0]
        for row in db.query(models.SystemConfig.key)
        .filter(models.SystemConfig.key.in_(keys)).all()
    }
    todo = [(k, d, desc) for (k, d, desc) in items if k not in existing]
    for key, default, desc in todo:
        db.add(models.SystemConfig(key=key, value=default, description=desc))
    if todo:
        db.commit()
    return [k for k, _, _ in todo]


def _migrate_legacy_meta_keys(db) -> list[str]:
    """一次性迁移：把废弃的 ``meta_source_keys_tmdb`` 并入 ``tmdb_api_keys``

    Phase 6b 早期版本给 TMDB 开了第二个密钥存储，于是同一把钥匙存了两份、
    后台显示两遍。本函数在启动自愈时把它们合成一处：

    - 只在**正式入口为空、旧键非空**时搬（两边都有值时不猜，以正式的为准）；
    - 搬完把旧键清空（不清的话下次升级又搬一遍，而用户可能已经改过正式入口）；
    - 幂等：旧键清空后再跑就是空操作。

    返回实际迁移过的 key 列表（给调用方记日志用，不记密钥原文）。
    """
    from backend import models
    from backend.emby_server.metasources import config as ms_config
    from backend.emby_server.tmdb import _split_keys

    primary_key = ms_config.TMDB_KEY_STORAGE
    moved: list[str] = []
    for source_id, legacy_key in ms_config.LEGACY_KEY_CONFIG.items():
        try:
            legacy_row = db.query(models.SystemConfig).filter(
                models.SystemConfig.key == legacy_key).first()
            primary_row = db.query(models.SystemConfig).filter(
                models.SystemConfig.key == primary_key).first()
            legacy_keys = _split_keys(getattr(legacy_row, "value", "") or "")
            primary_keys = _split_keys(getattr(primary_row, "value", "") or "")
            if not legacy_keys or primary_keys:
                # 无需迁移：没有旧键，或正式入口已经有值（以正式的为准）
                continue
            if primary_row is None:
                db.add(models.SystemConfig(
                    key=primary_key, value=",".join(legacy_keys),
                    description="TMDB API Keys（元数据来源，多 key 逗号分隔）"))
            else:
                primary_row.value = ",".join(legacy_keys)
            if legacy_row is not None:
                legacy_row.value = ""
            db.commit()
            moved.append(legacy_key)
        except Exception:  # noqa: BLE001 — 迁移失败不该阻断启动（下次启动会重试）
            logger.warning("[配置自愈] 迁移 %s → %s 失败，本次跳过",
                           legacy_key, primary_key, exc_info=True)
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
    if moved:
        # 只记键名，不记密钥原文
        logger.info("[配置自愈] 已把废弃的元数据源密钥配置 %s 并入 %s",
                    ", ".join(moved), primary_key)
    return moved


def heal_system_config(db) -> list[str]:
    """SystemConfig 自愈：DB 里没有的 key 插入默认值行；返回补了的 key 列表。"""
    from sqlalchemy.exc import IntegrityError

    items = collect_system_config_defaults()
    try:
        healed = _insert_missing(db, items)
    except IntegrityError:
        # 多进程同时启动的竞态：对方刚好补了同一批，回滚后重查一遍再补仍然缺的
        db.rollback()
        logger.warning("[配置自愈] 并发写入冲突，重试一次")
        healed = _insert_missing(db, items)
    for key in healed:
        default = next(d for (k, d, _) in items if k == key)
        logger.info("[配置自愈] system_configs 补齐 %s=%r", key, default)
    if healed:
        logger.info("[配置自愈] system_configs 本次共补齐 %d 项", len(healed))
    _migrate_legacy_meta_keys(db)
    return healed


def run_config_self_heal(db=None, example_path: Path = ENV_EXAMPLE,
                         env_path: Path = ENV_FILE) -> dict:
    """启动时调用的统一入口：先补 .env，再补 system_configs。

    任何一步失败都只记日志、不抛异常、不阻断启动。
    db 为空时自己开一个 Session（用完关闭）。
    """
    result: dict[str, list[str]] = {"env_healed": [], "db_healed": []}
    try:
        result["env_healed"] = heal_env_file(example_path, env_path)
    except Exception:  # noqa: BLE001 — 自愈失败不能拦住启动
        logger.warning("[配置自愈] .env 自愈失败（已跳过）", exc_info=True)

    own_session = False
    if db is None:
        from backend.database import SessionLocal
        db = SessionLocal()
        own_session = True
    try:
        try:
            result["db_healed"] = heal_system_config(db)
        except Exception:  # noqa: BLE001
            logger.warning("[配置自愈] system_configs 自愈失败（已跳过）", exc_info=True)
    finally:
        if own_session:
            db.close()

    n_env, n_db = len(result["env_healed"]), len(result["db_healed"])
    if n_env or n_db:
        logger.info("[配置自愈] 完成：.env 补 %d 项，system_configs 补 %d 项", n_env, n_db)
    else:
        logger.info("[配置自愈] 检查完成，无缺失项")
    return result
