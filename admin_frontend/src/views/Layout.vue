<script setup lang="ts">
/**
 * 管理后台外壳（Console v6）
 *
 * 结构：固定侧边栏（≤1024px 变抽屉）+ 顶栏（页面标题 / 刷新 / 管理员菜单）+ 内容区。
 * 手机上抽屉打开时锁背景滚动、Esc / 点遮罩 / 切路由都能关。
 *
 * v2.54（暗房影院）：外壳与用户端 AppHeader 同一套语言——不透明暖黑顶栏 + 发丝线、
 * 衬线大写拉字距的品牌字、琥珀放映机标、三枚同配方的圆形图标按钮。侧边栏不再是
 * 「可折叠手风琴」：分区标题是常驻的小眉题，每一项带自己的图标，当前页 = 正文色 +
 * 实色表面 + 右侧一颗琥珀指示灯。分区按交付链重排为 7 组（安全准入从「用户与账号」
 * 拆出，单项的「服务支持」并入「内容与服务」），所有路径与权限标记一个没少。
 *
 * v2.25.0：一级导航按**交付链**重组（仪表盘 / 用户与账号 / 媒体与交付 / 求片与内容 /
 * 运营中心 / 服务支持 / 系统与审计）——“多服”不再是要先理解的一级概念：
 * 服与线路的归属关系在「服务器与线路」页按范围看，顶栏那个切换器只管当前作用域。
 * 所有页面路径一个字没动（旧收藏、外部链接照旧打开），改的只是分组与名称。
 */
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { RouterView, RouterLink, useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { toAdminError } from '@/utils/errorMessage'
import {
  LayoutDashboard, Users, Package, Ticket, Settings, Server,
  Menu, X, ChevronDown, RefreshCw, LogOut, KeyRound, ExternalLink, Clapperboard,
  CheckCircle2, Route as RealmIcon, Lock, Search, Wallet, Crown, MessageSquareDashed,
  TriangleAlert, ScrollText, Smartphone, MonitorCog, ShieldAlert, Ban, Library, Eye,
  Database, Cloud, Megaphone, Receipt, Trophy, TicketPercent, UserPlus, KeySquare,
  UserCog, Activity, Heart, Medal, HardDrive, MessageSquareText,
  Sun, Moon, MonitorSmartphone, Stethoscope, ListChecks,
} from 'lucide-vue-next'
import { changePassword } from '@/api/admin'
import { useAuthStore } from '@/stores/auth'
import { useRealmStore } from '@/stores/realm'
import { useBreakpoint } from '@/composables/useBreakpoint'
import { useFocusTrap } from '@/composables/useFocusTrap'
import CommandPalette, { type PaletteItem } from '@/components/CommandPalette.vue'
// v2.42.4：后台白日/黑暗双主题（与用户端同一套三档口径）
import { useAdminTheme } from '@/composables/useTheme'
// 站名 / Logo 来自「站点与品牌」能力（改完刷新即生效，不用重新构建）
import { APP_VERSION as APP_VERSION_BASE, branding, siteName } from '@/composables/branding'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const realm = useRealmStore()
const { isTablet } = useBreakpoint()
// 外观三档：跟随系统 / 白日 / 黑暗。composable 自带 onMounted 挂监听，
// 切换只改 html[data-theme]，浅色令牌全在 tokens.css 的浅色块里
const { preference: themePreference, setPreference: setThemePreference } = useAdminTheme()

interface ThemeOption {
  value: 'system' | 'light' | 'dark'
  label: string
  title: string
}

const themeOptions: ThemeOption[] = [
  { value: 'system', label: '自动', title: '跟随系统' },
  { value: 'light', label: '白日', title: '白日模式' },
  { value: 'dark', label: '黑暗', title: '黑暗模式' },
]

/** 顶栏外观按钮（v2.42.9）：侧栏 foot 那排三段切换器**已删**，外观在本端只剩这一枚按钮，
 * 点一下轮换一档：跟随系统 → 白日 → 黑暗。
 *
 * 「自动」必须进这个环：旧版按钮只在白日/黑暗之间来回、「自动」档得去侧栏选，
 * 侧栏那排删掉后就再也回不到跟随系统了——这是本轮唯一的功能性取舍，在这里备查。 */
function cycleTheme() {
  const idx = themeOptions.findIndex((o) => o.value === themePreference.value)
  setThemePreference(themeOptions[(idx + 1) % themeOptions.length].value)
}

/** 当前档 / 下一档：按钮只有图标，说明全在 title / aria-label 里 */
const themeCurrent = computed(
  () => themeOptions.find((o) => o.value === themePreference.value) || themeOptions[0],
)
const themeNext = computed(
  () => themeOptions[(themeOptions.indexOf(themeCurrent.value) + 1) % themeOptions.length],
)
const themeTitle = computed(() => `外观：${themeCurrent.value.label}（点击切到${themeNext.value.label}）`)

/** 当前账号是不是超级管理员（角色见 backend/admin_roles.py）：只影响导航上的标记 */
const isSuper = computed(() => auth.admin?.is_super !== false)
/** 为什么有些入口标着锁：不是 super 时那些页面只能看、不能改 */
const SUPER_ONLY_HINT = '需要超级管理员角色：可以查看，保存会被服务端拒绝'
/** 侧边栏里的角色名（来自后端 /auth/me）：以前写死「超级管理员」，有角色后就不再真实了 */
const roleLabel = computed(() => auth.admin?.role_label || '超级管理员')

const APP_VERSION = APP_VERSION_BASE

const drawerOpen = ref(false)

/** 移动端抽屉模式下的 focus trap：窄屏侧边栏是覆盖层抽屉，Tab 不能跑到背后去 */
const sidebarRef = ref<HTMLElement | null>(null)
const sidebarTrapActive = computed(() => drawerOpen.value && isTablet.value)
useFocusTrap(sidebarRef, sidebarTrapActive)

interface NavItem {
  path: string
  label: string
  /** 侧边栏 / 命令面板里这一项的图标 */
  icon: unknown
  /** 仅超级管理员可写（v2.26.0）：其他角色能看，改不了——入口上直接标出来 */
  superOnly?: boolean
}

interface NavGroup {
  title: string
  icon: unknown
  items: NavItem[]
}

/**
 * 导航分区（暗房影院重排）：按「谁在用 → 谁能进 → 内容从哪来 → 内容 / 服务 → 钱 → 系统」
 * 一条交付链排下来。路径一个字没动（旧收藏、外部链接照旧打开）。
 */
const navGroups: NavGroup[] = [
  { title: '概览', icon: LayoutDashboard, items: [{ path: '/', label: '仪表盘', icon: LayoutDashboard }] },
  {
    title: '用户与账号',
    icon: Users,
    items: [
      { path: '/users', label: '用户', icon: Users },
      { path: '/subscriptions', label: '订阅与权益', icon: Crown },
      { path: '/devices', label: '设备与安全', icon: Smartphone },
      { path: '/login-logs', label: '登录日志', icon: ScrollText },
    ],
  },
  {
    // 「谁能进、能做什么」三件事从用户组里拆出来：它们都是全站级开关，影响所有人
    title: '安全与准入',
    icon: ShieldAlert,
    items: [
      // 客户端能做什么（转码 / 清晰度 / 准入 / 下载与设备）：与「设备与安全」互为补充
      { path: '/client-policy', label: '客户端策略', icon: MonitorCog, superOnly: true },
      // 防共享：跨城市轨迹 + 同播检测。默认关闭，但「处置」档会停用账号 → 仅超管可改
      { path: '/share-guard', label: '防共享', icon: ShieldAlert, superOnly: true },
      // 访问拦截：UA 关键词 + IP 归属地。默认全关，但一旦打开就直接影响所有人能否访问 → 仅超管可改
      { path: '/access-guard', label: '访问拦截', icon: Ban, superOnly: true },
    ],
  },
  {
    // 交付链：谁在出流 → 内容从哪来 → 内容怎么组织（旧页里的 服管理 不再占一个一级入口）
    title: '媒体与交付',
    icon: Server,
    items: [
      { path: '/servers', label: '服务器与线路', icon: Server },
      { path: '/emby', label: '媒体库', icon: Library },
      // 谁能看到哪些库（服务器默认范围 + 指定用户覆盖）：默认关闭，不改现有行为
      { path: '/library-scope', label: '可见范围', icon: Eye },
      // 条目级的元数据（重刮 / 绑定 TMDB / 补全进度）与「按库配置」分开一处
      { path: '/metadata-sources', label: '元数据来源', icon: Database },
      { path: '/pan115', label: '115 账号', icon: Cloud },
      { path: '/gdrive', label: 'Google Drive', icon: HardDrive },
    ],
  },
  {
    // 求片 / 公告 / 工单都是「和用户对话」：原来工单单独占一个只有一项的分组
    title: '内容与服务',
    icon: MessageSquareDashed,
    items: [
      { path: '/media-seek', label: '求片管理', icon: MessageSquareDashed },
      { path: '/announcements', label: '公告管理', icon: Megaphone },
      { path: '/wall-posts', label: '留言墙管理', icon: MessageSquareText },
      { path: '/tickets', label: '工单', icon: Ticket },
    ],
  },
  {
    // 公益服：抽奖配置 / 积分配置（公益用户已并入「用户管理」，求片审核已并入「求片管理」）
    title: '公益服',
    icon: Heart,
    items: [
      // 求片审核已并入「求片管理」，菜单移除
      { path: '/welfare-lottery-rounds', label: '群抽奖活动', icon: Trophy },
      { path: '/welfare-points', label: '积分配置', icon: Wallet },
    ],
  },
  {
    title: '运营中心',
    icon: Package,
    items: [
      { path: '/goods', label: '订阅套餐', icon: Package },
      { path: '/orders', label: '订单', icon: Receipt },
      { path: '/coupons', label: '优惠券', icon: TicketPercent },
      { path: '/invitations', label: '邀请与积分', icon: UserPlus },
      { path: '/member-levels', label: '会员等级', icon: Medal },
      { path: '/codes', label: '码管理', icon: KeySquare },
    ],
  },
  {
    title: '系统与审计',
    icon: Settings,
    items: [
      { path: '/admins', label: '管理员与权限', icon: UserCog, superOnly: true },
      { path: '/settings', label: '系统设置', icon: Settings, superOnly: true },
      { path: '/logs', label: '操作日志', icon: ScrollText },
      // Batch E（健康体系合并）：「服务健康」＋「健康总览」合并为一页「系统健康」，导航只留一个入口
      { path: '/health', label: '系统健康', icon: Activity },
      // v2.56.0：任务总览——所有后台任务一页看完（状态/进度/开关）
      { path: '/tasks', label: '任务总览', icon: ListChecks },
      { path: '/diagnosis', label: '一键体检', icon: Stethoscope },
      { path: '/api-keys', label: 'API Key', icon: KeyRound, superOnly: true },
    ],
  },
]

/**
 * 不占导航位置、但仍要归属到某个分组的页面（服管理 = 媒体与交付里的一条支线）
 *
 * 放进导航就会让「多服」又变成要先理解的一级概念——它的入口在「服务器与线路」页里，
 * 这里只负责面包屑归属与分组展开。
 */
const OFF_NAV_GROUP: Record<string, string> = { '/realms': '媒体与交付' }

/** 某个路径归哪个分组（含不占导航位置的页面，见 OFF_NAV_GROUP） */
function groupOf(path: string): NavGroup | undefined {
  const group = navGroups.find((g) => g.items.some((i) => i.path === path))
  if (group) return group
  const title = OFF_NAV_GROUP[path]
  return title ? navGroups.find((g) => g.title === title) : undefined
}

/** 当前路由所在分组 */
const activeGroup = computed(() => groupOf(canonicalPath.value))

const pageTitle = computed(() => (route.meta.title as string) || '管理后台')
const crumbGroup = computed(() => (activeGroup.value && activeGroup.value.title !== '概览' ? activeGroup.value.title : ''))

// ==================== 抽屉 ====================

function closeDrawer() {
  drawerOpen.value = false
}

watch([drawerOpen, isTablet], ([open, tablet]) => {
  // 抽屉打开时锁住背景滚动（否则手机上滚的是底下的内容）
  document.body.style.overflow = open && tablet ? 'hidden' : ''
})

// 窗口从窄屏拉宽到桌面：侧边栏变回常驻，抽屉状态（与背景滚动锁）一并复位
watch(isTablet, (tablet) => {
  if (!tablet) closeDrawer()
})

function onKeydown(e: KeyboardEvent) {
  // ⌘K / Ctrl+K：全局命令搜索。面板自己会处理 Esc / 方向键 / Enter，
  // 这里只管开关（开着的时候按一下是收起来）
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault()
    paletteOpen.value = !paletteOpen.value
    // 命令面板与侧边栏抽屉都是浮层：开着抽屉时开面板，先把抽屉收掉，不叠两层遮罩
    if (paletteOpen.value) closeDrawer()
    return
  }
  if (e.key === 'Escape' && !paletteOpen.value) closeDrawer()
}

// ==================== 全局命令搜索（Phase 5） ====================

const paletteOpen = ref(false)

/** 命令面板里的「常用定位」：与仪表盘 KPI 用同一套深链，落地页筛选口径一致 */
const QUICK_JUMPS: PaletteItem[] = [
  { id: 'jump-tickets', label: '待处理工单', group: '常用定位', to: '/tickets?status=open', icon: Ticket, hint: '只看进行中的工单' },
  { id: 'jump-seeks', label: '待审求片', group: '常用定位', to: '/media-seek?status=pending', icon: MessageSquareDashed, hint: '只看待审核的求片' },
  { id: 'jump-orders', label: '待支付订单', group: '常用定位', to: '/orders?status=pending', icon: Wallet, hint: '只看待支付的订单' },
  { id: 'jump-expiring', label: '7 天内到期订阅', group: '常用定位', to: '/subscriptions?status=expiring', icon: Crown, hint: '续费窗口里的会员' },
]

/** 侧边栏的页面清单（含不占导航位置的 /realms）——命令面板不维护第二份 */
const pageCommands = computed<PaletteItem[]>(() => {
  const items: PaletteItem[] = []
  for (const group of navGroups) {
    for (const item of group.items) {
      // 超级管理员限定的页（如客户端策略 / 系统设置）与侧边栏同一个口径：
      // 不藏起来（藏了就找不到了），但在提示里说清「进去也没用」
      const locked = Boolean(item.superOnly) && !isSuper.value
      items.push({
        id: `page-${item.path}`,
        label: item.label,
        group: group.title,
        to: item.path,
        icon: item.icon,
        hint: locked ? SUPER_ONLY_HINT : item.path,
        keywords: locked ? '仅超级管理员' : undefined,
      })
    }
  }
  for (const [path, groupTitle] of Object.entries(OFF_NAV_GROUP)) {
    items.push({
      id: `page-${path}`,
      label: '服管理',
      group: groupTitle,
      to: path,
      icon: RealmIcon,
      hint: path,
      keywords: 'realm 服 切换',
    })
  }
  return items
})

/** 外观命令的提示写「点一下会切到哪一档」，免得点完不知道变成什么了 */
const themeCommandHint = computed(() => `切到${themeNext.value.label}`)

const actionCommands = computed<PaletteItem[]>(() => [
  { id: 'act-refresh', label: '刷新当前页', group: '操作', icon: RefreshCw, hint: '重新加载整页' },
  { id: 'act-theme', label: '切换外观', group: '操作', icon: MonitorSmartphone, hint: themeCommandHint.value },
  { id: 'act-password', label: '修改密码', group: '操作', icon: KeyRound, hint: '当前管理员账号' },
  { id: 'act-logout', label: '退出登录', group: '操作', icon: LogOut, hint: '清掉本地会话', danger: true },
  { id: 'act-danger', label: '危险操作', group: '操作', icon: TriangleAlert, hint: '熔断器 / 缓存 / 日志', keywords: '清理 清空 删除' },
].map((item) => ({ ...item, run: actionRun(item.id) })))

/** 动作命令统一走一张表 → 实际执行；`act-danger` 走深链（给 useQueryFilter 接住） */
function actionRun(id: string): () => void {
  if (id === 'act-refresh') return refreshPage
  if (id === 'act-theme') return cycleTheme
  if (id === 'act-password') return openPwdDialog
  if (id === 'act-danger') return () => router.push({ name: 'Settings', query: { tab: 'danger' } })
  return logout
}

const paletteItems = computed<PaletteItem[]>(() => [
  ...QUICK_JUMPS,
  ...pageCommands.value,
  ...actionCommands.value,
])

function onPaletteRun(item: PaletteItem) {
  if (item.to) {
    // 同一页只改 query 时 vue-router 不会重建组件，所以关面板 + 让目标页自己 watch query
    router.push(item.to)
    return
  }
  item.run?.()
}

// 切路由就收起抽屉（窄屏）
watch(
  () => route.path,
  () => closeDrawer(),
)

/**
 * 当前路由的「规范路径」：别名（/transfer-115 → /pan115）按命中的路由记录取声明的 path，
 * 否则走别名进来的页面在侧边栏里没有高亮、也找不到所属分组。
 */
const canonicalPath = computed(() => {
  const rec = route.matched[route.matched.length - 1]
  const p = rec?.path || route.path
  return p.startsWith('/') ? p : `/${p}`
})

/** 当前页判定：仪表盘只认精确的 /，其它入口认自己及子路径 */
function isActive(path: string): boolean {
  const cur = canonicalPath.value
  if (path === '/') return cur === '/'
  return cur === path || cur.startsWith(`${path}/`)
}

function refreshPage() {
  router.go(0)
}

function logout() {
  auth.logout()
  router.push('/login')
}

// ==================== 修改密码 ====================

const pwdVisible = ref(false)
const pwdLoading = ref(false)
const pwdForm = reactive({ old_password: '', new_password: '', confirm: '' })

function openPwdDialog() {
  pwdForm.old_password = ''
  pwdForm.new_password = ''
  pwdForm.confirm = ''
  pwdVisible.value = true
}

async function submitPwd() {
  if (pwdForm.new_password.length < 6) {
    ElMessage.warning('新密码至少 6 位')
    return
  }
  if (pwdForm.new_password !== pwdForm.confirm) {
    ElMessage.warning('两次输入的新密码不一致')
    return
  }
  pwdLoading.value = true
  try {
    await changePassword({ old_password: pwdForm.old_password, new_password: pwdForm.new_password })
    ElMessage.success('密码已修改，请重新登录')
    pwdVisible.value = false
    auth.logout()
    router.push('/login')
  } catch (e) {
    ElMessage.error(toAdminError(e, '修改失败'))
  } finally {
    pwdLoading.value = false
  }
}

function onAdminCommand(cmd: string) {
  if (cmd === 'password') openPwdDialog()
  else if (cmd === 'logout') logout()
}

// ==================== 当前服（多服运营）====================

/** 切换当前服：各页的作用域跟着切，所以切完整页重载一次，避免停在旧服的数据上 */
async function onRealmCommand(cmd: number | string) {
  if (cmd === '__manage') {
    router.push('/realms')
    return
  }
  const id = Number(cmd)
  if (!id || id === realm.activeId) return
  try {
    await realm.switchTo(id)
    ElMessage.success(`已切换到「${realm.activeName() || id}」`)
    window.setTimeout(() => window.location.reload(), 400)
  } catch {
    /* 拦截器已提示 */
  }
}

const initial = computed(() => (auth.admin?.username || 'A').charAt(0).toUpperCase())

onMounted(() => {
  window.addEventListener('keydown', onKeydown)
  // 当前服（服务端 active_realm_id 是权威来源）；失败不阻断后台，只是顶栏不显示服名
  realm.load().catch(() => undefined)
  // 管理员身份已由路由守卫里的 auth.ensureSession() 向 /auth/me 核对过（一次页面加载一次），
  // 这里不再重复请求一遍
})

onUnmounted(() => {
  window.removeEventListener('keydown', onKeydown)
  document.body.style.overflow = ''
})
</script>

<template>
  <div class="shell admin-layout">
    <!-- 抽屉遮罩（窄屏点它关闭） -->
    <transition name="mask">
      <div v-if="drawerOpen" class="shell-mask" @click="closeDrawer" />
    </transition>

    <!-- 侧边栏：宽屏常驻，≤1024px 变抽屉 -->
    <aside
      ref="sidebarRef"
      class="sidebar"
      :class="{ open: drawerOpen }"
      :role="isTablet ? 'dialog' : undefined"
      :aria-modal="isTablet ? 'true' : undefined"
      aria-label="管理导航"
    >
      <!-- 品牌：与用户端 AppHeader 同一套——琥珀放映机标 + 衬线大写拉字距的站名 -->
      <div class="brand">
        <RouterLink to="/" class="brand-link">
          <span class="brand-mark">
            <img v-if="branding.logo_url" :src="branding.logo_url" :alt="branding.site_name" />
            <Clapperboard v-else :size="17" />
          </span>
          <span class="brand-text">
            <strong class="brand-name">{{ siteName() }}</strong>
            <span class="brand-sub">管理控制台</span>
          </span>
        </RouterLink>
        <button class="round-btn brand-close" aria-label="关闭菜单" @click="closeDrawer">
          <X :size="17" />
        </button>
      </div>

      <nav class="nav" aria-label="后台页面">
        <section v-for="group in navGroups" :key="group.title" class="nav-section">
          <h2 class="nav-eyebrow">{{ group.title }}</h2>
          <RouterLink
            v-for="item in group.items"
            :key="item.path"
            :to="item.path"
            class="nav-item"
            :class="{ active: isActive(item.path) }"
            :aria-current="isActive(item.path) ? 'page' : undefined"
            @click="closeDrawer"
          >
            <component :is="item.icon" :size="16" class="nav-icon" />
            <span class="nav-label">{{ item.label }}</span>
            <Lock v-if="item.superOnly && !isSuper" :size="12" class="nav-super" :title="SUPER_ONLY_HINT" />
          </RouterLink>
        </section>
      </nav>

      <div class="sidebar-foot">
        <div class="who">
          <span class="avatar who-avatar">{{ initial }}</span>
          <div class="who-info">
            <div class="who-name">{{ auth.admin?.username || '管理员' }}</div>
            <div class="who-role">{{ roleLabel }}</div>
          </div>
        </div>
        <div class="foot-links">
          <button class="foot-link" @click="openPwdDialog">
            <KeyRound :size="15" />修改密码
          </button>
          <a class="foot-link" href="/">
            <ExternalLink :size="15" />回到前台
          </a>
          <button class="foot-link danger" @click="logout">
            <LogOut :size="15" />退出登录
          </button>
        </div>
        <div class="foot-version">{{ siteName() }} {{ APP_VERSION }}</div>
      </div>
    </aside>

    <!-- 主区域 -->
    <div class="main">
      <!-- 顶栏：不透明暖黑实底 + 一根发丝线（同用户端 .app-header），不再有毛玻璃 -->
      <header class="topbar">
        <button class="round-btn menu-btn" aria-label="打开菜单" @click="drawerOpen = true">
          <Menu :size="18" />
        </button>

        <div class="topbar-title">
          <span v-if="crumbGroup" class="topbar-crumb">{{ crumbGroup }}</span>
          <h1>{{ pageTitle }}</h1>
        </div>

        <div class="topbar-actions">
          <!-- 命令搜索：桌面带 ⌘K 提示，手机只留图标 -->
          <button
            class="cmd-btn"
            title="命令搜索（⌘K / Ctrl+K）"
            aria-label="命令搜索"
            @click="paletteOpen = true; closeDrawer()"
          >
            <Search :size="15" />
            <span class="cmd-btn-text">搜索页面或操作</span>
            <kbd class="cmd-btn-kbd">⌘K</kbd>
          </button>

          <!-- 当前服：面板可以同时运营多个服，切完各页的作用域跟着走 -->
          <el-dropdown v-if="realm.realms.length" trigger="click" @command="onRealmCommand">
            <button class="realm-chip" aria-label="切换当前服">
              <RealmIcon :size="15" />
              <span class="realm-chip-name">{{ realm.activeName() || '当前服' }}</span>
              <ChevronDown :size="14" class="chip-chev" />
            </button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item
                  v-for="r in realm.realms"
                  :key="r.id"
                  :command="r.id"
                  :disabled="!r.is_active && r.id !== realm.activeId"
                >
                  <CheckCircle2
                    v-if="r.id === realm.activeId"
                    :size="14"
                    style="margin-right: 6px"
                  />
                  <span v-else style="display: inline-block; width: 20px" />
                  {{ r.name }}
                  <span v-if="!r.is_active" class="realm-off">（已停用）</span>
                </el-dropdown-item>
                <el-dropdown-item divided command="__manage">管理服…</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>

          <!-- 外观：图标即当前档（跟随系统 / 白日 / 黑暗），点一下轮换一档；
               手动指定档 = 琥珀实底，跟随系统 = 中性底 + 琥珀细环（同用户端顶栏） -->
          <button
            class="round-btn theme-btn"
            :class="{ auto: themePreference === 'system' }"
            :title="themeTitle"
            :aria-label="themeTitle"
            @click="cycleTheme"
          >
            <MonitorSmartphone v-if="themePreference === 'system'" :size="17" />
            <Sun v-else-if="themePreference === 'light'" :size="17" />
            <Moon v-else :size="17" />
          </button>
          <button class="round-btn refresh-btn" title="刷新当前页" aria-label="刷新当前页" @click="refreshPage">
            <RefreshCw :size="17" />
          </button>
          <el-dropdown trigger="click" @command="onAdminCommand">
            <button class="round-btn admin-btn" :aria-label="`${auth.admin?.username || '管理员'} · 管理员菜单`" :title="auth.admin?.username || '管理员'">
              <span class="avatar">{{ initial }}</span>
            </button>
            <template #dropdown>
              <div class="admin-menu-head">
                <span class="admin-menu-name">{{ auth.admin?.username || '管理员' }}</span>
                <span class="admin-menu-role">{{ roleLabel }}</span>
              </div>
              <el-dropdown-menu>
                <el-dropdown-item command="password">
                  <KeyRound :size="15" style="margin-right: 8px" />修改密码
                </el-dropdown-item>
                <el-dropdown-item command="logout" divided>
                  <LogOut :size="15" style="margin-right: 8px" />退出登录
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </header>

      <main class="content admin-content">
        <RouterView />
      </main>
    </div>

    <CommandPalette
      v-model="paletteOpen"
      :items="paletteItems"
      @run="onPaletteRun"
    />

    <el-dialog v-model="pwdVisible" title="修改密码" width="min(420px, 92vw)">
      <el-form label-position="top" @submit.prevent>
        <el-form-item label="当前密码">
          <el-input v-model="pwdForm.old_password" type="password" show-password placeholder="请输入当前密码" />
        </el-form-item>
        <el-form-item label="新密码">
          <el-input v-model="pwdForm.new_password" type="password" show-password placeholder="至少 6 位" />
        </el-form-item>
        <el-form-item label="确认新密码">
          <el-input v-model="pwdForm.confirm" type="password" show-password placeholder="再次输入新密码" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="pwdVisible = false">取消</el-button>
        <el-button type="primary" :loading="pwdLoading" @click="submitPwd">确认修改</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.shell {
  display: flex;
  min-height: 100vh;
  min-height: 100dvh;
  background: var(--au-bg);
}

/* ==================== 圆形图标按钮（同用户端 AppHeader .round-btn） ==================== */
.round-btn {
  position: relative;
  width: 38px;
  height: 38px;
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-full);
  background: var(--au-surface);
  color: var(--au-text-2);
  cursor: pointer;
  transition: background var(--au-fast) var(--au-ease),
    border-color var(--au-fast) var(--au-ease),
    color var(--au-fast) var(--au-ease);
}

.round-btn:hover {
  color: var(--au-text);
  background: var(--au-surface-2);
  border-color: var(--au-border-strong);
}

.round-btn:active { transform: scale(0.96); }

.round-btn:focus-visible {
  outline: 2px solid var(--au-border-focus);
  outline-offset: 2px;
}

/* 外观按钮：手动档 = 琥珀实底；跟随系统 = 中性底 + 琥珀细环 */
.theme-btn.auto { border-color: var(--au-primary-border); }
.theme-btn.auto:hover { border-color: var(--au-primary); }

.round-btn.theme-btn:not(.auto) {
  background: var(--au-primary);
  border-color: var(--au-primary);
  color: var(--au-on-primary);
}

.round-btn.theme-btn:not(.auto):hover {
  background: var(--au-primary-strong);
  border-color: var(--au-primary-strong);
  color: var(--au-on-primary);
}

/* 头像：琥珀实底 + 深棕墨字母（同用户端 .avatar） */
.avatar {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  border-radius: var(--au-r-full);
  background: var(--au-primary);
  color: var(--au-on-primary);
  font-size: 13px;
  font-weight: 700;
}

.admin-btn { padding: 0; }

/* ==================== 侧边栏 ==================== */
.sidebar {
  position: fixed;
  inset: 0 auto 0 0;
  width: var(--sidebar-w);
  display: flex;
  flex-direction: column;
  background: var(--au-bg);
  border-right: 1px solid var(--au-border);
  z-index: var(--z-sticky);
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 14px 0 18px;
  min-height: 62px;
  border-bottom: 1px solid var(--au-border);
}

.brand-link {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  text-decoration: none;
}

.brand-mark {
  width: 26px;
  height: 26px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--au-r-sm);
  color: var(--au-primary);
}

.brand-mark img {
  width: 100%;
  height: 100%;
  object-fit: contain;
  border-radius: inherit;
}

.brand-text { display: flex; flex-direction: column; min-width: 0; line-height: 1.2; }

/* 品牌字：衬线 + 大写 + 拉开字距（片头字幕的气质，同用户端 .logo-text） */
.brand-name {
  font-family: var(--au-font-serif);
  font-size: 1rem;
  font-weight: 700;
  letter-spacing: 0.24em;
  text-transform: uppercase;
  color: var(--au-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.brand-sub {
  margin-top: 2px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.24em;
  color: var(--au-primary);
}

.brand-close { display: none; margin-left: auto; width: 34px; height: 34px; }

.nav {
  flex: 1;
  padding: 6px 10px 12px;
  overflow-y: auto;
  overscroll-behavior: contain;
}

.nav-section { padding-top: 14px; }

/* 分区眉题：小号、拉开字距、四级字（安静，不抢页面标题） */
.nav-eyebrow {
  margin: 0 0 4px;
  padding: 0 12px;
  font-family: var(--au-font-sans);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.2em;
  color: var(--au-text-4);
}

.nav-item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border: 1px solid transparent;
  border-radius: var(--au-r-md);
  color: var(--au-text-3);
  font-size: 13.5px;
  font-weight: 500;
  letter-spacing: 0.02em;
  text-decoration: none;
  transition: background var(--au-fast) var(--au-ease), color var(--au-fast) var(--au-ease),
    border-color var(--au-fast) var(--au-ease);
}

.nav-item + .nav-item { margin-top: 1px; }

.nav-icon { flex-shrink: 0; color: var(--au-text-4); transition: color var(--au-fast) var(--au-ease); }

.nav-item:hover { color: var(--au-text); background: var(--au-surface); }
.nav-item:hover .nav-icon { color: var(--au-text-2); }

.nav-item:focus-visible {
  outline: 2px solid var(--au-border-focus);
  outline-offset: -2px;
}

/* 当前页：正文色 + 实色表面 + 发丝描边 + 右侧一颗琥珀指示灯（同用户端主导航的小圆点） */
.nav-item.active {
  color: var(--au-text);
  font-weight: 600;
  background: var(--au-surface);
  border-color: var(--au-border);
}

.nav-item.active .nav-icon { color: var(--au-primary); }

.nav-item.active::after {
  content: '';
  position: absolute;
  right: 12px;
  top: 50%;
  width: 5px;
  height: 5px;
  margin-top: -2.5px;
  border-radius: 50%;
  background: var(--au-primary);
}

.nav-label { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.nav-super { color: var(--au-text-4); flex-shrink: 0; }
.nav-item.active .nav-super { margin-right: 12px; }

.sidebar-foot {
  padding: 12px;
  border-top: 1px solid var(--au-border);
  padding-bottom: calc(12px + env(safe-area-inset-bottom));
}

.who {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 4px 4px 12px;
}

.who-avatar { width: 34px; height: 34px; flex-shrink: 0; }

.who-info { flex: 1; min-width: 0; }
.who-name { font-size: 13px; font-weight: 600; color: var(--au-text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.who-role { font-size: 11.5px; color: var(--au-text-3); }

.foot-links { display: flex; flex-direction: column; gap: 1px; }

.foot-link {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 8px 12px;
  border: none;
  border-radius: var(--au-r-md);
  background: transparent;
  color: var(--au-text-3);
  font-size: 13px;
  text-align: left;
  text-decoration: none;
  transition: background var(--au-fast) var(--au-ease), color var(--au-fast) var(--au-ease);
}

.foot-link:hover { background: var(--au-surface); color: var(--au-text); }
.foot-link.danger:hover { background: var(--au-danger-soft); color: var(--au-danger); }

.foot-version {
  margin-top: 8px;
  padding: 0 12px;
  font-size: 11px;
  color: var(--au-text-4);
  letter-spacing: 0.06em;
}

/* ==================== 主区域 ==================== */
.main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  margin-left: var(--sidebar-w);
}

/* 顶栏：不透明暖黑实底 + 发丝线（同用户端 .app-header：滚过去的内容不透字） */
.topbar {
  position: sticky;
  top: 0;
  z-index: var(--z-float);
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 62px;
  padding: 0 24px;
  padding-top: env(safe-area-inset-top);
  background: var(--au-bg);
  border-bottom: 1px solid var(--au-border);
}

.topbar-title { display: flex; flex-direction: column; justify-content: center; min-width: 0; line-height: 1.2; }

.topbar-title h1 {
  margin: 0;
  font-family: var(--au-font-serif);
  font-size: 1.1875rem;
  font-weight: 700;
  letter-spacing: 0.02em;
  color: var(--au-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 面包屑：眉题样式，压在标题上方 */
.topbar-crumb {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.2em;
  color: var(--au-primary);
  margin-bottom: 2px;
}

.topbar-actions { margin-left: auto; display: flex; align-items: center; gap: 10px; flex-shrink: 0; }

/* 命令搜索入口：一条安静的输入框外观，宽屏展开文字 + ⌘K 提示 */
.cmd-btn {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  height: 38px;
  min-width: 220px;
  padding: 0 8px 0 12px;
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-full);
  background: var(--au-input-bg);
  color: var(--au-text-4);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
  transition: border-color var(--au-fast) var(--au-ease), color var(--au-fast) var(--au-ease);
}
.cmd-btn:hover { border-color: var(--au-border-strong); color: var(--au-text-2); }
.cmd-btn:focus-visible { outline: 2px solid var(--au-border-focus); outline-offset: 2px; }
.cmd-btn-text { flex: 1; text-align: left; }
.cmd-btn-kbd {
  padding: 0 6px;
  border: 1px solid var(--au-border);
  border-radius: var(--radius-xs, 6px);
  background: var(--au-surface);
  color: var(--au-text-3);
  font-family: var(--font-mono);
  font-size: 10.5px;
  line-height: 1.8;
}

/* 当前服：一眼看出“现在运营的是哪个服”，点开就能切 */
.realm-chip {
  display: flex;
  align-items: center;
  gap: 7px;
  height: 38px;
  padding: 0 12px;
  border-radius: var(--au-r-full);
  border: 1px solid var(--au-border);
  background: var(--au-surface);
  color: var(--au-text-2);
  font-size: 13px;
  font-weight: 500;
  transition: border-color var(--au-fast) var(--au-ease), color var(--au-fast) var(--au-ease);
}
.realm-chip svg:first-child { color: var(--au-primary); }
.realm-chip:hover { border-color: var(--au-primary-border); color: var(--au-text); }
.realm-chip-name { max-width: 140px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.chip-chev { color: var(--au-text-4); }
.realm-off { color: var(--au-text-4); font-size: 12px; }

/* 管理员菜单头（下拉是 Teleport 出去的，这里用 :global 才能命中） */
:global(.admin-menu-head) {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 12px 18px 8px;
  border-bottom: 1px solid var(--au-border);
}
:global(.admin-menu-name) { font-family: var(--au-font-serif); font-weight: 700; color: var(--au-text); font-size: 14px; }
:global(.admin-menu-role) { font-size: 11.5px; color: var(--au-text-3); }

.content {
  flex: 1;
  width: 100%;
  max-width: 1440px;
  margin: 0 auto;
  padding: 24px;
}

/* 抽屉遮罩 */
.shell-mask {
  position: fixed;
  inset: 0;
  z-index: calc(var(--z-sticky) - 1);
  background: var(--au-scrim);
}

.mask-enter-active,
.mask-leave-active { transition: opacity var(--au-med) var(--au-ease); }
.mask-enter-from,
.mask-leave-to { opacity: 0; }

/* ==================== 响应式 ==================== */

/* ≤1024px（平板 / 小窗）：侧边栏收成抽屉 */
@media (max-width: 1024px) {
  .sidebar {
    z-index: var(--z-modal);
    transform: translateX(-100%);
    visibility: hidden;
    transition: transform var(--au-med) var(--au-ease), visibility var(--au-med) var(--au-ease);
    box-shadow: var(--au-shadow-2);
  }

  .sidebar.open { transform: translateX(0); visibility: visible; }

  .brand-close { display: inline-flex; }
  .main { margin-left: 0; }
  .content { padding: 18px; }
  .cmd-btn { min-width: 0; }
}

@media (min-width: 1025px) {
  .menu-btn { display: none; }
  .sidebar { visibility: visible; }
}

/* 手机 */
@media (max-width: 768px) {
  .topbar { padding: 0 12px; padding-top: env(safe-area-inset-top); gap: 8px; min-height: 56px; }
  .topbar-title h1 { font-size: 1.0625rem; }
  .topbar-crumb { display: none; }
  .topbar-actions { gap: 6px; }
  .round-btn { width: 36px; height: 36px; }
  /* 手机：搜索只留放大镜图标；刷新收起（浏览器自己就能刷新），顶栏留给服切换与账号 */
  .cmd-btn { width: 36px; height: 36px; padding: 0; justify-content: center; }
  .cmd-btn-text,
  .cmd-btn-kbd,
  .refresh-btn { display: none; }
  .realm-chip { height: 36px; padding: 0 10px; }
  .realm-chip-name { max-width: 84px; }
  .content { padding: 12px 12px calc(28px + env(safe-area-inset-bottom)); }
  .nav-item { padding: 10px 12px; }
}
</style>
