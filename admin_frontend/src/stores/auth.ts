import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import axios from 'axios'
import { ADMIN_KEY, TOKEN_KEY } from '@/utils/request'
import type { AdminInfo } from '@/types'

/** 用户端（门户）在同一源上保存的会话键——两端同源、同一套 JWT，可直接复用 */
const PORTAL_TOKEN_KEY = 'access_token'
const PORTAL_REFRESH_KEY = 'refresh_token'
/** 主动退出后台后的「不再免登」标记（仅本标签页有效，关掉页面即失效） */
const SKIP_SSO_KEY = 'admin_skip_sso'

interface ProbeResult {
  info: AdminInfo | null
  status: number
  token: string
}

/**
 * 本地票据是否已过期（读 JWT 的 exp）
 *
 * 以前只看「本地有没有 token」：管理员上次的票据过期后进后台，会被当成已登录，
 * 先打一串必然 401 的请求、弹一句「无效或已过期的凭证」，才被送去登录页重新免登——
 * 用户看到的就是「进来先报两次错」。这里提前判掉，直接走免登流程。
 *
 * 解析不出来（不是 JWT）时不猜，交给后端判定。
 */
function tokenExpired(raw: string): boolean {
  const payload = raw.split('.')[1]
  if (!payload) return true
  try {
    const base64 = payload.replace(/-/g, '+').replace(/_/g, '/')
    const padded = base64.padEnd(base64.length + ((4 - (base64.length % 4)) % 4), '=')
    const exp = Number(JSON.parse(atob(padded))?.exp)
    if (!Number.isFinite(exp)) return false
    // 5 秒余量：刚好卡在过期瞬间的请求不再白发一次
    return exp * 1000 <= Date.now() + 5000
  } catch {
    return false
  }
}

/** 清掉本地后台会话（不动门户会话：免登还要用它）*/
function clearStoredSession(): void {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(ADMIN_KEY)
}

/** 启动时读本地会话：已过期的票据直接丢掉，免得先打一串必然 401 的请求 */
function restoreToken(): string | null {
  const raw = localStorage.getItem(TOKEN_KEY)
  if (!raw) return null
  if (tokenExpired(raw)) {
    clearStoredSession()
    return null
  }
  return raw
}

/** 用给定 token 探测管理端身份；失败只回状态码，不抛异常 */
async function probeAdmin(token: string): Promise<ProbeResult> {
  try {
    const res = await axios.get<AdminInfo>('/api/admin/auth/me', {
      headers: { Authorization: `Bearer ${token}` },
      timeout: 10000,
    })
    return { info: res.data, status: 200, token }
  } catch (e) {
    const status = axios.isAxiosError(e) ? e.response?.status ?? 0 : 0
    return { info: null, status, token }
  }
}

/**
 * 门户 access token 过期时用门户 refresh token 换一次新的
 *
 * 轮换后的新票据同时写回门户的存储键，用户端与本后台都不会掉线（JWT 无服务端撤销，
 * 以最后一次写入为准）。
 */
async function refreshPortalSession(): Promise<string | null> {
  const refresh = localStorage.getItem(PORTAL_REFRESH_KEY)
  if (!refresh) return null
  try {
    const res = await axios.post(
      '/api/user/auth/refresh',
      { refresh_token: refresh },
      { timeout: 10000 },
    )
    const access: string | undefined = res.data?.access_token
    if (!access) return null
    localStorage.setItem(PORTAL_TOKEN_KEY, access)
    if (res.data?.refresh_token) localStorage.setItem(PORTAL_REFRESH_KEY, res.data.refresh_token)
    return access
  } catch {
    return null
  }
}

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string | null>(restoreToken())
  const admin = ref<AdminInfo | null>(token.value ? restoreAdmin() : null)
  /** 免登探测只做一次（失败后不再反复打接口） */
  const ssoChecked = ref(false)
  /** 免登没成功的原因，用于登录页给出可操作提示 */
  const ssoNotice = ref('')
  /** 本次页面加载是否已把会话与服务端核对过（核对前本地票据不算「可信」） */
  const verified = ref(false)

  // 会话可能在页面开着的时候过期：每次判定都重新看 exp，过期即视为未登录（路由守卫会走免登）
  const isAuthenticated = computed(() => !!token.value && !tokenExpired(token.value))

  function restoreAdmin(): AdminInfo | null {
    try {
      const raw = localStorage.getItem(ADMIN_KEY)
      return raw ? (JSON.parse(raw) as AdminInfo) : null
    } catch {
      return null
    }
  }

  function setSession(newToken: string, info: AdminInfo) {
    token.value = newToken
    admin.value = info
    // 票据要么来自刚通过的探测、要么来自刚成功的登录，都已经是服务端认可过的
    verified.value = true
    localStorage.setItem(TOKEN_KEY, newToken)
    localStorage.setItem(ADMIN_KEY, JSON.stringify(info))
  }

  /**
   * 退出登录：先同步清本地（调用方不 await 也能立即生效），
   * 再最佳努力通知服务端吊销当前票据（P0-1 安全修复），不阻塞退出。
   *
   * 注意这里不动门户会话（免登还要用它），只吊销后台票据。
   */
  function logout() {
    const current = token.value
    token.value = null
    admin.value = null
    verified.value = false
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(ADMIN_KEY)
    // 主动退出后必须真的退出：本标签页内不再用门户会话自动免登（否则会被立刻「登回去」）
    sessionStorage.setItem(SKIP_SSO_KEY, '1')
    // 服务端吊销是最佳努力：失败也不影响本地已退出
    if (current) {
      import('@/api/admin')
        .then(({ adminLogout }) => adminLogout())
        .catch(() => {})
    }
  }

  /**
   * 门户免登：管理员在用户端登录后进管理后台，不再要求二次输入账号
   *
   * 管理后台与用户端同源，且共用同一套 JWT（`get_current_admin` 只认 `is_staff` 的 WebUser），
   * 所以门户的 access_token 本身就能通过 `/api/admin/auth/me`。这里静默探测一次：
   * - 通过 → 直接接管该会话（写入本后台的 token，后续请求照旧）
   * - 门户 token 已过期 → 用门户 refresh token 换新后再试
   * - 门户未登录 / 账号不是管理员 → 留在登录页并给出对应提示
   *
   * 探测走裸 axios：不能复用 utils/request，否则 401/403 会触发跳转与全局报错提示。
   */
  async function ssoFromPortal(): Promise<boolean> {
    if (ssoChecked.value) return isAuthenticated.value
    // 页面开着的时候票据过期了：这里也要当「没登录」处理，否则会把用户卡在登录页不再免登
    if (token.value && !tokenExpired(token.value)) return true
    if (token.value) {
      clearStoredSession()
      token.value = null
      admin.value = null
    }
    if (sessionStorage.getItem(SKIP_SSO_KEY)) {
      ssoChecked.value = true
      return false
    }
    const portalToken = localStorage.getItem(PORTAL_TOKEN_KEY)
    if (!portalToken) return false // 门户未登录：直接展示登录页，不发无谓请求
    ssoChecked.value = true

    let probe = await probeAdmin(portalToken)
    if (probe.status === 401) {
      const refreshed = await refreshPortalSession()
      if (refreshed) probe = await probeAdmin(refreshed)
    }

    if (probe.info) {
      setSession(probe.token, probe.info)
      ssoNotice.value = ''
      return true
    }

    ssoNotice.value =
      probe.status === 403
        ? '当前门户账号没有管理员权限，请使用管理员账号登录'
        : '门户登录状态已失效，请重新登录'
    return false
  }

  /**
   * 进入后台前把会话**一次性敲定**：本地票据 → 服务端核对 → 门户免登
   *
   * 为什么不能只看「本地有没有 token」：本地那张票可能是上一轮留下来的（换了 SECRET_KEY、
   * 账号被回收、或干脆是另一个会话的），它没过期但服务端已经不认。以前这种票会被当成
   * 「已登录」：页面先渲染一遍 → 8 个请求一起 401 → 弹错 + 重载 → 才免登回来，
   * 用户看到的就是「点进后台刷新了两次」。现在把核对放在首屏渲染之前，代价只有一次
   * `/api/admin/auth/me`，换来进入后台只有**一次**跳转、不再闪错。
   */
  async function ensureSession(): Promise<boolean> {
    if (verified.value) return isAuthenticated.value

    if (token.value && !tokenExpired(token.value)) {
      const probe = await probeAdmin(token.value)
      if (probe.info) {
        setSession(probe.token, probe.info)
        return true
      }
      // 过期 / 被回收 / 换了密钥：本地这份不再可信，清掉后走门户免登
      clearStoredSession()
      token.value = null
      admin.value = null
      if (probe.status === 403) {
        // 身份已经确认、但不具备管理员权限：这张票就是当前登录人本人，再探一次只会得到同样结果
        ssoNotice.value = '当前账号没有管理员权限，请使用管理员账号登录'
        return false
      }
    }

    return ssoFromPortal()
  }

  return {
    token,
    admin,
    ssoNotice,
    isAuthenticated,
    setSession,
    logout,
    ensureSession,
    ssoFromPortal,
  }
})
