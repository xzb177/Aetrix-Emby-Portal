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
