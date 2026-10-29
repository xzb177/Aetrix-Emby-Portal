/** 公益服管理后台 API（全部走 /api/admin/welfare/*） */
import { del, get, post, put } from '@/utils/request'

const E = '/welfare'

// ==================== 公益用户操作（v2.55 用户列表已并入用户管理，列表接口停用） ====================

export const grantWelfare = (data: { user_id: number; days: number; channel?: string }) =>
  post<{ success: boolean; expires_at: string | null }>(`${E}/grant`, data)

export const revokeWelfare = (user_id: number) =>
  post<{ success: boolean }>(`${E}/revoke`, { user_id })

export const bulkExtendWelfare = (data: { min_expired_days: number; max_expired_days: number; add_days: number }) =>
  post<{ success: boolean; affected: number }>(`${E}/bulk-extend`, data)

// ==================== 抽奖 ====================
// 注：求片审核已并入「求片管理」（MediaSeek.vue），welfare.ts 不再保留求片 API。

// ==================== 配置 ====================

export const fetchWelfareConfig = () =>
  get<Record<string, string>>(`${E}/config`)

export const saveWelfareConfig = (data: Record<string, string>) =>
  put<{ success: boolean; updated: string[] }>(`${E}/config`, data)

// ==================== 群抽奖活动（G2） ====================
export type LotteryRoundStatus = 'open' | 'drawing' | 'done' | 'cancelled'

export interface LotteryRoundRow {
  id: number
  title: string
  chat_id: string
  status: LotteryRoundStatus
  participant_count: number
  prize_count: number
  draw_at: string | null
  created_at: string | null
}

export interface LotteryRoundPrizeRow {
  id: number
  round_id: number
  name: string
  type: 'days' | 'points' | 'whitelist'
  value: number
  quantity: number
  sort: number
}

export interface LotteryRoundEntryRow {
  id: number
  user_id: number | null
  telegram_id: string | null
  joined_at: string | null
}

export interface LotteryRoundWinnerRow {
  id: number
  prize_id: number
  prize_name: string
  user_id: number | null
  telegram_id: string | null
  distributed: boolean
  distributed_at: string | null
}

export interface LotteryRoundDetail {
  round: Record<string, unknown>
  prizes: LotteryRoundPrizeRow[]
  entries: LotteryRoundEntryRow[]
  winners: LotteryRoundWinnerRow[]
  verify: Record<string, unknown> | null
}

export interface CreateRoundPayload {
  title: string
  chat_id: number
  prizes: Array<{
    name: string
    type: 'days' | 'points' | 'whitelist'
    value: number
    quantity: number
  }>
  draw_at?: string | null
  max_participants?: number | null
}

export const fetchLotteryRounds = (params: { page?: number; page_size?: number; status?: LotteryRoundStatus } = {}) =>
  get<{ total: number; items: LotteryRoundRow[] }>(`${E}/lottery/rounds`, params)

export const createLotteryRound = (data: CreateRoundPayload) =>
  post<Record<string, unknown>>(`${E}/lottery/rounds`, data)

export const fetchLotteryRound = (id: number) =>
  get<LotteryRoundDetail>(`${E}/lottery/rounds/${id}`)

export const drawLotteryRound = (id: number) =>
  post<{ success: boolean; winners: unknown[]; distribute: unknown }>(`${E}/lottery/rounds/${id}/draw`, {})

export const cancelLotteryRound = (id: number) =>
  post<{ success: boolean }>(`${E}/lottery/rounds/${id}/cancel`, {})

// ==================== 活动提案审批（等级治理 Step 3） ====================
// 用户端 Lv.2+ 提交提案（POST /api/lottery/proposals），管理员在这里审批：
// 批准 -> 后端按提案内容建轮次（created_by=提案人）并发群公告，之后走正常开奖流程；
// 拒绝 -> 只改提案行，不建轮次、不发奖。note 两边都可选（后端库字段上限 500 字）。
export type LotteryProposalStatus = 'pending' | 'approved' | 'rejected'

export interface LotteryProposalPrize {
  name: string
  type: 'days' | 'points' | 'whitelist'
  value: number
  quantity: number
}

/** 对齐 backend/lottery_governance.py::proposal_to_dict */
export interface LotteryProposalRow {
  id: number
  user_id: number
  title: string
  chat_id: number | string
  lottery_type: 'button' | 'password'
  password_keyword: string | null
  prizes: LotteryProposalPrize[]
  draw_at: string | null
  max_participants: number
  level_snapshot: number
  status: LotteryProposalStatus
  reviewer_id: number | null
  reviewed_at: string | null
  review_note: string | null
  round_id: number | null
  created_at: string | null
}

export interface ApproveProposalResult {
  proposal: LotteryProposalRow
  round: {
    id: number
    title: string
    chat_id: number | string
    status: LotteryRoundStatus
    lottery_type: string
    seed_hash: string | null
  }
  /** 群公告结果：成功 {sent, group}；通知异常时为 {ok: false}（不影响批准本身） */
  notify: { sent?: boolean; group?: boolean; ok?: boolean }
}

export const fetchLotteryProposals = (params: { page?: number; page_size?: number; status?: LotteryProposalStatus } = {}) =>
  get<{ total: number; items: LotteryProposalRow[] }>(`${E}/lottery/proposals`, params)

export const approveLotteryProposal = (id: number, note?: string | null) =>
  post<ApproveProposalResult>(`${E}/lottery/proposals/${id}/approve`, { note: note || null })

export const rejectLotteryProposal = (id: number, note?: string | null) =>
  post<LotteryProposalRow>(`${E}/lottery/proposals/${id}/reject`, { note: note || null })

// ==================== 抽奖黑名单（三重门·资格门） ====================
// 被拉黑的用户无法参加任何群抽奖（lottery.is_blacklisted 在参加时拦截）。
// 增删都以站内用户 ID（web_users.id）为准；移除按 user_id，不是黑名单行 id。
export interface LotteryBlacklistRow {
  id: number
  user_id: number
  username: string | null
  reason: string | null
  created_at: string | null
}

export const fetchLotteryBlacklist = (params: { page?: number; page_size?: number } = {}) =>
  get<{ total: number; page: number; page_size: number; items: LotteryBlacklistRow[] }>(`${E}/lottery/blacklist`, params)

export const addLotteryBlacklist = (data: { user_id: number; reason?: string }) =>
  post<{ success: boolean; id: number }>(`${E}/lottery/blacklist`, { user_id: data.user_id, reason: data.reason ?? '' })

export const removeLotteryBlacklist = (userId: number) =>
  del<{ success: boolean }>(`${E}/lottery/blacklist/${userId}`)
