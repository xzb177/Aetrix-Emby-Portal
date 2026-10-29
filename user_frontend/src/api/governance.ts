/**
 * 等级公益服活动治理·用户端 API（Step 2 举报 + Step 3 提案/直接发起）。
 * 端点与后端逐字对应：backend/api/governance_report.py（/api/user/governance）、
 * backend/api/lottery.py（/api/lottery）。月限、权重、停权判定一律以服务端为准，
 * 前端只展示与明示，不在本地算权限。
 */
import api from './index'

/** 举报原因：与后端 REPORT_REASONS 逐字一致 */
export type GovernanceReportReason = 'spam' | 'duplicate' | 'violation'

export interface GovernanceReport {
  id: number
  reporter_id: number
  target_type: string
  target_id: number
  reason: string
  detail: string | null
  status: string
}

export const governanceApi = {
  /** 举报一条求片（同一用户对同一条目终身只能举报 1 次，重复报 409） */
  createReport: (targetId: number, reason: GovernanceReportReason, detail?: string) =>
    api.post<never, GovernanceReport>('/api/user/governance/reports', {
      target_id: targetId,
      reason,
      ...(detail ? { detail } : {}),
    }),
}

/** 活动奖品：与后端 GovernanceRoundRequest.GovernancePrizeItem 同形 */
export interface LotteryPrizeInput {
  name: string
  type: 'days' | 'points' | 'whitelist'
  value: number
  quantity: number
}

/** 活动提案/直接发起请求体：与后端 GovernanceRoundRequest 同形 */
export interface LotteryRoundInput {
  title: string
  chat_id?: number | null
  prizes: LotteryPrizeInput[]
  draw_at?: string | null
  max_participants?: number | null
  lottery_type: 'button' | 'password'
  password_keyword?: string | null
}

/** 自己的活动提案行：与后端 proposal_to_dict 同字段 */
export interface LotteryProposal {
  id: number
  user_id: number
  title: string
  chat_id: number | null
  lottery_type: string
  password_keyword: string | null
  prizes: LotteryPrizeInput[]
  draw_at: string | null
  max_participants: number
  level_snapshot: number
  status: string
  reviewer_id: number | null
  reviewed_at: string | null
  review_note: string | null
  round_id: number | null
  created_at: string | null
}

export const lotteryGovernanceApi = {
  /** Lv.2+ 提交活动提案（只写提案行，不建轮次，需管理员批准） */
  submitProposal: (body: LotteryRoundInput) =>
    api.post<never, LotteryProposal>('/api/lottery/proposals', body),
  /** 查自己的活动提案（分页，默认倒序） */
  listMyProposals: (page = 1, pageSize = 20) =>
    api.get<never, { total: number; items: LotteryProposal[] }>('/api/lottery/proposals/mine', {
      params: { page, page_size: pageSize },
    }),
  /** Lv.5+ 直接发起活动（不经审批建轮次，事后审计） */
  directLaunch: (body: LotteryRoundInput) =>
    api.post<never, Record<string, unknown>>('/api/lottery/rounds/direct', body),
}
