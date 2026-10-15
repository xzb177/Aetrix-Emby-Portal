import axios from 'axios'
import { toUserMessage } from '@/utils/toUserMessage'

// 使用空 baseURL，让请求自动适配当前域名（前后端同源部署）
const api = axios.create({
  baseURL: '',
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
})

// ==================== Token 存取 ====================

export const tokenStore = {
  get access() {
    return localStorage.getItem('access_token')
  },
  get refresh() {
    return localStorage.getItem('refresh_token')
  },
  set(access: string, refresh?: string) {
    localStorage.setItem('access_token', access)
    if (refresh) localStorage.setItem('refresh_token', refresh)
  },
  clear() {
    localStorage.removeItem('access_token')
    localStorage.removeItem('refresh_token')
    localStorage.removeItem('user')
  },
}

// ==================== 请求拦截器 ====================

api.interceptors.request.use(
  (config) => {
    const token = tokenStore.access
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  (error) => Promise.reject(error)
)

// ==================== 401 自动刷新（refresh token 轮换） ====================

let isRefreshing = false
let refreshSubscribers: Array<(token: string) => void> = []
let refreshRejectSubscribers: Array<(error: unknown) => void> = []

function subscribeTokenRefresh(cb: (token: string) => void) {
  refreshSubscribers.push(cb)
}

function subscribeTokenRefreshFailure(cb: (error: unknown) => void) {
  refreshRejectSubscribers.push(cb)
}

function onRefreshed(newToken: string) {
  refreshSubscribers.forEach((cb) => cb(newToken))
  refreshSubscribers = []
  refreshRejectSubscribers = []
}

function onRefreshFailed(error: unknown) {
  refreshRejectSubscribers.forEach((cb) => cb(error))
  refreshSubscribers = []
  refreshRejectSubscribers = []
}

function forceLogout() {
  tokenStore.clear()
  // 用户端只有 /login 一个登录页（没有 /m/* 这套移动端路由），
  // 所以已过期会话必须送到 /login，否则会落到 catch-all 的 404 页
  const currentPath = window.location.pathname
  const isLoginPage = currentPath === '/login'
  if (!isLoginPage) {
    window.location.href = '/login'
  }
}

api.interceptors.response.use(
  (response) => {
    const res = response.data
    // 后端统一返回 JSON；兼容 { code: 200, data } 包装与裸数据
    if (res && typeof res === 'object' && 'code' in res && res.code === 200 && 'data' in res) {
      return res.data
    }
    return res
  },
  async (error) => {
    const originalRequest = error?.config
    // Batch G：拦截器即翻译点 —— 把人话文案挂到 error.userMessage 上，供调用方
    // `toast.error(err.userMessage ?? toUserMessage(err))` 使用；不新增任何全局提示，
    // 不改变原有 reject 行为（保持调用方原有静默/日志/弹窗逻辑）。
    if (error && !(error as { userMessage?: string }).userMessage) {
      ;(error as { userMessage?: string }).userMessage = toUserMessage(error)
    }
    if (!originalRequest) {
      return Promise.reject(error)
    }

    if (error.response?.status === 401 && !originalRequest._retry) {
      const isAuthRequest =
        originalRequest?.url?.includes('/api/user/auth/login') ||
        originalRequest?.url?.includes('/api/user/auth/register') ||
        originalRequest?.url?.includes('/api/user/auth/refresh')

      if (isAuthRequest) {
        return Promise.reject(error)
      }

      const refreshToken = tokenStore.refresh
      if (!refreshToken) {
        forceLogout()
        return Promise.reject(error)
      }

      if (isRefreshing) {
        return new Promise((resolve, reject) => {
          subscribeTokenRefresh((newToken: string) => {
            if (originalRequest.headers) {
              originalRequest.headers.Authorization = `Bearer ${newToken}`
            }
            // 排队重放也只许重试一次：否则新票据若仍被拒会无限循环刷新
            originalRequest._retry = true
            resolve(api(originalRequest))
          })
          subscribeTokenRefreshFailure((refreshError) => {
            reject(refreshError)
          })
        })
      }

      isRefreshing = true
      originalRequest._retry = true

      try {
        const res = await axios.post('/api/user/auth/refresh', { refresh_token: refreshToken })
        const data = res.data
        if (!data?.access_token) {
          throw new Error('refresh token response missing access_token')
        }
        tokenStore.set(data.access_token, data.refresh_token)
        onRefreshed(data.access_token)
        if (originalRequest.headers) {
          originalRequest.headers.Authorization = `Bearer ${data.access_token}`
        }
        return api(originalRequest)
      } catch (refreshError) {
        onRefreshFailed(refreshError)
        forceLogout()
        // Batch G：刷新失败的错误同样带上人话文案（此时登录页会接管展示）
        if (refreshError && !(refreshError as { userMessage?: string }).userMessage) {
          ;(refreshError as { userMessage?: string }).userMessage = toUserMessage(refreshError)
        }
        return Promise.reject(refreshError)
      } finally {
        isRefreshing = false
      }
    }

    // 公益服 TG 门禁：后端 403 + TG_NOT_BOUND → 全局事件，App.vue 统一引导去绑定
    if (error.response?.status === 403 && !originalRequest._tgNotified) {
      const detail = error.response?.data?.detail
      const code = detail && typeof detail === 'object' ? (detail as { code?: string }).code : null
      if (code === 'TG_NOT_BOUND') {
        originalRequest._tgNotified = true
        window.dispatchEvent(new CustomEvent('tg-not-bound', {
          detail: { message: (detail as { message?: string })?.message || '公益服功能需要绑定 Telegram，防小号，1 分钟搞定' },
        }))
      }
    }

    return Promise.reject(error)
  }
)

export default api

// ==================== 认证 API（backend/api/emby_portal.py） ====================

/** 佩戴中的成就徽章（后端存账号：换设备也在、换账号不串）；没戴为 null */
export interface WornBadge {
  key: string
  name: string
  icon: string
}

export interface AuthUser {
  id: number
  username: string
  email?: string | null
  emby_username?: string | null
  is_vip: boolean
  is_active: boolean
  /** 管理员：用户端据此显示「管理后台」入口（两端同源同 JWT，无需二次登录） */
  is_staff?: boolean
  /** 付费墙是否开启：开启且非会员时播放会被拦截；公益服恒为 false */
  subscription_required?: boolean
  /** 接入方式：free = 本服是公益服（免费开放，不需要会员） */
  realm_access_mode?: 'paid' | 'free'
  is_free_realm?: boolean
  /** 公益服规则文案（付费服为空串） */
  realm_access_note?: string
  /** 佩戴中的成就徽章；没戴为 null */
  worn_badge?: WornBadge | null
  created_at?: string | null
}

export interface AuthResponse {
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
  user: AuthUser
}

/** 人机验证挂件信息（能力：人机验证；未配置时 enabled=false，前端不渲染挂件） */
export interface CaptchaInfo {
  enabled: boolean
  provider: 'none' | 'turnstile' | 'recaptcha' | 'hcaptcha'
  label: string
  site_key: string
  script_url: string
  /** 哪些动作受保护（管理员可分别开关登录 / 注册） */
  actions: { login: boolean; register: boolean }
}

export const authApi = {
  login: (data: { username: string; password: string; captcha_token?: string }) =>
    api.post<never, AuthResponse>('/api/user/auth/login', data),

  register: (data: { username: string; password: string; email?: string; invitation_code?: string; captcha_token?: string }) =>
    api.post<never, AuthResponse>('/api/user/auth/register', data),

  /** bot 一键免密登录：用一次性 token 换登录态 */
  tgLogin: (data: { token: string }) =>
    api.post<never, AuthResponse>('/api/user/auth/tg-login', data),

  getCurrentUser: () => api.get<never, AuthUser>('/api/user/auth/me'),

  logout: () => api.post('/api/user/auth/logout'),

  changePassword: (data: { old_password: string; new_password: string }) =>
    api.post('/api/user/auth/change-password', data),

  /** 挂件配置：站点密钥是公开信息，私钥由后端保管（永不下发） */
  captcha: () => api.get<never, CaptchaInfo>('/api/user/auth/captcha'),
}

// ==================== 站点品牌（能力：站点与品牌，backend/api/site.py） ====================

export interface Branding {
  site_name: string
  logo_url: string
  theme_color: string
  seo_title: string
  seo_description: string
  seo_keywords: string
}

export const brandingApi = {
  /** 公开端点：站名 / Logo / 主题色 / SEO（不含任何凭据） */
  get: () => api.get<never, Branding>('/api/site/branding'),
}

// ==================== AI 助手（能力：AI 模型设置，backend/api/assistant.py） ====================

export interface AiStatus {
  /** 管理员是否已配置并启用（未配置时前端不显示入口） */
  enabled: boolean
  reason: string
  /** 每用户每日上限；0 表示不限 */
  daily_limit: number
  remaining: number | null
}

export interface AiAnswer extends AiStatus {
  answer: string
  // 不回传模型名：用户不需要看到具体模型（服务端也不下发）
}

export const aiApi = {
  status: () => api.get<never, AiStatus>('/api/user/ai/status'),
  ask: (question: string, history?: { role: 'user' | 'assistant'; content: string }[]) =>
    api.post<never, AiAnswer>('/api/user/ai/ask', { question, history }),
}

// ==================== 自建 Emby 门户 API（backend/emby_server/portal.py） ====================

/** 一个「服」的连接信息与会员状态（多服运营：同一个面板下可以有几个独立的服） */
export interface AccountRealmCard {
  id: number
  name: string
  slug: string
  base_url: string
  mode: string
  external: boolean
  subscribed: boolean
  /** 公益服：不需要订阅也能看（没订阅也会下发这张卡） */
  access_mode?: 'paid' | 'free'
  is_free?: boolean
  access_note?: string
  end_date: string | null
  plan_name: string
  is_default: boolean
  /** 该服的查看权限（没权限时 base_url 置空） */
  view_granted?: boolean
}

/** Emby 账号/线路查看权限（没权限时地址类字段置空不下发） */
export interface ViewPermission {
  granted: boolean
  /** 当前服是否为公益服（公益服用积分解锁，付费服用订阅） */
  realm_free: boolean
  /** 解锁需要的积分 */
  unlock_points: number
  /** 解锁有效期天数（0=永久） */
  unlock_days: number
  /** 用户当前积分余额 */
  points_balance: number
  /** 已解锁的到期时间（ISO，未解锁/永久为 null） */
  expires_at: string | null
}

export interface AccountCard {
  server_id: string
  server_name: string
  base_url: string
  emby_username: string
  emby_password: null
  has_password: boolean
  import_schemes: Record<string, string>
  /** 查看权限（没权限时 base_url/import_schemes/emby_username 置空） */
  view_permission?: ViewPermission
  /** 当前卡片对应的服（顶层字段是它的口径，兼容老前端） */
  realm_id?: number | null
  realm_name?: string
  /** 接入方式：free = 公益服（免费开放，不需要会员） */
  access_mode?: 'paid' | 'free'
  is_free?: boolean
  /** 公益服规则文案 */
  access_note?: string
  /** 该服是否允许下载（公益服默认不允许） */
  allow_download?: boolean
  /** 我在这几个服各自的地址与会员（多服时客户端该连哪台一目了然） */
  realms?: AccountRealmCard[]
}

export interface WatchStats {
  total_plays: number
  watched_items: number
  total_seconds: number
  recent: Array<{ item: string; type: string; device?: string; client?: string; at?: string | null }>
}

export interface PortalMediaItem {
  id: string
  name: string
  type: string
  year?: number | null
  rating?: number | null
  position_ticks?: number
  duration_ticks?: number
  progress?: number
  poster_url?: string | null
}

/**
 * 门户「继续观看」条目（/api/user/emby/resume）：电影一行；单集按剧聚合，
 * id / name 是剧的，episode_* 是最近在看的那一集。client / device 取自该条目
 * 最近一次播放会话（没有会话时为 null）。
 */
export interface PortalResumeItem extends PortalMediaItem {
  type: 'movie' | 'series' | string
  tmdb_id?: string | null
  last_played_at?: string | null
  client?: string | null
  device?: string | null
  episode_id?: string
  episode_name?: string | null
  season_number?: number | null
  episode_number?: number | null
  /** 剧名（单集聚合时后端明确给出，前端优先用它拼标题） */
  series_name?: string | null
}

/** 观看历史条目 */
export interface WatchHistoryItem {
  id: string
  name: string
  type: string
  year?: string | number | null
  poster_url?: string | null
  duration_ticks?: number | null
  position_ticks?: number | null
  played: boolean
  is_favorite: boolean
  play_count: number
  device?: string | null
  client?: string | null
  play_method?: string | null
  watched_at?: string | null
  /**
   * 单集观看记录按剧聚合后才有：点行进剧集详情并定位到该集。
   * 顶层只放 movie/series，单集只活在详情页内。
   */
  episode_id?: string | null
  episode_name?: string | null
  season_number?: number | null
  episode_number?: number | null
}

export interface WatchHistory {
  total: number
  unique_total: number
  items: WatchHistoryItem[]
}

/** 我的播放会话 */
export interface MyPlaybackSession {
  session_key: string
  item_id: string
  item: string
  item_type: string
  device?: string | null
  client?: string | null
  remote_addr?: string | null
  play_method?: string | null
  is_paused: boolean
  position_ticks?: number | null
  duration_ticks?: number | null
  progress: number
  started_at?: string | null
  updated_at?: string | null
}

export const embyApi = {
  // 账号卡（服务器地址 / Emby 用户名 / 播放器一键导入 scheme）
  getAccountCard: () => api.get<never, AccountCard>('/api/user/emby/server'),

  // 公益服：花积分解锁 Emby 账号/线路查看权限（幂等）
  unlockView: (realmId?: number) =>
    api.post<never, { success: boolean; already: boolean; expires_at: string | null; points_spent?: number; balance?: number }>(
      '/api/user/emby/unlock-view', { realm_id: realmId ?? null }),

  // 设置/修改 Emby 播放密码
  setPassword: (password: string) => api.post('/api/user/emby/password', { password }),

  // 续看列表（首页「继续观看」：单集按剧聚合，带上次播放的客户端 / 设备）
  getResume: (limit = 12) => api.get<never, { items: PortalResumeItem[] }>('/api/user/emby/resume', { params: { limit } }),

  // 收藏列表
  getFavorites: () => api.get<never, { items: PortalMediaItem[] }>('/api/user/emby/favorites'),

  // 收藏/取消收藏
  toggleFavorite: (itemId: string) => api.post<never, { is_favorite: boolean }>(`/api/user/emby/favorites/${itemId}`),

  // 观看统计
  getStats: () => api.get<never, WatchStats>('/api/user/emby/stats'),

  // 观看历史（按条目去重，含设备 / 客户端）
  getHistory: (params?: { limit?: number; offset?: number; item_type?: string }) =>
    api.get<never, WatchHistory>('/api/user/emby/history', { params }),

  // 我的正在播放会话
  getSessions: () => api.get<never, { sessions: MyPlaybackSession[] }>('/api/user/emby/sessions'),

  // 结束某个播放会话
  stopSession: (sessionKey: string) => api.delete(`/api/user/emby/sessions/${sessionKey}`),
}

// ==================== 站内消息 API（backend/api/user.py + admin 联动） ====================

export interface StationMessage {
  id: number
  title: string
  content: string
  message_type: string
  related_id?: number | null
  is_read: boolean
  created_at: string
  from_user?: string | null
}

export const messageApi = {
  getMessages: (params?: { unread_only?: boolean; limit?: number }) =>
    api.get<never, StationMessage[]>('/api/user/messages', { params }),

  getUnreadCount: () => api.get<never, { unread_count: number }>('/api/user/messages/unread-count'),

  markAsRead: (messageId: number) => api.post(`/api/user/messages/${messageId}/read`),

  markAllRead: () => api.post('/api/user/messages/read-all'),
}

// ==================== 公告 API ====================

export interface Announcement {
  id: number
  title: string
  content: string
  type: string
  is_pinned: boolean
  created_at: string
}

export const announcementApi = {
  getAnnouncements: () => api.get<never, Announcement[]>('/api/user/announcements'),
}

// ==================== 工单 API ====================

export interface Ticket {
  id: number
  title: string
  category: string
  status: string
  priority: string
  created_at: string
  updated_at: string
}

export interface TicketMessage {
  id: number
  message: string
  is_admin: boolean
  created_at: string
  admin_name?: string | null
}

export const ticketApi = {
  getMyTickets: (params?: { status_filter?: string }) =>
    api.get<never, Ticket[]>('/api/user/tickets', { params }),

  create: (data: { title: string; category?: string; message: string }) =>
    api.post<never, { success: boolean; ticket_id: number; message: string }>('/api/user/tickets', data),

  getMessages: (ticketId: number) => api.get<never, TicketMessage[]>(`/api/user/tickets/${ticketId}/messages`),

  reply: (ticketId: number, message: string) =>
    api.post(`/api/user/tickets/${ticketId}/messages`, { message }),

  close: (ticketId: number) => api.post(`/api/user/tickets/${ticketId}/close`),
}

// ==================== 求片 API ====================

export interface MediaSeekRequest {
  id: number
  movie_name: string
  year?: string | null
  type?: string | null
  note?: string | null
  status: string
  admin_note?: string | null
  /** 剧集按整季申请："1,2" 或 "all"（全季）；电影为空 */
  season?: string | null
  /** 季的展示文案（后端统一生成：全季 / 第 1、2 季） */
  season_label?: string
  /** TMDB 条目 id（从候选列表带过来；手填片名为空） */
  tmdb_id?: string | null
  /** 已入库的条目 guid：completed 时可直接跳详情观看 */
  emby_item_id?: string | null
  /** 这部片求给哪个服（多服运营时由用户选择 / 单服自动带出） */
  realm_id?: number | null
  realm_name?: string
  created_at: string
  /** v2 附议数 */
  vote_count?: number
  /** v2 当前用户是否已附议 */
  voted?: boolean
}

/** TMDB 搜索候选（求片表单用）：in_library=true 的已在库，直接看不用求 */
export interface MediaSeekCandidate {
  tmdb_id: string
  /** movie / series（剧集要选季） */
  kind: 'movie' | 'series'
  name: string
  year: string
  overview?: string
  poster_url?: string | null
  rating?: number | null
  in_library: boolean
  /** 在库时对应条目 guid（可直接跳详情） */
  library_item_id?: string | null
}

/** 剧集的一季（TMDB） */
export interface TvSeason {
  season_number: number
  name: string
  episode_count: number
  air_date?: string
}

export interface MediaSeekQuota {
  used_today: number
  daily_limit: number
  remaining: number
}

/** 库存检查命中项（在自建媒体库中已存在） */
export interface MediaLookupItem {
  id: string
  name: string
  type: string
  year?: string | number | null
  poster_url?: string | null
}

export const mediaSeekApi = {
  getMyRequests: (params?: { status_filter?: string }) =>
    api.get<never, { requests: MediaSeekRequest[]; quota: MediaSeekQuota }>('/api/user/media-seek', { params }),

  /** 求片前库存检查：片名是否已在库中（TMDB 未配置时的回退路径） */
  lookup: (name: string) =>
    api.get<never, { in_library: boolean; items: MediaLookupItem[] }>('/api/user/media-seek/lookup', {
      params: { name },
    }),

  /** TMDB 候选搜索：列条目并标出哪些已在库（configured=false 时前端回退本地查库） */
  search: (query: string, type?: string) =>
    api.get<never, { configured: boolean; degraded: boolean; results: MediaSeekCandidate[] }>('/api/user/media-seek/search', {
      params: { query, ...(type ? { type } : {}) },
    }),

  /** 剧集的季列表（按整季申请时的季选择器） */
  seasons: (tmdbId: string) =>
    api.get<never, { seasons: TvSeason[] }>('/api/user/media-seek/seasons', {
      params: { tmdb_id: tmdbId },
    }),

  create: (data: {
    movie_name: string
    year?: string
    type?: string
    note?: string
    /** 求给哪个服；多个服都买了会员时需要用户选一个 */
    realm_id?: number
    /** 从 TMDB 候选带过来的 id（管理端靠它匹配「真的入库了吗」） */
    tmdb_id?: string
    /** 剧集按整季申请："1,2" 或 "all"（全季） */
    season?: string
  }) =>
    api.post<never, { success: boolean; request_id: number; message: string }>('/api/user/media-seek', data),

  /** 撤回尚未处理的求片 */
  withdraw: (requestId: number) => api.delete(`/api/user/media-seek/${requestId}`),

  /** v2 附议 / 取消附议（每人每条只能附议一次，不能给自己附议） */
  vote: (requestId: number) =>
    api.post<never, { success: boolean; voted: boolean; vote_count: number }>(
      `/api/user/media-seek/${requestId}/vote`,
    ),

  /** v2 热门求片：所有人 pending 的求片（匿名），按附议数排序 */
  hot: () =>
    api.get<never, { requests: HotMediaSeekRequest[] }>('/api/user/media-seek/hot'),
}

/** v2 热门求片行（匿名展示） */
export interface HotMediaSeekRequest {
  id: number
  movie_name: string
  year?: string | null
  type?: string | null
  season_label?: string
  vote_count: number
  voted: boolean
  mine: boolean
  created_at: string
}

// ==================== 订阅 API（backend/api/user.py，管理员在后台授予） ====================

export interface MySubscription {
  id: number
  plan_name: string
  /** 会员属于哪个服（一个服一个，多服会员分开看） */
  realm_id?: number | null
  realm_name?: string
  start_date: string
  end_date: string
  status: string
  auto_renew: boolean
  days_left: number
}

/**
 * 临期阈值（天）：与后台「到期前提醒」同一口径（backend/reminders.py 默认 7/3/1）。
 * 用户端不自己写 7：改了后端档位后，界面上的提醒也该跟着换。
 */
export const EXPIRY_WARN_DAYS = 7

/** 是否即将到期（没有订阅或已过期都不算） */
export function isExpiringSoon(sub: MySubscription | null | undefined): boolean {
  return !!sub && sub.status === 'active' && sub.days_left > 0 && sub.days_left <= EXPIRY_WARN_DAYS
}

export const subscriptionApi = {
  // 我的订阅列表（含剩余天数）
  getMine: () => api.get<never, MySubscription[]>('/api/user/subscriptions'),
  // 终止订阅（立即失效）
  cancel: (id: number) => api.post<never, { success: boolean; message: string }>(`/api/user/subscriptions/${id}/cancel`),
}
