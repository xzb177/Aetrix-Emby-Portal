/**
 * 站点公开 API（门户后端 /api/site/*，无需鉴权）。
 */
import api from '@/api'

/** 登录页海报拼贴条目 */
export interface LoginPoster {
  id: string
  name: string
}

/** 登录页海报拼贴响应：条目 + EA 海报直链基地址 */
export interface LoginPostersResponse {
  items: LoginPoster[]
  /** 与 /api/user/emby/server 的 base_url 同源；为空时前端不渲染拼贴 */
  embyBaseUrl: string
}

/**
 * 获取登录页海报拼贴数据（公开接口）。
 * 5s 超时，失败返回空响应（前端降级为现有背景，不阻塞表单）。
 */
export async function fetchLoginPosters(): Promise<LoginPostersResponse> {
  const empty: LoginPostersResponse = { items: [], embyBaseUrl: '' }
  try {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), 5000)
    try {
      const res = await api.get<never, { items?: LoginPoster[]; emby_base_url?: string }>(
        '/api/site/login-posters',
        { signal: controller.signal },
      )
      const items = res?.items
      const embyBaseUrl = typeof res?.emby_base_url === 'string' ? res.emby_base_url : ''
      return {
        items: Array.isArray(items) ? items : [],
        embyBaseUrl,
      }
    } finally {
      clearTimeout(timer)
    }
  } catch {
    // 拿不到海报时静默降级：登录页回退到现有网格+底色背景
    return empty
  }
}

/**
 * 获取登录页 TG 登录入口的机器人用户名（公开接口）。
 * 5s 超时，失败/未配置一律静默返回空串：登录页据此不渲染入口，不阻塞表单。
 */
export async function fetchTgBotUsername(): Promise<string> {
  try {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), 5000)
    try {
      const res = await api.get<never, { bot_username?: string }>(
        '/api/site/tg-login-entry',
        { signal: controller.signal },
      )
      const name = res?.bot_username
      return typeof name === 'string' ? name.trim() : ''
    } finally {
      clearTimeout(timer)
    }
  } catch {
    return ''
  }
}
