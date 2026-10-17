<script setup lang="ts">
import { RouterLink, useRouter, useRoute } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { ref, computed, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import {
  Clapperboard, LogOut, Ticket, Inbox, Crown, Sparkles,
  Gift, Megaphone, AlertCircle, Clock, Sun, Moon, MonitorSmartphone, Bell,
  ChevronRight, Heart, ShieldCheck,
} from 'lucide-vue-next'
import api, {
  messageApi, announcementApi, isExpiringSoon, subscriptionApi,
  type StationMessage, type Announcement,
} from '@/api'
import type { MySubscription } from '@/api'
import { primaryNav, resolveActiveNavPath } from '@/config/navigation'
/** v4.1：头像菜单改为「探险设施直达」，设施清单唯一数据源 */
import { gameFacilities, gameHubConfig } from '@/config/game'
import { checkinApi, vitalityApi } from '@/api/economy'
// 站名与 Logo 来自「站点与品牌」能力（没配就用默认值，不会出现空标题）
import { branding } from '@/composables/useBranding'

/** 积分余额（顶栏 pill 显示，登录后加载） */
const pointsBalance = ref<number | null>(null)
async function loadPoints() {
  if (!userStore.isLoggedIn) {
    pointsBalance.value = null
    return
  }
  try {
    const st = await checkinApi.status()
    pointsBalance.value = st.points
  } catch {
    /* 拿不到就不显示，不打扰 */
  }
}
/** 体力（顶栏 pill 显示 x/14 格式，与报到页体力条统一，仅公益服用户，403 则不显示） */
const vitality = ref<number | null>(null)
const vitalityMax = ref<number | null>(null)
async function loadVitality() {
  if (!userStore.isLoggedIn) {
    vitality.value = null
    vitalityMax.value = null
    return
  }
  try {
    const st = await vitalityApi.status()
    vitality.value = st.vitality
    vitalityMax.value = st.max ?? null
  } catch {
    vitality.value = null
    vitalityMax.value = null
  }
}
// 三档外观（跟随系统 / 白日 / 黑暗），见 useTheme.ts 的口径说明
import { useTheme } from '@/composables/useTheme'

const userStore = useUserStore()
const router = useRouter()
const route = useRoute()
const { preference: themePreference, resolved: themeResolved, setPreference: setThemePreference } = useTheme()

/**
 * 外观（v2.42.9 三改）：顶栏常驻一枚圆形图标按钮，点一下直接在白日/黑暗之间切换，
 * 不要下拉菜单。跟随系统时按当前实际生效的反着切。
 */
const themeModes = [
  { value: 'system', label: '跟随系统', icon: MonitorSmartphone },
  { value: 'light', label: '白日', icon: Sun },
  { value: 'dark', label: '黑暗', icon: Moon },
] as const

/** 当前档在环上的位置：脏存储值落到第一档（与 useTheme 的读法一致，不会出现 undefined） */
const themeIndex = computed(() => {
  const idx = themeModes.findIndex((m) => m.value === themePreference.value)
  return idx < 0 ? 0 : idx
})
const themeCurrent = computed(() => themeModes[themeIndex.value])
/** 图标按钮没有文字：当前档写进 title / aria-label */
const themeTitle = computed(() => `外观：${themeCurrent.value.label}（点击切换）`)

/** 点一下直接切换：白日↔黑暗；跟随系统时按实际生效的反着切 */
function toggleTheme() {
  const cur = themePreference.value
  if (cur === 'light') setThemePreference('dark')
  else if (cur === 'dark') setThemePreference('light')
  else setThemePreference(themeResolved.value === 'light' ? 'dark' : 'light')
}

const userMenuOpen = ref(false)
const userMenuRef = ref<HTMLElement | null>(null)
/** 头像按钮：Esc 关闭用户菜单后把焦点还给它，键盘用户不迷路 */
const userBtnRef = ref<HTMLElement | null>(null)
const navRef = ref<HTMLElement | null>(null)
const unreadCount = ref(0)

/**
 * 会员状态（v2.42.8 起只留在头像菜单里）：顶栏那两颗常驻 pill（能量 / 会员生效中）
 * 已按设计稿删除——能量由首页「我的资产」与钱包承接，会员状态由菜单头部承接。
 * 与轮询同频刷新：购买 / 续费后回到任何页面都能立刻看到新状态。
 */
const activeSub = ref<MySubscription | null>(null)

const subPillExpiring = computed(() => (activeSub.value ? isExpiringSoon(activeSub.value) : false))

/** 菜单头部的状态徽章：会员金 / 公益服青，与资产卡同一套色彩编码 */
const menuBadge = computed(() => {
  if (activeSub.value) {
    return {
      icon: Crown,
      text: subPillExpiring.value ? `剩 ${activeSub.value.days_left} 天` : '会员生效中',
      cls: 'gold',
    }
  }
  if (userStore.isFreeRealm) {
    return { icon: Sparkles, text: '公益服 · 免费', cls: 'cyan' }
  }
  return { icon: Crown, text: '未开通会员', cls: 'muted' }
})

/** v4.1：头像菜单改为「探险设施直达」——直接列出 @/config/game.ts 的
 *  游戏设施（图标+名称+描述），点卡片直达对应 /game/xxx 页面。
 *  只显示 enabled 的设施，按注册表 order 排序——与大本营地图顺序、名字一字不差。
 *  设施开关状态由各游戏页自己处理（如报到未开启时页内提示）。
 *  退出登录独立成红色分区，不混在列表里 */
const menuFacilities = computed(() =>
  gameFacilities.filter((f) => f.enabled).sort((a, b) => a.order - b.order),
)

/** 昵称展示：登录名可能是一长串邮箱，头像旁只取 @ 前段；@id 用数字 id */
const displayName = computed(() => {
  const raw = userStore.user?.username || ''
  const at = raw.indexOf('@')
  return at > 0 ? raw.slice(0, at) : raw || '用户'
})

const userIdLabel = computed(() => (userStore.user?.id ? `@${userStore.user.id}` : ''))

/** 管理员判定：后端 AuthUser.is_staff（注释写明了就是为用户端「管理后台」入口准备的）。
 *  头像菜单里的管理后台行仅管理员渲染，普通用户看不见。 */
const isStaff = computed(() => !!userStore.user?.is_staff)

// 顶栏消息入口（v2.10.3 起是全站唯一的消息入口）：既显示「几条未读」，
// 点开还能先看预览再决定要不要进信鸽
const msgMenuOpen = ref(false)
const msgMenuRef = ref<HTMLElement | null>(null)
const msgPreview = ref<MsgPreviewItem[]>([])
const msgLoading = ref(false)

interface MsgPreviewItem {
  key: string
  title: string
  meta: string
  to: string
  icon: unknown
  unread: boolean
  /** 同名未读合并后的条数（1 = 真的只有一条） */
  count?: number
  /** 条数是否精确（拉取窗口被占满时，最旧那一组可能还没数完，显示成 N+） */
  countExact?: boolean
  /** 这条消息对应哪条公告（公告广播自带 related_id），用于避开同一公告列两遍 */
  announcementId?: number | null
}

/** 未读预览一次拉多少条：要够把「同名通知有几条」数准（消息很小，50 条约几十 KB） */
const UNREAD_WINDOW = 50

// 消息类型 → 图标（与信鸽的分类保持一致；只取预览需要的几种）
const MSG_ICONS: Record<string, unknown> = {
  system: AlertCircle,
  ticket: Ticket,
  announcement: Megaphone,
  subscription: Gift,
  media_seek: Clock,
  exchange_code: Gift,
}

const MSG_LABELS: Record<string, string> = {
  system: '系统',
  ticket: '求救信号',
  announcement: '公告',
  subscription: '会员',
  media_seek: '许愿池',
  exchange_code: '兑换码',
}

function relTime(iso?: string): string {
  if (!iso) return ''
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return ''
  const mins = Math.floor((Date.now() - t) / 60_000)
  if (mins < 1) return '刚刚'
  if (mins < 60) return `${mins} 分钟前`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours} 小时前`
  const days = Math.floor(hours / 24)
  if (days < 30) return `${days} 天前`
  return iso.slice(0, 10)
}

/** meta 拼装：时间非法时不留悬空分隔符 */
function msgMeta(label: string, iso?: string): string {
  const rt = relTime(iso)
  return rt ? `${label} · ${rt}` : label
}

/**
 * 铃铛预览：未读优先（同名合并成一条 + 条数），再接置顶公告。
 *
 * v2.10.3：铃铛是站内消息唯一的入口——首页底部那张消息卡已去掉（它和这里列的是
 * 同一批未读）。所以这一份预览就是全站的消息预览。
 */
async function loadMsgPreview() {
  msgLoading.value = true
  try {
    // 只拉未读：预览要回答的是「有什么在等我」，而不是「最近收到了什么」
    const [msgs, notices] = await Promise.all([
      messageApi.getMessages({ unread_only: true, limit: UNREAD_WINDOW }).catch((): StationMessage[] => []),
      announcementApi.getAnnouncements().catch((): Announcement[] => []),
    ])
    const unreadAll = (msgs || []).filter((x) => !x.is_read)

    const unreadRows: MsgPreviewItem[] = []
    const byTitle = new Map<string, MsgPreviewItem>()
    let last: MsgPreviewItem | null = null
    for (const m of unreadAll) {
      const groupKey = `${m.message_type}:${m.title}`
      const hit = byTitle.get(groupKey)
      if (hit) {
        hit.count = (hit.count || 1) + 1
        last = hit
        continue
      }
      const row: MsgPreviewItem = {
        key: `m${m.id}`,
        title: m.title,
        meta: msgMeta(MSG_LABELS[m.message_type] || '通知', m.created_at),
        to: '/game/pigeon',
        icon: MSG_ICONS[m.message_type] || Bell,
        unread: true,
        count: 1,
        countExact: true,
        // related_id 只有公告广播那条才指回公告（许愿池/求救信号的 related_id 是各自的业务 id，
        // 拿来跟公告 id 比会误伤）
        announcementId: m.message_type === 'announcement' ? (m.related_id ?? null) : null,
      }
      byTitle.set(groupKey, row)
      unreadRows.push(row)
      last = row
    }
    // 窗口被未读占满时，最旧那一组可能还有下一批没拉到，标成 N+ 而不是报一个偏小的数
    if (last && unreadAll.length >= UNREAD_WINDOW) last.countExact = false

    const unread = unreadRows.slice(0, 3)
    const listedAnnouncementIds = new Set(
      unread
        .filter((r) => r.announcementId != null)
        .map((r) => Number(r.announcementId)),
    )
    const pinned = [...(notices || [])]
      .filter((a) => !listedAnnouncementIds.has(a.id))
      .sort((a, b) => Number(b.is_pinned) - Number(a.is_pinned))
      .slice(0, 2)

    msgPreview.value = [
      ...unread,
      ...pinned.map((a) => ({
        key: `a${a.id}`,
        title: a.title,
        meta: msgMeta('公告', a.created_at),
        // 消息中心已迁到信鸽 /game/pigeon（PigeonView 暂不支持 tab 参数，进首页即可）
        to: '/game/pigeon',
        icon: Megaphone,
        unread: false,
      })),
    ]
  } finally {
    msgLoading.value = false
  }
}

/**
 * 下拉锚点（≥769px）：两个下拉 Teleport 到 body 之后，原来的 `position:absolute; top:100%`
 * 相对的是整页（初始包含块）而不是按钮——桌面上菜单落在首屏之外（y≈视口高度），点了像没反应。
 * 打开时按按钮位置算一次 fixed 坐标；≤768px 仍由 CSS 锚在底部坞之上，不加内联样式。
 */
const dropStyle = ref<Record<string, string>>({})
function anchorDropdown(el: HTMLElement | null) {
  if (!el || window.innerWidth <= 768) {
    dropStyle.value = {}
    return
  }
  const r = el.getBoundingClientRect()
  dropStyle.value = {
    position: 'fixed',
    top: `${Math.round(r.bottom + 8)}px`,
    right: `${Math.max(8, Math.round(document.documentElement.clientWidth - r.right))}px`,
  }
}

function toggleUserMenu() {
  msgMenuOpen.value = false
  userMenuOpen.value = !userMenuOpen.value
  if (userMenuOpen.value) {
    anchorDropdown(userMenuRef.value)
  }
}

/** 佩戴中的徽章（后端存账号，随登录出参进 store；换账号天然不串、
 *  换设备也在。旧版只存浏览器本地的写法已废弃，见 OutfitView 的一次性迁移） */
const wornBadge = computed(() => userStore.user?.worn_badge ?? null)

function toggleMsgMenu() {
  userMenuOpen.value = false
  msgMenuOpen.value = !msgMenuOpen.value
  if (msgMenuOpen.value) {
    anchorDropdown(msgMenuRef.value)
    loadMsgPreview()
  }
}

/**
 * 导航分工（v2.6.30，v4.1 更新）：全站只有一份导航定义，见 src/config/navigation.ts。
 *
 *   primaryNav   → 顶栏（宽屏横排、窄屏第二行滑动选项卡），全断点同一批条目、同一个顺序
 *   头像菜单     → v4.1 起改为「探险设施直达」，设施清单见 src/config/game.ts
 */

function isActive(path: string) {
  // 最长前缀匹配：/game/account 命中「我的」而不是「世界」
  return resolveActiveNavPath(route.path) === path
}

function closeMenus() {
  userMenuOpen.value = false
  msgMenuOpen.value = false
}

/** Esc 关闭用户菜单：焦点被困在菜单里时，一键出来并回到头像按钮 */
function onDocKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && userMenuOpen.value) {
    userMenuOpen.value = false
    userBtnRef.value?.focus()
  }
}

/** 跨断点 resize / 转屏：开着菜单时锚点会错乱，直接关掉最稳 */
function onViewportChange() {
  if (userMenuOpen.value || msgMenuOpen.value) closeMenus()
}

async function handleLogout() {
  closeMenus()
  await userStore.logout()
  router.push('/login')
}

function onDocClick(e: MouseEvent) {
  const target = e.target as Node
  // 下拉已 Teleport 到 body：判断「点在不在菜单里」时把传送出去的面板也算自己人，
  // 否则菜单内任何一次点击都会被当成外部点击而立刻关闭
  const el = target instanceof Element ? target : null
  if (
    userMenuRef.value &&
    !userMenuRef.value.contains(target) &&
    !el?.closest('.user-dropdown')
  ) {
    userMenuOpen.value = false
  }
  if (
    msgMenuRef.value &&
    !msgMenuRef.value.contains(target) &&
    !el?.closest('.msg-dropdown')
  ) {
    msgMenuOpen.value = false
  }
}

async function refreshSubscription() {
  if (!userStore.isLoggedIn) return
  try {
    const subs = await subscriptionApi.getMine()
    activeSub.value = (Array.isArray(subs) ? subs : []).find(
      (s) => s.status === 'active' && s.days_left > 0,
    ) || null
  } catch {
    /* 静默：订阅态拿不到就不显示 pill，不为此报错打扰 */
  }
}

async function poll() {
  if (!userStore.isLoggedIn) return
  try {
    const res = await api.get<never, { unread_count: number }>('/api/user/messages/unread-count')
    unreadCount.value = res?.unread_count || 0
  } catch {
    /* 静默失败 */
  }
  refreshSubscription()
}

/**
 * 窄屏的主导航是横向滑动的选项卡：保证当前项始终看得见。
 *
 * - center=true（切换页面）：把当前项滑到中间，点完立刻能看到高亮；
 * - center=false（首次挂载）：只保证看得见（深链直接落在第 5 个入口时不用手动滑）。
 * 用 scrollBy 只滚这一条，不会带着整页横向滑动；宽屏下整条都能放下，直接返回。
 */
async function focusActiveTab(center: boolean) {
  await nextTick()
  const box = navRef.value
  const el = box?.querySelector<HTMLElement>('.nav-link-active')
  if (!box || !el) return
  if (box.scrollWidth <= box.clientWidth) return

  const boxRect = box.getBoundingClientRect()
  const elRect = el.getBoundingClientRect()

  if (center) {
    const delta = elRect.left - boxRect.left - (boxRect.width - elRect.width) / 2
    const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    box.scrollBy({ left: delta, behavior: reduceMotion ? 'auto' : 'smooth' })
    return
  }
  if (elRect.left < boxRect.left || elRect.right > boxRect.right) {
    box.scrollBy({ left: elRect.left - boxRect.left - 12, behavior: 'auto' })
  }
}

// 补给站操作后回到任意页面时，头像菜单里的会员状态、顶栏能量即时刷新
//（2026-10-13：/wallet 旧页面已删；/game/checkin 报到后能量/体力变化，
// 回 /game 时也需要刷新顶栏体力值 —— R27 加入 economyPaths）
watch(() => route.path, (p, old) => {
  const economyPaths = ['/shop', '/game/checkin']
  if (userStore.isLoggedIn && (economyPaths.includes(old || '') || economyPaths.includes(p))) {
    refreshSubscription()
    loadPoints()
    loadVitality()
  }
})

watch(() => userStore.isLoggedIn, (loggedIn) => {
  if (loggedIn) {
    poll()
    loadPoints()
    loadVitality()
  } else {
    unreadCount.value = 0
    activeSub.value = null
    msgPreview.value = []
    msgMenuOpen.value = false
    userMenuOpen.value = false
    pointsBalance.value = null
    vitality.value = null
  }
})

// 换页后把当前选项卡滑到中间（宽屏下是空操作）
watch(() => route.path, () => focusActiveTab(true))

/** 未读轮询定时器句柄：SPA 内组件反复挂载时不清会累积泄漏，卸载时统一清掉 */
let pollTimer = 0

/** 顶栏滚动态（DESIGN.md §5-7）：未滚过时融入纸面，滚过才加 blur(12px)+1px 边框 */
const headerScrolled = ref(false)
function onPageScroll() {
  headerScrolled.value = window.scrollY > 8
}

onMounted(() => {
  document.addEventListener('click', onDocClick)
  document.addEventListener('keydown', onDocKeydown)
  window.addEventListener('resize', onViewportChange)
  window.addEventListener('orientationchange', onViewportChange)
  window.addEventListener('scroll', onPageScroll, { passive: true })
  onPageScroll()
  poll()
  loadPoints()
  loadVitality()
  pollTimer = window.setInterval(poll, 60_000)
  focusActiveTab(false)
})
onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick)
  document.removeEventListener('keydown', onDocKeydown)
  window.removeEventListener('resize', onViewportChange)
  window.removeEventListener('orientationchange', onViewportChange)
  window.removeEventListener('scroll', onPageScroll)
  window.clearInterval(pollTimer)
})
</script>

<template>
  <header class="app-header" :class="{ 'is-scrolled': headerScrolled }">
    <div class="header-container">
      <RouterLink to="/" class="header-logo" @click="closeMenus">
        <span class="logo-mark">
          <img decoding="async" fetchpriority="high" v-if="branding.logo_url" :src="branding.logo_url" :alt="branding.site_name" />
          <Clapperboard v-else :size="17" />
        </span>
        <span class="logo-text">{{ branding.site_name }}</span>
      </RouterLink>

      <!-- 主导航（全断点唯一一套）：≥900px 横排在品牌与账号操作之间，
           769~900px 落到第二行、变成可横向滑动的选项卡，
           ≤768px 整条让位给底部导航坞（PanelTabbar） -->
      <nav ref="navRef" class="main-nav" aria-label="主导航">
        <RouterLink
          v-for="item in primaryNav"
          :key="item.path"
          :to="item.path"
          class="nav-link"
          :class="{ 'nav-link-active': isActive(item.path) }"
        >
          <component :is="item.icon" :size="15" />
          {{ item.name }}
        </RouterLink>
      </nav>

      <!-- 右侧用户区（v2.42.9）：外观 + 消息 + 头像，三枚同一配方的圆形图标按钮，
           与管理后台顶栏同一形态。
           外观按钮放在登录判断**之外**：未登录也能切主题。点一下直接在白日/黑暗
           之间切换，不要下拉菜单 -->
      <div class="user-section">
        <!-- 积分徽章：登录后显示当前积分，点击进能量账本 -->
        <RouterLink
          v-if="userStore.isLoggedIn && pointsBalance !== null"
          to="/game/ledger"
          class="points-pill"
          title="我的能量，点击查看能量账本"
          aria-label="我的能量，点击查看能量账本"
        >
          <Sparkles :size="13" />
          <span>{{ pointsBalance }}</span>
        </RouterLink>
        <!-- 体力徽章：显示 x/14（与报到页体力条统一口径），点击进报到页 -->
        <RouterLink
          v-if="userStore.isLoggedIn && vitality !== null && vitalityMax !== null"
          to="/game/checkin"
          class="points-pill vitality-pill"
          :title="`我的体力 ${vitality}/${vitalityMax}，点击去报到`"
          :aria-label="`我的体力 ${vitality}/${vitalityMax}，点击去报到`"
        >
          <Heart :size="13" /><!-- 体力用红心（与报到页 StaminaBar 的 ❤️ 统一）；闪电只给能量 -->
          <span>{{ vitality }}/{{ vitalityMax }}</span>
        </RouterLink>
        <button
          class="theme-btn round-btn wide-only"
          :class="{ auto: themePreference === 'system' }"
          :title="themeTitle"
          :aria-label="themeTitle"
          @click="toggleTheme"
        >
          <component :is="themeCurrent.icon" :size="18" />
        </button>

        <template v-if="userStore.isLoggedIn">
          <!-- 消息：保持一个安静的音铃（不在顶栏抢文案），点开先给预览 -->
          <div ref="msgMenuRef" class="msg-menu wide-only" :class="{ 'has-unread': unreadCount > 0 }">
            <button
              class="msg-btn round-btn"
              :class="{ alert: unreadCount > 0, open: msgMenuOpen }"
              :title="unreadCount > 0 ? `${unreadCount} 条未读消息` : '信鸽'"
              :aria-label="unreadCount > 0 ? `${unreadCount} 条未读消息` : '信鸽'"
              @click="toggleMsgMenu"
            >
              <Inbox :size="18" />
              <span v-if="unreadCount > 0" class="msg-badge">
                {{ unreadCount > 99 ? '99+' : unreadCount }}
              </span>
            </button>

            <!-- 下拉用 Teleport 挂到 body：header 的 backdrop-filter 会把它变成
                 fixed 后代的包含块，fixed 定位会相对 header 而非视口（真机自测抓到过） -->
            <Teleport to="body">
            <Transition name="dd">
              <div v-if="msgMenuOpen" class="msg-dropdown" :style="dropStyle">
                <div class="msg-drop-head">
                  <span class="msg-drop-title">信鸽</span>
                  <span v-if="unreadCount > 0" class="msg-drop-unread">{{ unreadCount > 99 ? '99+' : unreadCount }} 条未读</span>
                  <span v-else class="msg-drop-clear">已全部读完</span>
                </div>

                <div v-if="msgLoading" class="msg-drop-loading" role="status" aria-label="正在拉取最新消息">
                  <div class="au-skeleton"></div>
                  <div class="au-skeleton"></div>
                </div>
                <template v-else-if="msgPreview.length">
                  <RouterLink
                    v-for="p in msgPreview"
                    :key="p.key"
                    :to="p.to"
                    class="msg-drop-item"
                    @click="closeMenus"
                  >
                    <span class="msg-drop-ic" :class="{ hot: p.unread }">
                      <component :is="p.icon" :size="14" />
                    </span>
                    <span class="msg-drop-body">
                      <span class="msg-drop-item-title">{{ p.title }}</span>
                      <span class="msg-drop-meta">
                        {{ p.meta }}<template v-if="(p.count || 1) > 1"> · 同名 {{ p.count }}{{ p.countExact === false ? '+' : '' }} 条</template>
                      </span>
                    </span>
                    <span v-if="p.unread" class="msg-drop-dot" aria-hidden="true"></span>
                  </RouterLink>
                </template>
                <p v-else class="msg-drop-hint">暂时没有新消息</p>

                <RouterLink to="/game/pigeon" class="msg-drop-foot" @click="closeMenus">
                  查看全部消息
                  <ChevronRight :size="13" />
                </RouterLink>
              </div>
            </Transition>
            </Teleport>
          </div>

          <div ref="userMenuRef" class="user-menu">
            <button
              ref="userBtnRef"
              class="user-btn round-btn"
              :aria-expanded="userMenuOpen"
              aria-haspopup="menu"
              :aria-label="unreadCount > 0 ? `${displayName} · 账号菜单 · ${unreadCount} 条未读消息` : `${displayName} · 账号菜单`"
              :title="displayName"
              @click="toggleUserMenu"
            >
              <span class="avatar">{{ displayName.charAt(0).toUpperCase() }}</span>
              <!-- ≤420px 消息铃铛隐藏：有未读时头像右上角点一颗小圆点提示 -->
              <span v-if="unreadCount > 0" class="avatar-dot narrow-only" aria-hidden="true"></span>
            </button>

            <Teleport to="body">
            <Transition name="dd">
              <!-- 用户菜单（v2.42.3 纸片人式重做）。Teleport 到 body 后 fixed 定位真正相对视口：
                   ≤768px 底边锚在拇指区（底部坞）之上、内部滚动；z-index 60 在根层高于
                   顶栏（50）与底部坞（在其上下文内），低于 Toast（120）与弹窗（80 不冲突：
                   弹窗打开时应盖住菜单）。点击外部关闭的判定见 onDocClick 的 closest 兼容 -->
              <div v-if="userMenuOpen" class="user-dropdown" role="menu" :style="dropStyle">
                <!-- ① 头部：头像 + 昵称 + @id + 状态徽章 -->
                <div class="dropdown-head">
                  <span class="avatar dropdown-avatar">{{ displayName.charAt(0).toUpperCase() }}</span>
                  <span class="dropdown-id">
                    <span class="dropdown-username">{{ displayName }}</span>
                    <span v-if="userIdLabel" class="dropdown-uid">{{ userIdLabel }}</span>
                    <span v-if="wornBadge" class="dropdown-worn" :title="`佩戴中的徽章：${wornBadge.name}`">{{ wornBadge.icon }} {{ wornBadge.name }}</span>
                  </span>
                  <span v-if="menuBadge" class="dropdown-status" :class="menuBadge.cls">
                    <component :is="menuBadge.icon" :size="11" />
                    {{ menuBadge.text }}
                  </span>
                </div>

                <!-- ② 探险设施直达（v4.1）：大本营 + 10 个游戏设施，图标+名称+描述，点卡片直达。
                     设施清单来自 @/config/game.ts，这里不 hardcode。
                     设施为空时整个分区（含标题）不渲染，不留裸奔标题。
                     原 12 个通用菜单项（探险预告/探险日志/我的订单/能量账本/
                     每日报到/邀请返利/许愿池中心/AI 助手/求救信号支持/信鸽/
                     外观/管理后台）已从头像菜单移除，对应页面路由保留；
                     头像菜单不得再出现信鸽/外观行（用户铁律：已删条目不许加回，
                     scripts/check_avatar_menu_guards.py 在 CI 拦住；窄屏外观切换走后续专项决策）。
                     管理后台例外：2026-10-13 用户拍板恢复，仅管理员可见（见下面 ②.6） -->
                <template v-if="menuFacilities.length > 0">
                <div class="dropdown-sec-title">🏕️ 营地设施 · 点卡片直接进</div>
                <RouterLink
                  to="/game"
                  class="dropdown-facility"
                  role="menuitem"
                  @click="closeMenus"
                >
                  <span class="facility-icon" aria-hidden="true">🏕️</span>
                  <span class="facility-body">
                    <span class="facility-name">{{ gameHubConfig.navName }} · 大本营</span>
                    <span class="facility-desc">{{ gameHubConfig.subtitle }}</span>
                  </span>
                  <span class="facility-go" aria-hidden="true">›</span>
                </RouterLink>
                <RouterLink
                  v-for="f in menuFacilities"
                  :key="f.key"
                  :to="f.path"
                  class="dropdown-facility"
                  role="menuitem"
                  @click="closeMenus"
                >
                  <span class="facility-icon" aria-hidden="true">{{ f.icon }}</span>
                  <span class="facility-body">
                    <span class="facility-name">{{ f.name }}</span>
                    <span class="facility-desc">{{ f.desc }}</span>
                  </span>
                  <span class="facility-go" aria-hidden="true">›</span>
                </RouterLink>
                </template>

                <!-- ②.6 管理后台（2026-10-13 用户拍板恢复）：仅管理员账号（is_staff）渲染，
                     同源同 JWT 直达 /admin/，免二次登录；普通用户完全看不见。
                     注意用 <a> 不用 RouterLink：/admin/ 是另一个前端应用。 -->
                <a
                  v-if="isStaff"
                  href="/admin/"
                  class="dropdown-item"
                  role="menuitem"
                >
                  <ShieldCheck :size="15" /> 管理后台
                </a>

                <!-- ③ 退出登录：独立红色分区（border-t 分隔） -->
                <button class="dropdown-item dropdown-logout" role="menuitem" @click="handleLogout">
                  <LogOut :size="15" /> 退出登录
                </button>
              </div>
            </Transition>
            </Teleport>
          </div>
        </template>

        <template v-else>
          <RouterLink to="/login" class="login-btn">登录</RouterLink>
        </template>
      </div>
    </div>
  </header>
</template>

<style scoped>
.app-header {
  position: sticky;
  top: 0;
  /* PWA 刘海屏：顶栏垫进状态栏安全区（非刘海机 env 为 0，无副作用） */
  padding-top: env(safe-area-inset-top, 0px);
  z-index: 50;
  /* DESIGN.md §5-7：内容未滚过时顶栏融入纸面（透明、无模糊、无边框）；
     内容滚动到顶栏下方时才加 blur(12px) + 1px #e3e3e2 下边框 */
  background: transparent;
  border-bottom: 1px solid transparent;
  overflow: hidden;
  transition: background-color var(--paper-fast, 0.16s) var(--paper-ease, ease),
    border-color var(--paper-fast, 0.16s) var(--paper-ease, ease);
}
.app-header.is-scrolled {
  background: var(--paper-chrome);
  -webkit-backdrop-filter: saturate(1.4) blur(12px);
  backdrop-filter: saturate(1.4) blur(12px);
  border-bottom: 1px solid #e3e3e2;
}

.header-container {
  position: relative;
  z-index: 1;
  max-width: 1080px;
  margin: 0 auto;
  padding: 0 1.25rem;
  min-height: 62px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
}

.header-logo {
  position: relative;
  display: flex;
  align-items: center;
  gap: 0.5rem;
  text-decoration: none;
  flex-shrink: 0;
}

/* 触点 44px：::after 透明扩展，品牌行视觉不变 */
.header-logo::after { content: ""; position: absolute; top: -10px; right: -8px; bottom: -10px; left: -8px; }

.logo-mark {
  width: 26px;
  height: 26px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--paper-r-sm);
  color: var(--paper-primary);
}

.logo-mark img {
  width: 100%;
  height: 100%;
  object-fit: contain;
  border-radius: inherit;
}

/* 品牌字：衬线 + 大写 + 拉开字距（片头字幕的气质） */
.logo-text {
  font-family: var(--paper-font-serif);
  font-size: 1.0625rem;
  font-weight: 700;
  letter-spacing: 0.28em;
  text-transform: uppercase;
  color: var(--paper-ink);
}

.main-nav {
  display: flex;
  align-items: center;
  gap: 0.375rem;
  min-width: 0;
}

/* 导航（DESIGN.md）：安静的纯文字，不再有胶囊底与图标；
   当前页 = 深青文字 + 下方一颗深青小圆点（#016a71，全站唯一彩色） */
.nav-link {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 0.375rem;
  padding: 0.4375rem 0.75rem;
  border: none;
  border-radius: var(--paper-r-sm);
  font-size: 0.875rem;
  font-weight: 500;
  letter-spacing: 0.04em;
  white-space: nowrap;
  color: var(--paper-ink-3);
  text-decoration: none;
  transition: color var(--paper-fast) var(--paper-ease);
}

/* 触点 44px：::before 透明扩展，安静纯文字导航视觉不变（::after 留给当前页圆点） */
.nav-link::before { content: ""; position: absolute; top: -7px; right: 0; bottom: -7px; left: 0; }

.nav-link svg { display: none; }

.nav-link:hover {
  color: var(--paper-ink);
}

.nav-link:active { transform: scale(0.98); }

.nav-link-active,
.nav-link-active:hover {
  color: #016a71;
  font-weight: 600;
}

.nav-link-active::after {
  content: '';
  position: absolute;
  left: 50%;
  bottom: 0;
  width: 4px;
  height: 4px;
  margin-left: -2px;
  border-radius: 50%;
  background: #016a71;
}

/* 键盘焦点环：与全站其余可聚焦元素同一配方（替代掉的 .theme-opt:focus-visible 同源） */
.nav-link:focus-visible {
  outline: 2px solid var(--paper-border-focus);
  outline-offset: 2px;
}

.user-section {
  display: flex;
  align-items: center;
  gap: 0.625rem;
  min-width: 0;
}

/* ==================== 右侧圆形图标按钮（v2.42.8） ====================
   与管理后台顶栏同一形态：一枚圆形按钮（浅色表面 + 1px 描边 + 居中的图标），
   hover 时描边与底色各抬一档。顶栏因此只剩「品牌 · 消息 · 头像」三件事——
   原常驻的能量 pill 与「会员生效中」pill 已删（图 4 圈出的两颗），
   能量仍在首页「我的资产」/ 钱包、会员状态仍在头像菜单头部各有入口。 */
.round-btn {
  position: relative;
  width: 44px;
  height: 44px;
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: 1px solid var(--paper-line);
  border-radius: var(--paper-r-full);
  background: var(--paper-card);
  color: var(--paper-ink-2);
  cursor: pointer;
  transition: background var(--paper-fast) var(--paper-ease),
    border-color var(--paper-fast) var(--paper-ease),
    color var(--paper-fast) var(--paper-ease);
}

.round-btn:hover {
  color: var(--paper-ink);
  background: var(--paper-card-2);
  border-color: var(--paper-line-strong);
}

.round-btn:active { transform: scale(0.96); }

.round-btn:focus-visible {
  outline: 2px solid var(--paper-border-focus);
  outline-offset: 2px;
}

/* ==================== 顶栏外观（v2.42.9 二改） ====================
   一改把外观从用户菜单搬到顶栏、做成「点一下轮换一档」的单枚圆钮——
   只有图标，发现性差（线上反馈「切换器被删了」）。二改：同一枚圆钮改成
   点开三选一的小菜单，当前档在菜单里用品牌色勾选——不是旧版三段切换器，
   也不需要盲猜轮换顺序。按钮的选中态分两类、靠底色一眼区分：
   · 手动指定档（白日 / 黑暗）→ 品牌色实底 + on-primary 图标 + 微光晕
     （与主导航当前页、菜单选中态同一套「主色实底压出来」的配方）；
   · 跟随系统 → 中性表面 + 品牌色细环 —— 表示这一档是生效中的「自动」，
     而不是「哪一个都没选」。 */
.theme-menu { position: relative; }

.theme-btn.auto { border-color: var(--paper-primary-border); }
.theme-btn.auto:hover { border-color: var(--paper-primary); }

.theme-btn:not(.auto) {
  background: var(--paper-primary);
  border-color: var(--paper-primary);
  color: var(--paper-on-primary);
}

.theme-btn:not(.auto):hover {
  background: var(--paper-primary-strong);
  border-color: var(--paper-primary-strong);
  color: var(--paper-on-primary);
}

/* 能量徽章：顶栏常驻小 pill，显示当前能量，点击进能量账本。
   2026-10-15 顶栏去圈（方案 A）：去掉胶囊边框和底色，只留图标+数字裸排；
   边框透明化而非删除，盒子尺寸与 ::after 44px 触点保持不变。 */
.points-pill {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 0.375rem;
  padding: 0.375rem 0.75rem;
  border: 1px solid transparent;
  border-radius: var(--paper-r-full);
  background: transparent;
  color: var(--paper-ink-2);
  font-size: 0.8125rem;
  font-weight: 700;
  text-decoration: none;
  white-space: nowrap;
  transition: color var(--paper-fast) var(--paper-ease);
}

/* 触点 44px：::after 透明扩展，胶囊视觉不变（R30 先例） */
.points-pill::after { content: ""; position: absolute; top: -9px; right: 0; bottom: -9px; left: 0; }

.points-pill:hover {
  color: var(--paper-ink);
}
.points-pill svg {
  color: var(--paper-primary);
  flex-shrink: 0;
}
/* 体力徽章：图标用红色（红心），与能量徽章的琥珀闪电区分 */
.vitality-pill svg {
  color: var(--paper-danger, #ff5c6c); /* 体力=红心（与报到页 ❤️ 体力条统一）；闪电的琥珀色只留给能量 */
}

/* 消息入口：2026-10-15 顶栏去圈（方案 A）——去掉圆形边框和底色，只留图标；
   44px 盒子、点击区域与 .msg-badge 角标位置不变。打开态/未读态只用图标颜色表达，
   不再引入背景色或边框（.round-btn 基类与 .theme-btn 不动）。 */
.msg-menu { position: relative; }

.msg-btn {
  border-color: transparent;
  background: transparent;
}
.msg-btn:hover {
  border-color: transparent;
  background: transparent;
}
.msg-btn.open {
  color: var(--paper-ink);
  background: transparent;
  border-color: transparent;
}
.msg-btn.alert { color: var(--paper-warning); border-color: transparent; background: transparent; }

.msg-badge {
  position: absolute;
  top: 4px;
  right: 4px;
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--paper-warning);
  color: var(--paper-on-warning);
  font-size: 0.8125rem;
  font-weight: 800;
  border-radius: var(--paper-r-full);
  /* 与页面底色同色的描边环，把徽章从任何背景上“抠”出来 */
  box-shadow: 0 0 0 2px var(--paper-overlay);
}

/* msg-dropdown / user-dropdown 及其内部样式已移至下方全局块（Teleport 到 body 后 scoped 不生效） */

.user-menu { position: relative; }

/* 头像按钮：2026-10-15 顶栏去圈（方案 A）——去掉圆形边框和底色；
   里面 30px 头像由圆形改圆角方（限定 .user-btn 内，下拉菜单头部的
   .dropdown-avatar 保持圆形）；金色底、白色字母、右上小圆点保留。 */
.user-btn {
  padding: 6px;
  border-color: transparent;
  background: transparent;
}
.user-btn:hover {
  border-color: transparent;
  background: transparent;
}
.user-btn .avatar {
  border-radius: 10px;
}

/* 头像右上角的未读点（只在 ≤420px 消息铃铛隐藏时出现） */
.avatar-dot {
  position: absolute;
  top: 1px;
  right: 1px;
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: var(--paper-warning);
  box-shadow: 0 0 0 2px var(--paper-bg);
}

.narrow-only { display: none; }

/* 佩戴中的徽章：昵称下方小红章，读 OutfitView 徽章墙写入的 localStorage */
.dropdown-worn {
  display: inline-block;
  font-size: 10px;
  font-weight: 800;
  color: var(--paper-danger);
  border: 1px solid var(--paper-danger);
  border-radius: 10px;
  padding: 1px 8px;
  margin-top: 4px;
  letter-spacing: 1px;
  max-width: 160px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.avatar {
  width: 30px;
  height: 30px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  background: var(--paper-primary);
  color: var(--paper-on-primary);
  font-size: 0.8125rem;
  font-weight: 800;
}

/* ==================== 用户菜单样式已移至全局块（Teleport） ==================== */

.login-btn {
  position: relative;
  display: inline-flex;
  align-items: center;
  height: 36px;
  padding: 0 1.125rem;
  background: var(--paper-primary);
  color: var(--paper-on-primary);
  border-radius: var(--paper-r-md);
  font-size: 0.8125rem;
  font-weight: 700;
  text-decoration: none;
  transition: transform var(--paper-fast);
}

/* 触点 44px：::after 透明扩展，琥珀按钮视觉尺寸不变（R30 先例） */
.login-btn::after { content: ""; position: absolute; top: -4px; right: -8px; bottom: -4px; left: -8px; }

.login-btn:hover { filter: brightness(1.08); }

/* 过渡：柔和上浮淡入（opacity+transform，合成器友好） */
.dd-enter-active, .dd-leave-active { transition: opacity var(--paper-med) var(--paper-ease), transform var(--paper-med) var(--paper-ease); }
.dd-enter-from, .dd-leave-to { opacity: 0; transform: translateY(-6px) scale(0.98); }

@media (prefers-reduced-motion: reduce) {
  .dd-enter-active, .dd-leave-active { transition: none; }
  /* 位移类 :active 缩放动效在减弱动态下关闭 */
  .nav-link:active, .round-btn:active, .dropdown-facility:active { transform: none; }
}

/* 窄屏：导航条目收窄（顶栏右侧只剩两枚圆形按钮，不再需要给用户名让宽度） */
@media (max-width: 1080px) {
  .nav-link { padding: 0.4688rem 0.625rem; }
}

/* 900~980px：品牌与账号操作都在一行时先去掉导航图标（比换行更像一根导航条） */
@media (max-width: 980px) and (min-width: 901px) {
  .nav-link { gap: 0; }
  .nav-link svg { display: none; }
}

/* 769~900px：窄屏保留两行形态（顶栏主导航仍是唯一导航，底部坞未出现） */
@media (max-width: 900px) {
  .header-container {
    flex-wrap: wrap;
    min-height: 0;
    padding: 0.5625rem 1rem 0;
    gap: 0.5rem;
  }

  .main-nav {
    order: 3;
    flex: 1 1 100%;
    margin-top: 0.125rem;
    padding: 0.5rem 0 0.5625rem;
    border-top: 1px solid var(--paper-line);
    overflow-x: auto;
    overscroll-behavior-x: contain;
    scrollbar-width: none;
    -ms-overflow-style: none;
    -webkit-overflow-scrolling: touch;
    /* 左右各留 14px 渐隐：提示「这一条还能继续滑」 */
    mask-image: linear-gradient(90deg, transparent 0, #000 14px, #000 calc(100% - 14px), transparent 100%);
    -webkit-mask-image: linear-gradient(90deg, transparent 0, #000 14px, #000 calc(100% - 14px), transparent 100%);
  }

  .main-nav::-webkit-scrollbar { display: none; }

  .nav-link {
    flex: 0 0 auto;
    padding: 0.4375rem 0.6875rem;
    border-radius: var(--paper-r-full);
    font-size: 0.8125rem;
  }

  .msg-dropdown { width: min(292px, calc(100vw - 1.5rem)); }
}

</style>

<!-- 下拉已 Teleport 到 body，scoped 样式不跨树：两个下拉的完整样式放全局块。
     data-v- 剔除后由类名本身保证作用域（user-dropdown / msg-dropdown 只在这一处渲染） -->
<style>
.msg-dropdown {
  position: absolute;
  right: 0;
  top: calc(100% + 8px);
  width: 292px;
  background: var(--paper-overlay-menu);
  border: 1px solid var(--paper-line-strong);
  border-radius: var(--paper-radius-lg);
  box-shadow: var(--paper-shadow-2);
  overflow: hidden;
  z-index: 60;
}

.user-dropdown {
  position: absolute;
  right: 0;
  top: calc(100% + 8px);
  width: 236px;
  background: var(--paper-overlay-menu);
  border: 1px solid var(--paper-line-strong);
  border-radius: var(--paper-radius-lg);
  box-shadow: var(--paper-shadow-2);
  overflow-y: auto;
  overscroll-behavior: contain;
  max-height: min(70vh, 480px);
  z-index: 60;
  scrollbar-width: thin;
}

@media (max-width: 768px) {
  /* 下拉菜单不超出屏幕 */
  .theme-dropdown, .msg-dropdown, .user-dropdown {
    max-width: calc(100vw - 32px);
  }
  /* 挂到 body 后 fixed 终于是真视口定位：底边锚在拇指区之上（--paper-dock-space
     由 aurora.css 按断点定义），宽度留 12px 呼吸边，高过视口时内部滚 */
  .user-dropdown,
  .msg-dropdown {
    position: fixed;
    top: auto;
    right: 12px;
    bottom: calc(var(--paper-dock-space) + 12px);
    width: min(320px, calc(100vw - 24px));
    max-height: calc(100dvh - 140px);
    overflow-y: auto;
  }
}

/* msg-dropdown 内部（原 scoped 规则原样搬来） */
.msg-drop-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.8125rem 1rem;
  border-bottom: 1px solid var(--paper-line);
}
.msg-drop-title { font-size: 0.875rem; font-weight: 700; color: var(--paper-ink); }
.msg-drop-unread {
  padding: 0.125rem 0.5rem;
  background: var(--paper-warning-soft);
  border-radius: var(--paper-r-full);
  color: var(--paper-warning);
  font-size: 0.8125rem;
  font-weight: 700;
}
.msg-drop-clear { font-size: 0.8125rem; color: var(--paper-ink-3); }
.msg-drop-item {
  display: flex;
  align-items: flex-start;
  gap: 0.625rem;
  padding: 0.6875rem 1rem;
  text-decoration: none;
  border-bottom: 1px solid var(--paper-line);
  transition: background var(--paper-fast) var(--paper-ease);
}
.msg-drop-item:hover { background: var(--paper-card-2); }
.msg-drop-ic {
  width: 26px;
  height: 26px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 8px;
  background: var(--paper-card-2);
  color: var(--paper-ink-3);
}
.msg-drop-ic.hot { background: var(--paper-warning-soft); color: var(--paper-warning); }
.msg-drop-body {
  display: flex;
  flex-direction: column;
  gap: 0.125rem;
  min-width: 0;
  flex: 1;
}
.msg-drop-item-title {
  font-size: 0.8125rem;
  font-weight: 600;
  color: var(--paper-ink);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.msg-drop-meta { font-size: 0.8125rem; color: var(--paper-ink-3); }
.msg-drop-dot {
  width: 6px;
  height: 6px;
  margin-top: 0.5rem;
  flex-shrink: 0;
  border-radius: 50%;
  background: var(--paper-warning);
}
.msg-drop-hint {
  margin: 0;
  padding: 1.125rem 1rem;
  text-align: center;
  font-size: 0.8125rem;
  color: var(--paper-ink-3);
  border-bottom: 1px solid var(--paper-line);
}
.msg-drop-loading {
  padding: 0.75rem 1rem;
  display: flex;
  flex-direction: column;
  gap: 0.625rem;
  border-bottom: 1px solid var(--paper-line);
}
.msg-drop-loading .au-skeleton { height: 44px; }
.msg-drop-foot {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 0.1875rem;
  padding: 0.875rem 1rem;
  font-size: 0.8125rem;
  font-weight: 600;
  color: var(--paper-primary);
  text-decoration: none;
  transition: background var(--paper-fast) var(--paper-ease);
}
.msg-drop-foot:hover { background: var(--paper-card-2); }

/* user-dropdown 内部 */
.dropdown-head {
  display: flex;
  align-items: center;
  gap: 0.625rem;
  padding: 0.875rem 1rem;
  border-bottom: 1px solid var(--paper-line);
}
.dropdown-avatar {
  width: 38px;
  height: 38px;
  flex-shrink: 0;
  font-size: 0.9375rem;
  box-shadow: 0 0 0 2px var(--paper-primary-soft);
}
.dropdown-id {
  display: flex;
  flex-direction: column;
  gap: 0.0625rem;
  min-width: 0;
  flex: 1;
}
.dropdown-username {
  font-size: 0.875rem;
  font-weight: 700;
  color: var(--paper-ink);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.dropdown-uid {
  font-size: 0.8125rem;
  color: var(--paper-ink-3);
  font-variant-numeric: tabular-nums;
}
.dropdown-status {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  gap: 0.1875rem;
  padding: 0.1875rem 0.5rem;
  border-radius: var(--paper-r-full);
  font-size: 0.8125rem;
  font-weight: 700;
}
.dropdown-status.gold {
  background: var(--paper-warning-soft);
  border: 1px solid var(--paper-warning-border);
  color: var(--paper-warning);
}
.dropdown-status.cyan {
  background: var(--paper-primary-soft);
  border: 1px solid var(--paper-primary-border);
  color: var(--paper-primary);
}
.dropdown-status.muted {
  background: var(--paper-card-2);
  border: 1px solid var(--paper-line);
  color: var(--paper-ink-3);
}
/* 外观三段切换器（.theme-seg / .theme-opt）与它的分组标题（.dropdown-group-title）
   已在 v2.42.9 删除：外观改由顶栏那枚圆形图标按钮承担（.theme-btn） */
.dropdown-item {
  display: flex;
  align-items: center;
  gap: 0.625rem;
  width: 100%;
  min-height: 44px;
  padding: 0.625rem 1rem;
  background: none;
  border: none;
  color: var(--paper-ink-2);
  font-size: 0.8125rem;
  text-decoration: none;
  cursor: pointer;
  transition: background-color var(--paper-fast) var(--paper-ease), color var(--paper-fast) var(--paper-ease);
  text-align: left;
}
.dropdown-item:hover { background: var(--paper-card-2); color: var(--paper-ink); }
.dropdown-item:active { background: var(--paper-card-3); }
.dropdown-item svg { color: var(--paper-ink-3); transition: color var(--paper-fast) var(--paper-ease); }
.dropdown-item:hover svg { color: var(--paper-primary); }
/* 探险设施直达（v4.1）：头像菜单里的 10 个游戏设施，纸色迷你卡片风。
   菜单 Teleport 到 body、不在 .v4-scope 内，v4 token 取不到，就地定义。
   卡片自带纸底色，深浅主题下都可读 */
.dropdown-sec-title {
  padding: 0.625rem 1rem 0.375rem;
  font-size: 0.75rem;
  font-weight: 700;
  letter-spacing: 1px;
  color: var(--paper-ink-3);
}
.dropdown-facility {
  /* 纸色 token 与 v4-paper.css .v4-scope 同步，改值只改那边，这里不许私自改 */
  --paper-card: #FFFDF6;
  --paper2: #EFE7D6;
  --line: #D8CDB8;
  --ink: #2A2620;
  --ink2: #6B6257;
  display: flex;
  align-items: center;
  gap: 0.625rem;
  margin: 0 0.5rem 0.375rem;
  padding: 0.5rem 0.625rem;
  background: var(--paper-card);
  border: 1.5px solid var(--line);
  border-radius: 10px;
  text-decoration: none;
  color: var(--ink);
  transition: transform 0.12s ease, border-color var(--paper-fast);
}
.dropdown-facility:hover { border-color: var(--ink); }
.dropdown-facility:active { transform: scale(0.98); }
/* 纸色风键盘焦点环：设施卡片 / 退出登录（与顶栏其余焦点环同配方，颜色取纸色墨色） */
.dropdown-facility:focus-visible,
.dropdown-logout:focus-visible {
  outline: 2px solid var(--ink, #2A2620);
  outline-offset: 2px;
}
.facility-icon {
  flex-shrink: 0;
  width: 36px;
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 20px;
  line-height: 1;
  background: var(--paper2);
  border: 1.5px solid var(--line);
  border-radius: 10px;
}
.facility-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}
.facility-name {
  font-size: 0.875rem;
  font-weight: 800;
  letter-spacing: 0.5px;
  color: var(--ink);
  white-space: nowrap;
}
.facility-desc {
  font-size: 0.75rem;
  color: var(--ink2);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.facility-go {
  flex-shrink: 0;
  font-size: 1.125rem;
  color: var(--ink2);
}
.dropdown-logout {
  color: var(--paper-danger);
  border-top: 1px solid var(--paper-line);
}
.dropdown-logout svg { color: var(--paper-danger); }
.dropdown-logout:hover { background: var(--paper-danger-soft); color: var(--paper-danger); }
.dropdown-logout:hover svg { color: var(--paper-danger); }
</style>

<style scoped>
/* ≤768px：顶栏退成单行（品牌 + 消息 + 头像），主导航交给底部坞（PanelTabbar）。
   两个下拉都改成 fixed 面板：① 挂在 header 的堆叠上下文里但 z 高于坞——
   底部坞是 fixed z-50（根级），下拉挂 body z-60，同视口层级 60 > 50 稳赢；
   ② max-height + 内部滚动保证菜单永不伸进底部坞的拇指区 */
@media (max-width: 768px) {
  .header-container {
    flex-wrap: nowrap;
    padding: 0 1rem;
    min-height: 54px;
    /* 手机顶栏与内容同宽（min(430px,100vw) 居中），消灭「头宽身窄」 */
    max-width: min(430px, 100vw);
    /* 手机顶栏是「品牌 + 外观 + 消息 + 头像」四件事（v2.42.9 起外观也常驻顶栏），
       间距用正常口径；双 pill 时代为塞下胶囊才把它压到 0.25rem */
    gap: 0.625rem;
  }

  /* 主导航整条让位给底部坞（PanelTabbar）；圆形按钮保持桌面口径不缩水 */
  .main-nav { display: none; }

  .header-logo { gap: 0.375rem; }
  .logo-mark { width: 30px; height: 30px; border-radius: 10px; }
}

/* 超窄屏（≤400px，含 iPhone SE（375））：两侧再各收 4px。
   三枚圆形按钮是拇指目标，尺寸不跟着缩——现在空出的宽度足够放下它们
   （375px 下：内距 24 + 品牌 ~91 + 三枚 114 + 两个 8px 间距 = ~245px） */
@media (max-width: 400px) {
  .header-container { padding: 0 0.75rem; gap: 0.5rem; }
}

/* ≤420px（iPhone 竖屏）：顶栏只留「品牌 · 能量 · 头像」三件事。
   外观切换按钮与消息铃铛带 .wide-only，在窄屏隐藏
   （头像菜单里的外观/信鸽行已按用户铁律删除，不再补位） */
@media (max-width: 420px) {
  .wide-only { display: none; }
  /* 有未读消息时窄屏也显示信鸽铃铛（权重 0,3,0 压过 .wide-only 0,1,0） */
  .msg-menu.wide-only.has-unread { display: block; }
  .narrow-only { display: block; }
  .logo-text { font-size: 0.9375rem; letter-spacing: 0.18em; }
  .logo-mark { width: 26px; height: 26px; }
  .points-pill { padding: 0.3125rem 0.625rem; }
}
</style>
