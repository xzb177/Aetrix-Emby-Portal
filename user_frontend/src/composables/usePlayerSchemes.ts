// 播放器一键导入：与接线员页同一份账号卡 import_schemes。
// 只接受 App scheme（rex:// / senplayer:// / vidhub://），用 location.href 拉起。
// 不接受 http(s)，避免把 Emby 网页地址写进地址栏、历史或长按菜单。
import { embyApi, type AccountCard } from '@/api'

export interface PlayerScheme {
  name: string
  url: string
}

/** 接线员按钮上的名字就是账号卡的键：Rex / senplayer / VidHub。 */
const SCHEME_ORDER = ['Rex', 'senplayer', 'VidHub']

export function isImportScheme(url: string): boolean {
  if (!url || /\s/.test(url)) return false
  if (/^https?:/i.test(url)) return false
  return /^[a-z][a-z0-9+.-]*:/i.test(url)
}

/** 接线员 openScheme 的跳转本体。空地址或网页地址直接拒绝。 */
export function assignImportScheme(url: string): boolean {
  if (!isImportScheme(url)) return false
  window.location.href = url
  return true
}

export function listImportSchemes(schemes: Record<string, string> | null | undefined): PlayerScheme[] {
  if (!schemes || typeof schemes !== 'object') return []
  const keys = Object.keys(schemes)
  const ordered = [
    ...SCHEME_ORDER.filter((k) => keys.includes(k)),
    ...keys.filter((k) => !SCHEME_ORDER.includes(k)),
  ]
  const out: PlayerScheme[] = []
  for (const name of ordered) {
    const url = schemes[name]
    if (typeof url === 'string' && isImportScheme(url)) out.push({ name, url })
  }
  return out
}

let cached: PlayerScheme[] | null = null
let inflight: Promise<PlayerScheme[]> | null = null

/** 账号卡 import_schemes。失败当成没有播放器，不抛到界面。 */
export function loadImportSchemes(force = false): Promise<PlayerScheme[]> {
  if (cached && !force) return Promise.resolve(cached)
  if (inflight && !force) return inflight
  inflight = embyApi
    .getAccountCard()
    .then((card: AccountCard) => {
      cached = listImportSchemes(card?.import_schemes)
      return cached
    })
    .catch(() => {
      cached = []
      return cached
    })
    .finally(() => {
      inflight = null
    })
  return inflight
}
