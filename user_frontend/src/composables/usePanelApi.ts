import api from '@/api/index'

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
  streak_days?: number
  letter_readable?: boolean
  [k: string]: unknown
}

export interface ParcelItem {
  id: string | number
  title?: string
  name?: string
  media_type?: string
  type?: string
  episode_count?: number
  resolution?: string
  arrived_at?: string
  unsealed?: boolean
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

export const panelApi = {
  /** 问候 / 今日封存件数 / 连签 */
  today: () => get<TodayData>('/api/panel/today'),
  /** 今日封存列表 */
  parcels: () => get<ParcelItem[]>('/api/panel/parcels'),
  /** 拆封（幂等） */
  unseal: (itemId: string | number) => post(`/api/panel/parcels/${itemId}/unseal`),
  /** 服务器脉搏 */
  pulse: () => get<Record<string, unknown>>('/api/panel/pulse'),
  /** 每日一封 */
  letter: () => get<Record<string, unknown>>('/api/panel/letter'),
  /** 收藏家数据 */
  collector: () => get<Record<string, unknown>>('/api/panel/collector'),
  /** 服务器状态 */
  server: () => get<Record<string, unknown>>('/api/panel/server'),
  /** 机关拨杆（仅管理员） */
  toggles: (payload: Record<string, unknown>) => post('/api/panel/server/toggles', payload),
}

/** 特性开关：VITE_PANEL_V4_ENABLED=0 时回退老首页；默认开 */
export function isPanelV4Enabled(): boolean {
  return import.meta.env.VITE_PANEL_V4_ENABLED !== '0'
}
