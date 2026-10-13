<script setup lang="ts">
import { RouterLink, useRouter, useRoute } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { ref, computed, watch, onMounted, onBeforeUnmount, nextTick } from 'vue'
import {
  Clapperboard, LogOut, Crown, Sparkles,
  Sun, Moon, MonitorSmartphone, Zap,
} from 'lucide-vue-next'
import {
  isExpiringSoon, subscriptionApi,
} from '@/api'
import type { MySubscription } from '@/api'
import { primaryNav } from '@/config/navigation'
/** v4.1：头像菜单改为「探险设施直达」，设施清单唯一数据源 */
import { gameFacilities } from '@/config/game'
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
/** 活力值（顶栏 pill 显示，仅公益服用户，403 则不显示） */
const vitality = ref<number | null>(null)
async function loadVitality() {
  if (!userStore.isLoggedIn) {
    vitality.value = null
    return
  }
  try {
    const st = await vitalityApi.status()
    vitality.value = st.vitality
  } catch {
    vitality.value = null
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

/**
 * 会员状态（v2.42.8 起只留在头像菜单里）：顶栏那两颗常驻 pill（积分 / 会员生效中）
 * 已按设计稿删除——积分由首页「我的资产」与钱包承接，会员状态由菜单头部承接。
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

/** v4.1：头像菜单改为「探险设施直达」——直接列出 @/config/game.ts 的 10 个
 *  游戏设施（图标+名称+描述），点卡片直达对应 /game/xxx 页面。
 *  设施开关状态由各游戏页自己处理（如报到未开启时页内提示），菜单不再过滤。
 *  退出登录独立成红色分区，不混在列表里 */
const menuFacilities = computed(() => gameFacilities)

/** 昵称展示：登录名可能是一长串邮箱，头像旁只取 @ 前段；@id 用数字 id */
const displayName = computed(() => {
  const raw = userStore.user?.username || ''
  const at = raw.indexOf('@')
  return at > 0 ? raw.slice(0, at) : raw || '用户'
})

const userIdLabel = computed(() => (userStore.user?.id ? `@${userStore.user.id}` : ''))


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
  userMenuOpen.value = !userMenuOpen.value
  if (userMenuOpen.value) anchorDropdown(userMenuRef.value)
}

/**
 * 导航分工（v2.6.30，v4.1 更新）：全站只有一份导航定义，见 src/config/navigation.ts。
 *
 *   primaryNav   → 顶栏（宽屏横排、窄屏第二行滑动选项卡），全断点同一批条目、同一个顺序
 *   头像菜单     → v4.1 起改为「探险设施直达」，设施清单见 src/config/game.ts
 */

function isActive(path: string) {
  if (path === '/') return route.path === '/'
  return route.path.startsWith(path)
}

function closeMenus() {
  userMenuOpen.value = false
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
  if (userMenuOpen.value) closeMenus()
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
    box.scrollBy({ left: delta, behavior: 'smooth' })
    return
  }
  if (elRect.left < boxRect.left || elRect.right > boxRect.right) {
    box.scrollBy({ left: elRect.left - boxRect.left - 12, behavior: 'auto' })
  }
}

// 签到 / 钱包操作后回到任意页面时，头像菜单里的会员状态、顶栏积分即时刷新
watch(() => route.path, (p, old) => {
  const economyPaths = ['/wallet', '/store']
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
    activeSub.value = null
    userMenuOpen.value = false
    pointsBalance.value = null
    vitality.value = null
  }
})

// 换页后把当前选项卡滑到中间（宽屏下是空操作）
watch(() => route.path, () => focusActiveTab(true))

onMounted(() => {
  document.addEventListener('click', onDocClick)
  document.addEventListener('keydown', onDocKeydown)
  window.addEventListener('resize', onViewportChange)
  window.addEventListener('orientationchange', onViewportChange)
  poll()
  loadPoints()
  loadVitality()
  window.setInterval(poll, 60_000)
  focusActiveTab(false)
})
onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick)
  document.removeEventListener('keydown', onDocKeydown)
  window.removeEventListener('resize', onViewportChange)
  window.removeEventListener('orientationchange', onViewportChange)
})
</script>

<template>
  <header class="app-header">
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
           ≤768px 整条让位给底部导航坞（AppDock） -->
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
        <!-- 积分徽章：登录后显示当前积分，点击进钱包 -->
        <RouterLink
          v-if="userStore.isLoggedIn && pointsBalance !== null"
          to="/wallet"
          class="points-pill"
          title="我的积分，点击查看钱包"
        >
          <Sparkles :size="13" />
          <span>{{ pointsBalance }}</span>
        </RouterLink>
        <!-- 活力值徽章：仅公益服用户显示，点击进钱包 -->
        <RouterLink
          v-if="userStore.isLoggedIn && vitality !== null"
          to="/wallet"
          class="points-pill vitality-pill"
          title="我的活力值，点击查看钱包"
        >
          <Zap :size="13" />
          <span>{{ vitality }}</span>
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
          <div ref="userMenuRef" class="user-menu">
            <button
              ref="userBtnRef"
              class="user-btn round-btn"
              :aria-expanded="userMenuOpen"
              aria-haspopup="menu"
              :aria-label="`${displayName} · 账号菜单`"
              :title="displayName"
              @click="toggleUserMenu"
            >
              <span class="avatar">{{ displayName.charAt(0).toUpperCase() }}</span>
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
                  </span>
                  <span v-if="menuBadge" class="dropdown-status" :class="menuBadge.cls">
                    <component :is="menuBadge.icon" :size="11" />
                    {{ menuBadge.text }}
                  </span>
                </div>

                <!-- ② 探险设施直达（v4.1）：10 个游戏设施，图标+名称+描述，点卡片直达。
                     设施清单来自 @/config/game.ts，这里不 hardcode。
                     设施为空时整个分区（含标题）不渲染，不留裸奔标题。
                     原 12 个通用菜单项（追新日历/探险日志/我的订单/积分流水/
                     每日签到/邀请返利/求片中心/AI 助手/工单支持/消息中心/
                     外观/管理后台）已从头像菜单移除，对应页面路由保留 -->
                <template v-if="menuFacilities.length > 0">
                <div class="dropdown-sec-title">🏕️ 营地设施 · 点卡片直接进</div>
                <RouterLink
                  v-for="f in menuFacilities"
                  :key="f.key"
                  :to="f.path"
                  class="dropdown-facility"
                  role="menuitemlink"
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
  z-index: 50;
  /* 暗房影院：顶栏 = 页面底色 0.96 + 模糊（滚过去的内容不再透出字形）+ 一根发丝线。
     两套主题都是同一个配方（--au-chrome 在浅色下是奶油纸色） */
  background: var(--au-chrome);
  -webkit-backdrop-filter: saturate(1.4) blur(16px);
  backdrop-filter: saturate(1.4) blur(16px);
  border-bottom: 1px solid var(--au-border);
  overflow: hidden;
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
  display: flex;
  align-items: center;
  gap: 0.5rem;
  text-decoration: none;
  flex-shrink: 0;
}

.logo-mark {
  width: 26px;
  height: 26px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--au-r-sm);
  color: var(--au-primary);
}

.logo-mark img {
  width: 100%;
  height: 100%;
  object-fit: contain;
  border-radius: inherit;
}

/* 品牌字：衬线 + 大写 + 拉开字距（片头字幕的气质） */
.logo-text {
  font-family: var(--au-font-serif);
  font-size: 1.0625rem;
  font-weight: 700;
  letter-spacing: 0.28em;
  text-transform: uppercase;
  color: var(--au-text);
}

.main-nav {
  display: flex;
  align-items: center;
  gap: 0.375rem;
  min-width: 0;
}

/* 导航（暗房影院）：安静的纯文字，不再有胶囊底与图标；
   当前页 = 正文色 + 下方一颗琥珀小圆点（像放映厅里那盏指示灯） */
.nav-link {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 0.375rem;
  padding: 0.4375rem 0.75rem;
  border: none;
  border-radius: var(--au-r-sm);
  font-size: 0.875rem;
  font-weight: 500;
  letter-spacing: 0.04em;
  white-space: nowrap;
  color: var(--au-text-3);
  text-decoration: none;
  transition: color var(--au-fast) var(--au-ease);
}

.nav-link svg { display: none; }

.nav-link:hover {
  color: var(--au-text);
}

.nav-link:active { transform: scale(0.98); }

.nav-link-active,
.nav-link-active:hover {
  color: var(--au-text);
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
  background: var(--au-primary);
}

/* 键盘焦点环：与全站其余可聚焦元素同一配方（替代掉的 .theme-opt:focus-visible 同源） */
.nav-link:focus-visible {
  outline: 2px solid var(--au-border-focus);
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
   原常驻的积分 pill 与「会员生效中」pill 已删（图 4 圈出的两颗），
   积分仍在首页「我的资产」/ 钱包、会员状态仍在头像菜单头部各有入口。 */
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

.theme-btn.auto { border-color: var(--au-primary-border); }
.theme-btn.auto:hover { border-color: var(--au-primary); }

.theme-btn:not(.auto) {
  background: var(--au-primary);
  border-color: var(--au-primary);
  color: var(--au-on-primary);
}

.theme-btn:not(.auto):hover {
  background: var(--au-primary-strong);
  border-color: var(--au-primary-strong);
  color: var(--au-on-primary);
}

/* 积分徽章：顶栏常驻小 pill，显示当前积分，点击进钱包 */
.points-pill {
  display: inline-flex;
  align-items: center;
  gap: 0.375rem;
  padding: 0.375rem 0.75rem;
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-full);
  background: var(--au-surface-2);
  color: var(--au-text-2);
  font-size: 0.8125rem;
  font-weight: 700;
  text-decoration: none;
  white-space: nowrap;
  transition: border-color var(--au-fast) var(--au-ease), color var(--au-fast) var(--au-ease);
}
.points-pill:hover {
  border-color: var(--au-primary);
  color: var(--au-text);
}
.points-pill svg {
  color: var(--au-primary);
  flex-shrink: 0;
}
/* 活力值徽章：图标用警示色，与积分徽章区分 */
.vitality-pill svg {
  color: var(--au-warning, #f59e0b);
}

/* user-dropdown 及其内部样式已移至下方全局块（Teleport 到 body 后 scoped 不生效） */

.user-menu { position: relative; }

/* 头像按钮：与消息按钮同一配方（.round-btn），里面正好嵌一颗 30px 头像
   （昵称不再出现在顶栏 → 菜单头部那一行仍是完整身份展示） */
.user-btn { padding: 3px; }

.narrow-only { display: none; }

.avatar {
  width: 30px;
  height: 30px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  background: var(--au-primary);
  color: var(--au-on-primary);
  font-size: 0.8125rem;
  font-weight: 800;
}

/* ==================== 用户菜单样式已移至全局块（Teleport） ==================== */

.login-btn {
  display: inline-flex;
  align-items: center;
  height: 36px;
  padding: 0 1.125rem;
  background: var(--au-primary);
  color: var(--au-on-primary);
  border-radius: var(--au-r-md);
  font-size: 0.8125rem;
  font-weight: 700;
  text-decoration: none;
  transition: transform var(--au-fast);
}
.login-btn:hover { filter: brightness(1.08); }

/* 过渡：柔和上浮淡入（opacity+transform，合成器友好） */
.dd-enter-active, .dd-leave-active { transition: opacity var(--au-med) var(--au-ease), transform var(--au-med) var(--au-ease); }
.dd-enter-from, .dd-leave-to { opacity: 0; transform: translateY(-6px) scale(0.98); }

@media (prefers-reduced-motion: reduce) {
  .dd-enter-active, .dd-leave-active { transition: none; }
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
    border-top: 1px solid var(--au-border);
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
    border-radius: var(--au-r-full);
    font-size: 0.8125rem;
  }

}

</style>

<!-- 下拉已 Teleport 到 body，scoped 样式不跨树：用户下拉的完整样式放全局块。
     data-v- 剔除后由类名本身保证作用域（user-dropdown 只在这一处渲染） -->
<style>

.user-dropdown {
  position: absolute;
  right: 0;
  top: calc(100% + 8px);
  width: 236px;
  background: var(--au-overlay-menu);
  border: 1px solid var(--au-border-strong);
  border-radius: var(--au-r-lg);
  box-shadow: var(--au-shadow-2);
  overflow-y: auto;
  overscroll-behavior: contain;
  max-height: min(70vh, 480px);
  z-index: 60;
  scrollbar-width: thin;
}

@media (max-width: 768px) {
  /* 下拉菜单不超出屏幕 */
  .theme-dropdown, .user-dropdown {
    max-width: calc(100vw - 32px);
  }
  /* 挂到 body 后 fixed 终于是真视口定位：底边锚在拇指区之上（--au-dock-space
     由 aurora.css 按断点定义），宽度留 12px 呼吸边，高过视口时内部滚 */
  .user-dropdown {
    position: fixed;
    top: auto;
    right: 12px;
    bottom: calc(var(--au-dock-space) + 12px);
    width: min(320px, calc(100vw - 24px));
    max-height: calc(100dvh - 140px);
    overflow-y: auto;
  }
}

/* user-dropdown 内部 */
.dropdown-head {
  display: flex;
  align-items: center;
  gap: 0.625rem;
  padding: 0.875rem 1rem;
  border-bottom: 1px solid var(--au-border);
}
.dropdown-avatar {
  width: 38px;
  height: 38px;
  flex-shrink: 0;
  font-size: 0.9375rem;
  box-shadow: 0 0 0 2px var(--au-primary-soft);
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
  color: var(--au-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.dropdown-uid {
  font-size: 0.8125rem;
  color: var(--au-text-3);
  font-variant-numeric: tabular-nums;
}
.dropdown-status {
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  gap: 0.1875rem;
  padding: 0.1875rem 0.5rem;
  border-radius: var(--au-r-full);
  font-size: 0.8125rem;
  font-weight: 700;
}
.dropdown-status.gold {
  background: var(--au-warning-soft);
  border: 1px solid var(--au-warning-border);
  color: var(--au-warning);
}
.dropdown-status.cyan {
  background: var(--au-primary-soft);
  border: 1px solid var(--au-primary-border);
  color: var(--au-primary);
}
.dropdown-status.muted {
  background: var(--au-surface-2);
  border: 1px solid var(--au-border);
  color: var(--au-text-3);
}
/* 外观三段切换器（.theme-seg / .theme-opt）与它的分组标题（.dropdown-group-title）
   已在 v2.42.9 删除：外观改由顶栏那枚圆形图标按钮承担（.theme-btn） */
.dropdown-item {
  display: flex;
  align-items: center;
  gap: 0.625rem;
  width: 100%;
  min-height: 40px;
  padding: 0.625rem 1rem;
  background: none;
  border: none;
  color: var(--au-text-2);
  font-size: 0.8125rem;
  text-decoration: none;
  cursor: pointer;
  transition: all var(--au-fast);
  text-align: left;
}
.dropdown-item:hover { background: var(--au-surface-2); color: var(--au-text); }
.dropdown-item:active { background: var(--au-surface-3); }
.dropdown-item svg { color: var(--au-text-3); transition: color var(--au-fast) var(--au-ease); }
.dropdown-item:hover svg { color: var(--au-primary); }
.dropdown-badge {
  margin-left: auto;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--au-primary);
  color: var(--au-on-primary);
  font-size: 0.8125rem;
  font-weight: 700;
  border-radius: var(--au-r-full);
}
/* 探险设施直达（v4.1）：头像菜单里的 10 个游戏设施，纸色迷你卡片风。
   菜单 Teleport 到 body、不在 .v4-scope 内，v4 token 取不到，就地定义。
   卡片自带纸底色，深浅主题下都可读 */
.dropdown-sec-title {
  padding: 0.625rem 1rem 0.375rem;
  font-size: 0.75rem;
  font-weight: 700;
  letter-spacing: 1px;
  color: var(--au-text-3);
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
  transition: transform 0.12s ease, border-color var(--au-fast);
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
  color: var(--au-danger);
  border-top: 1px solid var(--au-border);
}
.dropdown-logout svg { color: var(--au-danger); }
.dropdown-logout:hover { background: var(--au-danger-soft); color: var(--au-danger); }
.dropdown-logout:hover svg { color: var(--au-danger); }
</style>

<style scoped>
/* ≤768px：顶栏退成单行（品牌 + 消息 + 头像），主导航交给底部坞（AppDock）。
   两个下拉都改成 fixed 面板：① 挂在 header 的堆叠上下文里但 z 高于坞的绘制顺序问题
   已不存在——fixed 仍受 header 的 z-index:50 上下文约束，因此坞提到 40、
   下拉保持 60（见 AppDock 同步调整），同一上下文内 60 > 50 稳赢；
   ② max-height + 内部滚动保证菜单永不伸进底部坞的拇指区 */
@media (max-width: 768px) {
  .header-container {
    flex-wrap: nowrap;
    padding: 0 1rem;
    min-height: 54px;
    /* 手机顶栏是「品牌 + 外观 + 消息 + 头像」四件事（v2.42.9 起外观也常驻顶栏），
       间距用正常口径；双 pill 时代为塞下胶囊才把它压到 0.25rem */
    gap: 0.625rem;
  }

  /* 主导航整条让位给底部坞（AppDock）；圆形按钮保持桌面口径不缩水 */
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

/* ≤420px（iPhone 竖屏）：顶栏只留「品牌 · 积分 · 头像」三件事。
   外观切换与消息入口收进头像菜单（菜单里本就有「消息中心」一项，未读数照常显示），
   功能一样不少；品牌字距从 0.28em 收到 0.18em，免得四颗圆钮把品牌挤成两行 */
@media (max-width: 420px) {
  .wide-only { display: none; }
  .narrow-only { display: block; }
  .logo-text { font-size: 0.9375rem; letter-spacing: 0.18em; }
  .logo-mark { width: 26px; height: 26px; }
  .points-pill { padding: 0.3125rem 0.625rem; }
}
</style>
