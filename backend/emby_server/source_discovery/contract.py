"""找源契约：响应形状与诚实文案（只钉形状，不访问数据库）。

状态语义：
- 搜索只返回候选，绝不建任务；候选的 ``playable_now`` 恒为 False。
- 确认成功 → ``queued``（任务已建，状态以任务行为准）；
  24h 内已有任务 → ``already_queued``（返回已有任务，不重复建）；
  其余建不了 → ``failed``（按原因明说，不假装入队）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

DISABLED_DETAIL = "找源未开启"
NO_QUERY_MESSAGE = "请至少给一个查询条件（标题、TMDB 或 IMDb 编号）"
NO_CANDIDATE_MESSAGE = "候选不存在或来源已不可用"
TRIPLE_REQUIRED_MESSAGE = "确认需指定候选（本地条目、来源、上游条目）"
UNKNOWN_PROVIDER_MESSAGE = "该条目形状暂不支持转存"
NO_BINDING_MESSAGE = "请先绑定对应盘再确认转存"
MASTER_OFF_MESSAGE = "转存变体总闸未开"
NO_SOURCE_FID_MESSAGE = "115 条目无源 fid，无法转存"
NO_DRIVE_MESSAGE = "Drive 条目取不到源盘标识"
QUEUED_MESSAGE = "已建用户盘转存任务，副本就绪后可播"
ALREADY_QUEUED_MESSAGE = "24h 内已有转存任务，不重复建"
THROTTLED_MESSAGE = "24h 内已有转存任务"

SOURCE_DISCOVERY_TRIGGER = "source_discovery"


class CandidateOut(BaseModel):
    """一个找源候选的响应形状（与 types.Candidate 同字段）。"""

    adapter: str
    local_item_id: int
    local_title: str
    local_year: int | None = None
    local_type: str = ""
    tmdb_id: str | None = None
    imdb_id: str | None = None
    source_id: int | None = None
    source_name: str = ""
    upstream_item_id: str | None = None
    confidence: float = 0.0
    evidence: str = ""
    resolution: str | None = None
    container: str | None = None
    media_source: str | None = None
    size_bytes: int | None = None
    subtitle_languages: str | None = None
    acquisition: str = "transfer_after_confirm"
    playable_now: bool = False


class SearchResponse(BaseModel):
    """GET /search 的响应：搜索绝不建任务。"""

    candidates: list[CandidateOut] = Field(default_factory=list)
    total: int = 0
    adapters: list[str] = Field(default_factory=list)
    auto_transfer_enabled: bool = False


class ConfirmIn(BaseModel):
    """POST /confirm 的请求体。

    默认模式（自动模式关）必须给全候选三元组；自动模式开时可只给
    tmdb_id，由服务端按置信度自动挑最高候选（仍需一次确认调用，
    搜索本身在任何模式下都不建任务）。
    """

    local_item_id: int | None = None
    source_id: int | None = None
    upstream_item_id: str | None = None
    tmdb_id: str | None = None


class ConfirmOut(BaseModel):
    """POST /confirm 的响应：state 只取 queued / already_queued / failed。"""

    state: str
    job_id: int | None = None
    local_item_id: int | None = None
    source_id: int | None = None
    upstream_item_id: str | None = None
    message: str = ""
