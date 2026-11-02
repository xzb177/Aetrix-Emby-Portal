import { shallowRef } from 'vue'

/**
 * 网页端图片用的 Emby api_key 持有者（PA-036）。
 *
 * 图片端点加鉴权后，<img> 只能靠 URL 里的 api_key 带凭据；门户用户手里没有
 * Emby 客户端 token，所以后端在账号卡（GET /api/user/emby/server）里下发一把
 * 专用 web token（每次读卡轮换）。这里与 emby.ts 的 embyBaseRef 同款口径：
 * 响应式引用 + localStorage 持久化，key 一到，拼图片 URL 的 computed 自动重算。
 *
 * 独立成模块是因为 api/index.ts 与 api/emby.ts 互相引用：账号卡的两条读取
 * 路径（index 的 getAccountCard、emby 的 fetchEmbyBase）都要往这里同步，
 * 放谁那边都会成环。
 */
const EMBY_API_KEY_STORAGE = 'emby_api_key'

function readStoredKey(): string {
  try {
    return localStorage.getItem(EMBY_API_KEY_STORAGE) || ''
  } catch {
    return ''
  }
}

export const embyApiKeyRef = shallowRef<string>(readStoredKey())

export function setEmbyApiKey(key: string): void {
  embyApiKeyRef.value = key || ''
  try {
    if (embyApiKeyRef.value) {
      localStorage.setItem(EMBY_API_KEY_STORAGE, embyApiKeyRef.value)
    } else {
      localStorage.removeItem(EMBY_API_KEY_STORAGE)
    }
  } catch {
    /* 隐私模式下写不进去无所谓，内存里还有 */
  }
}

export function getEmbyApiKey(): string {
  return embyApiKeyRef.value
}

/** 给一条图片地址补 api_key 查询参数（已有 api_key 或手里没 key 时原样返回） */
export function withImageKey(url: string): string {
  const key = embyApiKeyRef.value
  if (!url || !key || url.includes('api_key=')) return url
  const sep = url.includes('?') ? '&' : '?'
  return `${url}${sep}api_key=${encodeURIComponent(key)}`
}
