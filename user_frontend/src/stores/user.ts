import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { authApi, tokenStore, type AuthUser } from '@/api'
import { resetEmbyBaseUrl, refreshEmbyBaseUrl } from '@/api/emby'

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
    // PA-036：登录后立刻拉一次账号卡，把图片 api_key 落地——否则首屏海报
    // 要等别的页面顺带读卡才有凭据（App 启动预取在未登录时是跳过的）
    refreshEmbyBaseUrl()
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

  function logout() {
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
