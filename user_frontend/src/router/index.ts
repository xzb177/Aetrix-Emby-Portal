import { createRouter, createWebHistory } from 'vue-router'
import { useUserStore } from '@/stores/user'
// 标题里的站名来自「站点与品牌」能力（未配置时用默认值）
import { pageTitle } from '@/composables/useBranding'
// v4 面板特性开关（VITE_PANEL_V4_ENABLED=0 时回退老首页）
import { isPanelV4Enabled } from '@/composables/usePanelApi'
// chunk 加载失败兜底的提示：useToast 不支持 action/点击回调，
// 重试按钮用原生 DOM 挂一个小横条（只用 --au-* 现有 token）
import { useToast } from '@/composables/useToast'

const panelV4 = isPanelV4Enabled()

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  // R28：处理站内 hash 跳转（如信鸽兑换通知 → /shop#shop-redeem 的核销区滚动）
  scrollBehavior(to, _from, savedPosition) {
    if (savedPosition) return savedPosition
    if (to.hash) {
      const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches
      return { el: to.hash, behavior: reduce ? 'auto' : 'smooth' }
    }
    return undefined
  },
  routes: [
    {
      path: '/',
      name: 'home',
      // 首页即发现页（旧 HomeView 已删除，不再回退）
      component: () => import('@/views/panel/DiscoveryView.vue'),
      meta: { title: '发现' },
    },
    // v4 面板：收藏家 / 服务器（需要登录，panel 接口全部要鉴权）
    ...(panelV4
      ? [
          {
            path: '/collector',
            name: 'collector',
            component: () => import('@/views/panel/CollectorView.vue'),
            meta: { title: '收藏家', requiresAuth: true },
          },
          // 我的收藏：收藏列表页（老 /favorites 地址复活承接；入口在账号设置，底栏不占位）
          {
            path: '/favorites',
            name: 'favorites',
            component: () => import('@/views/panel/FavoritesView.vue'),
            meta: { title: '我的收藏', requiresAuth: true },
          },
          // 合集：货架页 + 详情页（只读用户口；不在底栏占位，入口在发现页合集区段）
          {
            path: '/collections',
            name: 'collections',
            component: () => import('@/views/panel/CollectionShelfView.vue'),
            meta: { title: '合集', requiresAuth: true },
          },
          {
            path: '/collections/:id',
            name: 'collection-detail',
            component: () => import('@/views/panel/CollectionView.vue'),
            meta: { title: '合集', requiresAuth: true },
          },
          {
            path: '/server',
            name: 'server',
            component: () => import('@/views/panel/ServerView.vue'),
            meta: { title: '服务器', requiresAuth: true },
          },
          {
            path: '/shop',
            name: 'shop',
            component: () => import('@/views/panel/ShopView.vue'),
            meta: { title: '补给站', requiresAuth: true },
          },
          // v4 游戏化 Hub：探险日志统一入口（需要登录）
          {
            path: '/game',
            name: 'game-hub',
            component: () => import('@/views/game/GameHubView.vue'),
            meta: { title: '探险日志', requiresAuth: true },
          },
          // v4 游戏化第一批：营地设施（需要登录）
          {
            path: '/game/checkin',
            name: 'game-checkin',
            component: () => import('@/views/game/CheckinView.vue'),
            meta: { title: '每日报到', requiresAuth: true },
          },
          {
            path: '/game/calendar',
            name: 'game-calendar',
            component: () => import('@/views/game/CalendarView.vue'),
            meta: { title: '探险预告', requiresAuth: true },
          },
          {
            path: '/game/wishlist',
            name: 'game-wishlist',
            component: () => import('@/views/game/WishlistView.vue'),
            meta: { title: '许愿池', requiresAuth: true },
          },
          // v4 游戏化第二批：营地设施（需要登录）
          {
            path: '/game/signal',
            name: 'game-signal',
            component: () => import('@/views/game/SignalView.vue'),
            meta: { title: '求救信号', requiresAuth: true },
          },
          {
            path: '/game/pigeon',
            name: 'game-pigeon',
            component: () => import('@/views/game/PigeonView.vue'),
            meta: { title: '信鸽', requiresAuth: true },
          },
          {
            path: '/game/ledger',
            name: 'game-ledger',
            component: () => import('@/views/game/LedgerView.vue'),
            meta: { title: '能量账本', requiresAuth: true },
          },
          {
            path: '/game/supply',
            name: 'game-supply',
            component: () => import('@/views/game/SupplyLogView.vue'),
            meta: { title: '补给记录', requiresAuth: true },
          },
          // v4 游戏化第三批：营地设施（需要登录）
          {
            path: '/game/companion',
            name: 'game-companion',
            component: () => import('@/views/game/CompanionView.vue'),
            meta: { title: '结伴同行', requiresAuth: true },
          },
          {
            path: '/game/guide',
            name: 'game-guide',
            component: () => import('@/views/game/GuideView.vue'),
            meta: { title: '向导', requiresAuth: true },
          },
          {
            path: '/game/outfit',
            name: 'game-outfit',
            component: () => import('@/views/game/OutfitView.vue'),
            meta: { title: '换装', requiresAuth: true },
          },
          {
            path: '/game/operator',
            name: 'game-operator',
            component: () => import('@/views/game/OperatorView.vue'),
            meta: { title: '接线员', requiresAuth: true },
          },
          // v4 游戏化：账号设置设施（旧 ProfileView 的 P0 功能承接：改密码 / 我的订阅 / 我的设备）
          {
            path: '/game/account',
            name: 'game-account',
            component: () => import('@/views/game/AccountSettingsView.vue'),
            meta: { title: '账号设置', requiresAuth: true },
          },
        ]
      : []),
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

    // ==================== 经济系统（v2.3.0；v2.11.0 商店/钱包拆分；2026-10-13 商店统一：/store 301 到 /shop 补给站） ====================
    {
      path: '/store',
      redirect: '/shop',
    },
    // 注册后 TG 绑定引导页：仅注册流程进入，未绑定且管理员开启时展示
    {
      path: '/tg-bind',
      name: 'tg-bind-guide',
      component: () => import('@/views/TgBindGuideView.vue'),
      meta: { title: '绑定 Telegram', requiresAuth: true },
    },
    // 想看商店：独立页 /want（/store 已 301 到补给站，不能复用）。
    // 访客可浏览查片（后端目录与详情只读），想看写口与清单要登录 + 会员，
    // 所以这里不挂 requiresAuth，页内按 401/403 给登录与会员提示。
    {
      path: '/want',
      name: 'want-store',
      component: () => import('@/views/panel/StoreView.vue'),
      meta: { title: '想看商店' },
    },
    // 求片登记：独立页 /store-requests（R1 前端接线批）。
    // 登记与榜单三个口都要登录 + 会员，路由守卫先拦未登录，页内再按 403 给会员提示。
    // 不挂首页/发现页、不占底栏；本页不放任何播放入口。
    {
      path: '/store-requests',
      name: 'store-requests',
      component: () => import('@/views/panel/StoreRequestsView.vue'),
      meta: { title: '求片登记', requiresAuth: true },
    },
    // 活动提案：独立页 /activities（等级治理用户端补完批）。
    // 仅公益服开放，页内按等级给门槛提示（Lv.2+ 提案、Lv.5+ 直接发起），低等级不画饼。
    // 不挂首页/发现页、不占底栏；入口在补给站公益服等级区治理权表。
    {
      path: '/activities',
      name: 'activities',
      component: () => import('@/views/panel/ActivityProposalView.vue'),
      meta: { title: '活动提案', requiresAuth: true },
    },
    // 找源：独立页 /source-discovery（找源前端挂接批）。
    // 搜索与确认两个口都要登录，路由守卫先拦未登录；总闸关时页内明示未开启。
    // 不挂首页/发现页、不占底栏；候选绝不挂播放入口（playable_now 恒 false）。
    {
      path: '/source-discovery',
      name: 'source-discovery',
      component: () => import('@/views/panel/SourceDiscoveryView.vue'),
      meta: { title: '找源', requiresAuth: true },
    },
    // 隐私政策：公开页（Google OAuth 验证要求挂在本站域名下），
    // 不带 requiresAuth，路由守卫只拦 requiresAuth 路由，未登录也能直接打开。
    {
      path: '/privacy',
      name: 'privacy',
      component: () => import('@/views/PrivacyView.vue'),
      meta: { title: '隐私政策' },
    },
    // 追新日历（2026-10-13 旧页面清理：已删，走 /game/calendar 探险预告）
    // R28：旧 /calendar 书签重定向到新地址，避免落 404
    {
      path: '/calendar',
      redirect: '/game/calendar',
    },
    // ==================== 媒体详情与播放（v2.42.5：媒体库/搜索浏览页已删；
    // 2026-10-13 旧页面清理：/profile、/media/:id 两路由已删） ====================
    // 老地址 /profile、/media/:id、/media、/library、/search、/history
    // 的书签与外部链接：媒体浏览统一走第三方客户端（Infuse 等），这些入口不再有对应页面，
    // 全部落到兜底 404 页（页面里会给「回首页」的出口）。
    // 注意：/profile 的承接入口是 /game/account（账号设置设施）。
    // 302 Next 搜索页（MVP 非商店部分）：独立页，不复活已废弃的 /search，
    // 不改底栏、不碰首页/发现页、不挂想看按钮（想看是商店那单的 B1）。
    {
      path: '/search-next',
      name: 'search-next',
      component: () => import('@/views/panel/SearchNextView.vue'),
      meta: { title: '搜索', requiresAuth: true },
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

  // 分段页标题由 LibraryHomeView 按 ?tab= 覆写（媒体库 / 收藏 / 观看记录）
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

// v4 面板第一版只做纸色浅色：在 v4 路由下强制 light，离开时恢复
// （v4 视图自带 .v4-scope 纸色变量，这里只处理 html data-theme 与浏览器工具栏色）
export const V4_PATHS = new Set(['/', '/collector', '/server', '/shop', '/want', '/store-requests', '/activities', '/source-discovery', '/game', '/game/checkin', '/game/calendar', '/game/wishlist', '/game/signal', '/game/pigeon', '/game/ledger', '/game/supply', '/game/companion', '/game/guide', '/game/outfit', '/game/account'])
// 底部坞由 App.vue 全局常驻渲染（PanelTabbar，≤768px 唯一一套，
// 桌面 ≥769px 由 CSS 隐藏）；各视图模板不再自带 <PanelTabbar/>。
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
  if (!panelV4) return
  // 尾斜杠规范化：/game/ 与 /game 是同一页；根路径 '/' 去掉斜杠会变空串，要兜回 '/'
  const v4Path = to.path.replace(/\/+$/, '') || '/'
  if (V4_PATHS.has(v4Path)) {
    document.documentElement.setAttribute('data-theme', 'light')
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', '#f5f1ea')
  } else {
    restoreUserTheme()
  }
})

// SPA 部署竞态兜底（2026-10-13 实锤：#788 部署把全部 chunk 换了新哈希，
// 用户旧构建点 /game 时懒加载 GameHubView 旧 chunk → 404 → 页面打不开）。
// 动态 import 失败（chunk 已被新部署替换）时自动刷新拿新版 index.html 后恢复。
// 计数放 sessionStorage（跨 reload 持久）：连续失败最多刷新 2 次就停，
// 杜绝 index.html 被缓存时的死循环；每次导航成功清零，下次部署有新预算。
const CHUNK_RELOAD_KEY = 'spa-chunk-reload-count'
// chunk 加载失败：预算耗尽后用户停在旧页/空白页，挂一个可点的重试横条。
// 点「重试」清掉重载计数后 location.reload()；toast 负责文案提示。
const CHUNK_RETRY_BAR_ID = 'spa-chunk-retry'
function showChunkRetryBar() {
  if (typeof document === 'undefined' || document.getElementById(CHUNK_RETRY_BAR_ID)) return
  const bar = document.createElement('div')
  bar.id = CHUNK_RETRY_BAR_ID
  bar.setAttribute('role', 'alert')
  bar.style.cssText = [
    'position:fixed', 'top:12px', 'left:50%', 'transform:translateX(-50%)',
    'z-index:99999', 'display:flex', 'align-items:center', 'gap:12px',
    'padding:10px 14px', 'border-radius:12px',
    'background:var(--au-bg-soft,#11100e)',
    'border:1px solid var(--au-danger-border,rgba(217,119,107,.28))',
    'color:var(--au-text,#f3ede4)', 'font-size:13px',
    'box-shadow:0 8px 32px rgba(0,0,0,.6)',
  ].join(';')
  const tip = document.createElement('span')
  tip.textContent = '页面没加载出来'
  const btn = document.createElement('button')
  btn.type = 'button'
  btn.textContent = '重试'
  btn.style.cssText = [
    'padding:6px 16px', 'border-radius:8px', 'cursor:pointer',
    'font-size:13px', 'font-weight:700',
    'border:1px solid var(--au-danger,#d9776b)',
    'background:var(--au-danger,#d9776b)', 'color:#fff',
  ].join(';')
  btn.addEventListener('click', () => {
    try { sessionStorage.removeItem(CHUNK_RELOAD_KEY) } catch { /* 隐私模式忽略 */ }
    window.location.reload()
  })
  bar.append(tip, btn)
  document.body.appendChild(bar)
}
router.onError((err: unknown) => {
  const msg = String((err as Error)?.message ?? err ?? '')
  if (
    /Failed to fetch dynamically imported module|Importing a module script failed|Loading chunk [\w-]+ failed|ChunkLoadError/i.test(msg)
  ) {
    let n = 0
    try {
      n = Number(sessionStorage.getItem(CHUNK_RELOAD_KEY) ?? 0) || 0
    } catch { /* 隐私模式忽略 */ }
    if (n < 2) {
      try {
        sessionStorage.setItem(CHUNK_RELOAD_KEY, String(n + 1))
      } catch { /* 隐私模式忽略 */ }
      window.location.reload()
    } else {
      // 预算耗尽：不再静默停留，给用户一个手动重试的出口
      useToast().error('页面没加载出来，点重试重新加载', 10000)
      showChunkRetryBar()
    }
  }
})
router.afterEach(() => {
  try {
    sessionStorage.removeItem(CHUNK_RELOAD_KEY)
  } catch { /* 隐私模式忽略 */ }
  // 导航成功：收掉重试横条
  document.getElementById(CHUNK_RETRY_BAR_ID)?.remove()
})

// 关键路由预取：用户登录后大概率会去补给站（v4 /shop）/ 账号设置（/game/account），空闲时预加载对应 chunk
//（2026-10-13 旧页面清理：ProfileView 已删，改预取 AccountSettingsView）
if (typeof requestIdleCallback !== 'undefined') {
  requestIdleCallback(() => {
    import('@/views/panel/ShopView.vue')
    import('@/views/game/AccountSettingsView.vue')
  }, { timeout: 3000 })
}

export default router
