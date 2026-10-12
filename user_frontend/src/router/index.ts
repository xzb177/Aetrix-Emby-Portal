import { createRouter, createWebHistory } from 'vue-router'
import { useUserStore } from '@/stores/user'
// 标题里的站名来自「站点与品牌」能力（未配置时用默认值）
import { pageTitle } from '@/composables/useBranding'

// v4 探险家日志面板是唯一的用户端：发现 / 收藏家 / 服务器。
// 登录相关（/login、/tg-login、/tg-bind）保留；管理后台是另一套（admin_frontend），不在这里。
const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/',
      name: 'home',
      component: () => import('@/views/panel/DiscoveryView.vue'),
      meta: { title: '发现' },
    },
    {
      path: '/collector',
      name: 'collector',
      component: () => import('@/views/panel/CollectorView.vue'),
      meta: { title: '收藏家', requiresAuth: true },
    },
    {
      path: '/server',
      name: 'server',
      component: () => import('@/views/panel/ServerView.vue'),
      meta: { title: '服务器', requiresAuth: true },
    },
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginView.vue'),
      meta: { title: '登录' },
    },
    // TG 一键登录：后端 tg_bot/login_token.py 生成 {site_base_url}/tg-login?token=xxx，
    // 路由缺失会导致 404（2026-10-10 审计发现）
    {
      path: '/tg-login',
      name: 'tg-login',
      component: () => import('@/views/TgLoginView.vue'),
      meta: { title: 'Telegram 快捷登录' },
    },
    // 注册后 TG 绑定引导页：仅注册流程进入，未绑定且管理员开启时展示
    {
      path: '/tg-bind',
      name: 'tg-bind-guide',
      component: () => import('@/views/TgBindGuideView.vue'),
      meta: { title: '绑定 Telegram', requiresAuth: true },
    },
    {
      path: '/:pathMatch(.*)*',
      name: 'not-found',
      component: () => import('@/views/NotFoundView.vue'),
      meta: { title: '页面未找到' },
    },
  ],
})

// 路由守卫
let initialized = false

router.beforeEach(async (to) => {
  const userStore = useUserStore()

  if (!initialized) {
    userStore.init()
    initialized = true
  }

  if (to.meta.title) {
    document.title = pageTitle(to.meta.title as string | undefined)
  }

  // 未登录访问受保护页面 → 跳转登录页并记录回跳地址
  if (to.meta.requiresAuth) {
    const hasToken = localStorage.getItem('access_token')
    if (!hasToken && !userStore.isLoggedIn) {
      return { name: 'login', query: { redirect: to.fullPath } }
    }
    // 有 token 但没有用户信息（刷新页面），拉取一次
    if (!userStore.user) {
      try {
        await userStore.fetchUser()
      } catch {
        return { name: 'login', query: { redirect: to.fullPath } }
      }
    }
  }

  // 已登录访问登录页 → 回首页
  if (to.name === 'login' && userStore.isLoggedIn) {
    return { name: 'home' }
  }

  return true
})

// v4 面板只做纸色浅色：在 v4 路由下强制 light，离开时恢复用户主题
// （v4 视图自带 .v4-scope 纸色变量，这里只处理 html data-theme 与浏览器工具栏色）
const V4_PATHS = new Set(['/', '/collector', '/server'])
function restoreUserTheme() {
  // 从 useTheme.ts 同 key 读用户偏好（默认 dark），恢复 data-theme
  let pref = 'dark'
  try {
    const s = localStorage.getItem('aetrix-theme')
    if (s === 'light' || s === 'dark' || s === 'system') pref = s
  } catch { /* ignore */ }
  const html = document.documentElement
  const light =
    pref === 'light' ||
    (pref === 'system' &&
      typeof window !== 'undefined' &&
      window.matchMedia?.('(prefers-color-scheme: light)').matches)
  if (light) html.setAttribute('data-theme', 'light')
  else html.removeAttribute('data-theme')
  // 浏览器工具栏色跟回用户主题（与 useTheme.ts applyTheme 一致）
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute('content', light ? '#f5f1ea' : '#0c0a09')
}
router.afterEach((to) => {
  if (V4_PATHS.has(to.path)) {
    document.documentElement.setAttribute('data-theme', 'light')
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', '#f5f1ea')
  } else {
    restoreUserTheme()
  }
})

export default router
