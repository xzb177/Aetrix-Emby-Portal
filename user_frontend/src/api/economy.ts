/**
 * 经济系统与邀请返利 API（backend/api/economy.py + invitation.py）
 */
import api from './index'
import type {
  MySubscription, Announcement, WatchStats, MediaSeekRequest, MediaSeekQuota,
  Ticket, MyPlaybackSession,
} from './index'

/**
 * 首页聚合接口：一次请求取回首屏 9 项数据（backend/api/home_summary.py）
 *
 * 原来是 9 个并发请求，浏览器同源 HTTP/1.1 只有 6 条并发上限，要分两波排队，
 * 骨架屏要熬两轮 RTT。合并成一个后首屏只剩一轮往返。
 *
 * 字段与原 9 个接口逐一对应（types 直接复用），服务端逐项独立容错：
 * 任一项失败只把该字段置 null，其余照常，前端各归各的降级。
 */
export interface HomeSummary {
  points: { balance: number } | null
  checkin: CheckinStatus | null
  invite: MyInviteInfo | null
  subscriptions: MySubscription[] | null
  announcements: Announcement[] | null
  stats: WatchStats | null
  media_seek: { requests: MediaSeekRequest[]; quota: MediaSeekQuota } | null
  tickets: Ticket[] | null
  sessions: { sessions: MyPlaybackSession[] } | null
}

export const homeApi = {
  summary: () => api.get<never, HomeSummary>('/api/user/home/summary'),
}

// ==================== 类型 ====================

export interface CheckinStatus {
  enabled: boolean
  checked_today: boolean
  streak: number
  base_points: number
  streak_bonus: number
  streak_max_bonus: number
  points: number
  /** R103-1：当前用户是否为公益服（是否会获得积分），后端 CheckinStatusResponse.points_enabled（2026-10-09 已上线） */
  points_enabled: boolean
}

export interface PointsLogEntry {
  id: number
  amount: number
  balance_after: number
  type: string
  description: string | null
  ref_id: string | null
  created_at: string | null
}

export interface PointsLogResponse {
  total: number
  balance: number
  logs: PointsLogEntry[]
}

export interface RechargePackage {
  id: number
  name: string
  amount: number
  bonus: number
  price: number
  is_popular: boolean
  icon: string | null
  total_points: number
}

export interface SubscriptionPlan {
  id: number
  name: string
  description: string | null
  price: number
  points_price: number | null
  duration_days: number
  features: string[] | null
  /** 订阅分档权益（后端经 parse 归一下发，未声明为 {}）；商店权益文案行据此自动生成 */
  entitlements?: Record<string, number> | null
  is_popular: boolean
  /** 套餐属于哪个服（一个服一个）：买哪份就开哪个服的会员 */
  realm_id?: number | null
  realm_name?: string
}

export interface PaymentMethod {
  id: string
  name: string
  enabled: boolean
}

export interface OrderRow {
  order_id: string
  kind: 'recharge' | 'subscription'
  item_name: string
  /** 实付金额（用券后就是折后价） */
  amount: number
  status: string
  created_at: string | null
  paid_at: string | null
  // 退款留痕（v2.9.0）：用户端要能直接看到「为什么退了」，否则只会来问客服
  refunded_at?: string | null
  refund_reason?: string
  // 优惠快照（v2.10.0）：原价 / 优惠金额 / 用的哪个码，用户要能自己对账
  list_price?: number
  discount_amount?: number
  coupon_code?: string
}

export interface MyInviteInfo {
  code: string
  use_count: number
  max_uses: number
  invited_count: number
  config: {
    enabled: boolean
    reward_points: number
    invitee_reward_points: number
    rebate_percent: number
  }
}

export interface InvitationRecordRow {
  id: number
  invitee_username: string
  reward_points: number
  created_at: string | null
}

export interface RebateRow {
  id: number
  amount: number
  description: string | null
  ref_id: string | null
  created_at: string | null
}

/** 推广奖励一笔（v2.44.0）：邀请成功后另发的，类型余额/有效期，数值全在服务端配置里 */
export interface PromotionRewardRow {
  id: number
  invitee_id: number
  invitee_username: string
  reward_type: 'balance' | 'days'
  reward_type_label: string
  reward_value: number
  realm_id: number | null
  created_at: string | null
}

export interface MyPromotions {
  total: number
  config: {
    enabled: boolean
    reward_type: string
    amount: number
    days: number
    reward_types: string[]
    reward_type_labels: Record<string, string>
  }
  rewards: PromotionRewardRow[]
}

// ==================== 签到 ====================

export const checkinApi = {
  status: () => api.get<never, CheckinStatus>('/api/user/economy/checkin/status'),
  doCheckin: () => api.post<never, { success: boolean; points_awarded: number; streak: number; balance: number; message: string }>('/api/user/economy/checkin'),
}

// ==================== 活力值 ====================

export interface VitalityStatus {
  success: boolean
  vitality: number
  max: number
  limit_threshold: number
  can_play: boolean
  suspended: boolean
}

/** 本地登录态里缓存的用户信息是否明确标记为公益服用户：
 * true = 是；false = 明确不是；null = 没标记（老登录态），以服务端为准。 */
function cachedUserIsWelfare(): boolean | null {
  try {
    const raw = localStorage.getItem('user')
    if (!raw) return null
    const u = JSON.parse(raw) as { is_welfare?: unknown }
    return typeof u.is_welfare === 'boolean' ? u.is_welfare : null
  } catch {
    return null
  }
}

export const vitalityApi = {
  status: () => {
    // 活力值仅限公益服用户：本地已明确知道不是时直接跳过网络请求——
    // 所有调用点本来就把失败当「不适用」隐藏，省掉这次注定 403 的往返
    //（开服当晚该接口在服务端刷了 111 条 403）。标记缺失时不拦，照常问服务端。
    if (cachedUserIsWelfare() === false) {
      return Promise.reject(new Error('vitality: not applicable for this user'))
    }
    return api.get<never, VitalityStatus>('/api/user/economy/vitality')
  },
  recharge: (points: number) =>
    api.post<never, { success: boolean; vitality_gained: number; points_spent: number; vitality: number; points_balance: number }>('/api/user/economy/vitality/recharge', { points }),
}

// ==================== 积分 ====================

export interface TransferConfig {
  enabled: boolean
  fee_pct: number
  min: number
  max: number
  daily_cap: number
}
export interface TransferResult {
  success: boolean
  amount: number
  fee: number
  recipient: string
  balance: number
}
export const pointsApi = {
  log: (params?: { limit?: number; offset?: number; type_filter?: string }) =>
    api.get<never, PointsLogResponse>('/api/user/economy/points/log', { params }),
  /** 积分转账开关与规则（关闭时用户端隐藏转账入口） */
  transferConfig: () =>
    api.get<never, TransferConfig>('/api/user/economy/points/transfer-config'),
  /** 积分转账：recipient 对方用户名，amount 正整数积分 */
  transfer: (recipient: string, amount: number) =>
    api.post<never, TransferResult>('/api/user/economy/points/transfer', { recipient, amount }),
}

// ==================== 兑换码 ====================

export const exchangeApi = {
  /** 兑换规则与开关（关闭时用户端隐藏核销入口，避免提交后报错） */
  config: () =>
    api.get<never, { enabled: boolean; invitee_note?: string }>('/api/user/economy/exchange/config'),
  redeem: (code: string) =>
    api.post<never, { success: boolean; reward_type?: string; points?: number; balance?: number; plan_name?: string; days?: number; message: string }>('/api/user/economy/exchange/redeem', { code }),
}

// ==================== 会员卡码（注册码 / 续期码 / 白名单码） ====================

export interface CodePreview {
  valid: boolean
  // coupon（v2.10.1）：优惠券与卡码/兑换码共用同一个入口，由后端预检识别来源
  kind: 'code' | 'invite' | 'exchange' | 'coupon' | 'unknown'
  code_type?: number
  type_name?: string
  days?: number
  days_text?: string
  discount_text?: string
  is_named?: boolean
  target_username?: string | null
  remaining_uses?: number
  /** 这张卡码开通哪个服的会员（一个服一个） */
  realm_id?: number
  realm_name?: string
  message: string
}

export const membershipApi = {
  /** 预检卡码：仅识别类型与天数，不核销（POST 以免卡码进访问日志） */
  preview: (code: string) =>
    api.post<never, CodePreview>('/api/user/membership/redeem/preview', { code }),
  /** 核销卡码：成功后直接开通或叠加会员 */
  redeem: (code: string) =>
    api.post<never, {
      success: boolean
      message: string
      code_type?: number
      days?: number
      realm_id?: number
      realm_name?: string
      end_date?: string
    }>(
      '/api/user/membership/redeem', { code }),
}

// ==================== 我的设备（第三方播放器登录设备） ====================

export interface MyDevice {
  device_id: string
  name: string | null
  client: string | null
  app_version: string | null
  ip: string | null
  is_blocked: boolean
  first_seen_at: string | null
  last_seen_at: string | null
  is_online_recent: boolean
}

export interface MyDevicesResponse {
  limit: number
  count: number
  active_count: number
  remaining: number | null
  auto_evict: boolean
  active_days: number
  devices: MyDevice[]
}

export const deviceApi = {
  mine: () => api.get<never, MyDevicesResponse>('/api/user/emby/devices'),
  remove: (deviceId: string) =>
    api.delete<never, { success: boolean; message: string }>(
      `/api/user/emby/devices/${encodeURIComponent(deviceId)}`),
}

// ==================== 我的网盘（用户自带存储绑定，BYOS） ====================
// 后端口径（backend/emby_server/portal.py user_emby_router，前缀 /api/user/emby）：
// GET /storage-bindings → { bindings: [...] }；POST /storage-bindings/drive/oauth/start
// { account_label } → { authorization_url, expires_in }；POST /storage-bindings
// { provider: 'pan115', account_label, credential } → 201 绑定行（验活失败属三档内时
// 仍 201 返回该行，status=invalid/expired + error_message，由客户端展示重绑引导）；
// POST /storage-bindings/{id}/verify → 绑定行；DELETE /storage-bindings/{id} → { success: true }。
// 115 的 credential 是 115 Cookie（后端经 transfer115.normalize_cookie 后密封存），
// 前端只原样送出一次，绝不在日志与报错里回显。
// 绑定行字段照抄 backend/emby_server/user_bindings.py serialize_binding：
// id / provider / account_label / status / last_verified_at / last_error_kind /
// error_message / has_credential / created_at，绝不含凭据原文。
export interface StorageBinding {
  id: number
  provider: string
  account_label: string
  status: string
  last_verified_at: string | null
  last_error_kind: string | null
  error_message: string | null
  has_credential: boolean
  created_at: string | null
}

export interface StorageBindingsResponse {
  bindings: StorageBinding[]
}

export interface DriveOAuthStartResponse {
  authorization_url: string
  expires_in: number
}

// 后台实际启用的挂载类型映射后的 provider 名单（GET /storage-bindings/providers）：
// 只含 provider 名串（如 drive / pan115），不含挂载路径、凭据、账号等敏感字段。
// 前端“我的网盘”按这份名单渲染对应块，名单里没有的块整个不渲染。
export interface StorageBindingProvidersResponse {
  providers: string[]
}

export const storageBindingApi = {
  list: () =>
    api.get<never, StorageBindingsResponse>('/api/user/emby/storage-bindings'),
  providers: () =>
    api.get<never, StorageBindingProvidersResponse>('/api/user/emby/storage-bindings/providers'),
  startDriveOAuth: (accountLabel: string) =>
    api.post<never, DriveOAuthStartResponse>(
      '/api/user/emby/storage-bindings/drive/oauth/start',
      { account_label: accountLabel },
    ),
  createPan115: (accountLabel: string, cookie: string) =>
    api.post<never, StorageBinding>(
      '/api/user/emby/storage-bindings',
      { provider: 'pan115', account_label: accountLabel, credential: cookie },
    ),
  verify: (bindingId: number) =>
    api.post<never, StorageBinding>(
      `/api/user/emby/storage-bindings/${bindingId}/verify`),
  remove: (bindingId: number) =>
    api.delete<never, { success: boolean }>(
      `/api/user/emby/storage-bindings/${bindingId}`),
}

// ==================== 优惠券（v2.10.0） ====================

/** 试算结果：能不能用、省多少、实付多少（与真正下单同一套口径） */
export interface CouponQuote {
  valid: boolean
  code: string
  coupon_id: number
  item_name: string
  list_price: number
  discount_amount: number
  paid_amount: number
  discount_type: 'percent' | 'fixed'
  value: number
}

export const couponApi = {
  /** 功能开关：关闭时钱包页不展示优惠码输入框（不让用户白填） */
  config: () =>
    api.get<never, { enabled: boolean }>('/api/user/economy/payment/coupon/config'),
  /** 试算：POST 以免优惠码进访问日志/浏览器历史 */
  quote: (data: { code: string; kind: 'recharge' | 'subscription'; item_id: number }) =>
    api.post<never, CouponQuote>('/api/user/economy/payment/coupon/quote', data),
}

/** 货币体系公开信息（P2/C4）：用户端自定义充值换算用 */
export const currencyApi = {
  info: () =>
    api.get<never, { name: string; recharge_ratio: number; quick_amounts: number[] }>('/api/user/economy/currency'),
}

export interface DiscountCredit {
  has_credit: boolean
  discount_pct?: number
  expires_at?: string | null
}
/** 兑换码折扣权益：用户核销 discount 型兑换码后获得，订阅下单自动抵扣 */
export const discountCreditApi = {
  get: () => api.get<never, DiscountCredit>('/api/user/economy/exchange/discount-credit'),
}

// ==================== 支付 ====================

export const paymentApi = {
  methods: () => api.get<never, PaymentMethod[]>('/api/user/economy/payment/methods'),
  packages: () => api.get<never, { enabled: boolean; packages: RechargePackage[] }>('/api/user/economy/payment/packages'),
  // is_free / access_mode = 当前服是公益服（免费开放）：此时不展示套餐与购买引导
  plans: () =>
    api.get<never, { enabled: boolean; plans: SubscriptionPlan[]; access_mode?: 'paid' | 'free'; is_free?: boolean; access_note?: string }>(
      '/api/user/economy/payment/plans',
    ),
  createOrder: (data: { kind: 'recharge' | 'subscription'; item_id?: number; payment_method: string; coupon_code?: string; custom_amount?: number; pay_with_points?: boolean }) =>
    api.post<never, { success: boolean; order_id: string; amount: number; list_price?: number; discount_amount?: number; coupon_code?: string; exchange_discount_pct?: number; pay_url?: string; message: string; paid_with_points?: boolean; points_cost?: number }>('/api/user/economy/payment/order', data),
  orders: (params?: { kind?: string; limit?: number }) =>
    api.get<never, { orders: OrderRow[] }>('/api/user/economy/payment/orders', { params }),
}

// ==================== 邀请返利 ====================

export const inviteApi = {
  config: () => api.get<never, MyInviteInfo['config']>('/api/user/invite/config'),
  myCode: () => api.get<never, MyInviteInfo>('/api/user/invite/my-code'),
  records: (params?: { limit?: number }) =>
    api.get<never, { total: number; records: InvitationRecordRow[] }>('/api/user/invite/records', { params }),
  rebates: (params?: { limit?: number }) =>
    api.get<never, { total_rebate: number; rebates: RebateRow[] }>('/api/user/invite/rebates', { params }),
  /** 我的推广奖励明细（邀请即发的一次性奖励，与充值返利分开两张表） */
  promotions: (params?: { limit?: number }) =>
    api.get<never, MyPromotions>('/api/user/invite/promotions', { params }),
}

// ==================== 会员等级（P1 统一货币体系） ====================

export interface MemberLevelInfo {
  level: number
  name: string
  xp_threshold: number
  discount_pct: number
  benefits: string[]
  badge_icon: string
  badge_color: string
}

export interface MyMemberInfo {
  level: number
  level_name: string
  xp: number
  badge_icon: string
  badge_color: string
  discount_pct: number
  next_level: number | null
  next_threshold: number | null
  xp_to_next: number | null
  progress_pct: number
  levels: MemberLevelInfo[]
}

export const memberApi = {
  /** 当前会员等级 / 经验 / 进度 / 全等级列表（含公开权益） */
  info: () => api.get<never, MyMemberInfo>('/api/user/member'),
}
