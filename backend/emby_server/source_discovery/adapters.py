"""源适配器接口与注册表（框架层，不含任何真实源地址）。

后续适配器实现本接口即可接入编排层：
- ``name``：适配器名（与配置清单、分源开关、候选 ``adapter`` 字段一致）。
- ``search(db, query)``：按标题 / TMDB / IMDb 查候选，只读不写，
  绝不建转存任务、绝不调外部写接口；返回的候选必须诚实（未知字段
  填 None，不猜、不编、不带凭据与上游地址）。
"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.orm import Session

from backend.emby_server.source_discovery.types import Candidate, SearchQuery


class SourceAdapter(Protocol):
    """源适配器协议（结构化协议，实现类无需继承）。"""

    name: str

    def search(self, db: Session, query: SearchQuery) -> list[Candidate]:
        ...


_REGISTRY: dict[str, SourceAdapter] = {}


def register_adapter(adapter: SourceAdapter) -> None:
    _REGISTRY[adapter.name] = adapter


def get_adapter(name: str) -> SourceAdapter | None:
    return _REGISTRY.get(name)


def list_registered_adapters() -> list[str]:
    return sorted(_REGISTRY)


# 内置适配器随模块导入注册（只注册已授权的上游映射源）。
from backend.emby_server.source_discovery import upstream_mapping  # noqa: E402

register_adapter(upstream_mapping.UpstreamMappingAdapter())
