"""配置加载器。

D-CONF-1: 所有阈值、策略、模板、规则全部进配置文件，代码里不出现业务常量。
"""

from __future__ import annotations

import os
from typing import Any

import yaml


class ConfigError(Exception):
    """配置错误（缺失 key、类型错误等），启动时快速失败。"""


def load_yaml(path: str) -> dict[str, Any]:
    """加载 YAML 文件，不存在或解析失败时抛 ConfigError。"""
    if not os.path.exists(path):
        raise ConfigError(f"配置文件不存在: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise ConfigError(f"配置文件解析失败 {path}: {e}")
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件顶层必须是 dict: {path}")
    return data


def _require(cfg: dict[str, Any], *keys: str) -> Any:
    """逐层取 key，缺失时抛 ConfigError（带完整路径）。"""
    cur = cfg
    trail = []
    for k in keys:
        trail.append(k)
        if not isinstance(cur, dict) or k not in cur:
            raise ConfigError(f"配置缺失: {'.'.join(trail)}")
        cur = cur[k]
    return cur


class Config:
    """强类型配置访问器。缺失 key 启动时报错，不在运行时静默用默认值。"""

    def __init__(self, data: dict[str, Any], base_dir: str = ""):
        self._data = data
        self._base = base_dir

    @classmethod
    def load(cls, path: str) -> "Config":
        data = load_yaml(path)
        base = os.path.dirname(os.path.abspath(path))
        return cls(data, base)

    def get(self, *keys: str, default: Any = None) -> Any:
        cur: Any = self._data
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    def require(self, *keys: str) -> Any:
        return _require(self._data, *keys)

    def resolve(self, path: str) -> str:
        """相对路径相对于配置文件所在目录解析。"""
        if os.path.isabs(path):
            return path
        return os.path.join(self._base, path)

    # ── 快捷访问 ──
    @property
    def strm_root(self) -> str:
        return self.require("target", "strm_root")

    @property
    def url_template(self) -> str:
        return self.require("target", "url_template")

    @property
    def state_db_path(self) -> str:
        return self.require("incremental", "state_db")

    @property
    def dry_run(self) -> bool:
        return bool(self.get("run", "dry_run", default=False))
