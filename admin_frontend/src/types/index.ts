/** v2.2.0 管理后台类型定义（对齐后端 /api/admin/* 契约） */

export interface AdminInfo {
  id: number
  username: string
  is_staff: boolean
  created_at?: string | null
  /** 角色（v2.26.0，见 backend/admin_roles.py）：super / operator / viewer */
  admin_role?: AdminRole
  role_label?: string
  /** 能不能做写操作（只读角色为 false）：界面据此置灰，服务端仍会自己判一次 */
  can_write?: boolean
  is_super?: boolean
}

/** 管理员角色：super = 全部；operator = 日常运营；viewer = 只读审计 */
export type AdminRole = 'super' | 'operator' | 'viewer'

export interface AdminRoleMeta {
  value: AdminRole
  label: string
  hint: string
}

export interface AdminRow {
  id: number
  username: string
  email: string
  is_active: boolean
  admin_role: AdminRole
  role_label: string
  last_login_at: string | null
  created_at: string | null
}

export interface AdminListResponse {
  admins: AdminRow[]
  roles: AdminRoleMeta[]
  me: { id: number; admin_role: AdminRole }
  super_count: number
  limit: number
}

/** 播放与客户端策略（后端 playback_policy.py，EM / EA 共用同一个库） */
export interface PlaybackPolicy {
  /** 客户端 UA 黑名单（子串，逗号分隔） */
  /** 客户端 UA 白名单（填了就只放列表内的客户端） */
  /** Drive 302 直链总开关（#957 的回滚闸；关掉后 Drive 文件播放返回 410） */
  drive_direct_play_enabled: boolean
}

/** 策略的运行态：只反映**本进程**（当前出流节点） */export interface PlaybackRuntime {
  playback_node: string
}

/** 一条线路的降级原因（本进程累计，按次数降序） */
export interface LineDegradeReason {
  reason: string
  count: number
}

/** 一张线路卡片的**效果**数据：各线路不同，由后端按线路给（缓存给命中率、CDN 给缓存口径…） */
export interface LineEffect {
  /** cache：命中率（null = 还没有过任何一次查找） */
  hit_rate?: number | null
  hits?: number
  misses?: number
  bytes_used?: number
  max_bytes?: number
  entries?: Record<string, number>
  entries_total?: number
  /** cdn：分片缓存口径 / 生效域名 */
  segment_cache_header?: string
  domain?: string
  /** direct（已下线）：历史口径里「不经本机」这类说明 */
  vps_bytes?: number
  note?: string
}

/** 播放路径可观测（2026-10 简化：单路径，/api/admin/playback/lines） */
export interface PlayLineCard {
  line: string
  label: string
  requests: number
  bytes_out: number
  idle_seconds: number | null
}

export interface PlayPathCacheState {
  enabled: boolean
  ready_note: string
  hit_rate: number | null
  hits: number
  misses: number
  bytes_used: number
  max_bytes: number
  entries_total: number
}

export interface PlayPathCdnState {
  enabled: boolean
  domain: string
  ready_note: string
}

export interface PlayLinesSnapshot {
  path: string
  label: string
  summary: string
  requests: number
  bytes_out: number
  idle_seconds: number | null
  uptime_seconds: number
  /** cdn 已随 2026-10-17 删除（后端不再下发），留可选键防旧快照 */
  cdn?: PlayPathCdnState
  /** 本地缓存段已随 #959 整删（后端不再下发），留可选键防旧快照 */
  cache?: PlayPathCacheState
  /** 口径说明 */
  scope_note: string
  /** 兼容字段 */
  lines: PlayLineCard[]
  total_users: number
  default_line: string
}

export interface LoginResponse {
  access_token: string
  refresh_token: string
  token_type: string
  user: AdminInfo
}

export interface AdminUserRow {
  id: number
  username: string
  email: string | null
  is_active: boolean
  is_staff: boolean
  emby_username: string | null
  has_subscription: boolean
  subscription_id: number | null
  subscription_end: string | null
  last_login_at: string | null
  /** 注册渠道（v2.44.0 归因）：空串 = 升级前存量，label 显示「未记录」 */
  register_channel: string
  register_channel_label: string
  created_at: string
  /** 用户类型（v2.55 公益服并入用户管理）：welfare=公益服，paid=付费，normal=普通 */
  is_welfare: boolean
  welfare_expires_at: string | null
  welfare_grant_channel: string | null
  user_type: 'welfare' | 'paid' | 'normal'
  /** TG 绑定状态（公益服门禁用）：是否绑定 Telegram */
  tg_bound: boolean
}

export interface UsersResponse {
  total: number
  users: AdminUserRow[]
  /** 后端下发的渠道枚举（含「未记录」哨兵值），前端不自己拼一份 */
  channels?: Array<{ value: string; label: string }>
}

export type CodeType = 1 | 2 | 3

/** 会员卡码状态：可用 / 已停用 / 已过期 / 已用尽 */
export type CodeState = 'active' | 'disabled' | 'expired' | 'used_up'

export interface RegistrationCode {
  id: number
  code: string
  /** 这张会员卡码开通哪个服的会员（一个服一个） */
  realm_id?: number | null
  realm_name?: string
  code_type: CodeType
  code_type_name: string
  days: number
  days_text: string
  is_permanent: boolean
  is_decoy: boolean
  target_username: string
  source: string
  max_uses: number
  use_count: number
  is_active: boolean
  state: CodeState
  note: string | null
  expires_at: string | null
  used_by: { id: number; username: string }[]
  created_at: string
}

export interface CodeTypeStat {
  code_type: CodeType
  code_type_name: string
  total: number
  used: number
  available: number
}

export interface CodeStats {
  total: number
  active: number
  disabled: number
  expired: number
  used_up: number
  decoy: { total: number; triggered: number }
  days_granted: number
  /** 统计范围：当前服的名称，或「全部服」 */
  realm_id?: number | null
  realm_name?: string
  by_type: CodeTypeStat[]
}

/** 播放设备（用户第三方客户端登录设备） */
export interface DeviceRow {
  device_id: string
  name: string | null
  client: string | null
  app_version: string | null
  ip: string | null
  is_blocked: boolean
  first_seen_at: string | null
  last_seen_at: string | null
  user_id: number
  username: string
  is_user_active: boolean
  is_online_recent: boolean
}

export interface DeviceStats {
  total: number
  active_30d: number
  blocked: number
  users: number
  limit_per_user: number
  auto_evict: boolean
  active_days: number
}

/** 登录 / 安全事件日志 */
export interface LoginLogRow {
  id: number
  user_id: number | null
  username: string | null
  ip: string | null
  /** IP 归属地（能力：IP 与地理位置）；未配置提供方时为空串 */
  region: string
  user_agent: string | null
  success: boolean
  reason: string | null
  reason_label: string
  detail: string | null
  created_at: string | null
}

export interface LoginLogsResponse {
  total: number
  summary: { failed_24h: number; risk_24h: number }
  reasons: { value: string; label: string }[]
  logs: LoginLogRow[]
}

export interface RegistrationSettings {
  // 注册码门禁（'code'）已下线：后端不再接受 'code'；DB 残留值由前端归一为 'open' 展示
  mode: 'open' | 'closed' | 'code'
  message: string
  // 注册限流（C5）：后端带默认值下发；可选以兼容旧构造处
  ratelimit_enabled?: boolean
  ratelimit_max?: number
  ratelimit_window?: number
}

export interface Announcement {
  id: number
  title: string
  content: string
  type: string
  is_pinned: boolean
  is_active?: boolean
  created_at: string
  updated_at?: string | null
}

export interface TicketRow {
  id: number
  title: string
  category: string
  status: string
  priority: string
  user_name: string
  created_at: string
  updated_at: string
  latest_message: string
}

export interface TicketMessageRow {
  id: number
  message: string
  is_admin: boolean
  created_at: string
  admin_name: string | null
}

export interface MediaSeekRow {
  id: number
  movie_name: string
  year: string | null
  type: string | null
  note: string | null
  status: string
  admin_note: string | null
  /** 剧集按整季申请："1,2" 或 "all"（全季）；电影为空 */
  season?: string | null
  /** 季的展示文案（后端统一生成：全季 / 第 1、2 季） */
  season_label?: string
  /** TMDB 条目 id（用户从候选列表选则时有；手填片名为空） */
  tmdb_id?: string | null
  /** 已入库条目的 guid（标记入库时记下，用户端可直接去观看） */
  emby_item_id?: string | null
  user_id: number
  user_name: string
  /** 这部片是给哪个服求的（用户提交时选/单服自动带出）；空 = 未标注 */
  realm_id?: number | null
  realm_name?: string
  created_at: string
  /** 已转交的外部服务：moviepilot（已提交订阅）/ qbittorrent（已交给下载器） */
  push_target: string | null
  push_status: string | null
  push_message: string | null
  /** v2 附议数 */
  vote_count?: number
  pushed_at: string | null
}

// ==================== 服务器清单 ====================

// ==================== 多服运营（server_realms） ====================

/** 一个服 = 一套可以独立运营的播放服务：自己的套餐/订阅/媒体库/播放节点 */
export interface RealmNode {
  id: number
  name: string
  url: string
  /** AE 用 NODE_KEY 认领这条记录；空串表示还没认领 */
  node_key: string
  is_enabled: boolean
  is_active: boolean
  online: boolean
  last_checked_at: string | null
  last_check_message: string
}

export interface RealmStats {
  libraries: number
  enabled_libraries: number
  items: number
  mounts: number
  plans: number
  active_subscriptions: number
  expiring_subscriptions: number
  subscribers: number
  nodes: number
  nodes_online: number
  pending_requests: number
}

export interface RealmRow {
  id: number
  name: string
  slug: string
  url: string
  description: string
  is_active: boolean
  sort_order: number
  /** 默认服：沿用历史配置键名，不能删除 */
  is_default: boolean
  /** 接入方式（v2.7.0）：paid = 付费服（需要订阅）/ free = 公益服（免费开放） */
  access_mode: 'paid' | 'free'
  /** 是不是公益服（= access_mode === 'free'，前后端同一口径） */
  is_free: boolean
  /** 公益服规则文案（用户端展示；付费服为空串） */
  access_note: string
  /** 下载策略：null = 跟随全局（公益服默认禁止）/ true = 允许 / false = 禁止 */
  allow_download: boolean | null
  /** 用户端该连的地址（服自己的地址 → 该服 Emby 入口 → 全局环境变量） */
  public_url: string
  nodes: RealmNode[]
  stats: RealmStats
  created_at: string | null
}

export interface RealmSummary {
  total_realms: number
  enabled_realms: number
  /** 公益服 / 付费服的服数（面板顶部与概览页展示） */
  free_realms: number
  paid_realms: number
  libraries: number
  items: number
  plans: number
  active_subscriptions: number
  subscribers: number
  nodes: number
  nodes_online: number
  pending_requests: number
}

export interface RealmsResponse {
  realms: RealmRow[]
  active_realm_id: number
  active_realm_name: string
  summary: RealmSummary
}

export interface RealmOverview {
  realms: {
    id: number
    name: string
    slug: string
    url: string
    is_active: boolean
    is_default: boolean
    /** 接入方式（v2.7.0）：free = 公益服（免费开放） */
    access_mode: 'paid' | 'free'
    is_free: boolean
    public_url: string
    stats: RealmStats
  }[]
  active_realm_id: number
  summary: RealmSummary
}

/** 同步（体检）一个服的所有播放节点后的逐台结果 */
export interface RealmNodeSync {
  id: number
  name: string
  url: string
  ok: boolean
  message: string
  node_key_claimed: string
  realm_slug_reported: string
  libraries: number
  /** 节点自称的服与面板记录不一致：REALM / NODE_KEY 配错了 */
  realm_mismatch: boolean
}

/** 服务器类型：ea（后端服）/ emby（已有 Emby）/ moviepilot / qbittorrent */
export type ServerKind = 'ea' | 'emby' | 'moviepilot' | 'qbittorrent'

/** 类型声明的一个配置字段（由后端类型元数据下发，前端不再维护一份） */
export interface ServerFieldMeta {
  key: string
  label: string
  type?: 'text' | 'password' | 'number'
  secret?: boolean
  placeholder?: string
  help?: string
}

export interface ServerKindMeta {
  value: ServerKind
  label: string
  short: string
  desc: string
  group: string
  /** 只有 EA / Emby 有「当前使用」的概念 */
  activatable: boolean
  fields: ServerFieldMeta[]
}

export interface RemoteServerRow {
  id: number
  name: string
  kind: ServerKind
  kind_label: string
  kind_group: string
  url: string
  /** 属于哪个服（多服运营）：EA / Emby 一定属于某个服 */
  realm_id: number | null
  realm_name: string
  /** 内容自动化（MoviePilot / qB）声明了「全服共用」：不属于某个服，每个服都能用 */
  shared?: boolean
  /** EA 认领用的节点标识（面板在这里填，EA 用同名 NODE_KEY 启动） */
  node_key: string
  config: Record<string, string>
  /** 已配置密钥的字段名（密钥明文永不下发） */
  secret_keys: string[]
  is_enabled: boolean
  is_active: boolean
  remark: string
  last_checked_at: string | null
  last_check_ok: boolean | null
  last_check_message: string
  activatable: boolean
  can_push_media_seek: boolean
}

export interface ServerKindSummary {
  kind: ServerKind
  label: string
  short: string
  group: string
  activatable: boolean
  total: number
  enabled: number
  reachable: number
  active_id: number | null
  active_name: string
  active_url: string
}

export interface ServerSummary {
  kinds: Record<string, ServerKindSummary>
  total: number
  reachable: number
  unchecked: number
  /** 当前统计范围（服）；null = 全部服 */
  realm_id?: number | null
  /** 可以用来接收求片推送的类型（已启用且体检通过） */
  push_ready: ServerKind[]
}

/** EA 自称的身份（`live` 模式下真去问那台 EA：你是谁、属于哪个服、负责哪些库） */
export interface ServerOverviewIdentity {
  ok: boolean
  error?: string
  claimed?: boolean
  node_key?: string
  node_name?: string
  realm_slug?: string
  realm_name?: string
  libraries?: number
  /** 这台进程是否真的在按服 / 节点过滤内容 */
  filtering?: boolean
}

/** EA 视角的挂载体检（按服保存）：挂载里的本机路径是主机相对的，只有那台机器说了算 */
export interface ServerOverviewMounts {
  ok: boolean
  checked_at: string | null
  error: string
  total: number
  failed_count: number
  unreachable: string[]
  /** 从没查过（不是错误，但面板要提示「还不知道」） */
  never_checked: boolean
}

/** Emby 总览的一行 = 一台出流入口（EA / 已有 Emby） */
export interface ServerOverviewRow extends RemoteServerRow {
  realm_slug: string
  realm_is_default: boolean
  is_entry: boolean
  is_current_entry: boolean
  identity: ServerOverviewIdentity
  mounts: ServerOverviewMounts | null
  /** 归这台节点扫描 / 出流的媒体库数 */
  libraries_assigned: number
  /** 同服里还没分配给任何节点的库数（未分配 = 所有节点可见、由面板扫描） */
  libraries_unassigned: number
  warnings: string[]
}

/** 总览里的一张服卡片：这个服到底用哪个入口出流、库归谁 */
export interface ServerOverviewRealm {
  id: number
  name: string
  slug: string
  url: string
  description: string
  is_active: boolean
  is_default: boolean
  public_url: string
  sort_order: number
  entry: {
    mode: string
    label: string
    url: string
    server_id: number | null
    server_name: string
  }
  libraries: { total: number; unassigned: number; by_node: Record<string, number> }
  mounts: ServerOverviewMounts
  warnings: string[]
}

/** Emby 总览（`/api/admin/servers/overview`）：一行一台入口，把散在各页的事实汇到一起 */
export interface ServerOverview {
  /** null / 0 = 全部服；数字 = 只看这个服 */
  realm_id: number | null
  active_realm_id: number
  live: boolean
  realms: ServerOverviewRealm[]
  rows: ServerOverviewRow[]
  totals: {
    entry: number
    online: number
    warnings: number
    libraries: number
    libraries_unassigned: number
  }
  summary: ServerSummary
  kinds: ServerKindMeta[]
}

/** 服务器维度媒体运维里的一个库（**只读事实**：配置仍在媒体库页改） */
export interface ServerOpsLibrary {
  id: number
  name: string
  enabled: boolean
  virtual: boolean
  platform: string | null
  item_count: number
  paths: number
  mount_ids: number[]
  last_scan_at: string | null
  last_scan: EmbyScanResult | null
  /** live 有值 = 此刻在队列里 / 在扫（含 source=other：由归属节点在扫） */
  live: EmbyScanLive | null
  /** 从 live 归约出来的状态；空闲为 idle */
  state: EmbyScanTaskState | 'idle'
}

/** 整服扫描流水里的一条（比单库的多了库名，跨库合并后按时间倒序） */
export interface ServerOpsRun extends EmbyScanRun {
  library_id: number
  library_name: string
}

/** 扫描队列（只含这个节点范围内的行） */
export interface ServerOpsQueue {
  view: 'panel' | 'db' | string
  enabled: boolean
  max_parallel: number | null
  mount_serial: boolean
  running: EmbyScanTask[]
  waiting: EmbyScanTask[]
  history: EmbyScanTask[]
  /** 挂载 id → 名字（只带这个范围正在等的那些） */
  mount_names: Record<string, string>
  remote: { lists: number; reused: number; inflight: number; peak_inflight: number; last_at: string | null } | null
}

/** 刮削补全 / 探测 / 修复：按这个节点范围内的条目聚合 */
export interface ServerOpsPipeline {
  items: number
  /** enrich_status → 条数（pending / enriching / done / failed） */
  enrich: Record<string, number>
  /** probe_status → 条数 */
  probe: Record<string, number>
  repair: {
    total: number
    items: {
      id: string
      name: string
      type: string | null
      library_id: number
      requested_at: string | null
      file_exists: boolean
    }[]
  }
  note?: string
}

/**
 * 内容转交（求片 → MoviePilot / qBittorrent）
 *
 * scope=realm：**服级**，不是这台节点在跑（外部服务自己找片、下载、整理入库），
 * 面板不能把它写成「这台服务器在转存」。
 */
export interface ServerOpsHandoff {
  scope: 'realm' | 'none' | string
  realm_id?: number | null
  total: number
  items: {
    id: number
    movie_name: string
    type: string | null
    season: string | null
    status: string
    push_target: string | null
    push_status: string | null
    push_message: string | null
    pushed_at: string | null
  }[]
  note?: string
}

/** 服务器维度媒体运维快照（`GET /api/admin/servers/{id}/ops`） */
export interface ServerOpsSnapshot {
  server_id: number
  realm_id: number | null
  server: RemoteServerRow
  scope: {
    include_unassigned: boolean
    libraries: number
    enabled: number
    /** 同服里还没分配给任何节点的库数（默认不算进本节点视图） */
    unassigned: number
    runs_limit: number
    scan_cap: number
  }
  libraries: ServerOpsLibrary[]
  /** 整服视角扫描历史：跨库合并，新的在前 */
  runs: ServerOpsRun[]
  queue: ServerOpsQueue
  pipeline: ServerOpsPipeline
  handoff: ServerOpsHandoff
  /** 把「这个视图意味着什么」写成人话；面板直接展示，不自己推断 */
  notes: string[]
}

/** 一键扫描的结果（`POST /api/admin/servers/{id}/ops/scan`） */
export interface ServerOpsScanResult {
  success: boolean
  server_id: number
  server_name: string
  /** 本地入队的库 */
  queued: { id: number; name: string; message: string }[]
  /** 已在队列 / 正在扫的（不重复推） */
  already: { id: number; name: string; message: string; state?: string }[]
  /** 转发给归属节点并成功的 */
  forwarded: { id: number; name: string; node_id: number; node_name: string }[]
  /** 转发失败的：只有这一类会让整键变成「部分失败」 */
  failed: { id: number; name: string; node_id: number; node_name: string; error: string }[]
  /** 停用 / 虚拟库 / 超出单次上限被跳过的（带原因） */
  skipped: { id: number; name: string; reason: string }[]
  message: string
}

export interface ServerProbeResult {
  ok: boolean
  message?: string
  status_code?: number | null
  server_name?: string
  version?: string
  can_query?: boolean
  can_subscribe?: boolean
  subscribe_count?: number | null
  torrent_count?: number | null
}

export interface AdminLogRow {
  id: number
  action: string
  target_type: string | null
  target_id: number | null
  details: Record<string, unknown> | null
  ip_address: string | null
  created_at: string
  admin_name: string
  /** 错误码大白话：后端按 details 原文翻译，无命中为 null */
  error_hint?: {
    id: string
    category: string
    title: string
    what: string
    steps: string[]
  } | null
}

export interface OverviewStats {
  users: { total: number; active: number }
  tickets: { open: number }
  media_seeks: { pending: number }
  emby: { total_items: number; active_sessions: number }
}

export interface PlaybackStats {
  today: { plays: number; users: number }
  user_ranking: { username: string; plays: number }[]
  item_ranking: { name: string; type: string; plays: number }[]
}

// ==================== v2.4.0 运营管理补全 ====================

/** 用户 360° 详情（GET /api/admin/users/{id}） */
export interface UserDetailProfile {
  id: number
  username: string
  email: string | null
  is_active: boolean
  is_staff: boolean
  emby_username: string | null
  points: number
  last_login_at: string | null
  created_at: string | null
}

export interface UserSubscriptionRow {
  id: number
  plan_name: string
  start_date: string | null
  end_date: string | null
  days_left: number
  status: string
}

export interface UserDetail {
  profile: UserDetailProfile
  subscription: { active: UserSubscriptionRow | null; history: UserSubscriptionRow[] }
  points: {
    balance: number
    income: number
    expense: number
    recent: {
      id: number
      amount: number
      balance_after: number
      type: string
      description: string | null
      created_at: string | null
    }[]
  }
  orders: {
    paid_total: number
    recharge: { order_id: string; item_name: string; amount: number; points: number; status: string; created_at: string | null }[]
    subscription: { order_id: string; item_name: string; amount: number; status: string; created_at: string | null }[]
  }
  invitation: {
    count: number
    rebate_total: number
    invitees: { username: string; reward_points: number; created_at: string | null }[]
  }
  checkin: { total: number; last_date: string | null; streak: number }
  watch: { plays: number; watched_items: number }
}

/** 授权资源卡片 · 一个服一张（GET /api/admin/user-grants/{user_id}） */
export interface UserGrantSubscription {
  subscription_id: number | null
  plan_name: string
  start_date: string | null
  end_date: string | null
  days_left: number
  auto_renew: boolean
  /** 退款撤销（与「自然到期」分开说） */
  cancelled: boolean
}

/** 公益服的花积分解锁记录（付费服恒为 undefined） */
export interface UserGrantUnlock {
  unlocked: boolean
  unlocked_at: string | null
  expires_at: string | null
  points_spent: number
}

export interface UserGrantCard {
  realm_id: number
  realm_name: string
  slug: string
  is_default: boolean
  /** 服本身是否启用（停用不改变播放判定，只提示管理员该清理） */
  is_active: boolean
  access_mode: 'paid' | 'free'
  is_free: boolean
  access_note: string
  /** 授权来源：subscription / expired / none / unlock / free_open */
  grant: 'subscription' | 'expired' | 'none' | 'unlock' | 'free_open'
  grant_label: string
  /** 配色语义：ok 生效 / warn 即将到期 / off 未授权或已过期 */
  state: 'ok' | 'warn' | 'off'
  expiring_soon: boolean
  /** 这份授权能做什么 */
  can_play: boolean
  view_granted: boolean
  download_allowed: boolean
  /** 这个服提供什么（null = 统计读取失败，显示「—」而不是谎报 0） */
  resources: {
    libraries: number | null
    enabled_libraries: number | null
    items: number | null
    mounts: number | null
    nodes: number | null
    nodes_online: number | null
  }
  subscription?: UserGrantSubscription
  unlock?: UserGrantUnlock
}

export interface UserGrants {
  user: { id: number; username: string; is_staff: boolean; is_active: boolean }
  summary: {
    realms_total: number
    realms_playable: number
    realms_expiring: number
    realms_expired: number
    /** 设备数（null = 读取失败，显示「—」而不是谎报 0） */
    devices_used: number | null
    device_limit: number | null
    play_line: string
    play_line_label: string
    scope_note: string
  }
  cards: UserGrantCard[]
}

/** 趋势统计（GET /api/admin/stats/trend） */
export interface TrendPoint {
  date: string
  new_users: number
  plays: number
  revenue: number
  checkins: number
}

export interface TrendStats {
  days: number
  series: TrendPoint[]
  totals: { new_users: number; plays: number; revenue: number; checkins: number }
}

/** 订阅总览（GET /api/admin/realms/{id}/subscriptions，`realm_id=0` = 全部服） */
export interface SubscriptionOverviewRow {
  id: number
  user_id: number
  username: string
  plan_name: string
  /** 会员属于哪个服：一个服一个，多服下必须分开看 */
  realm_id?: number | null
  realm_name?: string
  start_date: string | null
  end_date: string | null
  days_left: number
  status: string
}

export interface SubscriptionOverview {
  summary: { total: number; active: number; expiring_7d: number; expired: number }
  subscriptions: SubscriptionOverviewRow[]
}

/** 按服订阅清单（GET /api/admin/realms/{id}/subscriptions） */
export interface RealmSubscriptionsResponse extends SubscriptionOverview {
  total: number
  /** 本次统计范围：null = 全部服 */
  realm_id: number | null
  realm_name: string
  active_realm_id: number
}

export interface EconomyOverview {
  total_points: number
  checkins_today: number
  orders: { pending: number; revenue: number }
  exchange_codes: { total: number; used: number }
  invitations: number
}

/** 存储后端：本地文件 / Rclone / 115 网盘（label 由后端下发，前端不自己维护） */
export interface StorageBackend {
  value: string
  label: string
}

/**
 * 媒体库的一条路径（**路径与存储后端分开**，界面不出现 mount:// 前缀）
 *
 * - `path`：本机来源给绝对路径（/media/电影）；挂载来源给挂载内路径（/video/剧集/国产剧）
 * - `backend` / `backend_label`：这条路径走哪种存储
 * - `mount_id`：远程来源绑定的 storage_mounts.id
 * - `raw`：拼回前缀后的入库形态，只用于改动对比，界面不显示
 */
export interface LibraryPathEntry {
  path: string
  backend: string
  backend_label: string
  mount_id: number | null
  mount_name: string
  source: 'mount' | 'local' | 'prefix'
  raw: string
}

/** 提交给后端的路径（只有这三项；label / mount_name / raw 都是回显用的） */
export interface LibraryPathInput {
  path: string
  backend: string
  mount_id: number | null
}

export interface EmbyLibrary {
  id: number
  guid: string
  name: string
  collection_type: string
  /** 归属服与播放节点：多服 / 多机部署下“这个库归谁”一眼可见 */
  realm_id?: number | null
  realm_name?: string
  node_id?: number | null
  node_name?: string
  /** 归属节点最近一次体检是否通过；null = 未体检 */
  node_online?: boolean | null
  paths: string[]
  /**
   * 简化路径条目（v2.43.0）：界面用它渲染路径列表（隐藏 mount:// 前缀，带存储后端标签）。
   * 老后端不返回这个字段时回退到 paths —— 所以它是可选的。
   */
  path_entries?: LibraryPathEntry[]
  /**
   * 本机目录可读性（v2.46.0，后端真实 scandir 得出）：
   * true=能列举 / false=有本机路径读不到（挂载断了等）/ null=纯远程来源或没配本机路径。
   * 纯远程时读不到是正常的，所以置 null 而不是 false。
   */
  local_dirs_readable?: boolean | null
  /** 绑定的存储挂载（storage_mounts.id）：本机目录 / 115 / rclone / Drive */
  mount_ids: number[]
  /**
   * 封面自动生成：样式 + 标题文字。为 null/空表示仍在用直传的 cover_path。
   * 标题支持 {library} {type} {year} 三个变量。
   */
  cover_template?: 'poster' | 'visual' | 'filmstrip' | null
  cover_title?: string | null
  cover_subtitle?: string | null
  /**
   * 新片入库后自动重新生成封面（v2.43.1）：扫完一轮且确实有新增条目时，按当前模板重拼一次。
   * 没有封面模板时这个开关是空转的（后端会自己跳过）。
   */
  cover_auto_regen?: boolean
  /** 增量扫描（v2.44.0，默认开）：走目录/文件指纹秒跳，没变动的目录不重扫 */
  incremental_scan?: boolean
  /** 本机目录实时监听（v2.44.0，默认开）：有变动自动触发增量扫描；远程挂载仍靠定时扫描 */
  fs_watch?: boolean
  is_enabled: boolean
  is_scanning: boolean
  /**
   * 这个库此刻在扫吗（v2.27.0）：排队中 / 扫描中（含阶段与进度）；空闲为 null
   *
   * 只靠 is_scanning 看不出「排在第几位、在等谁、扫到哪了」——四个库同点扫描时
   * 界面只能显示「扫描中」，而它其实还在排队。source=other 表示由归属节点在扫。
   */
  scan_live?: EmbyScanLive | null
  last_scan_at: string | null
  /** 最近一次扫描的结果（null = 从未扫描过）；见后端 scan_result_payload */
  last_scan?: EmbyScanResult | null
  item_count: number
  /** 刮削策略：missing_only / 3m / 6m / 1y / all */
  scrape_policy?: string
  /** 管理员直传的媒体库封面；由后端鉴权后返回二进制 */
  cover_url?: string | null
  /** 按发行平台自动生成的虚拟媒体库（无自己的目录） */
  is_virtual?: boolean
  platform?: string | null
  /** 绑定的 115 账号配置档（不填则回退默认账号 / 服务器级 PAN115_COOKIE） */
  account_115_id?: number | null
  /**
   * 播放可达性（v2.28.0）：内容在「出流的那台机器」上真的拿得到吗
   *
   * 分离部署最容易踩的坑：面板（EM）扫描一切正常、item_count 也在涨，但内容只存在于
   * EM 那台机器上（本机目录 / local 挂载），出流的 EA 读不到 —— 客户端点播放才 404/502。
   * ok = 有把握；warn = 无法确认（没拉过 EA 体检 / 来源是本机路径）；bad = 有证据说明读不到。
   */
  playback?: EmbyPlaybackReachability | null
}

/** 一条播放可达性问题：level 分级 + 人话原因 + 改法 */
export interface EmbyPlaybackProblem {
  level: 'ok' | 'warn' | 'bad'
  /** 原因码（local_path_unchecked / mount_unreachable_on_node / mount_unchecked …） */
  code: string
  message: string
  fix: string
  /** 关联的来源标签（挂载名或本机路径） */
  source: string
}

/** 一条库（或用户端地址）的可达性判定 */
export interface EmbyPlaybackReachability {
  level: 'ok' | 'warn' | 'bad'
  code: string
  message: string
  fix: string
  /** 当前谁在出流：面板本机 / 后端播放节点（EA）/ 已有的 Emby */
  playback: 'panel' | 'ea' | 'external'
  playback_label: string
  /** 会出流的具体机器名（EA 模式下） */
  targets: string[]
  /** 主机相对的来源（本机目录 / local 挂载）：只存在于扫描的那台机器上 */
  local_sources: string[]
  /** 共享来源（WebDAV / rclone / 115 / AList）：两台机器读同一份 */
  shared_sources: string[]
  problems: EmbyPlaybackProblem[]
  /** EA 体检快照时间（null = 还没拉过） */
  checked_at: string | null
}

/**
 * 用户端地址是从哪来的（v2.32.0）
 *
 * realm / config / env 都是人配的（服地址、服务入口、环境变量）；none = 还没配，
 * 用户端账号卡按**当前访问用的地址**下发（见后端 resolve_emby_base_url_with_source）。
 */
export type EmbyClientUrlSource = 'realm' | 'config' | 'env' | 'none'

/** 播放可达性报告：出流方式 + 逐库判定 + 用户端地址一致性（一条接口拿全） */
export interface EmbyReachabilityReport {
  realm_id: number | null
  realm_name: string
  playback: {
    mode: 'panel' | 'ea' | 'external'
    label: string
    /** 面板自己是否还开着协议面（ENABLE_EMBY_GATEWAY） */
    gateway_enabled: boolean
    targets: { id: number; name: string; enabled: boolean; url: string; online: boolean | null }[]
    /** 用户端账号卡里会显示的地址；none 时为空串（未配，面板按当前访问地址显示） */
    client_url: string
    client_url_source: EmbyClientUrlSource
  }
  ea_health_at: string | null
  ea_health_ok: boolean
  counts: { libraries: number; ok: number; warn: number; bad: number }
  level: 'ok' | 'warn' | 'bad'
  client_endpoint: EmbyPlaybackReachability & {
    url: string
    url_source: EmbyClientUrlSource
    gateway_enabled: boolean
  }
  libraries: (EmbyPlaybackReachability & { library_id: number; library_name: string })[]
}

/**
 * 一条扫描来源的明细：这条来源扫到了多少文件、这批文件的处理结果
 *
 * 「挂载配好了、目录也在，但里面一条文件都没有」（挂载点被清空 / 账号范围变了 / 路径写错
 * 但目录恰好存在）只看总体统计是完全看不出来的——那是所有来源加在一起的结果。
 */
export interface EmbyScanSource {
  /** 来源标签：本机路径，或「挂载名（类型）」 */
  label: string
  /** unavailable = 这条来源读不到（同时也计进 failed_roots）；缺省 = 正常参与了这一轮 */
  kind?: string
  /** 这条来源自己的失败原因（读不到 / 遍历中途出错） */
  error?: string
  /** 这条来源里**发现**的媒体文件数（增量扫描跳过未变化的文件也算，不是写库条数） */
  files: number
  added: number
  updated: number
  probed: number
  scraped: number
  repaired: number
  unchanged: number
}

/** 一轮扫描的统计：最近一次与扫描流水共用同一份字段（后端 _scan_metrics） */
export interface EmbyScanMetrics {
  added: number
  updated: number
  removed: number
  probed: number
  scraped: number
  repaired: number
  /** 增量扫描跳过的未变化条目数 */
  unchanged: number
  /** 本轮被放回可见的下架条目数（v2.48.0 软删除：文件回来即复活，连播放进度一起） */
  resurrected: number
  /** 来源不完整，本轮跳过了「清理已删除条目」 */
  removal_skipped: boolean
  /** 读不到的来源（路径 / 挂载 + 原因） */
  failed_roots: string[]
  /** 按来源拆分的明细（顺序 = 媒体库里的配置顺序；虚拟库为空数组） */
  sources: EmbyScanSource[]
}

/** 扫描状态：running = 正在跑；success = 跑完且来源都正常；partial = 有来源读不到（已跳过清理）；failed = 异常中断 */
export type EmbyScanStatus = 'running' | 'success' | 'partial' | 'failed' | 'interrupted'

/** 媒体库最近一次扫描的结果：管理端刷新后仍然可查（不用去翻服务器日志） */
export interface EmbyScanResult extends EmbyScanMetrics {
  status: EmbyScanStatus
  finished_at: string | null
  duration_ms: number | null
  /** 失败原因摘要（仅 partial / failed 时有值） */
  error: string | null
}

/**
 * 扫描任务的四个状态：
 * queued = 排队等（等前面的扫描 / 等同一个远程挂载腾出来）；running = 正在跑；
 * done = 跑完（结果看 result：success / partial / failed）；failed = 异常中断；canceled = 排队时被取消
 */
export type EmbyScanTaskState = 'queued' | 'running' | 'done' | 'failed' | 'canceled'

/** 扫描进行中的实时进度：回答「扫到哪了、卡在谁身上、慢在网络吗」 */
export interface EmbyScanProgress {
  library_id: number
  name?: string
  /** enumerating（枚举文件）/ processing（处理条目）/ cleanup（清理） */
  phase: string
  /** 阶段的中文名（后端下发，前端不再维护一份） */
  phase_label: string
  /** 已发现的媒体文件数 */
  enumerated: number
  /** 已处理（写库）的条目数 */
  processed: number
  /** 当前处理的目录 / 文件 */
  current: string
  elapsed_ms: number
  started_at: string | null
  phase_changed_at?: string | null
  updated_at?: string | null
  /** 本轮扫描期间的远程目录列举：真实请求数 / 内存复用数 / 最近一次时间 */
  remote_lists: number
  remote_reused: number
  remote_last_at: string | null
}

/** 队列里的一条扫描任务（排队中 / 正在跑 / 最近完成） */
export interface EmbyScanTask {
  library_id: number
  name: string
  /** 谁触发的：manual（面板）/ client（客户端刷新）/ node（归属节点）/ repair（修复队列） */
  trigger: string
  state: EmbyScanTaskState
  /** 这个库要读的远程挂载；同一挂载同时只允许一个扫描任务 */
  mount_ids: number[]
  local_sources: number
  requested_at: string
  started_at: string | null
  finished_at: string | null
  /** 排队等了多久（毫秒） */
  queued_ms: number
  duration_ms: number | null
  /** 正在等哪些挂载（id）：同一远程挂载被别的库占着时在这里写明 */
  waiting_for: number[]
  result: string | null
  error: string | null
  /** 连续被点了几次（合并成一条） */
  request_count: number
  remote_lists: number
  remote_reused: number
  /** 排队中的位置（第几位）；正在跑 / 已结束为 null */
  position?: number | null
  progress?: EmbyScanProgress | null
  /**
   * 这条记录从哪来：缺省 = 本进程的队列；
   * redis = 排队项（Redis 里只有位置与触发方）；
   * db = 从执行扫描的节点写进库里的状态合成（进度按刷盘间隔更新，不是实时的）
   */
  via?: 'redis' | 'db'
  /** v2.49.0: 新增/更新/删除条数 */
  added?: number | null
  updated?: number | null
  removed?: number | null
}

/** 一个媒体库的实时扫描状态（媒体库列表里的 scan_live） */
export interface EmbyScanLive {
  library_id: number
  name?: string
  state: EmbyScanTaskState
  /** panel = 本进程的队列（有实时进度）；other = 由归属节点在扫（进度不在本进程） */
  source: 'panel' | 'other'
  message?: string
  position?: number | null
  waiting_for?: number[]
  mount_ids?: number[]
  progress?: EmbyScanProgress | null
}

/** 扫描队列快照：正在跑的、排队的、最近完成的，以及远程 IO 计数 */
export interface EmbyScanQueue {
  /** 队列开关（EMBY_SCAN_QUEUE=0 时回到升级前行为） */
  enabled: boolean
  /** 同时最多跑几个扫描 */
  max_parallel: number
  /** 同一个远程挂载是否串行化 */
  mount_serial: boolean
  running: EmbyScanTask[]
  waiting: EmbyScanTask[]
  history: EmbyScanTask[]
  /**
   * running / history 两列的数据来源：panel = 本进程的队列（实时）；
   * db = 扫描由别的进程执行，这两列来自执行节点写进库里的状态
   */
  view: 'panel' | 'db'
  /** 挂载 id → 正在占着它的媒体库 id */
  mount_owners: Record<string, number>
  /** 挂载 id → 名字（面板直接写名字，不用去存储来源页对号） */
  mount_names: Record<string, string>
  remote: {
    lists: number
    reused: number
    inflight: number
    peak_inflight: number
    last_at: string | null
  }
}

/** 一条扫描流水（最近若干轮）：「这个库每轮都失败」和「只是最近一轮失败」是两件事 */
export interface EmbyScanRun extends EmbyScanMetrics {
  id: number
  status: EmbyScanStatus
  /** 谁触发的：manual（面板）/ client（客户端刷新）/ node（归属节点）/ repair（修复队列） */
  trigger: string | null
  started_at: string | null
  finished_at: string | null
  duration_ms: number | null
  error: string | null
}

/** 存储挂载的类型元数据（后端下发，前端不自己维护一份） */
export interface MountTypeField {
  key: string
  label: string
  placeholder?: string
  secret?: boolean
  required?: boolean
  /** account115 = 渲染成 115 账号下拉；select 配合 options */
  type?: string
  options?: { label: string; value: string }[]
}

export interface MountTypeMeta {
  value: string
  label: string
  kind: 'local' | 'remote'
  /** 图标分组：local / cloud / gateway */
  group?: string
  hint: string
  needs_path: boolean
  /** 是否支持后台目录浏览 */
  browse?: boolean
  /** 「根目录标识」字段名，浏览时点目录会写回它 */
  root_key?: string
  /** 是否可以从 rclone.conf 拉取「已配置的 remote 列表」（rclone） */
  remotes?: boolean
  fields: MountTypeField[]
}

/** rclone.conf：用户粘贴的标准 INI 文本（面板不代管，只负责跑 rclone） */
export interface RcloneConfStatus {
  /** 实际落盘路径（data/rclone/rclone.conf） */
  path: string
  /** 是否已配置（至少有一个 remote 段） */
  configured: boolean
  /** 已配置的 remote 名，带尾冒号 */
  remotes: string[]
  total: number
}

/** 存储挂载：媒体库的内容来源（local / 115 / rclone / drive，类型由路径前缀决定） */
export interface StorageMount {
  id: number
  name: string
  /** 属于哪个服（storage_mounts.realm_id）；空 = 未标注（所有服都看得到） */
  realm_id?: number | null
  /** 由哪台 EA 去读（每台 EA 一份 rclone.conf）；空 = 老数据，回退到本服 EA */
  server_id?: number | null
  /** server_id 对应的服务器名（后端顺手下发，前端不自己查） */
  server_name?: string | null
  /** 已下线类型的残留行：不自动删，这里给出怎么改（前端直接显示并报红） */
  legacy_note?: string
  mount_type: string
  mount_type_label: string
  kind: 'local' | 'remote'
  path: string
  /** 已脱敏的配置；secret_keys 里的键永远不出明文 */
  config: Record<string, string>
  secret_keys: string[]
  is_enabled: boolean
  remark: string
  last_checked_at: string | null
  last_check_ok: boolean | null
  last_check_message: string | null
  library_ids: number[]
  /** 本地硬盘的路径是否存在（远程类型为 null） */
  path_exists: boolean | null
  /** EM（面板进程）能不能用它：等于后台「测试连接」的结果 */
  em_reachable: boolean | null
  em_message: string
  em_checked_at: string | null
  /** EA（分离部署的播放节点）能不能用它；null 表示还没体检过 */
  ea_reachable: boolean | null
  ea_message: string
}

/** 当前真正出流的是谁：EA 分离部署 / 外部 Emby / 面板自己（一体化） */
export type PlaybackNode = 'ea' | 'external' | 'panel'

/** EA 视角挂载体检快照的汇总（逐条结果在 StorageMount 上） */
export interface EaMountHealth {
  ok: boolean
  checked_at: string | null
  error: string
}

export interface MountDirEntry {
  name: string
  rel: string
  is_dir: boolean
  size: number
  /** 提供者眼里的目录标识（115 的 cid）；浏览时可直接选为挂载根目录 */
  entry_id?: string
}

/** 115 账号配置档（Cookie 型，不回传明文） */
export interface Pan115Account {
  id: number
  name: string
  cookie_preview: string
  has_cookie: boolean
  /** 该配置档发 115 请求用的 UA（空 = 跟服务器级 MOUNT_UA） */
  ua: string
  is_default: boolean
  is_enabled: boolean
  remark: string
  last_verified_at: string | null
  last_verify_ok: boolean | null
  last_verify_message: string | null
  created_at: string | null
}

/** 115 目录项（目标路径选择器） */
export interface Pan115DirEntry {
  fid: string
  cid: string
  name: string
  is_dir: boolean
  size: number
  pickcode: string
}

export interface EmbySessionRow {
  session_key: string
  username: string
  item: string
  item_type: string
  device: string
  client: string
  remote_addr: string
  play_method: string
  position_ticks: number
  duration_ticks: number
  is_paused: boolean
  started_at: string | null
}

