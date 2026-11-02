import { shallowRef } from 'vue'

/**
 * 网页端图片用的 Emby api_key 持有者（PA-036）。
 *
 * 图片端点加鉴权后，<img> 只能靠 URL 里的 api_key 带凭据；门户用户手里没有
 * Emby 客户端 token，所以后端在账号卡（GET /api/user/emby/server）里下发一把
 * 专用 web token。这里与 emby.ts 的 embyBaseRef 同款口径：响应式引用 +
 * localStorage 持久化，key 一到，拼图片 URL 的 computed 自动重算。
 *
 * PA-054：后端已改成“带回来的 key 够新就复用、不轮换”，但轮换仍可能发生
 * （新设备读卡、key 快过期）；同一浏览器开两个标签页时，A 页写进
 * localStorage 的新 key 必须立刻同步到 B 页内存，否则 B 页新图 401。
 * storage 事件只在“别的标签页”触发，正好补这个口子：收到事件就把
 * ref 同步成新值，模板重算后图片地址自动换上新 key（写本页不触发
 * 事件，写本页的 setEmbyApiKey 已直接改了 ref，不重复）。
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

// PA-054：跨标签页同步——别的标签页写 localStorage 时本页收到 storage
// 事件，把内存 ref 同步成新值（本页自己写不触发事件，不会自循环）。
// key 被清掉（别页登出）时 newValue 为 null，按空串处理，图片随即失凭据。
if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
  window.addEventListener('storage', (event) => {
    if (event.key !== EMBY_API_KEY_STORAGE) return
    const next = event.newValue || ''
    if (next !== embyApiKeyRef.value) embyApiKeyRef.value = next
  })
}

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
