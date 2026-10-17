<template>
  <div class="v4-scope">
    <!-- 桌面「探险世界」：≥1024px + 已登录 + 用户选了世界视图时，首页整屏变探险地图；其余情况（手机/经典视图）走下面原有版式，一行不改 -->
    <WorldHome v-if="homeWorldActive" />
    <template v-else>
    <!-- C档第2批：≥1280px 三栏仪表盘（d3-root 网格）；<1280px 保持现行单栏，移动端零变化 -->
    <div class="v4-page d3-root">
      <!-- 左栏：问候 / 锚点 / 服务器卡 / 收藏家入口 / 脉搏摘要 / 每日一封 -->
      <div class="d3-left">
        <!-- 日志头：问候 + 封存件数 + 连签（移动端专属；桌面由问候卡接管） -->
        <header class="v4-loghead m-only d3-ord-loghead" aria-live="polite">
          <span class="v4-stamp">探险日志 · {{ dateLabel }}</span>
          <h1>{{ greeting }}，{{ nickname }}</h1>
          <p v-if="today.data && parcelTotal !== null && parcelTotal > 0">今天片库里有 {{ parcelTotal }} 件新封存，等你拆。</p>
          <p v-else-if="today.data && parcelTotal === 0">今天暂时没有新封存，明天再来看看。</p>
          <div v-else-if="today.loading" class="v4-skeleton" aria-hidden="true" style="height:28px;width:70%;margin-top:10px"></div>
          <div v-else-if="today.error" class="v4-err" style="margin-top:10px">今日概览加载失败：{{ today.error }}<br><button type="button" class="v4-retry" @click="() => panel.loadToday(true)">重试</button></div>
          <p v-else-if="today.data">今天的新封存数还没算出来 —— 看看下面的货架上有啥。</p>
          <p v-else>今天片库有更新，看看下面的货架上有啥。</p>
          <div v-if="streak > 0" class="v4-fire">🔥 连签 <b>{{ streak }}</b> 天 · 明天有惊喜</div>
          <!-- 经典视图回世界入口：视口 1024–1279px + 已登录 + 经典视图时出现；
               ≥1280px 时本 header（m-only）display:none 不渲染，桌面世界入口
               由右栏「世界」卡片承担 -->
          <button
            v-if="homeIsDesktop && user.isLoggedIn && homeMode === 'classic'"
            type="button"
            class="v4-world-switch"
            @click="setHomeMode('world')"
          >🗺 探险世界</button>
        </header>

        <!-- 个人问候卡（桌面专属） -->
        <GreetingCard v-if="isWide" class="d-only" />

        <!-- 区块锚点导航（桌面专属）：纯文字，激活态只许深青淡底 -->
        <nav v-if="isWide" class="d3-card d3-anchor d-only" aria-label="区块导航">
          <a
            v-for="a in anchors"
            :key="a.id"
            :href="'#' + a.id"
            :class="{ on: activeAnchor === a.id }"
            @click.prevent="jumpTo(a.id)"
          >{{ a.label }}</a>
        </nav>

        <!-- 首页服务器卡片：服清单数据驱动（发现页最显眼的位置） -->
        <div v-if="visibleCount === 0 && !serversLoading && !serversLoadError" class="v4-sec d3-ord-ticket">
          <div class="v4-empty">暂无可见服务器，去<RouterLink to="/game/settings">设置</RouterLink>恢复显示</div>
        </div>
        <TicketCard
          v-else
          class="d3-ord-ticket"
          :loading="serversLoading"
          :servers="visibleServers"
          :mode="cardMode"
          :watch-strip="watchStrip"
          :error="serversLoadError"
          :skeleton-count="lastServerCount"
          @retry="loadServers"
        />

        <!-- 收藏家入口：退出底栏后收进发现页（DESIGN.md v1.1）；页面本身保留在 /collector -->
        <RouterLink to="/collector" class="v4-collector-entry d3-ord-collector" aria-label="收藏家">
          <span class="v4-collector-ic" aria-hidden="true">🏅</span>
          <span class="v4-collector-body">
            <span class="v4-collector-title">收藏家</span>
            <span class="v4-collector-sub">你的成就与收藏，一览无余</span>
          </span>
          <span class="v4-collector-go" aria-hidden="true">›</span>
        </RouterLink>

        <!-- 脉搏一行摘要（桌面专属） -->
        <PulseLine v-if="isWide" class="d-only" />

        <!-- 每日一封 -->
        <section class="v4-sec v4-sec-letter d3-ord-letter" id="disc-letter">
          <div class="m-only">
            <div class="v4-sec-title">✉️ 每日一封</div>
            <div class="v4-sec-sub">昨天的你，留了一封信 —— 点开看看</div>
          </div>
          <div class="d3-sec-head d-only" style="margin-bottom:10px">
            <div><span class="d3-k">LETTER</span><div class="d3-sec-title">每日一封</div><div class="d3-sec-sub">昨天的你，留了一封信 —— 点开看看</div></div>
          </div>
          <div v-if="letter.loading" class="v4-skeleton" aria-hidden="true" style="height:120px"></div>
          <div v-else-if="letter.error" class="v4-err">信件加载失败：{{ letter.error }}<br><button type="button" class="v4-retry" @click="() => panel.loadLetter(true)">重试</button></div>
          <WaxLetter v-else-if="hasLetterData" :letter="letterData" />
          <div v-else class="v4-empty">昨天的信还在路上，先去看片吧</div>
        </section>
      </div>

      <!-- 中栏：今日封存 / 继续观看 /（移动端）服务器脉搏 -->
      <div class="d3-mid">
        <!-- 今日封存：盲盒 -->
        <section class="v4-sec d3-ord-shelf" id="disc-shelf">
          <div class="m-only">
            <div class="v4-sec-title">📦 今日封存</div>
            <div class="v4-sec-sub">新入库的片子，点一下开封看看</div>
          </div>
          <div class="d3-sec-head d-only">
            <div><span class="d3-k">FRESH IN</span><div class="d3-sec-title">今日封存</div><div class="d3-sec-sub">新入库的片子，点一下开封看看</div></div>
          </div>
          <div v-if="parcels.loading" class="v4-skel-row">
            <div class="v4-skeleton" aria-hidden="true" style="width:132px;height:216px;flex:0 0 auto"></div>
            <div class="v4-skeleton" aria-hidden="true" style="width:132px;height:216px;flex:0 0 auto"></div>
            <div class="v4-skeleton" aria-hidden="true" style="width:132px;height:216px;flex:0 0 auto"></div>
          </div>
          <div v-else-if="parcels.error" class="v4-err">封存加载失败：{{ parcels.error }}<br><button type="button" class="v4-retry" @click="() => panel.loadParcels(true)">重试</button></div>
          <div v-else-if="parcelList.length === 0" class="v4-empty">今天还没有新封存，明天再来拆 📦</div>
          <div v-else class="d3-shelf-wrap">
            <button v-if="isWide && shelfHasOverflow" type="button" class="d3-arrow l" aria-label="货架向左滚动" @click="scrollShelf(-1)">‹</button>
            <div ref="parcelsEl" class="v4-parcels">
              <ParcelBox
                v-for="p in parcelList"
                :key="String(p.id)"
                :parcel="p"
                :busy="unsealingIds.has(String(p.id))"
                :variant="isWide ? 'cover' : 'parcel'"
                @unseal="onUnseal"
              />
            </div>
            <button v-if="isWide && shelfHasOverflow" type="button" class="d3-arrow r" aria-label="货架向右滚动" @click="scrollShelf(1)">›</button>
          </div>
        </section>

        <!-- 继续观看（桌面专属；数据源见组件注释） -->
        <ContinueWatch v-if="isWide" />

        <!-- 实时动态流（桌面专属；C档第3批：后端 /api/panel/feed 已随 PR #1003 上线；
             无全量页面，故不设「查看全部」；realm_id 不传=跟随当前服） -->
        <section v-if="isWide" id="feed" class="d3-card" aria-label="实时动态">
          <div class="d3-sec-head">
            <div><span class="d3-k">LIVE FEED</span><div class="d3-sec-title">实时动态</div><div class="d3-sec-sub">入库 · 刮削 · 播放 · 跟随当前服</div></div>
          </div>
          <div v-if="feed.loading" class="d3-skel" style="height:300px" aria-hidden="true"></div>
          <div v-else-if="feed.error" class="d3-err">动态加载失败：{{ feed.error }}<button type="button" class="d3-retry" @click="() => panel.loadFeed(true)">重试</button></div>
          <div v-else-if="feedItems.length === 0" class="d3-empty">动态还没长出来——入库、刮削或播放后，这里会有记录</div>
          <div v-else>
            <div v-for="it in feedItems" :key="it.key" class="d3-feed-row">
              <span class="ft">{{ it.time }}</span>
              <span class="fx">{{ it.text }}</span>
              <span v-if="it.library" class="fk">{{ it.library }}</span>
            </div>
          </div>
        </section>

        <!-- 服务器脉搏（移动端专属；桌面由左栏脉搏摘要 + 服务器页接管） -->
        <section class="v4-sec m-only d3-ord-pulse">
          <div class="v4-sec-title">💓 服务器脉搏</div>
          <div class="v4-sec-sub">服务器实时动态</div>
        </section>
        <!-- pulse.error 时整盒不渲染，避免留黑色空矩形（错误态在下面统一渲染） -->
        <div v-if="!pulse.error" class="v4-pulse-box m-only d3-ord-pulse">
          <div class="v4-pulse-row">
            <span class="v4-pulse-tag">● 近况 · 服务器在忙什么</span>
            <span class="v4-pulse-dot"></span>
          </div>
          <!-- R38 P3-13：骨架 112px 与真实内容（EcgCanvas 64px + PulseActivity min-height:40px + margin-top:8px）同高，避免切屏跳动 -->
          <div v-if="pulse.loading" class="v4-skeleton" aria-hidden="true" style="height:112px"></div>
          <template v-else>
            <EcgCanvas />
            <PulseActivity :items="activityTexts" />
          </template>
        </div>
        <div v-if="pulse.error" class="v4-sec m-only d3-ord-pulse"><div class="v4-err">脉搏加载失败：{{ pulse.error }}<br><button type="button" class="v4-retry" @click="() => panel.loadPulse(true)">重试</button></div></div>
      </div>

      <!-- 右栏：世界入口 / 观影碑 / 告示牌 / 抽签 / 留言墙（桌面专属） -->
      <DiscoveryRightRail v-if="isWide" />
      <!-- 底部全宽：媒体库速览带（桌面专属） -->
      <LibraryBand v-if="isWide" />
    </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { usePanelStore } from '@/stores/panel'
import { useUserStore } from '@/stores/user'
import { useToast } from '@/composables/useToast'
import ParcelBox from '@/components/panel/ParcelBox.vue'
import EcgCanvas from '@/components/panel/EcgCanvas.vue'
import PulseActivity from '@/components/panel/PulseActivity.vue'
import WaxLetter from '@/components/panel/WaxLetter.vue'
import TicketCard from '@/components/panel/TicketCard.vue'
import WorldHome from '@/components/panel/WorldHome.vue'
import GreetingCard from '@/components/panel/GreetingCard.vue'
import PulseLine from '@/components/panel/PulseLine.vue'
import ContinueWatch from '@/components/panel/ContinueWatch.vue'
import DiscoveryRightRail from '@/components/panel/DiscoveryRightRail.vue'
import LibraryBand from '@/components/panel/LibraryBand.vue'
import { useHomeViewMode } from '@/composables/useHomeViewMode'
import { RouterLink } from 'vue-router'
import { useServerCards } from '@/composables/useServerCards'
import { useServerCardPrefs } from '@/composables/useServerCardPrefs'
import { humanizeError, type FeedItem, type ParcelItem, type ParcelsData } from '@/composables/usePanelApi'

const panel = usePanelStore()
const user = useUserStore()
// 首页形态：桌面世界视图（探险地图）/ 经典视图切换（偏好按用户存 localStorage）
const {
  worldActive: homeWorldActive,
  isDesktop: homeIsDesktop,
  mode: homeMode,
  setMode: setHomeMode,
} = useHomeViewMode()
const { today, parcels, pulse, letter, feed } = storeToRefs(panel)
// 首页服务器卡片：服清单数据驱动 + 混合展示 + 用户自选（逻辑见 useServerCards / useServerCardPrefs）
const serverCards = useServerCards()
const cardPrefs = useServerCardPrefs()
const { mode, isVisible } = cardPrefs
// 顶层解构：模板里直接用，ref 自动解包
const {
  loading: serversLoading,
  loadError: serversLoadError,
  servers: serverList,
  watchStrip,
  load: loadServers,
} = serverCards
// 用户关掉显示的服不进清单
const visibleServers = computed(() => serverList.value.filter((s) => isVisible(s.id)))
// 可见服数量（0 服时首页走空态，见模板）
const visibleCount = computed(() => visibleServers.value.length)
const LAST_SERVER_COUNT_KEY = 'aetrix:lastServerCount'
// 上一轮非空服数量：骨架屏按此渲染，避免数据到达后高度跳动（优先取本地存过的真实数量）
const lastServerCount = ref(2)
try {
  const n = Number(localStorage.getItem(LAST_SERVER_COUNT_KEY))
  if (Number.isInteger(n) && n > 0) lastServerCount.value = n
} catch { /* 取不到就用默认 2 */ }
watch(serverList, (v) => {
  if (v.length > 0) {
    lastServerCount.value = v.length
    try { localStorage.setItem(LAST_SERVER_COUNT_KEY, String(v.length)) } catch { /* 忽略 */ }
  }
})
// 展示形态：用户自选优先；自动档按可见服数量切换（1 个=大卡，多个=总览）
const cardMode = computed<'cards' | 'overview'>(() => {
  if (mode.value !== 'auto') return mode.value
  return visibleCount.value === 1 ? 'cards' : 'overview'
})

/* ---------- 桌面三栏断点（C档第2批：≥1280px 三栏，<1280px 现行单栏） ---------- */
const isWide = ref(false)
let wideMq: MediaQueryList | null = null
function syncWide() {
  isWide.value = !!wideMq?.matches
}

/* ---------- 区块锚点导航（桌面专属）：滚动联动高亮 ---------- */
const anchors = [
  { id: 'disc-shelf', label: '今日封存' },
  { id: 'disc-watch', label: '继续观看' },
  { id: 'disc-letter', label: '每日一封' },
  { id: 'feed', label: '实时动态' },
  { id: 'disc-libs', label: '媒体库' },
  { id: 'disc-world', label: '世界' },
]
const activeAnchor = ref('disc-shelf')
let spy: IntersectionObserver | null = null
function setupSpy() {
  if (!isWide.value || spy || typeof IntersectionObserver === 'undefined') return
  spy = new IntersectionObserver(
    (entries) => {
      for (const en of entries) {
        if (en.isIntersecting) activeAnchor.value = en.target.id
      }
    },
    { rootMargin: '-30% 0px -60% 0px' },
  )
  for (const a of anchors) {
    const el = document.getElementById(a.id)
    if (el) spy.observe(el)
  }
}
function teardownSpy() {
  spy?.disconnect()
  spy = null
}
function jumpTo(id: string) {
  activeAnchor.value = id
  document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}
watch(isWide, (v) => {
  if (v) void nextTick(() => setupSpy())
  else teardownSpy()
})

/* ---------- 货架桌面交互：左右箭头 + 鼠标拖拽（手机触屏横滑不动） ---------- */
function scrollShelf(dir: 1 | -1) {
  const el = parcelsEl.value
  if (!el) return
  el.scrollBy({ left: dir * Math.max(240, el.clientWidth * 0.8), behavior: 'smooth' })
}
let shelfDrag: { startX: number; startScroll: number; moved: boolean } | null = null
function onShelfPointerDown(e: PointerEvent) {
  if (!isWide.value || e.pointerType !== 'mouse' || e.button !== 0) return
  const el = parcelsEl.value
  if (!el) return
  shelfDrag = { startX: e.clientX, startScroll: el.scrollLeft, moved: false }
  const onMove = (ev: PointerEvent) => {
    if (!shelfDrag) return
    const dx = ev.clientX - shelfDrag.startX
    if (Math.abs(dx) > 6) shelfDrag.moved = true
    if (shelfDrag.moved) el.scrollLeft = shelfDrag.startScroll - dx
  }
  const onUp = () => {
    window.removeEventListener('pointermove', onMove)
    window.removeEventListener('pointerup', onUp)
    if (shelfDrag?.moved) {
      // 吞掉拖拽松手后的那次 click，避免误触开封
      const swallow = (ce: Event) => {
        ce.stopPropagation()
        ce.preventDefault()
      }
      el.addEventListener('click', swallow, { capture: true, once: true })
      setTimeout(() => el.removeEventListener('click', swallow, { capture: true }), 0)
    }
    shelfDrag = null
  }
  window.addEventListener('pointermove', onMove)
  window.addEventListener('pointerup', onUp)
}

let resizeRaf = 0
/** resize 高频触发时用 rAF 合并滚动提示重算 */
function onResize() {
  if (resizeRaf) return
  resizeRaf = requestAnimationFrame(() => {
    resizeRaf = 0
    updateScrollHint()
  })
}

onMounted(() => {
  // perf 2026-10-13：合并为 1 次请求（4 个 RTT → 1 个 RTT）
  void panel.loadHome()
  void panel.loadFeed()
  void loadServers()
  window.addEventListener('resize', onResize)
  wideMq = window.matchMedia('(min-width: 1280px)')
  syncWide()
  wideMq.addEventListener('change', syncWide)
  requestAnimationFrame(() => setupSpy())
})
onUnmounted(() => {
  window.removeEventListener('resize', onResize)
  wideMq?.removeEventListener('change', syncWide)
  teardownSpy()
  if (resizeRaf) cancelAnimationFrame(resizeRaf)
})

/* ---------- 日志头 ---------- */
const todayData = computed(() => (today.value.data || {}) as Record<string, unknown>)
const dateLabel = computed(() => {
  const raw = todayData.value.date as string | undefined
  if (raw) {
    // 'YYYY-MM-DD' 手动拆分后按本地时区构造，避免 new Date 按 UTC 解析导致日期偏差
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(raw)
    if (m) return `${Number(m[2])}.${Number(m[3])}`
    const d = new Date(raw)
    if (!Number.isNaN(d.getTime())) return `${d.getMonth() + 1}.${d.getDate()}`
  }
  const d = new Date()
  return `${d.getMonth() + 1}.${d.getDate()}`
})
const greeting = computed(() => {
  const g = todayData.value.greeting as string | undefined
  if (g) return g
  const h = new Date().getHours()
  if (h < 5) return '夜深了'
  if (h < 9) return '早上好'
  if (h < 12) return '上午好'
  if (h < 14) return '中午好'
  if (h < 18) return '下午好'
  return '晚上好'
})
const nickname = computed(() => {
  const n = todayData.value.nickname as string | undefined
  if (n) return n
  const u = user.user as Record<string, unknown> | null
  return (u?.nickname as string) || (u?.username as string) || '探险家'
})
const parcelTotal = computed(() => {
  const v = todayData.value.parcel_count
  return typeof v === 'number' ? v : null
})
const streak = computed(() => {
  const v = todayData.value.streak_days
  return typeof v === 'number' ? v : null
})

/* ---------- 今日封存 ---------- */
const parcelList = computed((): ParcelItem[] => {
  const d = parcels.value.data as ParcelsData | null
  return Array.isArray(d?.parcels) ? d.parcels : []
})
/* R29：货架右侧渐隐只在有溢出可滑时显示（照 EcoGarden can-scroll 模式） */
const parcelsEl = ref<HTMLElement | null>(null)
/* r43 P3：货架箭头也只在有溢出时显示（封存只有 1 件时箭头不再空转摆设） */
const shelfHasOverflow = ref(false)
function updateScrollHint() {
  const el = parcelsEl.value
  if (!el) { shelfHasOverflow.value = false; return }
  const overflow = el.scrollWidth > el.clientWidth + 1
  el.classList.toggle('can-scroll', overflow)
  shelfHasOverflow.value = overflow
  updateScrollEnd()
}
/** 滚到末尾时去掉右侧渐隐，避免用户误以为后面还有内容 */
function updateScrollEnd() {
  const el = parcelsEl.value
  if (!el) return
  el.classList.toggle('at-end', el.scrollLeft + el.clientWidth >= el.scrollWidth - 1)
}
function bindParcelsScroll() {
  const el = parcelsEl.value
  if (el && !el.dataset.scrollBound) {
    el.dataset.scrollBound = '1'
    el.addEventListener('scroll', updateScrollEnd, { passive: true })
    el.addEventListener('pointerdown', onShelfPointerDown)
  }
}
watch(parcelList, () => requestAnimationFrame(() => {
  updateScrollHint()
  bindParcelsScroll()
}), { immediate: true, flush: 'post' })
/* 拆封中 id 集合：busy 与请求生命周期绑定（请求完成/失败才释放），替代旧的固定 600ms 释放 */
const unsealingIds = ref(new Set<string>())
async function onUnseal(id: string | number) {
  const key = String(id)
  if (unsealingIds.value.has(key)) return
  unsealingIds.value.add(key)
  try {
    await panel.unseal(id)
  } catch (e) {
    // 失败不再静默：toast 告诉用户具体原因
    useToast().error(humanizeError(e))
  } finally {
    unsealingIds.value.delete(key)
  }
}

/* ---------- 服务器脉搏 ---------- */
/** 防御：后端曾把 Python dict 直接 str() 后塞进 text（如 {'level': 'down', ...}），
 *  这里把 message 抠出来，避免原始 dict 显示给用户。后端已修（panel.py），
 *  此为兜底（处理旧缓存/其他漏网路径）。 */
function cleanActivityText(t: unknown): string {
  // 非字符串直接给人话兜底，不暴露 [object Object] 这类原始结构
  if (typeof t !== 'string') return '服务器刚才忙去了，这条动态没写清楚'
  const s = t.trim()
  if (s.startsWith('{')) {
    if (/['"]message['"]/.test(s)) {
      const m1 = s.match(/'message'\s*:\s*'((?:[^'\\]|\\.)*)'/)
      if (m1) return m1[1].replace(/\\'/g, "'")
      const m2 = s.match(/"message"\s*:\s*"((?:[^"\\]|\\.)*)"/)
      if (m2) return m2[1].replace(/\\"/g, '"')
    }
    // 无 message 键的结构化残留：不暴露原始结构
    return '服务器刚才忙去了，这条动态没写清楚'
  }
  return s
}
const activityTexts = computed((): string[] => {
  const d = (pulse.value.data || {}) as Record<string, unknown>
  const acts = d.activities as Array<{ text?: string }> | undefined
  if (Array.isArray(acts)) {
    const texts = acts.map((a) => cleanActivityText(a?.text)).filter(Boolean)
    if (texts.length) return texts
  }
  return ['暂无动态']
})

/* ---------- 实时动态流（C档第3批） ---------- */
/** 事件时间展示：当天只显示 HH:MM（图纸样式），跨天带上 M-D */
function fmtFeedTime(at: string | null): string {
  if (!at) return ''
  const d = new Date(at)
  if (Number.isNaN(d.getTime())) return ''
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  const now = new Date()
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate()
  return sameDay ? `${hh}:${mm}` : `${d.getMonth() + 1}-${d.getDate()} ${hh}:${mm}`
}
const feedItems = computed((): Array<{ key: string; time: string; text: string; library: string | null }> => {
  const d = feed.value.data as { items?: FeedItem[] } | null
  const items = Array.isArray(d?.items) ? d.items : []
  return items.map((it, i) => ({
    key: `${i}-${it.at || ''}-${(it.text || '').slice(0, 12)}`,
    time: fmtFeedTime(it.at),
    text: typeof it.text === 'string' && it.text.trim() ? it.text : '服务器刚才忙去了，这条动态没写清楚',
    library: typeof it.library === 'string' && it.library ? it.library : null,
  }))
})

/* ---------- 每日一封 ---------- */
/** 非对象/数组一律视为空对象，避免后端异常返回时渲染"伪信封" */
const letterData = computed(() => {
  const d = letter.value.data
  return d !== null && typeof d === 'object' && !Array.isArray(d)
    ? (d as Record<string, unknown>)
    : {}
})
/** 空对象 = 后端没给信，不渲染"伪造"信封，走空态 */
const hasLetterData = computed(() => Object.keys(letterData.value).length > 0)
</script>

<style scoped>
/* 封存包裹行：横向滑动货架（见下方 .v4-parcels） */
/* 封存包裹行：横向滑动货架（单行横滑 + scroll-snap，行尾露一截提示可滑） */
.v4-parcels {
  display: flex;
  gap: 12px;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  overscroll-behavior-x: none;
  scroll-snap-type: x proximity;
  margin: 0 -20px;
  padding: 4px 36px 12px 20px;
  scrollbar-width: none;
}
/* R29：右侧渐隐只在货架有溢出可横滑时显示，无溢出不误导 */
.v4-parcels.can-scroll {
  -webkit-mask-image: linear-gradient(90deg, #000 0, #000 calc(100% - 44px), transparent 100%);
  mask-image: linear-gradient(90deg, #000 0, #000 calc(100% - 44px), transparent 100%);
}
.v4-parcels.can-scroll.at-end {
  -webkit-mask-image: none;
  mask-image: none;
}
.v4-parcels::-webkit-scrollbar { display: none; }
/* R29：封存骨架屏与横滑货架同形态（3 个 132×216 卡形横排） */
.v4-skel-row { display: flex; gap: 12px; overflow: hidden; margin: 0 -20px; padding: 4px 36px 12px 20px; }
.v4-sec-letter { padding-bottom: 20px; }
/* 脉搏黑盒：取自设计稿 .pulse-box */
.v4-pulse-box {
  background: var(--ink);
  border-radius: 14px;
  padding: 16px;
  margin: 0 20px;
  color: var(--paper2); /* R27：原 #EDE6D6，近似 --paper2 #EFE7D6，收敛 */
  position: relative;
  overflow: hidden;
}
.v4-pulse-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}
.v4-pulse-tag {
  font-size: 12px;
  letter-spacing: 2px;
  color: var(--pulse-green);
}
.v4-pulse-dot {
  width: 8px;
  height: 8px;
  border-radius: var(--paper-r-full, 50%);
  background: var(--pulse-green);
  animation: v4-blink 1.6s infinite;
}
@keyframes v4-blink {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.25; }
}

/* R27：减少动态偏好下常亮不闪 */
@media (prefers-reduced-motion: reduce) {
  .v4-pulse-dot {
    animation: none;
    opacity: 1;
  }
}

/* 收藏家入口卡（DESIGN.md v1.1：退出底栏后收进发现页）—— 纸面语言：浅卡 + 1px 发丝边 + 16px 圆角，零投影 */
.v4-collector-entry {
  display: flex;
  align-items: center;
  gap: 12px;
  margin: 14px 16px 0;
  padding: 12px 14px;
  min-height: 44px;
  background: var(--paper-card, #FFFDF6);
  border: 1px solid #d1d1cd;
  border-radius: 16px;
  text-decoration: none;
  color: var(--ink, #2A2620);
}
.v4-collector-ic {
  font-size: 24px;
  line-height: 1;
  flex-shrink: 0;
}
.v4-collector-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.v4-collector-title {
  font-size: 14px;
  font-weight: 500;
}
.v4-collector-sub {
  font-size: 12px;
  color: var(--ink2, #6B6257);
}
.v4-collector-go {
  font-size: 20px;
  color: var(--ink2, #6B6257);
  flex-shrink: 0;
}

/* 经典视图 → 探险世界入口（仅桌面渲染，显隐由模板 v-if 控制） */
.v4-world-switch {
  margin-top: 12px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: var(--ink);
  color: var(--paper2);
  border: none;
  border-radius: 999px;
  padding: 12px 18px;
  min-height: 44px;
  font-size: 13px;
  font-weight: 700;
  cursor: pointer;
}
</style>
