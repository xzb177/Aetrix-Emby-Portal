"""挂载直读出流（2026-10-30 立项；设计 reports/mount-serving-design-20261030.md）。

共享盘原件的主线新出法：``video_stream`` 在门禁/副本流程之后解析
原件时，若原件所在 mount 命中白名单且配了本地挂载根，直接以本地
文件（rclone FUSE 挂载点）经 ``streaming.serve_file`` 出流——绕开
公开链 usercontent 的 Quota 风控与 SA 中继的交付不稳定（试点实测，
2026-10-30：挂载冷态顺序 13.06 MB/s 稳定、seek 1.3–1.6s 零失败；
公开链整段必撞 Quota 页、Range 约半数被拒；生产中继定点读可慢至
60 秒 0 字节）。

口径（定死）：
- 本模块只做判定与路径解析，不做 IO 出流；出流复用 serve_file，
  本模块不改 streaming 任何既有函数行为。
- engages 三条件同时成立：总开关开、mount id 在白名单、mount id
  在本地根映射里。任一不成立 → 返回 None，调用方走现行出法，
  行为与未接入时逐字一致。白名单默认空 ＝ 全站零行为变化；
  配置热读失败（fail-safe）回默认，默认方向永远是「不介入」。
- engages 后本地文件不存在 → 抛 MountServingUnavailable 响亮
  失败，**绝不静默改走公开链/中继**：静默回落会污染灰度数据，
  且等于把用户偷偷送回已证实不可用的路。
- 凭据不在本层：字节由宿主机 rclone 挂载进程以其配置凭据从
  Drive 取数，应用只读本地文件；本层不持有、不拼任何网盘令牌。
- 配置三键走 SystemConfig 热读（integrations.store，全项目统一
  的带缓存读法），管理端保存即生效；回滚 = 清空白名单（或关
  总开关），热生效、不重启、不发版。
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from sqlalchemy.orm import Session

from backend.emby_server import mounts as mount_lib
from backend.emby_server.playback_security import safe_local_path

logger = logging.getLogger(__name__)

#: 总开关 SystemConfig 键；默认开（用户亲令＋部署默认开口径）。
#: 关＝挂载直读分支一行不走，完整回退现行出法。
KEY_MOUNT_SERVING_ENABLED = "mount_serving_enabled"
MOUNT_SERVING_ENABLED_DEFAULT = True

#: 白名单 SystemConfig 键；逗号分隔的 storage_mounts id 列表，
#: 默认空。**空＝行为与现行完全一致**，也是回滚位：清空即
#: 一步回滚（热生效）。
KEY_MOUNT_SERVING_MOUNT_IDS = "mount_serving_mount_ids"

#: 本地根映射 SystemConfig 键；格式 ``id=/绝对路径`` 逗号分隔，
#: 默认空。engages 需同时命中白名单与本映射：白名单是「允许」，
#: 根映射是「接好了」——只配白名单没配根＝未接入，不介入。
KEY_MOUNT_SERVING_LOCAL_ROOTS = "mount_serving_local_roots"

DESCRIPTIONS = {
    KEY_MOUNT_SERVING_ENABLED: "挂载直读出流总开关（默认开；白名单为空时无行为变化）",
    KEY_MOUNT_SERVING_MOUNT_IDS: "挂载直读白名单：逗号分隔的 storage_mounts id，默认空",
    KEY_MOUNT_SERVING_LOCAL_ROOTS: "挂载直读本地根映射：id=/绝对路径，逗号分隔，默认空",
}

_FALSE_WORDS = ("0", "false", "no", "off")


class MountServingUnavailable(RuntimeError):
    """挂载直读已 engages 但本地文件不可用（响亮失败，不回落）。"""


def _raw_config(db: Optional[Session], key: str) -> str:
    """SystemConfig 热读原始串：无 db / 无行 / 异常一律空串（回退）。"""
    if db is None:
        return ""
    try:
        from backend.integrations import store

        return str(store.get_value(db, key, "") or "")
    except Exception:  # noqa: BLE001 — 配置读取不许把播放打停
        return ""


def serving_enabled(db: Optional[Session]) -> bool:
    """总开关：未配行回默认开；0/false/no/off 视为关。"""
    raw = _raw_config(db, KEY_MOUNT_SERVING_ENABLED).strip().lower()
    if not raw:
        return MOUNT_SERVING_ENABLED_DEFAULT
    return raw not in _FALSE_WORDS


def parse_mount_ids(raw: str, *, strict: bool = False) -> list[int]:
    """白名单串解析：逗号分隔正整数。读路径宽容跳过坏项；
    strict（写入校验）时坏项抛 ValueError。"""
    out: list[int] = []
    for part in str(raw or "").split(","):
        token = part.strip()
        if not token:
            continue
        try:
            value = int(token)
            if value <= 0:
                raise ValueError
        except (TypeError, ValueError):
            if strict:
                raise ValueError(
                    f"挂载直读白名单必须是逗号分隔的正整数 id：{token!r}")
            continue
        if value not in out:
            out.append(value)
    return out


def parse_local_roots(raw: str, *, strict: bool = False) -> dict[int, str]:
    """根映射串解析：``id=/绝对路径`` 逗号分隔。

    路径不许含逗号（格式约定，挂载点命名避开即可）；根必须是
    绝对路径。读路径宽容跳过坏项；strict 时坏项/重复 id 抛
    ValueError。"""
    out: dict[int, str] = {}
    for part in str(raw or "").split(","):
        token = part.strip()
        if not token:
            continue
        key, sep, value = token.partition("=")
        key = key.strip()
        value = value.strip()
        try:
            mount_id = int(key)
            if mount_id <= 0 or not sep or not os.path.isabs(value):
                raise ValueError
        except (TypeError, ValueError):
            if strict:
                raise ValueError(
                    f"挂载直读本地根映射格式须为 id=/绝对路径：{token!r}")
            continue
        if mount_id in out and strict:
            raise ValueError(f"挂载直读本地根映射 id 重复：{mount_id}")
        out[mount_id] = value
    return out


def whitelist(db: Optional[Session]) -> list[int]:
    return parse_mount_ids(_raw_config(db, KEY_MOUNT_SERVING_MOUNT_IDS))


def local_roots(db: Optional[Session]) -> dict[int, str]:
    return parse_local_roots(_raw_config(db, KEY_MOUNT_SERVING_LOCAL_ROOTS))


def resolve_local_file(db: Session, item) -> Optional[str]:
    """判定本条目是否走挂载直读，走则返回本地文件绝对路径。

    - 不 engages（非 mount 路径 / 开关关 / 不在白名单 / 没配根 /
      挂载不存在或停用）→ None，调用方走现行出法；
    - engages 但路径非法或文件不存在 → MountServingUnavailable
      （响亮失败，调用方转 503；绝不返回 None 装作没这回事）。
    """
    parsed = mount_lib.parse_mount_path(getattr(item, "file_path", None))
    if parsed is None:
        return None
    mount_id, rel = parsed
    if not serving_enabled(db):
        return None
    if mount_id not in whitelist(db):
        return None
    root = local_roots(db).get(mount_id)
    if not root:
        return None
    mount = None
    try:
        mount = mount_lib.load_mount(db, mount_id)
    except Exception:  # noqa: BLE001 — 查不到挂载按未接入处理
        mount = None
    if mount is None or not getattr(mount, "is_enabled", False):
        return None
    try:
        path = safe_local_path(root, (rel or "/").lstrip("/"))
    except ValueError as exc:
        raise MountServingUnavailable(
            f"挂载直读路径非法（mount #{mount_id}）：{exc}") from exc
    if not os.path.isfile(path):
        raise MountServingUnavailable(
            f"挂载直读已启用但文件不在挂载点（mount #{mount_id}: "
            f"{rel}），未改走其他来源")
    return path


def config_payload(db: Optional[Session] = None) -> dict:
    """管理后台读取：当前值 + 默认值（前端卡片用）。"""
    ids = whitelist(db)
    roots = local_roots(db)
    return {
        "enabled": serving_enabled(db),
        "mount_ids": ids,
        "local_roots": ",".join(
            f"{mid}={roots[mid]}" for mid in sorted(roots)),
        "defaults": {
            "enabled": MOUNT_SERVING_ENABLED_DEFAULT,
            "mount_ids": [],
            "local_roots": "",
        },
    }


def write_config(db: Session, *, enabled: bool, mount_ids,
                 local_roots: str) -> dict:
    """管理后台写入：校验 → 落库（config_service 家族编排）→
    失效热缓存。非法值抛 ValueError（端点转 400）。"""
    from backend import config_service
    from backend.integrations import store

    if isinstance(mount_ids, (list, tuple)):
        ids: list[int] = []
        for value in mount_ids:
            try:
                ivalue = int(value)
            except (TypeError, ValueError):
                raise ValueError(
                    f"挂载直读白名单必须是正整数 id：{value!r}")
            if ivalue <= 0:
                raise ValueError(
                    f"挂载直读白名单必须是正整数 id：{value!r}")
            if ivalue not in ids:
                ids.append(ivalue)
    else:
        ids = parse_mount_ids(str(mount_ids or ""), strict=True)
    roots = parse_local_roots(str(local_roots or ""), strict=True)
    values = {
        KEY_MOUNT_SERVING_ENABLED: "true" if enabled else "false",
        KEY_MOUNT_SERVING_MOUNT_IDS: ",".join(str(i) for i in ids),
        KEY_MOUNT_SERVING_LOCAL_ROOTS: ",".join(
            f"{mid}={roots[mid]}" for mid in sorted(roots)),
    }
    config_service.save_config(db, values, schema="mount_serving",
                               descriptions=DESCRIPTIONS)
    store.invalidate(KEY_MOUNT_SERVING_ENABLED,
                     KEY_MOUNT_SERVING_MOUNT_IDS,
                     KEY_MOUNT_SERVING_LOCAL_ROOTS)
    return config_payload(db)
