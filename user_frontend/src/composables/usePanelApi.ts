import api from '@/api/index'
import type { WornBadge } from '@/api/index'
import { toUserMessage } from '@/utils/toUserMessage'

/**
 * v4 面板 API 封装（PR-2 地基）
 * 后端 envelope 为 {code: 0, msg, data}（与老接口 {code: 200} 不同，
 * 全局拦截器只解 code===200，这里单独处理 code===0）。
 * 全部接口需要登录（Bearer token 由全局拦截器自动带）。
 */

export interface PanelEnvelope<T> {
  code: number
  msg: string
  data: T
}

function unwrap<T>(res: unknown): T {
  const r = res as PanelEnvelope<T> | T | null
  if (r && typeof r === 'object' && 'code' in r) {
    if ((r as PanelEnvelope<T>).code !== 0) {
      throw new Error((r as PanelEnvelope<T>).msg || 'panel api error')
    }
    return (r as PanelEnvelope<T>).data
  }
  return r as T
}

export interface TodayData {
  greeting?: string
  date_label?: string
  parcel_count?: number
  unsealed_count?: number
  streak_days?: number
  letter_readable?: boolean
  [k: string]: unknown
}

/** 服主告示牌（/api/panel/home 的 board 区块） */
export interface BoardData {
  text?: string
  signature?: string
  visible?: boolean
  [k: string]: unknown
}

export interface ParcelItem {
  id: string | number
  title?: string
  name?: string
  media_type?: string
  item_type?: string
  type?: string
  episode_count?: number
  resolution?: string
  video_resolution?: string
  production_year?: number
  arrived_at?: string
  added_at?: string
  unsealed?: boolean
  deep_link?: string
  [k: string]: unknown
}

export interface ParcelsData {
  parcels: ParcelItem[]
}

/** 实时动态流（GET /api/panel/feed，C档第3批；realm_id 不传=面板当前服，跟随当前服） */
export interface FeedItem {
  /** ISO 时间串；后端为空时为 null */
  at: string | null
  /** scan / enrich / play / system */
  type: string
  /** 展示文案（播放事件已脱敏为「一位影迷」） */
  text: string
  /** 库名标签；系统事件可为 null */
  library: string | null
  [k: string]: unknown
}

export interface FeedData {
  items: FeedItem[]
  realm_id: number | null
  limit: number
}

/** 收藏家：口味护照 / 成就星图 / 馆灵 */
export interface TasteStamp {
  name: string
  stamped: boolean
  count: number
}

export interface Achievement {
  key: string
  name: string
  desc: string
  icon: string
  unlocked: boolean
}

export interface PetStage {
  name: string
  icon: string
}

export interface PetData {
  stage: number
  name: string
  icon: string
  hours: number
  next_name: string | null
  next_icon: string | null
  next_hours: number | null
  progress: number
  stages: PetStage[]
}

export interface CollectorData {
  nickname?: string
  points?: number
  watch_hours?: number
  streak?: number
  achievement_count?: number
  tastes?: Record<string, TasteStamp>
  achievements?: Achievement[]
  /** 佩戴中的成就徽章（存账号，后端已校验解锁/上架）；没戴为 null */
  worn_badge?: WornBadge | null
  new_unlocks?: string[]
  pet?: PetData
  [k: string]: unknown
}

/** 服务器页：生命体 / 雷达 / 生态 / 机关 */
export interface ServerVitals {
  status?: string
  level?: string
  backlog?: number
  version?: string
  [k: string]: unknown
}

export interface EcoItem {
  library_id: number
  name: string
  count: number
  [k: string]: unknown
}

export interface RadarTask {
  key: string
  name: string
  active: boolean
  [k: string]: unknown
}

export interface GrowthDay {
  date: string
  weekday: string
  count: number
  [k: string]: unknown
}

export interface ServerData {
  vitals?: ServerVitals
  eco?: EcoItem[]
  radar_tasks?: RadarTask[]
  growth?: GrowthDay[]
  is_admin?: boolean
  [k: string]: unknown
}

async function get<T>(url: string): Promise<T> {
  const res = await api.get(url)
  return unwrap<T>(res)
}

async function post<T>(url: string, body?: unknown): Promise<T> {
  const res = await api.post(url, body)
  return unwrap<T>(res)
}

async function put<T>(url: string, body?: unknown): Promise<T> {
  const res = await api.put(url, body)
  return unwrap<T>(res)
}

export const panelApi = {
  /** 问候 / 今日封存件数 / 连签 */
  today: () => get<TodayData>('/api/panel/today'),
  /** 今日封存列表（后端返回 {parcels: [...]} 包裹） */
  parcels: () => get<ParcelsData>('/api/panel/parcels'),
  /** 拆封（幂等） */
  unseal: (itemId: string | number) => post(`/api/panel/parcels/${itemId}/unseal`),
  /** 服务器脉搏 */
  pulse: () => get<Record<string, unknown>>('/api/panel/pulse'),
  /** 实时动态流（C档第3批）：不传 realm_id，后端按面板当前服（active_realm_id）解析，跟随当前服 */
  feed: () => get<FeedData>('/api/panel/feed'),
  /** 每日一封 */
  letter: () => get<Record<string, unknown>>('/api/panel/letter'),
  /** 首页合并（perf 2026-10-13）：一次返回 today/parcels/pulse/letter；单区块失败时该字段为 null */
  home: () => get<{
    today: TodayData | null
    parcels: ParcelsData | null
    pulse: Record<string, unknown> | null
    letter: Record<string, unknown> | null
    board: BoardData | null
  }>('/api/panel/home'),
  /** 收藏家数据 */
  collector: () => get<CollectorData>('/api/panel/collector'),
  /** 观影碑榜单：board=hot/duration，period=day/week */
  leaderboard: (board: 'hot' | 'duration', period: 'day' | 'week') =>
    get<{
      board: string
      period: string
      window_start: string
      items: Array<Record<string, unknown>>
      my_rank: number | null
    }>(`/api/user/emby/leaderboard?board=${board}&period=${period}`),
  /** 留言墙：最新 20 条 */
  wallList: () => get<{ posts: Array<Record<string, unknown>> }>('/api/panel/wall'),
  /** 留言墙：发布 */
  wallPost: (content: string) => post<{ id: number }>('/api/panel/wall', { content }),
  /** 抽一签：今日结果 */
  fortuneToday: () => get<Record<string, unknown>>('/api/panel/fortune'),
  /** 抽一签：抽取（每天一次，幂等） */
  fortuneDraw: () => post<Record<string, unknown>>('/api/panel/fortune'),
  /** 隐私：是否允许上榜 */
  privacyGet: () => get<{ show_on_leaderboard: boolean }>('/api/panel/privacy'),
  /** 隐私：设置是否允许上榜 */
  privacySet: (show: boolean) => put<{ show_on_leaderboard: boolean }>('/api/panel/privacy', { show_on_leaderboard: show }),
  /** 佩戴/摘下徽章（存账号）；key=null 为摘下 */
  setWornBadge: (key: string | null) => post<{ worn_badge: WornBadge | null }>('/api/panel/worn-badge', { key }),
  /** 服务器状态 */
  server: () => get<ServerData>('/api/panel/server'),
}

/**
 * 错误文案人话化（Batch G 收敛：横切能力只许一套实现）
 *
 * 本函数保留签名做兼容壳，实际翻译全部收敛到 `toUserMessage`：
 * 后端人话 detail 透传、英文技术信息按场景映射、其余走加载兜底。
 */
export function humanizeError(e: unknown): string {
  return toUserMessage(e, 'loadFailed')
}

/** 特性开关：VITE_PANEL_V4_ENABLED=0 时回退老首页；默认开 */
export function isPanelV4Enabled(): boolean {
  return import.meta.env.VITE_PANEL_V4_ENABLED !== '0'
}
