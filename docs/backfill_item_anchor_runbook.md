# 存量身份锚回填 Runbook

目标：给存量 `mount://` 老条目补 `source_id` + `provider_file_id`，
解开断旧路的第一道前置。本脚本不断旧路、不删代码与列。

## 规则（与 `scripts/backfill_item_anchor.py` docstring 一致）

- `source_id`：`parse_mount_path` → 同库 + 同挂载 + `is_enabled` 非 False
  + 目录边界最长前缀的 `library_sources` 行；无此行只上报、不猜。
- `provider_file_id`：仅 Drive/rclone 型挂载取 `drive_file_id`（入库双写
  同一 `_fid`）；115 型不推导；空值留空。
- 只填空、永不覆盖；重跑自动跳过已填行；默认 dry-run。

## 执行顺序

1. 试跑（只读，容器里）：
   `docker exec aetrix-api python scripts/backfill_item_anchor.py --sample 20`
   核对：`unmapped_by_reason` 分布合理、样例推导链与路径一致。
2. 评审试跑报告后，用户点头，才可执行全量：
   `docker exec aetrix-api python scripts/backfill_item_anchor.py --apply`
   按 id 游标分批提交，中断后重跑自动续上（已填行被查询条件排除）。
3. 执行后验证（psql，活条目 = `deleted_at IS NULL AND merged_into_id IS NULL`）：
   - `source_id IS NULL` 的 `mount://` 行应只剩试跑报告里的不可映射类；
   - `provider_file_id` 缺失行应只剩结构行与 115/未知类型行；
   - `/api/health` 绿；抽查若干行按 `item_anchor.resolve_anchor` 应返回 ok
    （需真实播放流量后，Redis `aetrix:item_anchor:v1` 的回落率才会动；
     回落率归零另需真实播放 + 兼容期，不由本脚本保证）。

## 风险与回滚

- 写操作是逐列只填空，不覆盖已有锚；误写可用备份恢复单列（执行前
  建议先对 `emby_items` 做常规备份；本脚本不自动备份）。
- 若某批提交后中断：重跑即可，幂等保证不重复写。
- 若推导规则事后发现有误：已写锚需按新规则重算（另立项，不可直接
  全量清零——清零会把新入库的正确锚一并删掉）。
