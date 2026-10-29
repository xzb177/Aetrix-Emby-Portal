/**
 * 等级公益服活动治理·管理端 API（Step 2：举报终审队列 + 治理停权）。
 *
 * 端点与后端逐字对应：backend/api/governance_report.py 的 admin_router
 * （前缀 /api/admin/governance；axios baseURL 已是 /api/admin，这里只写余下部分）。
 * 鉴权走 get_current_admin：只读审计（viewer）角色的写请求会被服务端 403，
 * 页面据 auth.admin.can_write 置灰，服务端仍自己判一次。
 *
 * 注意：这里的「停权」是 Step 2 的 GovernanceSuspension（恶意举报自动触发 / 手动），
 * 与 Step 4 的 GovernanceFreeze（/governance/freeze）是两套独立机制。
 */
import { get, post } from '@/utils/request'

const G = '/governance'

/** 举报状态：与 backend/governance_report.py 的 REPORT_STATUS_* 逐字一致 */
export type GovernanceReportStatus =
  | 'pending'
  | 'escalated'
  | 'accepted'
  | 'rejected'
  | 'review_rejected'

/** 举报原因：与后端 REPORT_REASONS 逐字一致 */
export type GovernanceReportReason = 'spam' | 'duplicate' | 'violation'

/** 终审决定：与后端 DecideIn.decision 一致 */
export type GovernanceDecision = 'accepted' | 'rejected'

/** 举报行（后端 _report_dict + _vote_stats） */
export interface GovernanceReportRow {
  id: number
  reporter_id: number
  /** v1 只有 media_seek（求片条目） */
  target_type: string
  target_id: number
  reason: GovernanceReportReason | string
  detail: string | null
  status: GovernanceReportStatus | string
  admin_note: string | null
  admin_id: number | null
  escalated_at: string | null
  decided_at: string | null
  created_at: string | null
  updated_at: string | null
  /** 复核票数（按人数） */
  ballots: number
  /** 同意侧权重合计 */
  agree_weight: number
  /** 不同意侧权重合计 */
  disagree_weight: number
}

/** 复核票（仅管理端详情返回） */
export interface GovernanceReviewVote {
  id: number
  reviewer_id: number
  decision: 'agree' | 'disagree' | string
  weight: number
  created_at: string | null
}

export interface GovernanceReportDetail extends GovernanceReportRow {
  votes: GovernanceReviewVote[]
}

/** 停权行（后端 _suspension_dict） */
export interface GovernanceSuspensionRow {
  id: number
  user_id: number
  /** auto_abuse（终审驳回计数自动触发）/ manual（管理员手动） */
  reason: 'auto_abuse' | 'manual' | string
  note: string | null
  /** 自动停权为 null */
  created_by_id: number | null
  starts_at: string | null
  ends_at: string | null
  revoked_at: string | null
  revoked_by_id: number | null
  created_at: string | null
}

/** 终审结果：驳回可能顺带触发举报人的自动停权 */
export interface GovernanceDecideResult extends GovernanceReportRow {
  auto_suspension?: GovernanceSuspensionRow
}

// ==================== 举报终审 ====================

/** 举报列表：不传 status 时后端只列终审队列（escalated，最早在前） */
export const fetchGovernanceReports = (params: { status?: string; limit?: number } = {}) =>
  get<GovernanceReportRow[]>(`${G}/reports/queue`, params)

/** 举报详情（含全部复核票） */
export const fetchGovernanceReport = (id: number) =>
  get<GovernanceReportDetail>(`${G}/reports/${id}`)

/** 终审：受理 accepted / 驳回 rejected（仅 escalated 可判，否则 409） */
export const decideGovernanceReport = (
  id: number,
  data: { decision: GovernanceDecision; note?: string },
) => post<GovernanceDecideResult>(`${G}/reports/${id}/decide`, data)

// ==================== 治理停权 ====================

/** 停权列表（最新在前） */
export const fetchGovernanceSuspensions = (
  params: { user_id?: number; active_only?: boolean; limit?: number } = {},
) => get<GovernanceSuspensionRow[]>(`${G}/suspensions`, params)

/** 手动停权：冻结治理特权（举报／复核），不扣经验、不降级；已有生效中停权 409 */
export const createGovernanceSuspension = (data: { user_id: number; days: number; note?: string }) =>
  post<GovernanceSuspensionRow>(`${G}/suspensions`, data)

/** 撤销停权（仅生效中的可撤销，否则 409） */
export const revokeGovernanceSuspension = (id: number) =>
  post<GovernanceSuspensionRow>(`${G}/suspensions/${id}/revoke`)
