"""strm_v2 核心包。

8层架构 (D-ARCH-1: 层间只通过数据结构通信，不直接调用):
- collector: 采集层
- state_db: 状态层
- planner: 规划层
- executor: 执行层
- verifier: 校验层
- rules_engine: 规则引擎
- scheduler: 调度层（横切）
- reporter: 报告层
"""

__version__ = "2.0.0"
