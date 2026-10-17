/** v2.3.0 经济系统管理 API（/api/admin/economy/*，详见 backend/api/admin.py） */
import { get, post, put, del } from '@/utils/request'
import type { SubscriptionOverview } from '@/types'

// ==================== 类型 ====================

export interface EconomyStats {
  total_points: number
  checkins_today: number
  orders: { pending: number; revenue: number }
  exchange_codes: { total: number; used: number }
  invitations: number
}

export interface PlanRowFull {
  id: number
  name: string
  description: string | null
  price: number
  points_price: number | null
  duration_days: number
  features: string[] | null
  is_active: boolean
  is_popular: boolean
  sort_order: number
  /** 一个服一个：留空时后端归到面板当前服 */
  realm_id?: number | null
  realm_name?: string
}

export interface PackageRow {
  id: number
  name: string
  amount: number
  price: number
  bonus: number
  is_active: boolean
  is_popular: boolean
  sort_order: number
}

export interface ExchangeCodeRow {
  id: number
  code: string
  type: 'points' | 'subscription' | 'discount'
  points_value: number
  plan_name: string | null
  duration_days: number
  discount_pct: number
  max_uses: number
  use_count: number
  is_active: boolean
  note: string | null
  expires_at: string | null
  used_by: { id: number; username: string }[]
  created_at: string
}

export interface OrderRow {
  order_id: string
  kind: 'recharge' | 'subscription'
  item_name: string
  username: string
  user_id: number
  amount: number
  status: string
  payment_method: string | null
  created_at: string | null
  paid_at: string | null
}

export interface InvitationRow {
  id: number
  inviter: string
  invitee: string
  reward_points: number
  created_at: string | null
}

export interface PointsLogRow {
  id: number
  user_id: number
  username: string
  amount: number
  balance_after: number
  type: string
  description: string | null
  ref_id: string | null
  audited: boolean // C3：是否有 hash（审计开启后写入的流水）
  created_at: string | null
}

export interface PointsAuditResult {
  ok: boolean
  user_id: number
  username: string
  total: number
  verified: number
  legacy_skipped: number
  broken_at: number | null
  broken_reason: string
}

export interface EconomySettings {
  [key: string]: string
}

// ==================== 统计 ====================

export const fetchEconomyStats = () => get<EconomyStats>('/economy/stats')

// ==================== 订阅总览 ====================

export const fetchSubscriptions = (params: { status_filter?: string; search?: string; limit?: number } = {}) =>
  get<SubscriptionOverview>('/economy/subscriptions', params)

/** 按用户直接授予订阅（与用户管理页共享后端逻辑） */
export const grantUserSubscription = (
  userId: number,
  data: { plan_id: number; duration_days: number; realm_id?: number }
) => post<{ success: boolean }>(`/users/${userId}/subscriptions`, data)

export const extendUserSubscription = (subscriptionId: number, days: number) =>
  post<{ success: boolean }>(`/subscriptions/${subscriptionId}/extend`, { days })

// ==================== 订阅套餐 ====================

/** 套餐清单；`realm_id=0` = 全部服，不传 = 当前服 */
export const fetchEconomyPlans = (realm_id?: number) =>
  get<{
    plans: PlanRowFull[]
    realm_id: number | null
    active_realm_id: number
    realms: { id: number; name: string }[]
  }>('/economy/plans', realm_id === undefined ? undefined : { realm_id })

export const createEconomyPlan = (data: Omit<PlanRowFull, 'id'>) => post<{ success: boolean }>('/economy/plans', data)

export const updateEconomyPlan = (id: number, data: Omit<PlanRowFull, 'id'>) =>
  put<{ success: boolean }>(`/economy/plans/${id}`, data)

export const deleteEconomyPlan = (id: number) => del<{ success: boolean; message?: string }>(`/economy/plans/${id}`)

// ==================== 充值套餐 ====================

export const fetchEconomyPackages = () => get<{ packages: PackageRow[] }>('/economy/packages')

export const createEconomyPackage = (data: Omit<PackageRow, 'id'>) =>
  post<{ success: boolean }>('/economy/packages', data)

export const updateEconomyPackage = (id: number, data: Omit<PackageRow, 'id'>) =>
  put<{ success: boolean }>(`/economy/packages/${id}`, data)

export const deleteEconomyPackage = (id: number) =>
  del<{ success: boolean; message?: string }>(`/economy/packages/${id}`)

// ==================== 兑换码 ====================

export const fetchExchangeCodes = (params: { limit?: number } = {}) =>
  get<{ codes: ExchangeCodeRow[] }>('/economy/exchange-codes', params)

// R40：删除死导出 createExchangeCodes——调用方 ExchangeCodes.vue 已删除，全仓零引用

export const updateExchangeCode = (id: number, is_active: boolean) =>
  put<{ success: boolean }>(`/economy/exchange-codes/${id}`, { is_active })

// ==================== 订单 ====================

export const fetchEconomyOrders = (params: {
  status_filter?: string
  kind?: string
  search?: string
  limit?: number
  offset?: number
} = {}) => get<{ total: number; orders: OrderRow[] }>('/economy/orders', params)

export const markOrderPaid = (orderId: string) =>
  post<{ success: boolean }>(`/economy/orders/${orderId}/mark-paid`)

// ==================== 订单关单 / 退款（v2.9.0） ====================
// 后端：backend/api/orders_admin.py

export interface RefundResult {
  success: boolean
  order_id: string
  kind: 'recharge' | 'subscription'
  status: string
  /** 实际扣回的积分（充值时） */
  revoked_points: number
  /** 实际回滚的会员天数（订阅时） */
  revoked_days: number
  /** 同时撤回的邀请人返利 */
  rebate_reversed: number
  /** 回滚后是否已撤销该订阅 */
  cancelled: boolean
  subscription_end?: string
}

export interface RefundRecord {
  order_id: string
  kind: 'recharge' | 'subscription'
  status: 'refunded' | 'closed'
  username: string
  reason: string
  at: string | null
}

/** 关闭未支付的订单（作废，不碰权益） */
export const closeOrder = (orderId: string) =>
  post<{ success: boolean; status: string }>(`/economy/orders/${orderId}/close`)

/** 退款：默认按账本回滚权益（积分 / 会员天数） */
export const refundOrder = (
  orderId: string,
  data: { reason?: string; revoke_entitlement?: boolean; allow_negative?: boolean; reverse_rebate?: boolean },
) => post<RefundResult>(`/economy/orders/${orderId}/refund`, data)

/** 最近的退款 / 关单记录（带原因，供复盘对账） */
export const fetchRefundRecords = (limit = 20) =>
  get<{ records: RefundRecord[] }>('/economy/refunds', { limit })

// ==================== 优惠券（v2.10.0） ====================
// 后端：backend/api/coupons_admin.py + backend/coupons.py
// 额度是预订制的：下单占额度（reserved）、付款转已用（consumed）、关单/退款释放（released）。

export interface CouponRow {
  id: number
  code: string
  kind: 'all' | 'subscription' | 'recharge'
  discount_type: 'percent' | 'fixed'
  /** percent: 实付百分比（90 = 九折）；fixed: 减免金额（元） */
  value: number
  min_amount: number
  max_discount: number
  realm_id: number | null
  realm_name: string
  /** 总次数上限，0 = 不限 */
  max_uses: number
  /** 已占用（预订 + 已消费） */
  use_count: number
  /** 每人限用，0 = 不限 */
  per_user_limit: number
  valid_from: string | null
  valid_until: string | null
  is_active: boolean
  note: string
  created_at: string | null
  stats: { reserved: number; consumed: number; released: number }
  usable: boolean
}

export interface CouponUsageRow {
  id: number
  coupon_id: number
  code: string
  user_id: number
  username: string
  order_id: string
  kind: string
  status: 'reserved' | 'consumed' | 'released'
  list_price: number
  discount_amount: number
  paid_amount: number
  created_at: string | null
  closed_at: string | null
}

export interface CouponSettings {
  enabled: boolean
  /** 超时未支付的预订多久自动关单并还额度（小时，0 = 不自动清理） */
  reserve_hours: number
  /** 当前占用中的预订数 */
  active_usage: number
}

export interface CouponCreatePayload {
  count: number
  code?: string
  kind: 'all' | 'subscription' | 'recharge'
  discount_type: 'percent' | 'fixed'
  value: number
  min_amount?: number
  max_discount?: number
  realm_id?: number | null
  max_uses?: number
  per_user_limit?: number
  valid_until?: string
  valid_days?: number
  note?: string
}

export const fetchCoupons = (params: { kind?: string; active?: string; search?: string; limit?: number } = {}) =>
  get<{ enabled: boolean; total: number; coupons: CouponRow[] }>('/economy/coupons', params)

export const createCoupons = (data: CouponCreatePayload) =>
  post<{ success: boolean; count: number; coupons: CouponRow[] }>('/economy/coupons', data)

export const updateCoupon = (id: number, data: Partial<Omit<CouponCreatePayload, 'count' | 'code'>> & { is_active?: boolean; valid_from?: string }) =>
  put<{ success: boolean; coupon: CouponRow }>(`/economy/coupons/${id}`, data)

export const deleteCoupon = (id: number) =>
  del<{ success: boolean; message: string }>(`/economy/coupons/${id}`)

export const fetchCouponUsages = (params: { limit?: number; coupon_id?: number } = {}) =>
  get<{ records: CouponUsageRow[] }>('/economy/coupons/usages', params)

export const fetchCouponSettings = () => get<CouponSettings>('/economy/coupons/settings')

export const updateCouponSettings = (data: { enabled: boolean; reserve_hours?: number }) =>
  put<{ success: boolean; enabled: boolean; reserve_hours: number }>('/economy/coupons/settings', data)

// ==================== 邀请与积分 ====================

export const fetchInvitations = (params: { limit?: number } = {}) =>
  get<{ records: InvitationRow[] }>('/economy/invitations', params)

export const fetchPointsLogs = (params: { user_id?: number; type_filter?: string; limit?: number; offset?: number } = {}) =>
  get<{ total: number; logs: PointsLogRow[] }>('/economy/points-logs', params)

export const adjustUserPoints = (userId: number, data: { amount: number; reason?: string }) =>
  post<{ success: boolean; balance: number }>(`/economy/users/${userId}/points`, data)

// ==================== C3 流水审计 ====================

export const verifyPointsAudit = (user_id: number) =>
  post<PointsAuditResult>('/economy/points-audit/verify', { user_id })

// ==================== 经济设置 ====================

export const fetchEconomySettings = () => get<{ settings: EconomySettings }>('/economy/settings')

export const updateEconomySettings = (settings: EconomySettings) =>
  put<{ success: boolean; changed: string[] }>('/economy/settings', { settings })

// ==================== 防盗链（v2.52.0） ====================
// 后端：backend/api/admin_economy.py（/api/admin/hotlink-config）
// 配置读取唯一实现：backend/emby_server/play_sign.py

export interface HotlinkConfigPayload {
  enabled: boolean
  play_sign_ttl: number
  /** Referer 白名单（逗号分隔域名），不传则后端不改 */
  allowed_referers?: string
}

export interface HotlinkConfig extends HotlinkConfigPayload {
  defaults: { enabled: boolean; play_sign_ttl: number }
}

/** GET /hotlink-config 的白名单字段：后端返回归一化后的域名数组 */
export type HotlinkConfigGet = Omit<HotlinkConfig, 'allowed_referers'> & {
  allowed_referers?: string | string[]
}

export const fetchHotlinkConfig = () => get<HotlinkConfigGet>('/hotlink-config')

export const updateHotlinkConfig = (payload: HotlinkConfigPayload) =>
  put<HotlinkConfig>('/hotlink-config', payload)

// ==================== 到期续费提醒（v2.8.0） ====================
// 后端：backend/api/reminders_admin.py + backend/reminders.py

/** 一次提醒动作的对象（面板预览用） */
export interface ReminderDue {
  user_id: number
  subscription_id: number
  /** 7d / 3d / 1d / expired */
  kind: string
  days_left: number
}

export interface ReminderStatus {
  enabled: boolean
  /** 提前几天提醒（默认 7/3/1） */
  thresholds: number[]
  interval_seconds: number
  /** 当前待发：临期 / 已到期 */
  pending_reminders: number
  pending_expired: number
  total_sent: number
  recent: { id: number; username: string; kind: string; sent_at: string | null }[]
}

export interface ReminderRunSummary {
  enabled: boolean
  dry_run: boolean
  thresholds: number[]
  reminded: number
  expired_notified: number
  skipped: number
  failed: number
  due: ReminderDue[]
}

export const fetchReminderStatus = () => get<ReminderStatus>('/economy/expiry-reminders')

export const updateReminderSettings = (
  settings: { expiry_reminder_enabled?: boolean; expiry_reminder_days?: string }
) =>
  put<{ success: boolean; settings: Record<string, unknown>; status: ReminderStatus }>(
    '/economy/expiry-reminders/settings', { settings }
  )

/** 立即执行一轮；dryRun=true 只预览会发给谁（不落库、不发消息） */
export const runReminders = (dryRun = false) =>
  post<ReminderRunSummary>(`/economy/expiry-reminders/run${dryRun ? '?dry_run=true' : ''}`)

// ==================== 会员等级（P1 统一货币体系） ====================

export interface MemberLevelRow {
  id: number
  level: number
  name: string
  xp_threshold: number
  discount_pct: number
  benefits: string[]
  badge_icon: string
  badge_color: string
  is_active: boolean
}

export const fetchMemberLevels = () =>
  get<{ levels: MemberLevelRow[] }>('/member-levels')

export const updateMemberLevel = (id: number, data: Partial<MemberLevelRow>) =>
  put<{ level: MemberLevelRow }>(`/member-levels/${id}`, data)

export const toggleMemberLevel = (id: number) =>
  put<{ level: MemberLevelRow }>(`/member-levels/${id}/toggle`)

export const deleteMemberLevel = (id: number) =>
  del<{ deleted: boolean }>(`/member-levels/${id}`)
