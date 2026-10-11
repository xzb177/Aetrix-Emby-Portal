"""核心数据结构定义。

D-ARCH-1: 层与层之间只通过这些数据结构通信，不直接调用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional


@dataclass
class FileRecord:
    """采集层输出的标准化文件记录。"""
    file_id: str          # Drive file ID（稳定身份）
    path: str             # Drive 相对路径（/ 分隔）
    name: str             # 文件名
    size: int             # 字节数
    md5: Optional[str]    # md5Checksum（可能缺失，见 D-INCR-1 fallback）
    mtime: Optional[str]  # modifiedTime（仅记录，不参与变更判定）
    mime: str             # mimeType

    @property
    def shard(self) -> str:
        """分片键：顶层目录名（D-INCR-5）。"""
        parts = self.path.split("/")
        return parts[0] if parts else ""


@dataclass
class FileState:
    """状态层持久化的文件状态。"""
    file_id: str
    path: str
    md5: Optional[str]
    size: int
    strm_path: str         # 本地 .strm 相对路径
    status: str            # pending | done | verify_failed | orphan
    updated_at: str


@dataclass
class PlanItem:
    """规划层输出的单个任务。"""
    action: Literal["create", "update", "move", "delete", "skip"]
    record: Optional[FileRecord]   # create/update/move 时有值
    old_strm_path: Optional[str] = None   # move/delete 时旧路径
    new_strm_path: Optional[str] = None   # create/update/move 时新路径
    reason: str = ""                       # 决策原因（用于报告）


@dataclass
class Plan:
    """规划层输出的完整计划。"""
    items: list[PlanItem] = field(default_factory=list)
    dirty: list[DirtyHit] = field(default_factory=list)  # 脏数据命中

    def by_action(self, action: str) -> list[PlanItem]:
        return [i for i in self.items if i.action == action]


@dataclass
class DirtyHit:
    """规则引擎命中的脏数据。"""
    rule_id: str
    path: str
    detail: str
    suggestion: str = ""   # 归一化建议（D-DIRTY-2）
    auto_normalized: bool = False


@dataclass
class VerifyResult:
    """校验层输出的单条结果。"""
    strm_path: str
    level: str             # L1 | L2 | L3
    passed: bool
    detail: str = ""
    checked_at: str = ""
