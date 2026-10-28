"""找源通用能力：可插拔源适配器框架（后端能力，不做死业务）。

形态说明（2026-10-28 用户亲定“做成能力”，本批最后一个新功能）：
- 本包只提供“找候选”的通用能力：适配器按标题 / TMDB / IMDb 查候选，
  编排层汇总、去重、排序；候选确认后复用既有转存链路落到用户绑定盘。
- 不内置任何具体源站 / 磁链站 / 网盘名。唯一内置的真实源是
  ``upstream_mapping``（上游聚合映射，只用已授权的上游数据）。
- 后续适配器只留接口与配置示例，不在本包里写真实第三方源地址。
- 候选不是“能直接播”：候选只是“找得到”，播之前必须先确认转存到
  用户盘（走既有 BYOS / 转存链路，受 #1405 门禁与转存总闸约束）。
  找不到、转存失败都明确回失败态，绝不假装有源。
- 日志只记数量与 id，不记查询文本、标题、密钥、令牌与用户隐私。
"""

from backend.emby_server.source_discovery.adapters import (
    SourceAdapter,
    get_adapter,
    list_registered_adapters,
    register_adapter,
)
from backend.emby_server.source_discovery.orchestrator import (
    resolve_candidate,
    search_candidates,
)
from backend.emby_server.source_discovery.types import Candidate, SearchQuery

__all__ = [
    "Candidate",
    "SearchQuery",
    "SourceAdapter",
    "get_adapter",
    "list_registered_adapters",
    "register_adapter",
    "resolve_candidate",
    "search_candidates",
]
