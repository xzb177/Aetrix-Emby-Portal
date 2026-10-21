// 播放器一键导入：与接线员页同一份账号卡 import_schemes。
// 只接受 rex:// / senplayer:// / vidhub://（scheme 大小写不敏感），用 location.href 拉起。
// 其余一律拒绝，包括 javascript:、data:、http(s):。
import { embyApi, type AccountCard } from '@/api'
import { useUserStore } from '@/stores/user'

export interface PlayerScheme {
  name: string
  url: string
}

/** 账号卡的键。界面上 senplayer 显示成 SenPlayer。 */
const SCHEME_ORDER = ['Rex', 'senplayer', 'VidHub']
const SCHEME_LABEL: Record<string, string> = { senplayer: 'SenPlayer' }

const ALLOWED_SCHEME = /^(?:rex|senplayer|vidhub):\/\//i

export function isImportScheme(url: string): boolean {
  if (!url || /\s/.test(url)) return false
  return ALLOWED_SCHEME.test(url)
}

/** 接线员 openScheme 的跳转本体。不在白名单里的地址直接拒绝。 */
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
  for (const key of ordered) {
    const url = schemes[key]
    if (typeof url === 'string' && isImportScheme(url)) {
      out.push({ name: SCHEME_LABEL[key] ?? key, url })
    }
  }
  return out
}

let cached: { userId: number | null; schemes: PlayerScheme[] } | null = null
let inflight: { userId: number | null; promise: Promise<PlayerScheme[]> } | null = null

function cacheUserId(): number | null {
  return useUserStore().user?.id ?? null
}

/** 账号卡 import_schemes。失败不写入缓存，下次再拉；缓存按用户 id 分开，换号不串。 */
export function loadImportSchemes(force = false): Promise<PlayerScheme[]> {
  const userId = cacheUserId()
  if (cached && cached.userId !== userId) cached = null
  if (!force && cached && cached.userId === userId) return Promise.resolve(cached.schemes)
  if (!force && inflight && inflight.userId === userId) return inflight.promise

  const promise = embyApi
    .getAccountCard()
    .then((card: AccountCard) => {
      const schemes = listImportSchemes(card?.import_schemes)
      if (cacheUserId() === userId) cached = { userId, schemes }
      return schemes
    })
    .catch(() => [] as PlayerScheme[])
    .finally(() => {
      if (inflight?.promise === promise) inflight = null
    })
  inflight = { userId, promise }
  return promise
}
