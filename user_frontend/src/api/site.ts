/**
 * 站点公开 API（门户后端 /api/site/*，无需鉴权）。
 */
import api from '@/api'

/** 登录页海报拼贴条目 */
export interface LoginPoster {
  id: string
  name: string
}

/**
 * 获取登录页海报拼贴数据（公开接口）。
 * 5s 超时，失败返回空数组（前端降级为现有背景，不阻塞表单）。
 */
export async function fetchLoginPosters(): Promise<LoginPoster[]> {
  try {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), 5000)
    try {
      const res = await api.get<never, { items?: LoginPoster[] }>('/api/site/login-posters', {
        signal: controller.signal,
      })
      const items = res?.items
      return Array.isArray(items) ? items : []
    } finally {
      clearTimeout(timer)
    }
  } catch {
    // 拿不到海报时静默降级：登录页回退到现有网格+底色背景
    return []
  }
}
