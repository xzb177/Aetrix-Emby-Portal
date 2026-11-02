import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { authApi, tokenStore, type AuthUser } from '@/api'
import { resetEmbyBaseUrl } from '@/api/emby'

export type { AuthUser as User }

export const useUserStore = defineStore('user', () => {
  const user = ref<AuthUser | null>(null)
  const token = ref<string | null>(null)
  const loading = ref(false)
  /**
   * 登录会话序列：login/register/logout 时递增。
   * App.vue 的 KeepAlive 缓存 key 带上它，换号 / 登出 / 重新登录后旧页面实例
   * 不再复用——切 tab 秒回上次数据，但绝不会看到上一个登录态的数据。
   */
  const sessionSeq = ref(0)

  const isLoggedIn = computed(() => !!token.value)
  const isVIP = computed(() => !!user.value?.is_vip)
  /** 付费墙是否开启（后端配置），开启且非会员时播放会被拦截；公益服恒为 false */
  const subscriptionRequired = computed(() => !!user.value?.subscription_required)
  /** 需要被拦截：付费墙开启且当前账号不是会员 */
  const needsSubscription = computed(() => subscriptionRequired.value && !isVIP.value)
  /** 本服是不是公益服（免费开放，不需要会员） */
  const isFreeRealm = computed(() => !!user.value?.is_free_realm)
  /** 公益服的规则文案（付费服为空串） */
  const realmNote = computed(() => user.value?.realm_access_note || '')

  // 从 localStorage 恢复登录态
  function init() {
    const savedToken = localStorage.getItem('access_token')
    const savedUser = localStorage.getItem('user')
    if (savedToken) {
      token.value = savedToken
    }
    if (savedUser) {
      try {
        user.value = JSON.parse(savedUser)
      } catch {
        localStorage.removeItem('user')
      }
    }
  }

  function _persist(response: { access_token: string; refresh_token: string; user: AuthUser }) {
    token.value = response.access_token
    user.value = response.user
    sessionSeq.value += 1
    tokenStore.set(response.access_token, response.refresh_token)
    localStorage.setItem('user', JSON.stringify(response.user))
  }

  async function login(username: string, password: string, captchaToken?: string) {
    loading.value = true
    try {
      // captcha_token：站点开启人机验证时由挂件生成（未开启时后端不校验）
      const response = await authApi.login({ username, password, captcha_token: captchaToken || undefined })
      _persist(response)
      return true
    } finally {
      loading.value = false
    }
  }

  /** TG bot 一键免密登录：用一次性 token 换登录态 */
  async function loginWithTgToken(token: string) {
    loading.value = true
    try {
      const response = await authApi.tgLogin({ token })
      _persist(response)
      return true
    } finally {
      loading.value = false
    }
  }

  async function register(
    username: string,
    password: string,
    email?: string,
    invitationCode?: string,
    captchaToken?: string,
  ) {
    loading.value = true
    try {
      const response = await authApi.register({
        username,
        password,
        email,
        invitation_code: invitationCode || undefined,
        captcha_token: captchaToken || undefined,
      })
      _persist(response)
      return true
    } finally {
      loading.value = false
    }
  }

  async function fetchUser() {
    if (!token.value) return
    loading.value = true
    try {
      const response = await authApi.getCurrentUser()
      user.value = response
      localStorage.setItem('user', JSON.stringify(response))
    } catch (error) {
      // 获取失败（含刷新失败）时清除登录态；拦截器已处理跳转
      console.error('Fetch user failed:', error)
      logout()
      throw error
    } finally {
      loading.value = false
    }
  }

  // PA-048：退出必须先调后端注销口（吊销当前 access 的 jti＋bump token_version
  // 使全部 refresh 失效），不能只清本地——否则偷到的 refresh 可在 30 天内不断换票。
  // 顺序是先发请求、后清本地：请求拦截器从 tokenStore 取 Bearer 发请求，清早了
  // 服务端就定位不到要吊销的会话；注销口失败也继续本地清除，绝不因服务端
  // 失败把用户锁在登录态里。
  async function logout() {
    try {
      await authApi.logout()
    } catch {
      // 注销口失败（离线/500/401）时仍继续本地清除；残余服务端会话窗口
      // 由 access 120 分钟过期兜底，不以此阻塞用户退出。
    }
    user.value = null
    token.value = null
    sessionSeq.value += 1
    tokenStore.clear()
    // 缓存的 EA 地址属于上一个登录态，不清的话同浏览器换号会拿到别人的服地址
    resetEmbyBaseUrl()
    // 旧版佩戴徽章的本地键：已改存账号，清掉防残留串号
    try { localStorage.removeItem('aetrix_worn_badge_v1') } catch { /* 隐私模式忽略 */ }
  }

  function updateUser(userData: Partial<AuthUser>) {
    if (user.value) {
      user.value = { ...user.value, ...userData }
      localStorage.setItem('user', JSON.stringify(user.value))
    }
  }

  return {
    user,
    token,
    loading,
    sessionSeq,
    isLoggedIn,
    isVIP,
    subscriptionRequired,
    needsSubscription,
    isFreeRealm,
    realmNote,
    init,
    login,
    loginWithTgToken,
    register,
    fetchUser,
    logout,
    updateUser,
  }
})
