import { createRouter, createWebHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { adminTitle } from '@/composables/branding'
import { setupStatus } from '@/api/admin'

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'Login',
    component: () => import('@/views/Login.vue'),
    meta: { title: '管理员登录', requiresAuth: false },
  },
  {
    // 首次运行向导：setup_completed 为 false 时进这里建第一个管理员，
    // 完成后入口永久关闭（守卫见下方 beforeEach）
    path: '/setup',
    name: 'Setup',
    component: () => import('@/views/Setup.vue'),
    meta: { title: '初始化向导', requiresAuth: false },
  },
  {
    path: '/',
    component: () => import('@/views/Layout.vue'),
    meta: { requiresAuth: true },
    children: [
      // 页面标题与侧边栏分组保持一致（v2.25.0 按交付链重组信息架构）
      { path: '', name: 'Dashboard', component: () => import('@/views/Dashboard.vue'), meta: { title: '仪表盘' } },
      { path: 'users', name: 'Users', component: () => import('@/views/Users.vue'), meta: { title: '用户' } },
      { path: 'subscriptions', name: 'Subscriptions', component: () => import('@/views/Subscriptions.vue'), meta: { title: '订阅与权益' } },
      { path: 'goods', name: 'Goods', component: () => import('@/views/Goods.vue'), meta: { title: '商品管理' } },
      { path: 'orders', name: 'Orders', component: () => import('@/views/Orders.vue'), meta: { title: '运营·订单' } },
      // 会员卡码 / 兑换码已并入「码管理」（Codes.vue 两个标签页）：旧地址跳到对应标签，收藏不会落到仪表盘
      { path: 'exchange-codes', redirect: { path: '/codes', query: { tab: 'exchange' } } },
      { path: 'registration-codes', redirect: { path: '/codes', query: { tab: 'reg' } } },
      // 优惠券（v2.10.0）：与兑换码分工不同——兑换码不花钱拿东西，优惠券是付费时抵扣
      { path: 'coupons', name: 'Coupons', component: () => import('@/views/Coupons.vue'), meta: { title: '运营·优惠券' } },
      { path: 'invitations', name: 'Invitations', component: () => import('@/views/Invitations.vue'), meta: { title: '运营·邀请与积分' } },
      { path: 'codes', name: 'Codes', component: () => import('@/views/Codes.vue'), meta: { title: '码管理' } },
      { path: 'devices', name: 'Devices', component: () => import('@/views/Devices.vue'), meta: { title: '设备与安全' } },
      { path: 'login-logs', name: 'LoginLogs', component: () => import('@/views/LoginLogs.vue'), meta: { title: '登录日志' } },
      // v2.43.0：防共享（跨城市轨迹 + 同播检测）。默认关闭，「处置」档仅超管可开。
      { path: 'share-guard', name: 'ShareGuard', component: () => import('@/views/ShareGuard.vue'), meta: { title: '防共享' } },
      // 访问拦截：UA 关键词 + IP 归属地。全站级开关，默认全关，仅超管可改。
      { path: 'access-guard', name: 'AccessGuard', component: () => import('@/views/AccessGuard.vue'), meta: { title: '访问拦截' } },
      { path: 'announcements', name: 'Announcements', component: () => import('@/views/Announcements.vue'), meta: { title: '公告管理' } },
      // 探险世界 · 到此一游留言墙管理
      { path: 'wall-posts', name: 'WallPosts', component: () => import('@/views/WallPosts.vue'), meta: { title: '留言墙管理' } },
      { path: 'tickets', name: 'Tickets', component: () => import('@/views/Tickets.vue'), meta: { title: '工单' } },
      { path: 'media-seek', name: 'MediaSeek', component: () => import('@/views/MediaSeek.vue'), meta: { title: '求片管理' } },
      // 公益服：抽奖配置 / 积分配置（v2.55 公益用户页已并入用户管理，求片审核页已合并）
      // 求片审核已并入「求片管理」（MediaSeek.vue），路由移除
      { path: 'welfare-lottery-rounds', name: 'WelfareLotteryRounds', component: () => import('@/views/WelfareLotteryRounds.vue'), meta: { title: '公益服·群抽奖' } },
      // 旧抽奖页（WelfareLottery.vue）/ 公益用户 / 求片审核已删除或合并：旧地址跳到新页
      { path: 'welfare-lottery', redirect: '/welfare-lottery-rounds' },
      { path: 'welfare-users', redirect: '/users' },
      { path: 'welfare-requests', redirect: '/media-seek' },
      { path: 'welfare-points', name: 'WelfarePoints', component: () => import('@/views/WelfarePoints.vue'), meta: { title: '公益服·积分配置' } },
      // 会员等级（P1 统一货币体系）：运营中心 → 会员等级
      { path: 'member-levels', name: 'MemberLevels', component: () => import('@/views/MemberLevels.vue'), meta: { title: '会员等级' } },
      { path: 'emby', name: 'EmbyAdmin', component: () => import('@/views/EmbyAdmin.vue'), meta: { title: '媒体库' } },
      // 媒体库可见范围（v2.43.0）：服务器默认范围 + 指定用户单独覆盖，默认关闭
      { path: 'library-scope', name: 'LibraryScope', component: () => import('@/views/LibraryScope.vue'), meta: { title: '媒体库可见范围' } },
      // 元数据来源（Phase 6）：条目级的元数据纠偏与补全进度，从「媒体库」页迁到这里
      { path: 'metadata-sources', name: 'MetadataSources', component: () => import('@/views/MetadataSources.vue'), meta: { title: '元数据来源' } },
      // 「存储来源」独立页已于 v2.42.15 撒销：挂载建在「添加服务器」弹窗里（按服务器配置），
      // 目录浏览 / 体检 / 编辑都已搬过去。旧地址跳到服务器页，别人的书签不至于 404。
      { path: 'mounts', redirect: '/servers' },
      // v2.18.0：转存任务下线，页面只保留 115 账号；旧地址保留为跳转，收藏不会 404
      { path: 'pan115', alias: 'transfer-115', name: 'Pan115Accounts', component: () => import('@/views/Pan115Accounts.vue'), meta: { title: '115 账号' } },
      { path: 'gdrive', name: 'GDriveAccounts', component: () => import('@/views/GDriveAccounts.vue'), meta: { title: 'Google Drive 账号' } },
      { path: 'settings', name: 'Settings', component: () => import('@/views/Settings.vue'), meta: { title: '系统设置' } },
      // v2.26.0：管理员与权限（角色 super / operator / viewer）与播放、客户端策略
      { path: 'admins', name: 'Admins', component: () => import('@/views/Admins.vue'), meta: { title: '管理员与权限' } },
      { path: 'client-policy', name: 'ClientPolicy', component: () => import('@/views/ClientPolicy.vue'), meta: { title: '客户端策略' } },
      { path: 'logs', name: 'Logs', component: () => import('@/views/Logs.vue'), meta: { title: '操作日志' } },
      { path: 'health', name: 'SystemHealth', component: () => import('@/views/SystemHealth.vue'), meta: { title: '系统健康' } },
      // v2.56.0：任务总览——所有后台任务一页看完（状态/进度/开关）
      { path: 'tasks', name: 'TaskOverview', component: () => import('@/views/TaskOverview.vue'), meta: { title: '任务总览' } },
      // v2.56.0 健康一页：十个子系统红黄绿灯同屏（原 Health.vue 已并入「系统健康」）
      // Batch E（健康体系合并）：旧「健康总览」地址保留为跳转，收藏与外部链接不会落到 404
      { path: 'health-board', redirect: '/health' },
      // v2.57.0 一键体检：只读诊断规则引擎，大白话结论 + 检查步骤
      { path: 'diagnosis', name: 'Diagnosis', component: () => import('@/views/Diagnosis.vue'), meta: { title: '一键体检' } },
      // v2.58.0 API Key 管理：第三方 Service Token（创建/列表/吊销）
      { path: 'api-keys', name: 'ApiKeys', component: () => import('@/views/ApiKeys.vue'), meta: { title: 'API Key 管理' } },
      // 服管理以绝对路径声明：跳转契约检查要求跳转目标与声明的 path 完全一致
      { path: '/realms', name: 'Realms', component: () => import('@/views/Realms.vue'), meta: { title: '服管理' } },
      { path: 'servers', name: 'Servers', component: () => import('@/views/Servers.vue'), meta: { title: '服务器与线路' } },
      // 「Emby 服务入口」已并入「服务器」页（同一个清单里就能加 EA / Emby 并设为当前使用），
      // 旧地址保留为跳转，收藏与外部链接不会落到 404
      { path: 'emby-servers', redirect: '/servers' },
    ],
  },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

const router = createRouter({
  history: createWebHistory('/admin/'),
  routes,
  // 换页回到顶部（浏览器前进 / 后退仍回到原来的滚动位置）；同页只改 query（筛选深链）不跳
  scrollBehavior(to, from, savedPosition) {
    if (savedPosition) return savedPosition
    if (to.path === from.path) return false
    return { top: 0 }
  },
})

/**
 * 懒加载分块失效兜底：页面全部按路由懒加载，发版后旧标签页里的 chunk 文件名已经不存在，
 * 点侧边栏会抛「Failed to fetch dynamically imported module」，表现是「点了没反应」。
 * 遇到这类错误整页跳到目标地址拿新的入口；sessionStorage 节流，避免文件真缺失时无限刷新。
 */
const CHUNK_RELOAD_KEY = 'admin_chunk_reload_at'
router.onError((err, to) => {
  const msg = err instanceof Error ? err.message : String(err)
  const isChunkError = /dynamically imported module|Importing a module script failed|Loading chunk .* failed/i.test(msg)
  if (!isChunkError) return
  const last = Number(sessionStorage.getItem(CHUNK_RELOAD_KEY) || 0)
  if (Date.now() - last < 10000) return
  sessionStorage.setItem(CHUNK_RELOAD_KEY, String(Date.now()))
  window.location.assign(router.resolve(to.fullPath).href)
})

// 首次运行向导状态（setup_completed）：一次页面加载只问后端一次。
// 问失败时按「已完成」处理——别把正常管理员挡在向导页外面；
// POST /api/admin/setup 本身也会 403 兜底，这里只是分流。
let setupCompletedPromise: Promise<boolean> | null = null
function isSetupCompleted(): Promise<boolean> {
  if (!setupCompletedPromise) {
    setupCompletedPromise = setupStatus()
      .then((st) => st.setup_completed)
      .catch(() => true)
  }
  return setupCompletedPromise
}

router.beforeEach(async (to) => {
  const auth = useAuthStore()

  // 首次运行向导分流：没初始化 → 只能去向导页；已初始化 → 向导页不再可进
  const setupCompleted = await isSetupCompleted()
  if (!setupCompleted && to.name !== 'Setup') {
    return { name: 'Setup' }
  }
  if (setupCompleted && to.name === 'Setup') {
    return { name: 'Login' }
  }

  // 进入后台前先把会话敲定：本地票据也要向服务端核对一次（幂等，一次页面加载只做一次），
  // 再决定放行还是去登录页。门户免登（管理员在用户端已登录时直接接管会话）也在这一步。
  // 以前是「本地有 token 就放行」：过期/失效的票据会让页面先渲染一遍、再 401 重载，
  // 表现就是「点进后台刷新两次」。
  await auth.ensureSession()

  if (to.meta.requiresAuth !== false && !auth.isAuthenticated) {
    return { name: 'Login', query: { redirect: to.fullPath } }
  }
  if (to.name === 'Login' && auth.isAuthenticated) {
    return { path: '/' }
  }
  document.title = adminTitle(to.meta.title as string | undefined)
})

export default router
