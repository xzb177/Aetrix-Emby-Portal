# gen_strm_v2 — .strm 生成同步服务核心

v2 系统性重构版（设计文档：`DESIGN.md`）。

## 架构

8 层架构（D-ARCH-1：层间只通过数据结构通信）：

| 层 | 模块 | 职责 |
|---|---|---|
| 采集层 | `strm_v2/collector.py` | Drive API files.list / Changes API → FileRecord |
| 状态层 | `strm_v2/state_db.py` | SQLite：files + checkpoints + meta 表 |
| 规划层 | `strm_v2/planner.py` | diff → Plan{新增/更新/移动/删除/跳过} |
| 执行层 | `strm_v2/executor.py` | 并发生成/更新/删除 .strm（原子写出） |
| 校验层 | `strm_v2/verifier.py` | L1/L2/L3 校验 + 抽样策略 |
| 规则引擎 | `strm_v2/rules_engine.py` | YAML 脏数据规则 |
| 调度层 | `strm_v2/scheduler.py` | SA 轮换/限流/抖动/熔断（横切） |
| 报告层 | `strm_v2/reporter.py` | 脏数据/校验/运行报告 |

## 快速开始

```bash
# 1. 复制并修改配置
cp config/strm_gen.yaml config/my.yaml
vim config/my.yaml  # 填写 drive_root_id 等

# 2. 干跑（只出 Plan，不写盘）
python3 gen_strm_v2.py --config config/my.yaml --dry-run

# 3. 正式运行
python3 gen_strm_v2.py --config config/my.yaml

# 4. 只跑校验
python3 gen_strm_v2.py --config config/my.yaml --verify-only

# 5. 只跑指定库
python3 gen_strm_v2.py --config config/my.yaml --shard 电影 --shard 国产剧
```

## 核心设计决策（D-XXX）

详见 `DESIGN.md` §9。关键点：

- **D-INCR-1**：变更判定只看 `(file_id, md5, size)`，mtime 不参与——mtime 抖动不再触发重生成
- **D-FLOW-1**：tmp + fsync + rename 原子写出，中断不留半截文件
- **D-CONC-2**：全局限流 + 请求间隔 ±30% 随机抖动（反风控）
- **D-CONC-3**：SA token 按 SA 独立缓存，轮换顺序随机打乱
- **D-ARCH-2**：独立 SQLite，不连生产 PostgreSQL

## 与 v1 的差异

| | v1 | v2 |
|---|---|---|
| 采集 | rclone lsf 全量 | Drive API + Changes 增量 |
| 变更信号 | file_id | (file_id, md5, size) |
| 状态 | 简单 processed 表 | files + checkpoints + meta |
| 并发 | 单线程 | 三阶段流水线 |
| 校验 | 无 | L1/L2/L3 + 抽样 |
| 脏数据 | 无 | YAML 规则引擎 |
| 配置 | 硬编码常量 | 全 YAML |

v1 状态库（`/opt/strm/.gen_strm_state.db`）首次运行时自动迁移。

## 依赖

```bash
pip install pyyaml
```

其余只用 Python 标准库。
