/** 公共贡献池配置 API（/api/admin/pool/*，见 backend/api/admin_pool.py） */
import { get, patch, post, put } from '@/utils/request'

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

/** 巡检配置（闲置主动清理开关；与本体 16 键分开一份端点） */

export interface PoolJanitorConfig {
  pool_idle_evict_proactive_enabled: boolean
}

export const fetchPoolJanitorConfig = () =>
  get<PoolJanitorConfig>('/pool/janitor-config')

export const savePoolJanitorConfig = (payload: PoolJanitorConfig) =>
  put<PoolJanitorConfig>('/pool/janitor-config', payload)

/** 成员管理 API（PR-4，见 backend/api/pool_admin.py） */

export type PoolMemberStatus =
  | 'candidate' | 'pending' | 'active' | 'suspended'
  | 'exited' | 'rejected' | 'ejected'

export interface PoolMember {
  id: number
  user_id: number
  username: string | null
  binding_id: number | null
  account_label: string | null
  binding_status: string | null
  provider: string | null
  status: PoolMemberStatus
  suspended_reason: string | null
  breaker_until: string | null
  capacity_cap_bytes: number | null
  effective_capacity_bytes: number
  violation_flagged: boolean
  violation_reason: string | null
  violation_flagged_at: string | null
  violation_flagged_by: number | null
  violation_cleared_at: string | null
  violation_cleared_by: number | null
  joined_at: string | null
  status_changed_at: string | null
  created_at: string | null
  copies_count: number
  copies_bytes: number
  today_play_starts: number
  today_transfer_bytes: number
  today_transfer_jobs: number
  today_quota_hits: number
}

export interface PoolOverview {
  members_total: number
  members_candidate: number
  members_pending: number
  members_active: number
  members_suspended: number
  members_breaker: number
  copies_total: number
  copies_bytes: number
  today_transfer_jobs: number
  today_transfer_bytes: number
  today_play_starts: number
  today_quota_hits: number
}

export interface PoolCopy {
  id: number
  item_id: number
  item_name: string | null
  member_id: number | null
  status: string
  size_bytes: number | null
  last_played_at: string | null
  last_checked_at: string | null
  created_at: string | null
}

export const fetchPoolOverview = () => get<PoolOverview>('/pool/overview')

export const fetchPoolMembers = () =>
  get<{ members: PoolMember[] }>('/pool/members')

export const grantPoolCandidate = (userId: number) =>
  post<PoolMember>('/pool/candidates', { user_id: userId })

export const approvePoolMember = (id: number) =>
  post<PoolMember>(`/pool/members/${id}/approve`)

export const rejectPoolMember = (id: number) =>
  post<PoolMember>(`/pool/members/${id}/reject`)

export const suspendPoolMember = (id: number, reason?: string) =>
  post<PoolMember>(`/pool/members/${id}/suspend`, { reason: reason ?? null })

export const resumePoolMember = (id: number) =>
  post<PoolMember>(`/pool/members/${id}/resume`)

export const ejectPoolMember = (
  id: number,
  payload: { violation: boolean; violation_reason?: string | null },
) => post<PoolMember>(`/pool/members/${id}/eject`, payload)

export const clearPoolMemberViolation = (id: number) =>
  post<PoolMember>(`/pool/members/${id}/clear-violation`)

export const patchPoolMember = (
  id: number,
  payload: { capacity_cap_bytes: number | null },
) => patch<PoolMember>(`/pool/members/${id}`, payload)

export const fetchPoolCopies = (itemId: number) =>
  get<{ copies: PoolCopy[] }>('/pool/copies', { item_id: itemId })

export const releasePoolCopy = (variantId: number) =>
  post<{ id: number; status: string; item_id: number }>(
    `/pool/copies/${variantId}/release`)

/** 激励结算 API（PR-6，见 backend/api/pool_admin.py 与 admin_pool.py） */

export interface PoolRewardConfig {
  pool_reward_enabled: boolean
  pool_reward_payout_enabled: boolean
  pool_reward_period: 'monthly' | 'weekly'
  pool_reward_points_per_play: number
  pool_reward_points_per_active_day: number
  pool_reward_points_cap: number
  pool_reward_min_active_days: number
  pool_reward_min_counted_plays: number
  pool_reward_play_dedupe_window_sec: number
  pool_reward_daily_play_freeze: number
  pool_reward_welfare_days: number
  pool_reward_welfare_min_plays: number
  pool_reward_xp_per_play: number
  pool_reward_xp_cap: number
  pool_reward_subscription_enabled: boolean
  pool_reward_title_enabled: boolean
}

export type PoolRewardConfigPayload = Partial<PoolRewardConfig>

export const fetchPoolRewardConfig = () =>
  get<PoolRewardConfig>('/pool/reward-config')

export const savePoolRewardConfig = (payload: PoolRewardConfigPayload) =>
  put<PoolRewardConfig>('/pool/reward-config', payload)

export interface PoolRewardPreviewRow {
  member_id: number
  user_id: number
  username: string | null
  member_status: string
  counted_plays: number
  active_days: number
  raw_plays: number
  transfer_bytes: number
  points: number
  xp: number
  would_status: string
  reason: string | null
}

export interface PoolRewardPreview {
  period_key: string
  period_kind: string
  period_start: string
  period_end: string
  reward_enabled: boolean
  payout_enabled: boolean
  rows: PoolRewardPreviewRow[]
}

export const fetchPoolRewardPreview = (periodKey?: string) =>
  get<PoolRewardPreview>(
    '/pool/rewards/preview',
    periodKey ? { period_key: periodKey } : undefined)

export interface PoolRewardSettlement {
  id: number
  period_key: string
  period_kind: string
  period_start: string | null
  period_end: string | null
  status: string
  shadow: boolean
  created_by: number | null
  last_error: string | null
  members: number
  paid: number
  shadow_rows: number
  frozen: number
  skipped: number
  paid_points_total: number
  created_at: string | null
  finished_at: string | null
}

export interface PoolRewardDetail {
  id: number
  settlement_id: number
  member_id: number
  user_id: number
  username: string | null
  counted_plays: number
  active_days: number
  raw_plays: number
  transfer_bytes: number
  points: number
  xp: number
  welfare_days: number
  status: string
  reason: string | null
  points_log_id: number | null
  granted_at: string | null
  released_by: number | null
  released_at: string | null
  created_at: string | null
}

export const settlePoolRewards = (periodKey?: string) =>
  post<{ summary: Record<string, unknown>; settlement: PoolRewardSettlement }>(
    '/pool/rewards/settle', periodKey ? { period_key: periodKey } : {})

export const fetchPoolRewardSettlements = () =>
  get<{ settlements: PoolRewardSettlement[] }>('/pool/rewards/settlements')

export const fetchPoolRewardDetails = (settlementId: number) =>
  get<{ settlement: PoolRewardSettlement; details: PoolRewardDetail[] }>(
    `/pool/rewards/settlements/${settlementId}/details`)

export const releasePoolRewardDetail = (detailId: number) =>
  post<PoolRewardDetail>(`/pool/rewards/details/${detailId}/release`)
