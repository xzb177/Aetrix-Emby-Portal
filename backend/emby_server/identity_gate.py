"""身份准入货架：判据与总开关（身份准入货架一期，案 A）。

口径（2026-10-24 用户拍板，设计稿 reports/identity-gated-shelf-design-20261024.md）：
用户端分三面——**货架面**（浏览/列表/计数/推荐/最新，系统主动枚举）只露出
已绑定身份的条目；**搜索面不收紧**、**直达面**（凭 guid/链接直达）放行。
本模块只服务货架面：搜索/直达出口不许挂这里的条件。管理端出口不挂，
天然继续看见全部条目（横切能力只许一套：全仓身份判据只许经本模块，
任何出口不许私自判 tmdb_id）。

已绑定判据：
- movie/series：自身 ``tmdb_id`` 或 ``imdb_id`` 任一 strip 后非空。
  只有豆瓣身份（``external_ids`` 里的 douban）**不算**——豆瓣 ID 从不写入
  规范身份列、不参与合并分组；只有 IMDb **算**（合并 worker 与
  HasAnyProviderId 的现行口径都认 imdb_id 为正式身份）。
- season/episode：跟随所属 series 行（``series_id`` 指向行满足上面的
  判据）；自身有 tmdb/imdb 亦算（episode 口径与 AnyProviderIdEquals、
  DTO 层 ``_provider_ids`` 的「集继承剧编号」既有先例一致）。
- 其余类型（person 等）不在货架治理内，条件恒真。

落地方式：查询构造层共享 helper（设计稿 §5.1 方案甲）——每个货架出口在
自己的**基础查询**上显式 ``filter(identity_clause())``，SQL 层过滤不后置
内存剔除（守 Latest 注释立过的「先占 limit 名额」纪律）。不加冗余状态列
（治理流水线式状态列是三期可选项）。

总开关：env ``IDENTITY_SHELF_GATE``（默认 0=关），模块加载时读一次
（与既有 env 同口径，改需重启）。关 = 所有出口与旧行为逐字一致，是本
功能的唯一回滚手段。**切换开关必须配套全库 bump 数据版本**，清列表 ID
清单缓存与去重缓存（两层缓存的键不含开关状态，见 items_list_cache /
items_dedup_cache）；只改 env 不清缓存等于没切换——写进上线/回滚操作单。
"""
from __future__ import annotations

import logging

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.orm import aliased

from backend.emby_server import models as em
from backend.emby_server.env_util import env_int

logger = logging.getLogger(__name__)

#: 身份准入货架总开关（默认关）。启动时读一次，热路径不碰 env。
IDENTITY_SHELF_GATE = env_int("IDENTITY_SHELF_GATE", 0, 0, 1)

_TOP_TYPES = ("movie", "series")
_CHILD_TYPES = ("season", "episode")


def gate_enabled() -> bool:
    """货架身份门是否开启（全仓读开关的唯一入口；测试 patch 本模块常量）。"""
    return IDENTITY_SHELF_GATE > 0


def _own_bound(item) -> object:
    """行自身已绑定：tmdb_id / imdb_id 任一在 strip 后非空。

    ``item`` 可以是 MediaItem 实体类或 aliased 别名。存量数据可能有空串 /
    纯空白（扫描种子里就有 ``tmdb_id="   "`` 的行），必须 trim 后判空，
    只判 ``IS NOT NULL`` 会把空白串误判成已绑定。
    """
    return or_(
        and_(item.tmdb_id.isnot(None), func.trim(item.tmdb_id) != ""),
        and_(item.imdb_id.isnot(None), func.trim(item.imdb_id) != ""),
    )


def identity_clause():
    """挂在 MediaItem 查询上的身份准入条件（SQLAlchemy 表达式）。

    只在 :func:`gate_enabled` 为真时由调用方挂载；关时不许挂
    （关 = 旧口径逐字一致）。
    """
    series_alias = aliased(em.MediaItem)
    series_bound = exists(
        select(series_alias.id)
        .where(
            series_alias.id == em.MediaItem.series_id,
            # EXISTS 子查询里全局软删除钩子下推不到（soft_delete 文件头
            # 自述、_dedup_dropped_ids 同款坑），必须显式排除已软删的
            # series 行，否则删掉的剧还能替它的集「绑定」身份。
            series_alias.deleted_at.is_(None),
            _own_bound(series_alias),
        )
        .correlate(em.MediaItem)
    )
    return or_(
        and_(em.MediaItem.item_type.in_(_TOP_TYPES), _own_bound(em.MediaItem)),
        and_(
            em.MediaItem.item_type.in_(_CHILD_TYPES),
            or_(_own_bound(em.MediaItem), series_bound),
        ),
        # item_type 非空（表约束 nullable=False）；非四类不在治理内
        ~em.MediaItem.item_type.in_(_TOP_TYPES + _CHILD_TYPES),
    )
