/** 公共贡献池配置 API（/api/admin/pool/*，见 backend/api/admin_pool.py） */
import { get, put } from '@/utils/request'

export interface PoolConfig {
  pool_enabled: boolean
  pool_audience: 'all' | 'whitelist' | 'subscribers'
  pool_join_mode: 'apply_review' | 'admin_assign' | 'open'
  pool_enabled_providers: string
  pool_member_capacity_bytes: number
  pool_member_reserve_free_bytes: number
  pool_member_daily_transfer_bytes: number
  pool_member_daily_transfer_jobs: number
  pool_item_max_copies: number
  pool_copy_idle_evict_days: number
  pool_global_concurrency: number
  pool_enqueue_jitter_sec: number
  pool_breaker_quota_hits_per_day: number
  pool_breaker_cooldown_sec: number
  pool_pick_top_k: number
  pool_member_max_bindings_per_user: number
}

export type PoolConfigPayload = Partial<PoolConfig>

export const fetchPoolConfig = () => get<PoolConfig>('/pool/config')

export const savePoolConfig = (payload: PoolConfigPayload) =>
  put<PoolConfig>('/pool/config', payload)
