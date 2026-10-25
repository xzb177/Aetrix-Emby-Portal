# -*- coding: utf-8 -*-
"""库源健康过滤的试点范围机制（FMBY 挂载学习第 5 步，挂载-9）。

健康过滤（扫描侧 ``mount_sources.read_library_sources``、播放侧
``playback_source_health``）默认对全部库生效；设了试点清单后，
只对清单里的库生效——灰度先小后大，不必为试点另写一套过滤。

- 清单走 env ``LIBRARY_SOURCE_HEALTH_PILOT_LIBRARY_IDS``，逗号分隔
  的库 id（整数）；每次调用现读，不缓存，改配置即生效。
- 代码里不写死任何库名、库 id、业务内容：本模块只解析 env 给的
  数字，哪个库是试点由部署配置决定。
- 清单为空（未设 / 全空白 / 全非法）= 试点范围未激活，过滤对
  全部库生效，与设清单之前逐字一致——设清单是“收窄”，不是
  “试点外默认关健康过滤”的另一套开关。
- 非法段（非整数、负数、零）直接丢弃，不报错、不阻塞主流程；
  重复 id 去重，顺序不影响判定。

生产激活口径（本文档写明，不由本模块代劳）：已批的试点库是
演唱会库；生产激活时由父代理按库名查出库 id 再设 env，
本模块与本 PR 不动生产、不查生产库 id。
"""

from __future__ import annotations

import os

#: 试点库清单的环境键（逗号分隔的库 id）。
PILOT_LIBRARY_IDS_ENV = "LIBRARY_SOURCE_HEALTH_PILOT_LIBRARY_IDS"


def pilot_library_ids() -> frozenset:
    """当前试点库 id 集合（现读 env；空集合 = 试点范围未激活）。"""
    raw = os.environ.get(PILOT_LIBRARY_IDS_ENV) or ""
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except (TypeError, ValueError):
            continue
        if value > 0:
            ids.add(value)
    return frozenset(ids)


def pilot_scope_active() -> bool:
    """试点范围是否激活（清单非空即激活）。"""
    return bool(pilot_library_ids())


def in_pilot_scope(library_id) -> bool:
    """该库是否在健康过滤的作用范围内。

    - 试点范围未激活 → 全部库都在范围内（与设清单前一致）；
    - 已激活 → 只有清单里的库在范围内。
    """
    ids = pilot_library_ids()
    if not ids:
        return True
    try:
        return int(library_id) in ids
    except (TypeError, ValueError):
        return False
