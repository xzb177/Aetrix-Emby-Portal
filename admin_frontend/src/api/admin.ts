/** v2.2.0 管理端 API（全部走 /api/admin/*，详见 backend/api/admin.py 与 emby_server/portal.py） */
import { get, getBlob, getPublic, postBlob, post, put, patch, del, upload } from '@/utils/request'
import type {
  AdminInfo,
  AdminListResponse,
  AdminLogRow,
  AdminRole,
  AdminRow,
  Announcement,
  CdnConfig,
  CodeStats,
  DeviceRow,
  DeviceStats,
  LoginLogsResponse,
  EmbyLibrary,
  EmbyReachabilityReport,
  EmbyScanQueue,
  EmbyScanRun,
  EmbyScanTask,
  EmbySessionRow,
  LocalCacheConfig,
  LocalCacheEntryInfo,
  LocalCacheStats,
  LibraryPathInput,
  StorageBackend,
  Pan115Account,
  Pan115DirEntry,
  StorageMount,
  MountTypeMeta,
  MountDirEntry,
  PlaybackNode,
  PlaybackPolicy,
  PlaybackRuntime,
  PlayLinesSnapshot,
  EaMountHealth,
  RemoteServerRow,
  ServerKind,
  ServerKindMeta,
  ServerOverview,
  ServerOpsScanResult,
  ServerOpsSnapshot,
  ServerProbeResult,
  ServerSummary,
  LoginResponse,
  MediaSeekRow,
  RealmNodeSync,
  RealmOverview,
  RealmRow,
  RealmSubscriptionsResponse,
  RealmSummary,
  RealmsResponse,
  OverviewStats,
  PlaybackStats,
  RegistrationCode,
  RegistrationSettings,
  TicketMessageRow,
  TicketRow,
  TrendStats,
  UserDetail,
  UserGrants,
  UsersResponse,
} from '@/types'

// ==================== 认证 ====================

/** 管理员登录；站点开了「保护管理后台登录」时必须带人机验证令牌 */
export const login = (data: { username: string; password: string; captcha_token?: string }) =>
  post<LoginResponse>('/auth/login', data)

// ==================== 首次运行向导 ====================

/** 向导状态：初始化是否已完成（免鉴权，前端进 /admin/ 前先问一次） */
export const setupStatus = () => get<{ setup_completed: boolean }>('/setup/status')

/** 建第一个管理员；成功后向导入口永久关闭（再调直接 403）。
 * P0-4：部署时若配置了 SETUP_BOOTSTRAP_TOKEN，必须携带一致的 bootstrap_token。 */
export const createFirstAdmin = (data: { username: string; password: string; bootstrap_token?: string }) =>
  post<{ ok: boolean; username: string; setup_completed: boolean }>('/setup', data)

export const fetchMe = () => get<AdminInfo>('/auth/me')

export const changePassword = (data: { old_password: string; new_password: string }) =>
  post<{ success: boolean }>('/auth/change-password', data)

/**
 * 管理员登出：服务端吊销当前 access token 的 jti（P0-1 安全修复）。
 * 最佳努力：即使请求失败（比如 token 已失效）也继续走本地清理，不阻塞退出；
 * 因此永远 silent，不弹红色错误 toast。
 */
export const adminLogout = (refreshToken?: string) =>
  post<{ success: boolean }>(
    '/auth/logout',
    refreshToken ? { refresh_token: refreshToken } : {},
    { silent: true },
  )

// ==================== 用户管理 ====================

export const fetchUsers = (params: {
  search?: string
  active?: boolean
  /** 注册渠道：空 = 全部；__unrecorded = 升级前存量（未记录） */
  channel?: string
  /** 用户类型：空 = 全部；welfare = 公益服；paid = 付费；normal = 普通 */
  user_type?: string
  limit?: number
  offset?: number
}) => get<UsersResponse>('/users', params)

/** 用户 360° 详情：资料 / 订阅 / 积分 / 订单 / 邀请 / 签到 / 观看 */
export const fetchUserDetail = (id: number) => get<UserDetail>(`/users/${id}`)

/** 用户授权资源卡片（Phase 4）：一个服一张，看他现在能用什么 */
export const fetchUserGrants = (id: number) => get<UserGrants>(`/user-grants/${id}`)

/** 趋势统计（近 N 天，按日补零） */
export const fetchStatsTrend = (days = 14) => get<TrendStats>('/stats/trend', { days })

export interface PlanRow {
  id: number
  name: string
  description: string | null
  price: number
  duration_days: number
  is_popular: boolean
  /** 套餐属于哪个服（一个服一个）：授予订阅时按它开会员 */
  realm_id?: number | null
  realm_name?: string
}

/** 套餐清单（只取启用中的，供授予订阅下拉用）；`realm_id=0` = 全部服，不传 = 当前服 */
export const fetchPlans = (realm_id?: number) =>
  get<{ plans: PlanRow[]; realm_id: number | null; active_realm_id: number }>(
    '/economy/plans',
    {
      ...(realm_id === undefined ? {} : { realm_id }),
      only_active: true,
    }
  )

export const grantSubscription = (userId: number, data: { plan_id: number; duration_days: number }) =>
  post<{ success: boolean }>(`/users/${userId}/subscriptions`, data)

export const extendSubscription = (subscriptionId: number, days: number) =>
  post<{ success: boolean }>(`/subscriptions/${subscriptionId}/extend`, { days })

export const updateUser = (id: number, data: { is_active?: boolean; is_staff?: boolean }) =>
  put<{ success: boolean }>(`/users/${id}`, data)

export const resetUserPassword = (id: number, new_password: string) =>
  post<{ success: boolean }>(`/users/${id}/reset-password`, { new_password })

export const sendUserMessage = (id: number, data: { title: string; content: string }) =>
  post<{ success: boolean }>(`/users/${id}/messages`, data)

export const broadcastMessage = (data: { title: string; content: string }) =>
  post<{ success: boolean }>('/messages/broadcast', data)

// ==================== 卡码体系 ====================

/** 注册码门禁设置 */
export const fetchRegistrationSettings = () => get<RegistrationSettings>('/settings/registration')

export const updateRegistrationSettings = (data: {
  mode: string
  message?: string
  ratelimit_enabled?: boolean
  ratelimit_max?: number
  ratelimit_window?: number
}) => put<{ success: boolean }>('/settings/registration', data)

/** 类型化卡码生成（注册码 / 续期码 / 白名单码 / 诱饵码 / 指名码） */
export const generateCodes = (data: {
  code_type: number
  count: number
  days?: number | null
  max_uses: number
  expires_days: number
  algorithm?: string
  is_decoy?: boolean
  target_username?: string
  note?: string
  /** 这批码开通哪个服的会员（留空 = 当前服） */
  realm_id?: number
  /** M1：奖励类型 */
  reward_type?: string
  points_value?: number
  discount_pct?: number
}) => post<{
  success: boolean
  message: string
  realm_id: number
  realm_name: string
  codes: { id: number; code: string; days_text: string; expires_at: string }[]
}>(
  '/registration-codes/generate', data)

/** 卡码清单（按服；`realm_id=0` = 全部服，不传 = 当前服） */
export const fetchCodeList = (params: {
  code_type?: number
  state?: string
  keyword?: string
  realm_id?: number
  limit?: number
  offset?: number
} = {}) => get<{
  total: number
  realm_id: number | null
  realm_name: string
  realms: { id: number; name: string }[]
  codes: RegistrationCode[]
}>('/registration-codes/list', params)

export const fetchCodeStats = (realm_id?: number) =>
  get<CodeStats>('/registration-codes/stats', realm_id === undefined ? undefined : { realm_id })

export const patchRegistrationCode = (id: number, data: { is_active?: boolean; note?: string; realm_id?: number }) =>
  patch<{ success: boolean; code: RegistrationCode }>(`/registration-codes/${id}`, data)

export const deleteRegistrationCode = (id: number) =>
  del<{ success: boolean; message: string }>(`/registration-codes/${id}`)

// ==================== 设备风控 ====================

export const fetchDevices = (params: {
  user_id?: number
  keyword?: string
  only_blocked?: boolean
  limit?: number
  offset?: number
} = {}) => get<{ total: number; devices: DeviceRow[] }>('/devices', params)

export const fetchDeviceStats = () => get<DeviceStats>('/devices/stats')

export const setDeviceBlocked = (userId: number, deviceId: string, isBlocked: boolean) =>
  put<{ success: boolean; message: string }>(
    `/devices/${encodeURIComponent(deviceId)}`, { is_blocked: isBlocked }, { user_id: userId })

export const removeDevice = (userId: number, deviceId: string) =>
  del<{ success: boolean; message: string }>(
    `/devices/${encodeURIComponent(deviceId)}`, { user_id: userId })

// ==================== 登录 / 安全日志 ====================

export const fetchLoginLogs = (params: {
  username?: string
  ip?: string
  reason?: string
  success?: boolean
  limit?: number
  offset?: number
} = {}) => get<LoginLogsResponse>('/login-logs', params)

export const purgeLoginLogs = (days: number) =>
  post<{ success: boolean; message: string; deleted: number }>('/login-logs/purge', { days })

// ==================== 防共享：跨城市轨迹 + 同播检测 ====================

/** 四档处置：关闭 / 只记录 / 记录并告警 / 记录、告警并处置（缺省 off） */
export type ShareGuardAction = 'off' | 'record' | 'alert' | 'enforce'

export interface ShareGuardPolicy {
  travel_action: ShareGuardAction
  travel_window_minutes: number
  concurrent_action: ShareGuardAction
  concurrent_limit: number
  retention_days: number
  actions: ShareGuardAction[]
  action_labels: Record<string, string>
  /** 城市能不能查出来：没配「IP 与地理位置」能力时，跨城市检测只能不判定 */
  geo_ready: boolean
}

export interface ShareGuardEventRow {
  id: number
  user_id: number | null
  username: string
  kind: string
  kind_label: string
  action: string
  action_label: string
  ip: string
  region: string
  prev_region: string
  sessions: number
  detail: string
  created_at: string | null
}

export interface ShareGuardResponse {
  success: boolean
  policy: ShareGuardPolicy
  summary: {
    total: number
    travel_24h: number
    concurrent_24h: number
    enforced_24h: number
  }
  kinds: Array<{ value: string; label: string }>
  events: ShareGuardEventRow[]
}

export const fetchShareGuard = (params: { kind?: string; limit?: number; offset?: number } = {}) =>
  get<ShareGuardResponse>('/share-guard', params)

export const saveShareGuardPolicy = (policy: Partial<ShareGuardPolicy>) =>
  put<{ success: boolean; applied: Record<string, string>; policy: ShareGuardPolicy }>(
    '/share-guard/policy', { policy },
  )

export const purgeShareGuardEvents = (days: number | null) =>
  post<{ success: boolean; message: string; deleted: number }>('/share-guard/purge', { days })

// ==================== 访问拦截（UA 关键词 + IP 归属地） ====================

/** 归属地方向：屏蔽名单内 / 只允许名单内 */
export type AccessGuardRegionMode = 'block' | 'allow'

export interface AccessGuardPolicy {
  ua_enabled: boolean
  /** 已解析的关键词（后端存的也是规范化后的形式，不是原始长文本） */
  ua_allow: string[]
  ua_deny: string[]
  region_enabled: boolean
  region_mode: AccessGuardRegionMode
  region_countries: string[]
  region_keywords: string[]
  /** 是否真的在拦：开关都关了，或开了但一个关键词都没填，都算没生效 */
  active: boolean
  modes: AccessGuardRegionMode[]
  mode_labels: Record<string, string>
  /** 没配「IP 与地理位置」能力时，地区规则只能「不判定」并放行 */
  geo_ready: boolean
}

/** 试跑结果（不拦截任何东西，只回判定） */
export interface AccessGuardPreview {
  ip: string
  user_agent: string
  country: string
  region: string
  /** 归属地有没有查出来。false = 「不知道在哪」，此时一律放行 */
  resolved: boolean
  active: boolean
  blocked: boolean
  reason: string
  message: string
  matched: string
}

export const fetchAccessGuard = () =>
  get<{ success: boolean; policy: AccessGuardPolicy }>('/access-guard')

/**
 * 写入用的形状：关键词字段传**原始文本**（逗号/换行分隔）而不是数组。
 * 拆词、去重、大小写归一都由后端做（``access_guard.split_keywords``），
 * 前端不维护第二份解析规则；读回来时后端给的是已解析的数组。
 */
export interface AccessGuardPolicyInput {
  ua_enabled?: boolean
  ua_allow?: string
  ua_deny?: string
  region_enabled?: boolean
  region_mode?: AccessGuardRegionMode
  region_countries?: string
  region_keywords?: string
}

export const saveAccessGuardPolicy = (policy: AccessGuardPolicyInput) =>
  put<{ success: boolean; applied: Record<string, string>; policy: AccessGuardPolicy }>(
    '/access-guard/policy', { policy },
  )

export const previewAccessGuard = (payload: { ip: string; user_agent: string }) =>
  post<{ success: boolean; result: AccessGuardPreview }>('/access-guard/preview', payload)

// ==================== 邀请码管理（v2.44.0 第一阶段） ====================

/** 邀请码状态：可用 / 已用完 / 已过期 / 已作废 */
export type InvitationCodeState = 'active' | 'used_up' | 'expired' | 'revoked'

export interface InvitationCodeRow {
  id: number
  code: string
  owner_user_id: number
  owner_username: string
  max_uses: number
  use_count: number
  /** null = 不限次数（前端显示「不限」） */
  remaining: number | null
  whitelist: string[]
  is_active: boolean
  state: InvitationCodeState
  state_label: string
  expires_at: string | null
  expires_days_left: number | null
  reward_points: number
  created_at: string | null
}

export interface InvitationCodesResponse {
  success: boolean
  total: number
  summary: Record<string, number>
  states: Array<{ value: string; label: string }>
  codes: InvitationCodeRow[]
}

export const fetchInvitationCodes = (params: {
  keyword?: string
  owner_user_id?: number
  state?: string
  limit?: number
  offset?: number
} = {}) => get<InvitationCodesResponse>('/invitation-codes', params)

export const generateInvitationCodes = (data: {
  owner_user_id: number
  count: number
  max_uses: number
  expires_days: number
  whitelist?: string
}) =>
  post<{ success: boolean; message: string; codes: InvitationCodeRow[] }>(
    '/invitation-codes', data,
  )

export const updateInvitationCode = (
  id: number,
  data: { max_uses?: number; expires_days?: number; whitelist?: string; is_active?: boolean },
) => put<{ success: boolean; code: InvitationCodeRow }>(`/invitation-codes/${id}`, data)

export const revokeInvitationCodes = (ids: number[]) =>
  post<{ success: boolean; message: string; count: number }>('/invitation-codes/revoke', { ids })

// ==================== 推广奖励（v2.44.0 第一阶段） ====================

export interface PromotionPolicy {
  enabled: boolean
  reward_type: 'balance' | 'days'
  amount: number
  days: number
  reward_types: string[]
  reward_type_labels: Record<string, string>
  /** 真的会发吗（开关开着但值是 0 = 等同没开，后台要看得见） */
  active: boolean
}

export interface PromotionRewardRow {
  id: number
  invitee_id: number
  invitee_username: string
  inviter_username?: string
  reward_type: string
  reward_type_label: string
  reward_value: number
  realm_id: number | null
  created_at: string | null
}

export interface PromotionResponse {
  success: boolean
  policy: PromotionPolicy
  summary: { total: number; reward_24h: number }
  rewards: PromotionRewardRow[]
}

export const fetchPromotion = (params: { limit?: number; offset?: number } = {}) =>
  get<PromotionResponse>('/promotion', params)

/** 键是 SystemConfig 的键名（promotion_reward_*），不是 policy_payload 的读模型 */
export const savePromotionPolicy = (policy: Record<string, string>) =>
  put<{ success: boolean; applied: Record<string, string>; policy: PromotionPolicy }>(
    '/promotion/policy', { policy },
  )

// ==================== 媒体库可见范围：服务器默认 + 指定用户覆盖 ====================

/** 媒体库下拉选项（后端返回的全部库，含启用/虚拟状态） */
export interface LibraryScopeOption {
  id: number
  guid: string
  name: string
  is_enabled: boolean
  is_virtual: boolean
  item_count: number
}

/** 可选用户（后端已切上限；覆盖是按 user_id 存的，不依赖这份名单） */
export interface LibraryScopeUser {
  id: number
  username: string
  is_staff: boolean
  is_active: boolean
}

/** 某个用户的单独覆盖；`enabled=false` = 恢复跟随服务器默认（列表留着方便再开） */
export interface LibraryScopeOverride {
  enabled: boolean
  library_ids: number[]
}

export interface LibraryScopePolicy {
  default: {
    enabled: boolean
    library_ids: number[]
    /** 真的在生效吗（开着但选的库全被删了 = 没生效，后台要看得见） */
    active: boolean
  }
  overrides: Record<string, LibraryScopeOverride>
  libraries: LibraryScopeOption[]
  users: LibraryScopeUser[]
  counts: { libraries: number; overrides: number; users: number }
}

export interface LibraryScopeResponse extends LibraryScopePolicy {
  success: boolean
}

type LibraryScopeResult = {
  success: boolean
  applied: { enabled?: boolean; library_ids?: number[]; removed?: boolean; user_id?: number }
  policy: LibraryScopePolicy
}

export const fetchLibraryScope = () => get<LibraryScopeResponse>('/library-scope')

export const saveLibraryScopeDefault = (data: { enabled: boolean; library_ids: number[] }) =>
  put<LibraryScopeResult>('/library-scope', data)

export const saveLibraryScopeUser = (
  userId: number,
  data: { enabled: boolean; library_ids: number[] },
) => put<LibraryScopeResult>(`/library-scope/users/${userId}`, data)

export const removeLibraryScopeUser = (userId: number) =>
  del<LibraryScopeResult>(`/library-scope/users/${userId}`)

// ==================== 公告 ====================

export const fetchAnnouncements = (params: { active_only?: boolean } = {}) =>
  get<Announcement[]>('/announcements', params)

export const createAnnouncement = (data: { title: string; content: string; type?: string; is_pinned?: boolean }) =>
  post<{ success: boolean }>('/announcements', data)

export const updateAnnouncement = (id: number, data: Partial<{ title: string; content: string; type: string; is_pinned: boolean; is_active: boolean }>) =>
  put<{ success: boolean }>(`/announcements/${id}`, data)

export const deleteAnnouncement = (id: number) => del<{ success: boolean }>(`/announcements/${id}`)

// ==================== 探险预告 · 保密新大陆（揭晓位配置） ====================

/** 揭晓位配置（后端 GET/PUT /api/admin/teaser-config；字段见 backend/emby_server/teaser.py） */
export interface TeaserConfig {
  enabled: boolean
  /** 揭晓时间 ISO（服务器本地）；空串 = 一直保密 */
  reveal_at: string
  pre_title: string
  pre_subtitle: string
  post_title: string
  post_desc: string
  post_link: string
  /** 派生状态：off=已关闭 secret=保密中 revealed=已揭晓 */
  status: 'off' | 'secret' | 'revealed'
  countdown_days: number | null
}

export const fetchTeaserConfig = () =>
  get<TeaserConfig>('/teaser-config')

export const saveTeaserConfig = (values: Record<string, string | boolean>) =>
  put<TeaserConfig>('/teaser-config', { values })

// ==================== 工单 ====================

export const fetchTickets = (params: { status_filter?: string } = {}) =>
  get<TicketRow[]>('/tickets', params)

/** 工单状态 / 优先级调整 */
export const updateTicket = (id: number, data: { status?: string; priority?: string; category?: string }) =>
  put<{ success: boolean }>(`/tickets/${id}`, data)

export const fetchTicketMessages = (id: number) => get<TicketMessageRow[]>(`/tickets/${id}/messages`)

export const replyTicket = (id: number, data: { message: string; close_ticket?: boolean }) =>
  post<{ success: boolean }>(`/tickets/${id}/reply`, data)

export const closeTicket = (id: number) => post<{ success: boolean }>(`/tickets/${id}/close`)

// ==================== 求片 ====================

/** 求片清单；`realm_id=0` = 全部服，不传 = 当前服 */
export const fetchMediaSeeks = (params: { status_filter?: string; realm_id?: number; order?: string } = {}) =>
  get<MediaSeekRow[]>('/media-seek', params)

export const updateMediaSeek = (id: number, data: { status: string; admin_note?: string }) =>
  put<{ success: boolean }>(`/media-seek/${id}`, data)

/**
 * 入库后标记已入库：后端会先确认片真的在媒体库里（按 tmdb_id / 片名匹配），
 * 匹配不到返回 409；`force=true` 是人工兜底（片改了名入库时用）。
 */
export const markMediaSeekInLibrary = (id: number, force = false) =>
  post<{ success: boolean; matched: boolean; emby_item_id?: string | null; message: string }>(
    `/media-seek/${id}/mark-in-library`,
    { force },
    // silent：409（库里没找到）要由视图弹确认框，不能先弹一条红色错误
    { silent: true },
  )

/** 某用户的求片月度额度（公益/付费区分）：审核时查看该用户本月还剩几次 */
export interface MediaSeekMonthlyQuota {
  kind: 'welfare' | 'paid'
  monthly_limit: number
  monthly_used: number
  monthly_remaining: number
}
export const getMediaSeekUserQuota = (userId: number) =>
  get<MediaSeekMonthlyQuota>(`/media-seek/user-quota/${userId}`)

/**
 * 把求片交给外部服务：MoviePilot（提交订阅）或 qBittorrent（加种）。
 * qB 自己不会去找片子，所以选它时必须带链接（磁力或 .torrent 地址）。
 */
export const pushMediaSeek = (id: number, data: { target?: string; link?: string }) =>
  post<{ success: boolean; target: string; server?: string; message: string; status: string }>(
    `/media-seek/${id}/push`,
    data
  )

// ==================== 多服运营（/api/admin/realms） ====================

const R = '/realms'

/** 服清单（含每服的运营数据）+ 当前服 + 跨服汇总 */
export const fetchRealms = () => get<RealmsResponse>(R)

/** 只取各服的运营数据：给「数据概览」的服卡片用 */
export const fetchRealmOverview = () => get<RealmOverview>(`${R}/overview`)

export interface RealmPayload {
  name: string
  slug?: string
  url?: string
  description?: string
  is_active?: boolean
  /** paid（付费服，需要订阅）/ free（公益服，免费开放） */
  access_mode?: 'paid' | 'free'
  /** 公益服规则文案（用户端展示） */
  access_note?: string
  /** 下载策略：不传 = 跟随全局（公益服默认禁止下载） */
  allow_download?: boolean | null
}

export const createRealm = (data: RealmPayload) =>
  post<{ success: boolean; realm: RealmRow; summary: RealmSummary }>(R, data)

export interface RealmUpdateData {
  name?: string
  url?: string
  description?: string
  is_active?: boolean
  sort_order?: number
  access_mode?: 'paid' | 'free'
  access_note?: string
  /** 三态：follow = 跟随全局 / allow / deny */
  download_policy?: 'follow' | 'allow' | 'deny'
}

export const updateRealm = (id: number, data: RealmUpdateData) =>
  put<{ success: boolean; realm: RealmRow; summary: RealmSummary }>(`${R}/${id}`, data)

/** 切换面板当前操作的服：后台各页的作用域跟着切（服务端落库，多管理员一致） */
export const activateRealm = (id: number) =>
  post<{ success: boolean; active_realm_id: number; realm: RealmRow; summary: RealmSummary }>(
    `${R}/${id}/activate`
  )

/** 重新体检该服的所有播放节点（顺便带回节点自称的服与负责的库） */
export const syncRealmNodes = (id: number) =>
  post<{ success: boolean; nodes: RealmNodeSync[]; realm: RealmRow }>(`${R}/${id}/sync`)

/** 删除服；服里还有数据时必须传 move_to 指定数据移交给谁 */
export const deleteRealm = (id: number, moveTo?: number | null) =>
  del<{ success: boolean; deleted: number; moved_to: number | null; summary: RealmSummary }>(
    `${R}/${id}`,
    moveTo ? { move_to: moveTo } : undefined
  )

/** 某个服的订阅清单（`realmId=0` = 全部服）—— 订阅一个服一个，默认只看当前服 */
export const fetchRealmSubscriptions = (
  realmId: number,
  params: { status_filter?: string; search?: string; limit?: number; offset?: number } = {}
) =>
  get<RealmSubscriptionsResponse>(`${R}/${realmId}/subscriptions`, params)

// ==================== 服务器管理 ====================

const S = '/servers'

export const fetchServers = (params: { realm_id?: number } = {}) =>
  get<{
    servers: RemoteServerRow[]
    kinds: ServerKindMeta[]
    summary: ServerSummary
    realm_id: number | null
    active_realm_id: number
    realms: { id: number; name: string; slug: string }[]
  }>(S, params)

/**
 * Emby 总览：一行一台出流入口（EA / 已有 Emby），带归属服、连接、节点认领、
 * 挂载体检与媒体库归属。
 *
 * `live=true` 才会真的去问每台已启用的 EA「你是谁、属于哪个服」并重拉挂载体检；
 * 默认只读已落库的结论，不拖慢打开页面。
 */
export const fetchServersOverview = (params: { realm_id?: number; live?: boolean } = {}) =>
  get<ServerOverview>(`${S}/overview`, params)

export const fetchServersSummary = () => get<ServerSummary>(`${S}/summary`)

/**
 * 媒体运维快照（服务器维度）：整服扫描历史 + 扫描任务 + 刮削补全 / 修复 + 内容转交
 *
 * 按库的配置不在这里改，这里只回答「这台机器现在跑得怎么样」。
 * `include_unassigned=true` 把同服里还没分配节点的库也算进来（默认不算）。
 */
export const fetchServerOps = (serverId: number, params: {
  include_unassigned?: boolean
  runs_limit?: number
} = {}) => get<ServerOpsSnapshot>(`${S}/${serverId}/ops`, params)

/**
 * 一键把这台节点负责的库推入扫描队列（扫描 → 入库 → 刮削流水线）
 *
 * 与逐库「扫描」同一套机制：面板能扫的本地入队、归其它节点管的转发过去、
 * 已在队列的不重复推。
 */
export const runServerOpsScan = (serverId: number, params: { include_unassigned?: boolean } = {}) =>
  post<ServerOpsScanResult>(`${S}/${serverId}/ops/scan`, params)

export interface BackendServiceStatus {
  name: string; role: string; status: string
  last_heartbeat?: number | null; lag_seconds?: number | null
  pid?: number; started_at?: number; timestamp?: number
}
export const fetchBackendServices = () =>
  get<{ success: boolean; services: BackendServiceStatus[] }>(`/services/status`)

export interface ServerPayload {
  name: string
  kind: ServerKind
  url: string
  config?: Record<string, string>
  is_enabled?: boolean
  remark?: string
  /** 属于哪个服（留空 = 新增时归当前服，修改时不改归属） */
  realm_id?: number | null
}

export const createServer = (data: ServerPayload) =>
  post<{ success: boolean; server: RemoteServerRow; probe: ServerProbeResult }>(S, data)

export const updateServer = (id: number, data: ServerPayload) =>
  put<{ success: boolean; server: RemoteServerRow; probe: ServerProbeResult }>(`${S}/${id}`, data)

export const deleteServer = (id: number) =>
  del<{ success: boolean; summary: ServerSummary }>(`${S}/${id}`)

/** 测试「还没保存」的配置；编辑时带 server_id，密钥留空则沿用已保存的那份 */
export const testServerConfig = (data: {
  kind: ServerKind
  url: string
  config?: Record<string, string>
  server_id?: number
}) => post<ServerProbeResult>(`${S}/test`, data)

export const testServer = (id: number) => post<ServerProbeResult>(`${S}/${id}/test`)

/** 设为该类型的「当前使用」（EA / Emby）：服务端会先重新体检一次再切换 */
export const activateServer = (id: number) =>
  post<{
    success: boolean
    activated?: boolean
    mode?: string
    message?: string
    probe: ServerProbeResult
    server?: RemoteServerRow
    /** 激活 EA 时会顺带拉一次「EA 视角的挂载体检」，失败代表那台机器碰不到你的存储 */
    mounts_health?: { ok: boolean; error?: string; checked_at?: string | null; failed_count?: number } | null
  }>(`${S}/${id}/activate`)

export const toggleServer = (id: number) =>
  post<{ success: boolean; server: RemoteServerRow; summary: ServerSummary }>(`${S}/${id}/toggle`)

/** 重拉一次「EA 视角的挂载体检」（挂载页也有入口，这里给服务器页用） */
export const refreshServerMounts = () =>
  post<{ success: boolean; health: { ok: boolean; error?: string; checked_at?: string | null }; server: string }>(
    `${S}/mounts/health`
  )

// ==================== 日志与统计 ====================

export const fetchLogs = (params: { limit?: number; action_filter?: string } = {}) =>
  get<AdminLogRow[]>('/logs', params)

export const fetchOverview = () => get<OverviewStats>('/stats/overview')

export interface PanelHealth {
  status: string
  health_level?: string
  health_issues?: Array<{ level: string; key: string; message: string }>
  timestamp: string
  database: string
  online_users: number
  emby_server: string
  /** v2.56.0 可观测快照（后端开关 OBSERVABILITY_ENABLED 控制，未开时为 { enabled: false }） */
  observability?: {
    enabled: boolean
    level?: 'ok' | 'warn' | 'down'
    subsystems?: Record<string, { key: string; status: 'ok' | 'warn' | 'down'; message: string; metrics: Record<string, any> }>
    thresholds?: Record<string, number>
  }
}

/** EM 面板健康状态；不暴露密钥与连接串，仅返回可运营的信息。
 *
 *  ``/api/health`` **不在后台前缀下**，所以必须走 `getPublic`（无 baseURL 的实例）：
 *  用 `get` 的话 axios 会拼成 `/api/admin/api/health` → 404。同理不要再写成
 *  `'/../health'` —— 那是靠浏览器归一化“能用”的写法，换个反代就断。
 */
export const fetchPanelHealth = () => getPublic<PanelHealth>('/api/health')

/** 一键体检（v2.57.0 只读诊断；阶段二加自动修复）：诊断报告与规则清单 */
export interface DiagnosisFinding {
  rule_id: string
  name: string
  level: 'info' | 'warn' | 'critical'
  level_cn: string
  title: string
  description: string
  steps: string[]
  evidence: Record<string, any>
  fixable?: boolean
}

export interface DiagnosisFixInfo {
  name: string
  description: string
  confirm_text: string
}

export interface DiagnosisReport {
  ran_at: string
  summary: { total: number; info: number; warn: number; critical: number }
  findings: DiagnosisFinding[]
  note: string
}

export interface DiagnosisRules {
  rules: Array<{ rule_id: string; name: string }>
  thresholds: Record<string, number>
  levels: string[]
  fixes: Record<string, DiagnosisFixInfo>
  fix_thresholds: Record<string, number>
  manual_only: Record<string, string>
}

export interface DiagnosisFixResult {
  ok: boolean
  rule_id: string
  name: string
  result?: Record<string, any>
  error?: string
  message?: string
}

export const runDiagnosis = () => get<DiagnosisReport>('/diagnosis/run')
export const fetchDiagnosisRules = () => get<DiagnosisRules>('/diagnosis/rules')
export const fixDiagnosisRule = (rule_id: string) =>
  post<DiagnosisFixResult>('/diagnosis/fix', { rule_id, confirm: true })

// ==================== API Key / Service Token（/api/admin/api-keys/*） ====================

export interface ApiKeyRow {
  id: number
  key_id: string
  key_id_masked: string
  name: string
  scopes: string[]
  expires_at: string | null
  is_active: boolean
  last_used_at: string | null
  created_at: string | null
}

export interface ApiKeyCreateResponse extends ApiKeyRow {
  key: string // 明文，只在创建时返回一次
}

export const fetchApiKeys = () => get<{ items: ApiKeyRow[]; total: number }>('/api-keys')
export const createApiKey = (data: { name: string; scopes: string[]; expires_days: number | null }) =>
  post<ApiKeyCreateResponse>('/api-keys', data)
export const updateApiKey = (keyId: string, data: { name?: string; scopes?: string[]; expires_days?: number | null; is_active?: boolean }) =>
  put<ApiKeyRow>(`/api-keys/${keyId}`, data)
export const revokeApiKey = (keyId: string) => del<{ ok: boolean }>(`/api-keys/${keyId}`)

// 注：「Emby 服务入口」那两个格子的页面（及它的读写接口封装）已下线，
// EA / Emby 入口统一在「服务器」页维护（见 views/Servers.vue）。
// 后端 /api/admin/emby/servers 仍在（挂载体检与列表页要用），但前端不再直连。

export const fetchPlaybackStats = () => get<PlaybackStats>('/stats/playback')

// ==================== 自建 Emby 管理（/api/admin/emby/*） ====================

const E = '/emby'

export const fetchEmbyOverview = () => get<{ total_items: number; total_libraries: number; active_sessions: number; total_users: number }>(`${E}/overview`)

/** 封面自动生成的配置：样式 + 标题文字（都支持 {library}{type}{year} 变量） */
export interface LibraryCoverConfig {
  template: 'poster' | 'visual' | 'filmstrip'
  title: string
  subtitle: string
}

export const fetchLibraries = () => get<{ libraries: EmbyLibrary[]; storage_backends?: StorageBackend[] }>(`${E}/libraries`)

/** 拉取封面二进制；管理端图片请求也带 JWT，不把令牌拼进 URL。 */
export const fetchLibraryCover = (id: number) => getBlob(`${E}/libraries/${id}/cover`)

export const uploadLibraryCover = (id: number, file: File) => {
  const data = new FormData()
  data.append('file', file)
  return upload<{ success: boolean; cover_url: string; content_type: string }>(
    `${E}/libraries/${id}/cover`, data
  )
}

export const removeLibraryCover = (id: number) =>
  del<{ success: boolean }>(`${E}/libraries/${id}/cover`)

/** 封面自动生成：按当前配置渲染一张预览图（不落库，改一个字调一次） */
export const previewLibraryCover = (id: number, body: LibraryCoverConfig) =>
  postBlob(`${E}/libraries/${id}/cover/preview`, body)

/** 封面自动生成：渲染并落库落盘，返回新的封面 URL */
export const renderLibraryCover = (id: number, body: LibraryCoverConfig) =>
  post<{
    success: boolean
    cover_url: string
    content_type: string
    template: string
    library_name: string
    media_type: string
  }>(`${E}/libraries/${id}/cover/render`, body)

/** 封面自动生成：按库里已保存的样式/标题重新生成（刮削补完新片后换封面） */
export const regenerateLibraryCover = (id: number) =>
  post<{ success: boolean; cover_url: string }>(`${E}/libraries/${id}/cover/regenerate`)

export const createLibrary = (data: {
  name: string
  collection_type: string
  paths: string[]
  /** 简化路径（路径 + 存储后端分开）：传了它就以它为准，mount:// 前缀由后端拼 */
  path_entries?: LibraryPathInput[]
  /** 绑定的存储挂载（本机目录 / 115 / rclone） */
  mount_ids?: number[]
  /** 刮削策略：missing_only（仅缺失时）/ 3m / 6m / 1y / all（全部重刮） */
  scrape_policy?: string
  /** 115 账号配置档：建库时就能绑，省得建完再进设置改一趟 */
  account_115_id?: number
  /** 归属服（留空 = 面板当前服）：内容隔离的边界 */
  realm_id?: number
  /** 归属播放节点（留空 = 未分配：所有节点可见、由面板扫描） */
  node_id?: number
  /** 封面自动生成：样式 + 标题文字；空 = 用直传封面 */
  cover_template?: 'poster' | 'visual' | 'filmstrip' | null
  cover_title?: string | null
  cover_subtitle?: string | null
  /** 新片入库后自动重生成封面；不传 = 关 */
  cover_auto_regen?: boolean
  /** 扫描策略开关；不传 = 默认开 */
  incremental_scan?: boolean
  fs_watch?: boolean
}) => post<{ success: boolean; id: number; guid: string }>(`${E}/libraries`, data)

export const updateLibrary = (
  id: number,
  data: {
    name?: string
    collection_type?: string
    paths?: string[]
    /** 简化路径（路径 + 存储后端分开）；传 null / [] = 清空路径 */
    path_entries?: LibraryPathInput[]
    /** 存储挂载绑定：传 [] 表示解绑全部，省略则不修改 */
    mount_ids?: number[]
    is_enabled?: boolean
    scrape_policy?: string
    /** 115 账号配置档：传 null 表示解绑（回退默认账号），省略则不修改 */
    account_115_id?: number | null
    /** 归属服：传 null 表示不标注（所有服可见），省略则不修改 */
    realm_id?: number | null
    /** 归属播放节点：传 null 表示不分配（所有节点可见），省略则不修改 */
    node_id?: number | null
    /** 封面自动生成：传 null/空串表示清除（回退直传封面），省略则不修改 */
    cover_template?: 'poster' | 'visual' | 'filmstrip' | null
    cover_title?: string | null
    cover_subtitle?: string | null
    /** 新片入库后自动重生成封面；传 false = 关，省略则不修改 */
    cover_auto_regen?: boolean
    /** 扫描策略开关；传 false = 关，省略则不修改 */
    incremental_scan?: boolean
    fs_watch?: boolean
  }
) => put<{ success: boolean; rescan_required?: boolean }>(`${E}/libraries/${id}`, data)

/** 按发行平台自动生成虚拟媒体库（Netflix / Disney+ / Apple TV+ …） */
export const generateVirtualLibraries = (data: { platforms?: string[]; enabled?: boolean; prune?: boolean } = {}) =>
  post<{
    success: boolean
    created: { id: number; platform: string; name: string }[]
    updated: { id: number; platform: string; name: string }[]
    pruned: { id: number; platform: string; name: string }[]
    available_platforms: { platform: string; name: string }[]
  }>(`${E}/libraries/virtual`, data)

/** 待修复条目：数据库里有图片记录但取不到图（本地文件丢失 / 远程图失效） */
export const fetchRepairQueue = () =>
  get<{ total: number; items: { id: string; name: string; type: string; requested_at: string | null }[] }>(
    `${E}/libraries/repair/queue`
  )

export const runRepairQueue = () =>
  post<{ success: boolean; libraries: number[]; already?: number[] }>(`${E}/libraries/repair/run`)

/**
 * 触发一个媒体库的扫描（v2.27.0 起是「入队」）
 *
 * - `started: true`：没被挡住，已经开扫；
 * - `started: false` + `task.position`：排在第几位（`task.waiting_for` 写明在等哪个挂载）；
 * - `already: true`：这个库已经在扫描 / 已在队列里（重复点击不报错），`message` 里有原因。
 */
export const scanLibrary = (id: number, full = false) =>
  post<{
    success: boolean
    queued?: boolean
    already?: boolean
    started?: boolean
    state?: string
    message?: string
    task?: EmbyScanTask
  }>(`${E}/libraries/${id}/scan${full ? '?full=true' : ''}`)

/** 一键扫描全部：把所有启用的库按顺序加入扫描队列（增量）。新用户挂载后点这个。 */
export const scanAllLibraries = () =>
  post<{
    success: boolean
    queued_count: number
    already_count: number
    skipped_count: number
    queued: Array<{ id: number; name: string }>
    already: Array<{ id: number; name: string; state?: string }>
    skipped: Array<{ id: number; name: string; reason?: string }>
    message?: string
  }>('/emby/scan/all')

/** 本机目录实时监听状态（含降级原因；设置页用来告知“监听不可用，已改用定时扫描”） */
export const fetchFsWatchStatus = () => get<{
  available: boolean
  running: boolean
  debounce_sec: number
  min_interval_sec: number
  watched_libraries: number[]
  degraded: Record<string, string>
  stats: { events: number; triggers: number; coalesced: number; errors: number }
}>(`${E}/fs-watch/status`)

/** 扫描队列快照：正在跑 / 排队中 / 最近完成 + 远程 IO 计数（面板每几秒轮询一次） */
export const fetchScanQueue = () => get<EmbyScanQueue>(`${E}/scan-queue`)

// ==================== 元数据与刮削（EmbyAdmin.vue「元数据与刮削」分组） ====================

/** 密钥池里的一把 key（只给掩码与运行状态，**没有原文**） */
export interface TmdbKeyPoolRow {
  /** 展示序号（从 1 开始，与删除接口一致） */
  index: number
  masked: string
  /** 此刻正在用它发请求 */
  current: boolean
  /** 被限流 / 失效后进了冷却 */
  cooling: boolean
  cooldown_remaining: number
  reason: string
  hits: number
}

export interface TmdbKeysStatus {
  configured: boolean
  source: 'env' | 'db' | 'none'
  count: number
  masked: string[]
  env_present: boolean
  /** 实际在打请求/秒（自适应后） */
  rate?: number
  rate_ceiling?: number
  throttled?: number
  /** 逐把状态：哪把在用、哪把在冷却（还要 xx 秒） */
  pool?: TmdbKeyPoolRow[]
  keys_cooling?: number
  /** 当前生效的镜像地址（空配置 = 官方地址） */
  api_base?: string
  image_base?: string
  api_base_default?: string
  image_base_default?: string
  api_base_from_env?: boolean
  image_base_from_env?: boolean
}

/** TMDB Key 状态（只返回掩码与数量） */
export const fetchTmdbKeys = () => get<TmdbKeysStatus>(`${E}/scrape/tmdb-keys`)

/** 保存 TMDB API Keys（逗号/换行/空白分隔）：落库后立即热生效 */
export const saveTmdbKeys = (keys: string) =>
  put<{ success: boolean; saved: number; source: string; count: number }>(`${E}/scrape/tmdb-keys`, { keys })

/** 逐把增：往密钥池追加一把（去重；已存在返回 409） */
export const addTmdbKey = (key: string) =>
  post<{
    success: boolean
    count: number
    masked: string[]
    /** 环境变量里也有 key 时为 false（写入成功但暂不生效） */
    effective: boolean
    note: string
  }>(`${E}/scrape/tmdb-keys/add`, { key })

/** 逐把删：按展示序号（从 1 开始）删掉一把 */
export const deleteTmdbKey = (index: number) =>
  del<{ success: boolean; removed: string; count: number; masked: string[] }>(
    `${E}/scrape/tmdb-keys/${index}`)

/** 清除全部密钥的冷却（换完 key / 网络恢复后手动重来一次） */
export const resetTmdbKeyCooldown = () =>
  post<{ success: boolean; cleared: number; pool: TmdbKeyPoolRow[] }>(
    `${E}/scrape/tmdb-keys/cooldown/reset`)

export interface TmdbMirror {
  api_base: string
  image_base: string
  api_base_from_env?: boolean
  image_base_from_env?: boolean
  defaults: { api_base: string; image_base: string }
  cooldown_sec: number
  invalid_cooldown_sec: number
  /** 冷却时长对应的 SystemConfig 键（阈值不写死，可在配置里改） */
  cooldown_config_keys: string[]
}

export const fetchTmdbMirror = () => get<TmdbMirror>(`${E}/scrape/tmdb-mirror`)

/** 保存镜像地址（两个都传空 = 回到官方地址） */
export const saveTmdbMirror = (data: { api_base: string; image_base: string }) =>
  put<{ success: boolean; api_base: string; image_base: string; defaults: TmdbMirror['defaults'] }>(
    `${E}/scrape/tmdb-mirror`, data)

export interface TmdbLanguageStatus {
  language: string
  options: string[]
  default: string
  from_env: boolean
}

/** TMDB 首选语言的展示文案（后端只给值，文案前端定） */
export const TMDB_LANGUAGE_LABELS: Record<string, string> = {
  'zh-CN': '简体中文',
  'zh-TW': '繁体中文',
  'en-US': '英文',
  'ja-JP': '日文',
}

/** TMDB 首选语言：简介/标题/别名返回哪种语言 */
export const fetchTmdbLanguage = () => get<TmdbLanguageStatus>(`${E}/scrape/tmdb-language`)

export const saveTmdbLanguage = (language: string) =>
  put<{ success: boolean; language: string }>(`${E}/scrape/tmdb-language`, { language })

export interface EnrichEpisodeNightConfig {
  success: boolean
  enabled: boolean
  night_start: string
  night_end: string
  night_tz: string
  from_env: boolean
  in_window: boolean
  defaults: { enabled: boolean; night_start: string; night_end: string; night_tz: string }
  tz_options?: string[]
}

/** 单集夜间刮削：白天只刮顶层条目，单集留到夜间窗口刮 */
export const fetchEnrichEpisodeNight = () =>
  get<EnrichEpisodeNightConfig>(`${E}/scrape/enrich-episode-night`)

export const saveEnrichEpisodeNight = (enabled: boolean, night_start: string, night_end: string, night_tz: string) =>
  put<EnrichEpisodeNightConfig>(`${E}/scrape/enrich-episode-night`, {
    enabled,
    night_start,
    night_end,
    night_tz,
  })

/** 刮削性能参数（链路规划 Step 1）：env 旋钮搬进 SystemConfig + 后台 UI */
export interface EnrichTuningConfig {
  success: boolean
  values: Record<string, number | boolean>
  raw: Record<string, string>
  from_env: Record<string, boolean>
  restart_required: string[]
  order: string[]
  defaults: Record<string, string>
  descriptions: Record<string, string>
  env_names: Record<string, string | null>
  db_pool: {
    pool_size: number
    max_overflow: number
    from_env: boolean
    env_name: string
    default: number
    database_type: string
    restart_required: boolean
    saved_pool_size: string
    pending_restart: boolean
  }
}

/** 刮削性能参数：当前生效值 + 默认值 + env 接管状态 */
export const fetchEnrichTuning = () =>
  get<EnrichTuningConfig>(`${E}/scrape/enrich-tuning`)

export const saveEnrichTuning = (values: Record<string, string | number | boolean>, db_pool_size?: string) =>
  put<EnrichTuningConfig>(`${E}/scrape/enrich-tuning`, {
    values,
    db_pool_size: db_pool_size ?? null,
  })

export interface TmdbTestResult {
  index: number
  masked: string
  ok: boolean
  message: string
}

/** 一键测试全部密钥：keys 为空则测当前生效的密钥池（测不通的会被放进冷却） */
export const testTmdbKeys = (keys?: string) =>
  post<{ results: TmdbTestResult[]; pool?: TmdbKeyPoolRow[] }>(
    `${E}/scrape/tmdb-test`, { keys: keys || '' })

// ==================== 多源元数据（Phase 6b） ====================

/** 某个源密钥池里的一把 key（同样只给掩码与运行状态） */
export interface MetaSourceKeyRow {
  index: number
  masked: string
  cooling: boolean
  cooldown_remaining: number
  reason: string
  hits: number
}

/** 一个数据源在配置里的样子（静态能力 + 运行状态都在这里） */
export interface MetaSourceRow {
  id: string
  label: string
  /** 一句话能力说明：它强在哪、缺什么（从后端的源注册表来，不在前端写死） */
  note: string
  /** 当前位次（从 1 开始，与拖动后的顺序一致） */
  position: number
  enabled: boolean
  requires_key: boolean
  /** 源天然给哪种语言：zh / en */
  lang: string
  /** 两次请求最小间隔秒数（0 = 不限速） */
  rate: number
  key_count: number
  keys: MetaSourceKeyRow[]
  cooling: number
  /** 这一轮为什么不参与（空 = 会参与）：已关闭 / 缺密钥 */
  skipped_reason: string
  /** key 去哪申请（后台直接给链接与说明，不用去搜索引擎里找） */
  apply_url?: string
  apply_hint?: string
  /** 密钥存在哪个配置键（告诉用户“去哪填”） */
  key_storage?: string
  /** true = 已废弃的旧键里还有残留（启动自愈会合并，界面提醒看一眼） */
  keys_legacy?: boolean
  /** 密钥在哪里填：inline = 本卡片行内输入；pool_card = 在下面的密钥池卡片里（TMDB） */
  key_entry?: 'inline' | 'pool_card'
}

export interface MetaSourcesConfig {
  enabled: boolean
  prefer_chinese: boolean
  /** TMDB 搜过无命中时，中文标题的剧/电影是否走多源兜底（须与总开关同时开） */
  tmdb_miss_fallback: boolean
  order: string[]
  sources: MetaSourceRow[]
  default_order: string[]
  max_sources: number
  collect_timeout_sec: number
  active_count: number
}

export const fetchMetaSources = () => get<MetaSourcesConfig>(`${E}/scrape/meta-sources`)

/** 豆瓣优先配置：中文标题先走豆瓣（总开关默认开） */
export interface DoubanConfig {
  enabled: boolean
  min_interval: number
}
export const fetchDoubanConfig = () => get<DoubanConfig>(`${E}/scrape/douban-config`)
export const saveDoubanConfig = (enabled: boolean, min_interval: number) =>
  put<DoubanConfig>(`${E}/scrape/douban-config`, { enabled, min_interval })

/** 豆瓣封禁降级（链路规划 Step 0.5）：封禁期跳过豆瓣走多源兜底 */
export interface DoubanBanDowngradeConfig {
  success: boolean
  enabled: boolean
  banned: boolean
  ban_remaining_sec: number
}
export const fetchDoubanBanDowngrade = () =>
  get<DoubanBanDowngradeConfig>(`${E}/scrape/douban-ban-downgrade`)

export const saveDoubanBanDowngrade = (enabled: boolean) =>
  put<DoubanBanDowngradeConfig>(`${E}/scrape/douban-ban-downgrade`, { enabled })

/** 兜底链路统一（链路规划 Step 2）：一个豆瓣入口（豆瓣 rich → 多源引擎含豆瓣源） */
export interface UnifiedFallbackConfig {
  success: boolean
  enabled: boolean
}
export const fetchUnifiedFallback = () =>
  get<UnifiedFallbackConfig>(`${E}/scrape/unified-fallback`)

export const saveUnifiedFallback = (enabled: boolean) =>
  put<UnifiedFallbackConfig>(`${E}/scrape/unified-fallback`, { enabled })

/** 保存总开关 / 中文优先 / TMDB-miss 兜底开关 / 顺序 / 逐源开关 / 逐源限速（**不动密钥池**） */
export const saveMetaSources = (data: {
  enabled: boolean
  prefer_chinese: boolean
  tmdb_miss_fallback: boolean
  order: string[]
  toggles: Record<string, boolean>
  rates: Record<string, number>
}) => put<MetaSourcesConfig>(`${E}/scrape/meta-sources`, data)

/** 逐源逐把增：追加一把密钥（已存在返回 409） */
export const addMetaSourceKey = (sourceId: string, key: string) =>
  post<MetaSourcesConfig>(`${E}/scrape/meta-sources/${sourceId}/keys`, { key })

/** 逐源逐把删：按展示序号（从 1 开始）删一把 */
export const deleteMetaSourceKey = (sourceId: string, index: number) =>
  del<MetaSourcesConfig>(`${E}/scrape/meta-sources/${sourceId}/keys/${index}`)

/** 清除某个源全部密钥的冷却 */
export const resetMetaSourceCooldown = (sourceId: string) =>
  post<{ success: boolean; cleared: number; config: MetaSourcesConfig }>(
    `${E}/scrape/meta-sources/${sourceId}/keys/reset`)

/** 试采集的一行：命中了没有 / 为什么没问 / 失败原因 */
export interface MetaSourceOutcome {
  source: string
  label: string
  ok: boolean
  hit: boolean
  error: string
  elapsed_ms: number
  skipped: string
}

export interface MetaSourceProbe {
  fields: Record<string, unknown>
  external_ids: Record<string, string>
  outcomes: MetaSourceOutcome[]
  /** 哪个源赢了标题（空 = 都没命中） */
  primary: string
  prefer_chinese: boolean
}

/**
 * 试采集：用一个片名跑一遍，**不改任何数据**。
 * ``source`` 传了就只问那一个源（单独排查“是不是这个站在抽风”）。
 */
export const probeMetaSources = (data: {
  title: string
  year?: number | null
  kind: 'series' | 'movie'
  source?: string
}) => post<{
  success: boolean
  switch_on: boolean
  probe: MetaSourceProbe
  note: string
  sources: MetaSourceRow[]
}>(`${E}/scrape/meta-sources/test`, {
  title: data.title,
  year: data.year ?? null,
  kind: data.kind,
  source: data.source || '',
})

/** 逐把试某个源的密钥（测不通的直接进冷却） */
export const testMetaSourceKeys = (
  sourceId: string,
  data: { title: string; year?: number | null; kind: 'series' | 'movie' },
) => post<{
  results: Array<{
    index: number
    masked: string
    ok: boolean
    hit?: boolean
    message: string
    elapsed_ms?: number
  }>
  config: MetaSourcesConfig
}>(`${E}/scrape/meta-sources/${sourceId}/test`, {
  title: data.title,
  year: data.year ?? null,
  kind: data.kind,
})

/** 库级手动刮削：触发一次该库扫描，policy 只覆盖本轮快照（missing_only | all） */
export const rescrapeLibrary = (id: number, policy: 'missing_only' | 'all' = 'missing_only') =>
  post<{ success: boolean; started: boolean; already?: boolean; message: string }>(
    `${E}/scrape/libraries/${id}/rescrape`,
    { policy },
  )

export interface TmdbPreview {
  tmdb_id: string
  /** 输入是 IMDb ID 时有值（后端已换算成 tmdb_id） */
  imdb_id?: string | null
  title: string
  year: number | null
  poster: string | null
  current_name: string
  current_tmdb_id: string | number | null
  matches_current: boolean
}


export interface ItemSearchResult {
  id: number
  name: string
  year: number | null
  item_type: string
  tmdb_id: string | number | null
  /** 元数据锁定（P3，Emby 式）：True = 自动补全不再碰它 */
  metadata_locked: boolean
}

/** 按剧名关键字（+可选年份）搜索顶层条目，供「手动绑定 TMDB」选条目用，最多 20 条 */
export const searchItemsForBind = (q: string, year?: number | null) =>
  get<{ items: ItemSearchResult[] }>(`${E}/items/search`, { q, ...(year ? { year } : {}) })

/** TMDB 预览：绑定前先看清「这到底是哪部片」，只读不写库 */
export const previewTmdb = (id: number, tmdbId: string) =>
  get<TmdbPreview>(`${E}/scrape/items/${id}/tmdb-preview`, { tmdb_id: tmdbId })

export interface TmdbCandidate {
  tmdb_id: number
  title: string
  year: string | null
  overview: string
  poster_path: string | null
  media_type: string
}

/** Emby 式手动识别：按剧名搜 TMDB 返回候选列表（最多 10 条）；失败返回空列表 */
export const searchTmdbCandidates = (q: string, year?: number | null, kind: 'movie' | 'series' = 'series') =>
  get<{ candidates: TmdbCandidate[] }>(`${E}/scrape/tmdb/search`, { q, kind, ...(year ? { year } : {}) })

export interface TmdbBindResult {
  success: boolean
  unbound: boolean
  item: { id: number; name: string; item_type: string; tmdb_id?: string | number | null }
  notes: string[]
}

/** 手动绑定 TMDB ID（tmdbId 为空字符串 = 解绑）；绑定后自动补全缺失元数据
 *  mode：missing=仅补缺失（默认），all=全量刷新（先清空 TMDB 字段再重填）
 *  tmdbId 也支持 tt 开头的 IMDb ID（后端经 TMDB /find 换算成 TMDB ID） */
export const bindTmdb = (id: number, tmdbId: string, verify = true, mode: 'missing' | 'all' = 'missing') =>
  post<TmdbBindResult>(`${E}/scrape/items/${id}/bind-tmdb`, { tmdb_id: tmdbId, verify, mode })

export interface ItemLockResult {
  success: boolean
  item: { id: number; name: string; item_type: string; tmdb_id?: string | number | null; metadata_locked: boolean }
}

/** 锁定条目元数据（P3，Emby 式）：自动补全不再覆盖手动整理成果 */
export const lockItemMetadata = (id: number) =>
  post<ItemLockResult>(`${E}/scrape/items/${id}/lock`)

/** 解锁条目元数据：恢复自动补全资格 */
export const unlockItemMetadata = (id: number) =>
  post<ItemLockResult>(`${E}/scrape/items/${id}/unlock`)

/** 条目详情（含 metadata_locked） */
export const fetchItemLockStatus = (id: number) =>
  get<ItemLockResult>(`${E}/scrape/items/${id}`)


/** 阶段统计（v2.42.9）：次数 + 累计耗时 + 平均耗时。avg_ms 只对「计过时」的那几次求平均 */
export interface EnrichStageStat {
  label: string
  count: number
  ms: number
  avg_ms: number
  last_at: string | null
}

/** 完成速率（v2.42.9）：近 window_sec 内每分钟的完成条数 + 距上一次成功的秒数 */
export interface EnrichThroughput {
  done_per_min: number
  retry_per_min: number
  failed_per_min: number
  window_sec: number
  samples: number
  done_total: number
  retry_total: number
  failed_total: number
  last_done_at: string | null
  idle_sec: number | null
}

export interface EnrichProgress {
  enrich: { pending: number; enriching: number; done: number; failed: number; retrying: number }
  probe: Record<string, number>
  workers: number
  enabled: boolean
  /** v2.42.9：阶段用时分解。键：remote_list / local_list / nfo_read / nfo_hit / tmdb_req /
   *  image_dl / enrich_item；进程内计数，进程重启归零 */
  stages?: Record<string, EnrichStageStat>
  throughput?: EnrichThroughput
  /** 远程目录列举的累计计数（真实请求 / 复用 / 在飞 / 峰值） */
  mount_io?: Record<string, number | string | null>
}

/** 补全 worker 进度：enrich 待处理/进行中/成功/失败/重试中 */
export const fetchEnrichProgress = () =>
  get<{ success: boolean } & EnrichProgress>(`${E}/scrape/enrich-progress`)

// ==================== 任务总览（路线 2-A）====================

export type TaskCardStatus = 'running' | 'ok' | 'warning' | 'error' | 'idle' | 'disabled'

export interface TaskCardMetric {
  label: string
  value: string
}

export interface TaskCardSwitch {
  label: string
  enabled: boolean
  /** 开关切换时要一起带上的已有配置（各任务已有保存接口需要） */
  keep?: Record<string, unknown>
}

export interface TaskCard {
  key: string
  title: string
  status: TaskCardStatus
  status_text: string
  summary: string
  metrics: TaskCardMetric[]
  last_run: string | null
  last_run_label: string
  switch: TaskCardSwitch | null
  link: string | null
  /** 仅 scan 卡有：各库最近一轮扫描 */
  libraries?: Array<{
    id: number
    name: string
    scan_status: string | null
    last_status: string | null
    last_started_at: string | null
    last_finished_at: string | null
    last_error: string
  }>
}

export interface TasksOverview {
  success: boolean
  fetched_at: string
  tasks: TaskCard[]
}

/** 任务总览：一次拿回所有后台任务的状态、进度、开关 */
export const fetchTasksOverview = () => get<TasksOverview>('/tasks/overview')

/** 暂停后台探测（跨进程生效） */
export const pauseProbe = () => post<{ success: boolean; paused: boolean }>(`${E}/scrape/probe/pause`)

/** 恢复后台探测 */
export const resumeProbe = () => post<{ success: boolean; paused: boolean }>(`${E}/scrape/probe/resume`)

export interface AutoScanConfig {
  enabled: boolean
  /** 每天执行时间，"HH:MM"（服务器本地时间） */
  time: string
  /** 上次执行日期 "YYYY-MM-DD"，没跑过为空 */
  last_run: string
}

/** 定时扫描当前配置（开关 / 时间 / 上次执行） */
export const fetchAutoScan = () => get<{ success: boolean } & AutoScanConfig>(`${E}/scrape/auto-scan`)

/** 保存定时扫描配置：立即生效，无需重启；时间格式非法时后端 400 */
export interface ChaseNewConfig {
  enabled: boolean
  interval: number
  /** .strm 监听开关（默认开）：库路径是 /strm/... 时，strm_gen 产出的新 .strm 才会触发增量扫描 */
  strm_enabled: boolean
  /** v2.45.0：**排除**清单（逗号分隔的库 id）。空 = 全部启用库都监听 */
  excluded: string
  /** 已废弃：旧的包含清单，后端固定返回空串（保留字段以兼容老前端） */
  libraries: string
  last_check: string
  last_found: number
  /** drive_changes（Drive Changes API 增量发现）运行状态 */
  drive_changes: {
    running: boolean
    last_poll: string | null
    last_changes: number
    last_libs_triggered: number
  }
  /** 最近 10 轮追新运行历史（poll / drive-changes） */
  recent_runs: Array<{
    id: number
    started_at: string | null
    finished_at: string | null
    source: string
    libs_checked: number
    files_listed: number
    new_found: number
    scans_triggered: number
    status: string
    error: string
  }>
  /** 连续失败 >= 3 的源告警 */
  alerts: Array<{
    source_key: string
    consec_failures: number
    last_error: string
    last_ok_at: string | null
  }>
  /** chase_run.new_found 历史累计 */
  total_found: number
}

export const fetchChaseNew = () => get<{ success: boolean } & ChaseNewConfig>(`${E}/scrape/chase-new`)
export const saveChaseNew = (enabled: boolean, interval: number, excluded: string, strmEnabled: boolean) =>
  put<{ success: boolean } & ChaseNewConfig>(`${E}/scrape/chase-new`, { enabled, interval, excluded, strm_enabled: strmEnabled })

export const saveAutoScan = (enabled: boolean, time: string) =>
  put<{ success: boolean } & AutoScanConfig>(`${E}/scrape/auto-scan`, { enabled, time })

export interface BackupFile { name: string; size: number; created_at: string }
export interface BackupConfig {
  enabled: boolean
  /** 每天执行时间，"HH:MM"（服务器本地时间） */
  time: string
  /** 保留最近 N 天 */
  keep_days: number
  /** 上次执行日期 "YYYY-MM-DD"，没跑过为空 */
  last_run: string
  backups: BackupFile[]
}

/** 数据库备份配置 + 备份文件列表 */
export const fetchBackupConfig = () =>
  get<{ success: boolean } & BackupConfig>(`/system/backup`)

/** 保存数据库备份配置：立即生效，无需重启；非法时后端 400 */
export const saveBackupConfig = (enabled: boolean, time: string, keep_days: number) =>
  put<{ success: boolean } & BackupConfig>(`/system/backup`, { enabled, time, keep_days })

/** 立即手动备份一次 */
export const runBackupNow = () =>
  post<{ success: boolean; backup: { name: string; size: number } } & BackupConfig>(`/system/backup/run`, {})

/**
 * 播放可达性报告（v2.28.0）：出流方式 + 逐库判定 + 用户端地址一致性
 *
 * 「面板扫描正常、播放节点找不到媒体」这类问题在这里提前暴露：本机路径 / local 挂载的库
 * 在 EA 出流时拿不到内容（warn = 无法确认，bad = 有证据），不用等客户端点播放才 404。
 */
export const fetchReachability = () => get<EmbyReachabilityReport>(`${E}/reachability`)

/** 取消一个**还在排队**的扫描（正在跑的不能取消：停在中途会留下半个库的状态） */
export const cancelQueuedScan = (id: number) =>
  del<{ success: boolean; library_id: number }>(`${E}/scan-queue/${id}`)

/** 某个媒体库最近的扫描流水（新的在前）；keep = 后端每库保留条数 */
export const fetchLibraryScans = (id: number, limit = 20) =>
  get<{ library_id: number; keep: number; runs: EmbyScanRun[] }>(`${E}/libraries/${id}/scans?limit=${limit}`)

export const deleteLibrary = (id: number) => del<{ success: boolean }>(`${E}/libraries/${id}`)

export const fetchSessions = () => get<{ sessions: EmbySessionRow[] }>(`${E}/sessions`)

export const stopSession = (sessionKey: string) => del<{ success: boolean }>(`${E}/sessions/${sessionKey}`)

export const stopAllTranscodes = () => post<{ stopped: number }>(`${E}/transcodes/stop-all`)

// ==================== 管理员与权限（v2.26.0，/api/admin/admins） ====================
// 角色定义在后端（backend/admin_roles.py）：super = 全部；operator = 日常运营；
// viewer = 只读。角色判定发生在服务端鉴权依赖里，这里的函数只是调用入口。

export const fetchAdmins = () => get<AdminListResponse>('/admins')

/** 把已注册的用户提为管理员（只标记账号 + 定角色，不新建账号） */
export const grantAdmin = (data: { username?: string; email?: string; role: AdminRole }) =>
  post<{ success: boolean; admin: AdminRow }>('/admins', data)

export const updateAdmin = (userId: number, data: { role?: AdminRole; is_active?: boolean }) =>
  patch<{ success: boolean; changed: Record<string, unknown>; admin: AdminRow }>(
    `/admins/${userId}`, data,
  )

export const revokeAdmin = (userId: number) =>
  del<{ success: boolean }>(`/admins/${userId}`)

// ==================== 播放与客户端策略（v2.26.0，/api/admin/playback） ====================
// 转码开关 / 并发上限 / 码率上限 / 客户端准入：改完对 EM 与 EA 同时生效（同一个库）。

export const fetchPlaybackPolicy = () =>
  get<{ policy: PlaybackPolicy; keys: Record<string, string>; runtime: PlaybackRuntime }>(
    '/playback/policy',
  )

// ==================== 播放线路可观测（Phase 3，/api/admin/playback/lines） ====================
// 四条线路各一张卡片：能不能用 / 是不是在降级 / 有多少人多少流量 / 效果如何。
// **只读**：线路配置在 CDN / 本地缓存两个卡片里改，这里不改。
// 灰度发布（0% 流量影子评估）本期不做，所以也没有对应的开关。

export const fetchPlayLines = () => get<PlayLinesSnapshot>('/playback/lines')

export const updatePlaybackPolicy = (policy: Partial<PlaybackPolicy>) =>
  put<{ success: boolean; applied: Record<string, string>; policy: PlaybackPolicy }>(
    '/playback/policy', { policy },
  )

// ==================== CDN 域名预留（播放三层第 2/3 层，/api/admin/playback/cdn） ====================
// 只做域名预留：启用后播放 URL 走该 CDN 域名（回源到现有服务），热门分片由边缘缓存。

export const fetchCdnConfig = () =>
  get<{ success: boolean; cdn: CdnConfig; play_lines: string[] }>('/playback/cdn')

export const updateCdnConfig = (payload: { domain: string; enabled: boolean }) =>
  put<{ success: boolean; cdn: CdnConfig }>('/playback/cdn', payload)

// ==================== VPS 本地缓存（播放线路「本地缓存」，/api/admin/playback/local-cache） ====================

/** 本地缓存：配置 + 占用/命中率统计 + 条目列表（只读） */
export const fetchLocalCacheConfig = () =>
  get<{
    success: boolean
    local_cache: LocalCacheConfig
    stats: LocalCacheStats
    entries: LocalCacheEntryInfo[]
    play_lines: string[]
  }>('/playback/local-cache')

/** 写回本地缓存配置（开关 / 目录 / 配额 / 热门规则 / 限速），参数非法由后端 400 拒绝 */
export const updateLocalCacheConfig = (payload: {
  enabled: boolean
  dir: string
  max_gb: number
  hot_days: number
  hot_plays: number
  rate_mbps: number
}) => put<{ success: boolean; local_cache: LocalCacheConfig; stats: LocalCacheStats }>(
  '/playback/local-cache', payload,
)

/** 手动清理：ready = 清本机副本，failed = 清失败记录，all = 全部（不含下载中的） */
export const cleanLocalCache = (mode: 'ready' | 'failed' | 'all') =>
  post<{ success: boolean; cleaned: { mode: string; removed: number; freed_bytes: number }; stats: LocalCacheStats }>(
    '/playback/local-cache/clean', { mode },
  )

// ==================== 存储挂载（/api/admin/emby/mounts） ====================
// 挂载 = 媒体库的内容来源：local 是本机目录，115 / rclone 是远程来源。
// 类型只由路径前缀决定（115:/ / rclone: / 绝对路径），没有第四种类型。
// 远程挂载的条目在库里存 mount:// 路径，播放时由 EA 代理转发（凭据不下发）。

/** 挂载清单；`realm_id=0` = 全部服，不传 = 当前服（挂载是一个服一个的） */
export const fetchMounts = (realm_id?: number) =>
  get<{
    mounts: StorageMount[]
    mount_types: MountTypeMeta[]
    /** 当前出流的节点：只有它是 ea 时，「EA 不可达」才是阻断性告警 */
    playback_node: PlaybackNode
    ea_health: EaMountHealth
    realm_id: number | null
    realm_names: Record<string, string>
  }>(`${E}/mounts`, realm_id === undefined ? undefined : { realm_id })

/** 本机（EM）体检：逐条跑一遍与「测试连接」相同的探测并落库 */
export const probeMountsHealth = () =>
  post<{ mounts: StorageMount[]; total: number; ok_count: number; failed_count: number }>(
    `${E}/mounts/health`
  )

/** EA 体检：让 EM 向 EA 拉一次「以 EA 视角」的挂载体检并落库 */
export const refreshEaMountHealth = () =>
  post<{
    success: boolean
    health: { ok: boolean; error?: string; checked_at?: string | null; failed_count?: number }
  }>('/emby/servers/mounts/refresh')

export interface MountPayload {
  name?: string
  mount_type?: string
  path?: string
  /** 配置项；密钥类字段（password / token / cookie）留空表示不修改 */
  config?: Record<string, string>
  is_enabled?: boolean
  remark?: string
  /** 归属服（留空 = 当前服）；换服时引用它的媒体库会跟着走 */
  realm_id?: number
  /** 由哪台 EA 去读（每台 EA 一份 rclone.conf）；留空 = 本服已激活的 EA */
  server_id?: number
}

export const createMount = (data: MountPayload) =>
  post<{ success: boolean; mount: StorageMount }>(`${E}/mounts`, data)

export const updateMount = (id: number, data: MountPayload) =>
  put<{
    success: boolean
    mount: StorageMount
    rescan_required?: boolean
    /** 路径前缀变了 = 来源换了（115:/ ↔ rclone: ↔ 本机目录），旧条目会被重新解释 */
    mount_type_changed?: boolean
  }>(`${E}/mounts/${id}`, data)

export const deleteMount = (id: number) =>
  del<{ success: boolean; unbound_libraries: number }>(`${E}/mounts/${id}`)

export const testSavedMount = (id: number) =>
  post<{ success: boolean; result: { ok: boolean; message: string }; mount: StorageMount }>(
    `${E}/mounts/${id}/test`
  )

export const testMountConfig = (data: { mount_type: string; path?: string; config?: Record<string, string> }) =>
  post<{ success: boolean; result: { ok: boolean; message: string } }>(`${E}/mounts/test`, data)

export const browseMount = (id: number, rel = '/') =>
  get<{ rel: string; entries: MountDirEntry[]; total: number }>(`${E}/mounts/${id}/browse`, { rel })

/** 挂载路径选择器：只列子目录（媒体库表单「浏览」按钮用，走 /api/admin/mounts） */
export interface MountPickerDir {
  name: string
  path: string
}
export interface MountPickerCrumb {
  name: string
  path: string
}
export const browseMountDirs = (id: number, path?: string) =>
  get<{
    mount_id: number
    path: string
    parent: string | null
    crumbs: MountPickerCrumb[]
    dirs: MountPickerDir[]
    total: number
  }>(`/mounts/${id}/browse`, path ? { path } : undefined)

/**
 * 浏览**服务器本机**目录（本地文件来源选路径用）
 *
 * 与 `browseMountDirs` 返回**同一套结构**，所以新增路径弹窗只写一套列表 UI：
 * 本机目录与挂载目录的区别只在“加载函数”与“返回的路径要不要拼挂载前缀”。
 */
export const browseLocalDirs = (path = '/') =>
  get<{
    path: string
    parent: string | null
    crumbs: MountPickerCrumb[]
    dirs: MountPickerDir[]
    total: number
    /** 单层超过 1000 个子目录时会截断，弹窗提示一下 */
    truncated?: boolean
  }>('/mounts/local-dirs', { path })

/** rclone：列出用户粘贴的 rclone.conf 里已配置的 remote */
export const fetchMountRcloneRemotes = () =>
  get<{ remotes: string[]; total: number }>(`${E}/mounts/rclone/conf`)

/** .strm 直链目录配置（后端 GET/PUT /api/admin/emby/mounts/strm） */
export interface StrmConfig {
  enabled: boolean
  host_dir: string
  container_path: string
  defaults: { enabled: boolean; host_dir: string; container_path: string }
}
/** 当前 .strm 直链目录配置（总开关 / 宿主机目录 / 容器内挂载点） */
export const fetchStrmConfig = () =>
  get<{ success: boolean; strm: StrmConfig }>(`${E}/mounts/strm`)
/** 写回 .strm 直链目录配置；路径非法时后端 400，拦截器会弹出 detail */
export const saveStrmConfig = (data: { enabled: boolean; host_dir: string; container_path: string }) =>
  put<{ success: boolean; strm: StrmConfig }>(`${E}/mounts/strm`, data)

// ==================== .strm 生成器（后端 /api/admin/emby/strm-gen/*） ====================

/** .strm 生成器配置（总开关 / Drive 源目录 / 指定 Drive / 过期清理） */
export interface StrmGenConfig {
  enabled: boolean
  source_dir: string
  /** 指定 Drive ID，空=自动选择 */
  drive_id: string
  /** 清理 Drive 上已不存在的 .strm 文件及状态行（不可逆） */
  prune: boolean
  /** 增量间隔分钟数，0=关闭 */
  interval_minutes: number
  /** 数据源类型：drive=Google Drive，local=本地目录 */
  source_type: string
  /** 本地源根目录 */
  local_root: string
  /** 上次执行时间（ISO 字符串），可能为空 */
  last_run: string
}

/** 生成进度（phase: idle | listing | generating | verifying | done | error） */
export interface StrmGenProgress {
  running: boolean
  started_at: string | null
  finished_at: string | null
  phase: string
  total: number
  done: number
  generated: number
  skipped: number
  failed: number
  /** 最近的错误信息（最多 20 条） */
  errors: string[]
  /** 当前处理的文件 */
  current: string
}

/** Drive 列表项 */
export interface StrmGenDrive {
  drive_id: string
  remotes: string[]
  is_personal: boolean
}

/** 缺集报告里的一个剧 */
export interface StrmGenIncomplete {
  series: string
  drive_count: number
  strm_count: number
  missing_count: number
  missing_sample: string[]
}

/** 读 .strm 生成器配置 */
export const fetchStrmGenConfig = () =>
  get<{ success: boolean; config: StrmGenConfig }>(`${E}/strm-gen/config`)

/** 写 .strm 生成器配置（保存即热生效）；格式非法时后端 400 */
export const saveStrmGenConfig = (data: {
  enabled: boolean
  source_dir: string
  drive_id: string
  prune: boolean
  interval_minutes: number
  source_type: string
  local_root: string
  /** 多盘并行列举并发数（不传则后端回落默认 3） */
  drive_concurrency?: number
  /** Drive API 全局速率上限 QPS（不传则后端回落默认 8.0） */
  api_qps?: number
}) => put<{ success: boolean; config: StrmGenConfig }>(`${E}/strm-gen/config`, data)

/** 手动触发一次生成（full=false 增量，true 全量）；已有任务在跑时后端返回 success=false */
export const triggerStrmGen = (full = false) =>
  post<{ success: boolean; message?: string; error?: string; progress?: StrmGenProgress }>(
    `${E}/strm-gen/trigger`, { full })

/** 查生成进度（附带当前配置，前端轮询用） */
export const fetchStrmGenProgress = () =>
  get<{ success: boolean; progress: StrmGenProgress; config: StrmGenConfig }>(`${E}/strm-gen/progress`)

/** 列出发现的 Drive（供 drive_id 配置选择） */
export const fetchStrmGenDrives = () =>
  get<{ success: boolean; drives?: StrmGenDrive[]; error?: string }>(`${E}/strm-gen/drives`)

/** 缺集报告：上次生成时 Drive 有但 .strm 缺失的剧集 */
export const fetchStrmGenMissing = (limit = 100) =>
  get<{
    ok: boolean
    has_data?: boolean
    verify_mode?: string
    total_series?: number
    complete_series?: number
    incomplete_series?: number
    incomplete?: StrmGenIncomplete[]
    error?: string
  }>(`${E}/strm-gen/missing`, { limit })

// ==================== 每台 EA 一份 rclone.conf ====================

/** 读这台 EA 的 rclone.conf：**只返 remote 名，不返明文**（含 token） */
export const fetchServerRcloneConf = (serverId: number) =>
  get<{
    path: string
    server_id: number
    server_name: string
    configured: boolean
    remotes: string[]
    total: number
  }>(`${S}/${serverId}/rclone-conf`)

/** 保存这台 EA 的 rclone.conf（整份替换）。仅超级管理员可用 */
export const saveServerRcloneConf = (serverId: number, conf: string) =>
  post<{ success: boolean; path: string; remotes: string[]; total: number }>(
    `${S}/${serverId}/rclone-conf`, { conf })

// ==================== 115 账号与直挂（/api/admin/emby/115/*） ====================

export const fetchPan115Accounts = () =>
  get<{ accounts: Pan115Account[]; env_cookie_configured: boolean
        ua_presets: { value: string; label: string }[] }>(`${E}/115/accounts`)

export const createPan115Account = (data: {
  name: string
  cookie: string
  is_default?: boolean
  is_enabled?: boolean
  remark?: string
  /** 该配置档发 115 请求用的 UA（留空 = 服务器级 MOUNT_UA） */
  ua?: string
}) => post<{ success: boolean; account: Pan115Account }>(`${E}/115/accounts`, data)

export const updatePan115Account = (
  id: number,
  data: { name?: string; cookie?: string; is_default?: boolean; is_enabled?: boolean
          remark?: string; ua?: string }
) => put<{ success: boolean; account: Pan115Account }>(`${E}/115/accounts/${id}`, data)

export const deletePan115Account = (id: number) => del<{ success: boolean }>(`${E}/115/accounts/${id}`)

export const verifyPan115Account = (id: number) =>
  post<{ success: boolean; result: { ok: boolean; message?: string }; account: Pan115Account }>(
    `${E}/115/accounts/${id}/verify`
  )

export const verifyPan115Cookie = (cookie: string) =>
  post<{ success: boolean; result: { ok: boolean; message?: string } }>(`${E}/115/verify`, { cookie })

/** 浏览 115 目录（账号可用性实测 / 直挂目录结构）：账号配置档优先，表单 Cookie 次之 */
export const browsePan115 = (params: { cid?: string; account_id?: number; cookie?: string }) =>
  get<{ cid: string; cookie_source: string; entries: Pan115DirEntry[]; total: number }>(
    `${E}/115/browse`, params
  )


// ==================== rclone.conf（用户自己粘贴，面板只负责跑 rclone） ====================
// 后端只回 remote 名，不回 rclone.conf 原文——里面全是 token / secret。

export const fetchRcloneConf = () =>
  get<{ path: string; configured: boolean; remotes: string[]; total: number }>(
    `${E}/mounts/rclone/conf`)

export const saveRcloneConf = (conf: string) =>
  post<{ success: boolean; path: string; remotes: string[]; total: number }>(
    `${E}/mounts/rclone/conf`, { conf })

/** Google Drive SA 状态（Fix 6） */
export const fetchGDriveSaStatus = () =>
  get<{
    sa_count: number
    sa_files: string[]
    sa_truncated: boolean
    current_sa: string | null
    rotation_index: number
    disk: Record<string, unknown>
    cache_size_bytes: number
    cache_size_human: string
  }>('/gdrive/sa-status')

/** 手动触发 SA 轮换（Fix 6） */
export const rotateGDriveSa = () =>
  post<{ success: boolean; sa_file?: string; mode?: string; needs_remount?: boolean; remount_hint?: string }>('/gdrive/sa-rotate')


// ==================== 系统告警（后端 /api/admin/alerts/*） ====================

/** 告警严重度 */
export type AlertSeverity = 'info' | 'warning' | 'critical'
/** 告警渠道 */
export type AlertChannel = 'in_app' | 'email' | 'telegram' | 'webhook'
/** 告警记录状态 */
export type AlertStatus = 'open' | 'acknowledged' | 'resolved'

/** 告警规则 */
export interface AlertRule {
  id: number
  name: string
  event_type: string
  threshold: number
  severity: AlertSeverity
  channels: string[]
  recipients: Record<string, unknown>
  cooldown_sec: number
  is_active: boolean
  created_at: string | null
}

/** 告警规则更新入参 */
export interface AlertRuleUpdate {
  name: string
  threshold: number
  severity: AlertSeverity
  channels: string[]
  recipients: Record<string, unknown>
  cooldown_sec: number
  is_active: boolean
}

/** 告警全局设置 */
export interface AlertSettings {
  enabled: boolean
  enabled_source: 'env' | 'db'
  enabled_value: string
  check_interval_sec: number
  webhook_url: string
  webhook_secret_set: boolean
}

/** 告警设置部分更新入参（全部可选，后端只应用非 None 字段） */
export interface AlertSettingsUpdate {
  enabled?: boolean
  check_interval_sec?: number
  webhook_url?: string
  webhook_secret?: string
}

/** 告警记录 */
export interface AlertRecord {
  id: number
  rule_id: number
  event_type: string | null
  severity: string
  title: string
  message: string
  status: AlertStatus
  notified_ok: boolean
  created_at: string | null
  acknowledged_at: string | null
  resolved_at: string | null
}

/** 告警规则列表（含总开关 / 可用事件 / 可用渠道） */
export const fetchAlertRules = () =>
  get<{ enabled: boolean; events: string[]; allowed_channels: string[]; rules: AlertRule[] }>('/alerts/rules')

/** 更新一条告警规则 */
export const updateAlertRule = (ruleId: number, data: AlertRuleUpdate) =>
  put<AlertRule>(`/alerts/rules/${ruleId}`, data)

/** 告警全局设置（总开关 / 检查间隔 / webhook） */
export const fetchAlertSettings = () =>
  get<AlertSettings>('/alerts/settings')

/** 保存告警设置（部分更新，只传改了的字段） */
export const saveAlertSettings = (data: AlertSettingsUpdate) =>
  put<{ enabled: boolean; check_interval_sec: number; changed: Record<string, unknown> }>('/alerts/settings', data)

/** 告警记录（status: open / acknowledged / resolved / all） */
export const fetchAlerts = (status: AlertStatus | 'all' = 'open', limit = 50) =>
  get<{ alerts: AlertRecord[] }>('/alerts', { status, limit })

/** 确认一条告警（open → acknowledged） */
export const ackAlert = (alertId: number) =>
  post<{ id: number; status: AlertStatus; acknowledged_at: string | null }>(`/alerts/${alertId}/ack`)

/** 发送测试告警（只发给当前管理员） */
export const sendTestAlert = (channel: AlertChannel) =>
  post<{ ok: boolean; channel: string }>('/alerts/test', { channel })
