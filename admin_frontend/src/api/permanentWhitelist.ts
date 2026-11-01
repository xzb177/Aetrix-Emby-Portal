/** 永久账号白名单（全部走 /api/admin/permanent-whitelist/*）
 *
 * 名单里的账号是永久账号：公益资格、查看凭证、订阅三处过期判定恒有效。
 * 管理 axios 实例 baseURL 是 `/api/admin`。
 */
import { del, get, post, put } from '@/utils/request'

const E = '/permanent-whitelist'

export interface PermanentEntry {
  id: number
  user_id: number
  username: string | null
  email: string | null
  is_welfare: boolean
  note: string | null
  added_by: number | null
  added_by_name: string | null
  created_at: string | null
}

export interface PermanentSearchUser {
  id: number
  username: string
  email: string | null
  is_welfare: boolean
  already_listed: boolean
}

export const fetchPermanentEntries = () =>
  get<{ enabled: boolean; entries: PermanentEntry[] }>(`${E}/entries`)

export const searchPermanentUsers = (q: string) =>
  get<{ users: PermanentSearchUser[] }>(`${E}/search`, { q })

export const addPermanentEntry = (data: { user_id: number; note?: string }) =>
  post<{ success: boolean; entry: PermanentEntry }>(`${E}/entries`, data)

export const removePermanentEntry = (userId: number) =>
  del<{ success: boolean }>(`${E}/entries/${userId}`)

export const fetchPermanentConfig = () =>
  get<{ enabled: boolean }>(`${E}/config`)

export const savePermanentConfig = (enabled: boolean) =>
  put<{ enabled: boolean }>(`${E}/config`, { enabled })
