"""找源候选的数据形状（纯数据，不碰数据库、不读配置）。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SearchQuery:
    """一次找源查询：三个键至少给一个，空查询由路由层 400 拦下。"""

    title: str = ""
    tmdb_id: str = ""
    imdb_id: str = ""

    def normalized_title(self) -> str:
        return " ".join((self.title or "").split()).casefold()

    def is_empty(self) -> bool:
        return not (
            self.normalized_title() or (self.tmdb_id or "").strip()
            or (self.imdb_id or "").strip()
        )


@dataclass(frozen=True)
class Candidate:
    """一个找源候选：只描述“找得到什么”，不承诺“能直接播”。

    - ``playable_now`` 恒为 False：候选必须先确认转存到用户绑定盘，
      副本 ready 后才可播；搜索本身绝不产生可播地址。
    - ``acquisition`` 说明获取方式：目前只有 ``transfer_after_confirm``
      （确认后走既有转存链路），未来适配器可扩展其它诚实方式。
    - 清晰度 / 格式 / 大小 / 字幕信息只填本地条目已有的字段，
      没有的填 None，绝不猜、绝不编。
    - 绝不带上游 base_url、凭据、令牌等敏感信息（只带来源名与 id）。
    """

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

    def dedupe_key(self) -> tuple:
        return (
            self.adapter,
            self.local_item_id,
            self.source_id,
            self.upstream_item_id,
        )

    def to_dict(self) -> dict:
        return {
            "adapter": self.adapter,
            "local_item_id": self.local_item_id,
            "local_title": self.local_title,
            "local_year": self.local_year,
            "local_type": self.local_type,
            "tmdb_id": self.tmdb_id,
            "imdb_id": self.imdb_id,
            "source_id": self.source_id,
            "source_name": self.source_name,
            "upstream_item_id": self.upstream_item_id,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "resolution": self.resolution,
            "container": self.container,
            "media_source": self.media_source,
            "size_bytes": self.size_bytes,
            "subtitle_languages": self.subtitle_languages,
            "acquisition": self.acquisition,
            "playable_now": self.playable_now,
        }
