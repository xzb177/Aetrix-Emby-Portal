<template>
  <div class="world-root" :class="[`dp-${daypart}`, { 'is-game': variant === 'game' }]">
    <div class="game-goalbar" v-if="variant === 'game'">
      <div class="m-goal-text">今日探险 <b>{{ questDoneCount }}/{{ quests.length }}</b><template v-if="questNextLabel"> · 还差：{{ questNextLabel }}</template><template v-else> · 全部完成 ✓</template></div>
      <div class="m-goal-bar"><i :style="{ width: questPercent + '%' }"></i></div>
    </div>

    <div class="world-col">
      <div class="map-frame">
      <div class="hud">
        <span class="brand">Aetrix<b>.</b></span>
        <span class="worldname">🗺 探险世界</span>
        <div class="right">
          <span class="pill" v-if="streak > 0">🔥 连签 {{ streak }} 天<template v-if="streakHint"> · {{ streakHint }}</template></span>
          <button type="button" class="pill amber" v-if="pointsText" @click="goLedger">🪙 {{ pointsText }} 积分</button>
          <span class="pill" v-if="memberInfo"><span class="lv">Lv.{{ memberInfo.level }}</span> {{ memberInfo.level_name }}</span>
          <button type="button" class="avatar" @click="goAccount" aria-label="我的账号">{{ avatarChar }}</button>
        </div>
      </div>

      <div class="hello" v-if="variant !== 'game'">
        <div class="d">{{ dateLine }}</div>
        <h2>{{ greetingText }}，{{ nickname }}</h2>
        <p v-if="remaining > 0">世界里有 <b>{{ remaining }} 个包裹</b> 等你拆<template v-if="hasLetter">，邮局里有一封昨天的信。</template><template v-else>。</template></p>
        <p v-else-if="hasLetter">邮局里有一封昨天的信，点邮局拆开看看。</p>
        <p v-else>今天片库有更新，去影院看看新到的片子。</p>
        <p class="hello-line" v-if="daypartLine">{{ daypartLine }}</p>
      </div>

      <button type="button" class="classic" v-if="variant === 'home'" @click="toClassic">经典视图 ⇄</button>

      <div class="world-scroll" role="region" aria-label="探险地图，可左右滑动">
      <div class="world">
      <WorldCanvas
        :subs="anchorSubs"
        :on="anchorHandlers"
        :remaining="remaining"
        :has-letter="hasLetter"
        :letter-read="letterRead"
        :fortune-drew="fortuneDrew"
        :avatar-char="avatarChar"
        :show-quest-path="showQuestPath"
      />
      </div>
      </div>
      <p class="map-scroll-hint">左右滑动，看完整营地</p>
      </div>
      <WorldMarks :open-region-key="openRegionKey" :open-region="onRegionClick" />
      <WorldPanels
        :feed-items="feedItems"
        :feed-state="feedState"
        :quests="quests"
        :quest-percent="questPercent"
        :on-quest="onQuest"
      />
    </div>

    <div class="w-overlay" v-if="overlay !== 'none'" @click.self="closeOverlay">
      <div class="w-sheet" :class="{ 'is-stele': overlay === 'stele', 'is-region': overlay === 'region' }" role="dialog" aria-modal="true" tabindex="-1" ref="sheetEl"
           :id="overlay === 'region' ? 'region-sheet' : undefined"
           :aria-labelledby="overlay !== 'stele' ? 'w-sheet-title' : undefined"
           :aria-label="overlay === 'stele' ? '观影碑' : undefined"
           @touchstart="onSheetTouchStart" @touchend="onSheetTouchEnd">
        <div class="w-grip" aria-hidden="true"></div>
        <div class="w-sheet-head" v-if="overlay !== 'stele'">
          <span class="w-sheet-title" id="w-sheet-title">{{ sheetTitle }}</span>
          <button type="button" class="w-sheet-close" @click="closeOverlay" aria-label="关闭">✕</button>
        </div>
        <ParcelsSheet
          v-if="overlay === 'parcels'"
          :parcel-list="parcelList"
          :parcels-loading="parcelsLoading"
          :unsealing-ids="unsealingIds"
          :on-unseal="onUnseal"
        />
        <LetterSheet v-else-if="overlay === 'letter'" :letter-data="letterData" />
        <SteleSheet v-else-if="overlay === 'stele'" :stele="stele" @close="closeOverlay" />
        <BoardSheet v-else-if="overlay === 'board'" :board-data="boardData" />
        <WallSheet
          v-else-if="overlay === 'wall'"
          v-model="wallInput"
          :wall-posts="wallPosts"
          :wall-loading="wallLoading"
          :wall-posting="wallPosting"
          :fmt-wall-time="fmtWallTime"
          @submit="submitWall"
        />
        <FortuneSheet
          v-else-if="overlay === 'fortune'"
          :fortune="fortune"
          :fortune-loading="fortuneLoading"
          :fortune-drew="fortuneDrew"
          :fortune-drawing="fortuneDrawing"
          @draw="drawFortune"
        />
        <RegionSheet
          v-else-if="overlay === 'region'"
          :lead="regionLead"
          :facilities="regionFacilities"
          :sub-for="subFor"
          :status-for="statusFor"
          :selected-key="selectedKey"
          @select="selectFromRegion"
        />
      </div>
    </div>
  </div>
</template>
<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { useRouter } from 'vue-router'
import { usePanelStore } from '@/stores/panel'
import { useUserStore } from '@/stores/user'
import { humanizeError, panelApi } from '@/composables/usePanelApi'
import type { ParcelItem, ParcelsData } from '@/composables/usePanelApi'
import { memberApi } from '@/api/economy'
import { useHomeViewMode } from '@/composables/useHomeViewMode'
import { useToast } from '@/composables/useToast'
import { gameFacilities, gameFacilityGroups, type GameFacility } from '@/config/game'
import WorldCanvas, { type WorldAnchorSubs, type WorldAnchorHandlers } from './WorldCanvas.vue'
import WorldMarks from './WorldMarks.vue'
import WorldPanels from './WorldPanels.vue'
import RegionSheet from './RegionSheet.vue'
import ParcelsSheet from './overlays/ParcelsSheet.vue'
import LetterSheet from './overlays/LetterSheet.vue'
import SteleSheet from './overlays/SteleSheet.vue'
import BoardSheet from './overlays/BoardSheet.vue'
import WallSheet from './overlays/WallSheet.vue'
import FortuneSheet from './overlays/FortuneSheet.vue'
import { useDaypart } from './composables/useDaypart'
import { useWorldQuests } from './composables/useWorldQuests'
import { useSteleTop } from './composables/useSteleTop'

/**
 * 世界地图总调度：只管 variant（home | game）、daypart、选中设施、overlaySheet。
 * 数据与业务逻辑与旧 WorldMap.vue 单文件版一一对应（拆分不改行为）。
 */
withDefaults(defineProps<{ variant?: 'home' | 'game' }>(), { variant: 'home' })

const router = useRouter()
const panel = usePanelStore()
const user = useUserStore()
const { setMode } = useHomeViewMode()

const { today, parcels, pulse, letter, board, collector } = storeToRefs(panel)

/* ---------- 本地状态 ---------- */
const memberRaw = ref<Record<string, unknown> | null>(null)

const overlay = ref<'none' | 'parcels' | 'letter' | 'stele' | 'board' | 'wall' | 'fortune' | 'region'>('none')
const regionKey = ref<string | null>(null)
const unsealingIds = ref(new Set<string>())
const sheetEl = ref<HTMLElement | null>(null)
let lastTrigger: HTMLElement | null = null

/* ---------- 基础数据 ---------- */
const todayD = computed<Record<string, unknown>>(
  () => (today.value.data ?? {}) as Record<string, unknown>,
)

const userRaw = computed(
  () => (user.user ?? null) as unknown as Record<string, unknown> | null,
)

const userId = computed(() => {
  const id = userRaw.value?.id
  return id === undefined || id === null || id === '' ? 'guest' : String(id)
})

const nickname = computed(() => {
  const n = todayD.value.nickname
  if (typeof n === 'string' && n.trim()) return n
  const u = userRaw.value
  const un = u?.nickname
  if (typeof un === 'string' && un.trim()) return un
  const name = u?.username
  if (typeof name === 'string' && name.trim()) return name
  return '探险家'
})

const avatarChar = computed(() => Array.from(nickname.value)[0] ?? '探')

const greetingText = computed(() => {
  const g = todayD.value.greeting
  if (typeof g === 'string' && g.trim()) return g
  return '你好'
})

const dateLine = computed(() => {
  const d = new Date()
  return `${d.getFullYear()} 年 ${d.getMonth() + 1} 月 ${d.getDate()} 日`
})

/* ---------- 昼夜（composable，自带分钟时钟） ---------- */
const { daypart } = useDaypart()

/* ---------- 积分 / 会员 / 副标题 ---------- */
const pointsText = computed<string | null>(() => {
  const c = quest.checkin.value?.points
  if (typeof c === 'number') return Math.trunc(c).toLocaleString('en-US')
  const d = collector.value.data
  if (d && typeof d === 'object' && !Array.isArray(d)) {
    const p = (d as Record<string, unknown>).points
    if (typeof p === 'number') return Math.trunc(p).toLocaleString('en-US')
  }
  return null
})

const memberInfo = computed<{ level: number; level_name: string } | null>(() => {
  const m = memberRaw.value
  if (!m) return null
  const lvl = m.level
  if (typeof lvl !== 'number') return null
  return { level: lvl, level_name: String(m.level_name || '探险家') }
})

const streak = computed(() => {
  const s = todayD.value.streak_days
  if (typeof s === 'number') return s
  const c = quest.checkin.value?.streak
  if (typeof c === 'number') return c
  const d = collector.value.data
  if (d && typeof d === 'object' && !Array.isArray(d)) {
    const cs = (d as Record<string, unknown>).streak
    if (typeof cs === 'number') return cs
  }
  return 0
})

const streakHint = computed(() => {
  const m = todayD.value.streak_milestone as
    | { next?: unknown; remaining?: unknown; warn_within?: unknown }
    | null
    | undefined
  if (!m) return ''
  const next = Number(m.next ?? 0)
  const remaining = Number(m.remaining ?? 0)
  const warnWithin = Number(m.warn_within ?? 0)
  if (remaining >= 1 && remaining <= warnWithin) {
    return remaining === 1 ? `明天满 ${next} 天` : `差 ${remaining} 天满 ${next} 天`
  }
  return ''
})

const daypartLine = computed(() => String(todayD.value.daypart_line ?? '').trim())

/* ---------- 包裹 ---------- */
const parcelList = computed<ParcelItem[]>(() => {
  const d = parcels.value.data
  if (!d || typeof d !== 'object' || Array.isArray(d)) return []
  const arr = (d as ParcelsData).parcels
  return Array.isArray(arr) ? (arr as ParcelItem[]) : []
})

const parcelsLoading = computed(() => parcels.value.loading === true)

const parcelCount = computed(() => {
  const n = todayD.value.parcel_count
  if (typeof n === 'number') return n
  return parcelList.value.length
})
const unsealedCount = computed(() => parcelList.value.filter((p) => p.opened).length)
const remaining = computed(() => parcelCount.value - unsealedCount.value)
const parcelsSub = computed(() => {
  if (parcels.value.data === null && !parcels.value.error) return '封存驿站'
  if (remaining.value <= 0) return '今日包裹已拆完'
  return `今日待拆 ${remaining.value} 个`
})

/* ---------- 信件 ---------- */
const letterData = computed<Record<string, unknown>>(
  () => (letter.value.data ?? {}) as Record<string, unknown>,
)
const hasLetter = computed(() => letterData.value.has === true)
const letterReadKey = computed(() => `aetrix:letter-read:${userId.value}:${letterDateKey.value}`)
const letterReadState = ref(false)
const letterDateKey = computed(() => {
  const d = new Date()
  return `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`
})

function refreshLetterRead() {
  try {
    letterReadState.value = localStorage.getItem(letterReadKey.value) === '1'
  } catch {
    letterReadState.value = false
  }
}

const letterRead = computed(() => letterReadState.value)

function markLetterRead() {
  letterReadState.value = true
  try {
    localStorage.setItem(letterReadKey.value, '1')
  } catch {
    /* 静默失败 */
  }
}

/* ---------- 建筑锚点动态副标题（与旧版一字不差） ---------- */
const cinemaSub = computed(() => {
  if (parcelCount.value <= 0) return '片库 · 随时开场'
  let base = `片库 · 今日新到 ${parcelCount.value} 部`
  const first = parcelList.value[0]
  const raw = String(first?.title || first?.name || '').trim()
  if (raw) {
    const name = raw.length > 7 ? `${raw.slice(0, 7)}…` : raw
    base += `，含《${name}》`
  }
  return base
})

const postSub = computed(() => {
  if (letter.value.data === null && !letter.value.error) return '邮局'
  if (!hasLetter.value) return '暂无新信'
  if (letterRead.value) return '昨天的信到了'
  const teaser = typeof letterData.value.teaser === 'string' ? letterData.value.teaser.trim() : ''
  if (teaser) {
    const t = teaser.length > 12 ? `${teaser.slice(0, 12)}…` : teaser
    return `信里说：『${t}』`
  }
  const rec = typeof letterData.value.recommendation === 'string' ? letterData.value.recommendation.trim() : ''
  if (rec) {
    const r = rec.length > 12 ? `${rec.slice(0, 12)}…` : rec
    return `今日推荐：『${r}』`
  }
  return '昨天的信到了'
})

const lighthouseSub = computed(() => {
  const d = pulse.value.data
  if (!d || typeof d !== 'object' || Array.isArray(d)) return '服务器'
  const health = (d as Record<string, unknown>).health
  if (health && typeof health === 'object' && !Array.isArray(health)) {
    const n = (health as Record<string, unknown>).issues_count
    if (typeof n === 'number' && n > 0) return `服务器 · 有 ${n} 件事在处理`
  }
  return '服务器 · 一切正常'
})

const collectorSub = computed(() => {
  const d = collector.value.data
  if (d && typeof d === 'object' && !Array.isArray(d)) {
    const rec = d as Record<string, unknown>
    const list = rec.achievements
    if (Array.isArray(list) && list.length > 0) {
      const lit = list.filter(
        (a) => a && typeof a === 'object' && (a as Record<string, unknown>).unlocked === true,
      ).length
      if (list.length > lit) {
        if (list.length - lit === 1) return `徽章墙 · ${lit}/${list.length} 枚 · 还差 1 枚`
        return `徽章墙 · 已点亮 ${lit}/${list.length} 枚`
      }
    }
    const n = rec.achievement_count
    if (typeof n === 'number') return `徽章墙 · 已点亮 ${n} 枚`
  }
  return '徽章墙'
})

/* ---------- 动态 ---------- */
function cleanActivityText(t: unknown): string {
  if (typeof t !== 'string') return '服务器刚才忙去了，这条动态没写清楚'
  const s = t.trim()
  if (s.startsWith('{')) {
    if (/['"]message['"]/.test(s)) {
      const m1 = s.match(/'message'\s*:\s*'((?:[^'\\]|\\.)*)'/)
      if (m1) return m1[1].replace(/\\'/g, "'")
      const m2 = s.match(/"message"\s*:\s*"((?:[^"\\]|\\.)*)"/)
      if (m2) return m2[1].replace(/\\"/g, '"')
    }
    return '服务器刚才忙去了，这条动态没写清楚'
  }
  return s
}

const feedItems = computed<Array<{ time: string; text: string }>>(() => {
  const d = pulse.value.data
  if (!d || typeof d !== 'object' || Array.isArray(d)) return []
  const acts = (d as Record<string, unknown>).activities
  if (!Array.isArray(acts)) return []
  const out: Array<{ time: string; text: string }> = []
  for (const a of acts.slice(0, 3)) {
    const rec = (a ?? {}) as Record<string, unknown>
    const text = cleanActivityText(rec.text)
    if (!text) continue
    let time = ''
    const at = rec.at
    if (typeof at === 'string' || typeof at === 'number') {
      const dt = new Date(at)
      if (!Number.isNaN(dt.getTime())) {
        time = `${String(dt.getHours()).padStart(2, '0')}:${String(dt.getMinutes()).padStart(2, '0')}`
      }
    }
    out.push({ time, text })
  }
  return out
})

const feedState = computed<'loading' | 'error' | 'ok'>(() => {
  if (pulse.value.error) return 'error'
  if (pulse.value.loading && !pulse.value.data) return 'loading'
  return 'ok'
})

/* ---------- 任务（composable） ---------- */
const quest = useWorldQuests({
  parcelCount: () => parcelCount.value,
  unsealedCount: () => unsealedCount.value,
  hasLetter: () => hasLetter.value,
  letterRead: () => letterRead.value,
  openParcels: () => openParcels(),
  openLetter: () => openLetter(),
})
const { quests, questPercent, questDoneCount, questNextLabel, onQuest } = quest
const showQuestPath = computed(() => questPercent.value < 100)

/* ---------- 观影碑（composable） ---------- */
const stele = useSteleTop({
  openSheet: (kind) => openOverlaySheet(kind),
  todayViewers: () => Number(todayD.value.today_viewers ?? 0),
  nickname: () => nickname.value,
})
const { steleSub } = stele

/* ---------- 服主告示牌 ---------- */
const boardData = computed<Record<string, unknown>>(
  () => (board.value.data ?? {}) as Record<string, unknown>,
)

const boardSub = computed(() => {
  if (board.value.data === null && !board.value.error) return '告示牌'
  if (boardData.value.visible === true) {
    const sig = String(boardData.value.signature ?? '服主')
    const text = typeof boardData.value.text === 'string' ? boardData.value.text.trim() : ''
    if (!text) return `服主留言 · ${sig}`
    const hook = text.length <= 12 ? text : `${text.slice(0, 12)}…`
    return `『${hook}』——${sig}`
  }
  return '等服主留言'
})

function openBoard() {
  openOverlaySheet('board')
}

/* ---------- 到此一游 · 留言墙 ---------- */
const wallPosts = ref<Array<Record<string, unknown>>>([])
const wallLoaded = ref(false)
const wallLoading = ref(false)
const wallInput = ref('')
const wallPosting = ref(false)

async function loadWall() {
  wallLoading.value = true
  try {
    const d = await panelApi.wallList()
    wallPosts.value = Array.isArray(d.posts) ? d.posts : []
  } catch {
    wallPosts.value = []
  } finally {
    wallLoading.value = false
    wallLoaded.value = true
  }
}

async function submitWall() {
  const c = wallInput.value.trim()
  if (!c || wallPosting.value) return
  if (c.length > 60) {
    useToast().warning('留言最多 60 字')
    return
  }
  wallPosting.value = true
  try {
    await panelApi.wallPost(c)
    wallInput.value = ''
    await loadWall()
  } catch (e) {
    useToast().error(humanizeError(e))
  } finally {
    wallPosting.value = false
  }
}

function parseWallTime(v: unknown): number {
  if (v === null || v === undefined || v === '') return NaN
  if (typeof v === 'number') return v < 1e12 ? v * 1000 : v
  if (typeof v === 'string') {
    const t = Date.parse(v)
    return Number.isNaN(t) ? NaN : t
  }
  return NaN
}

const wallSub = computed(() => {
  if (!wallLoaded.value) return '留言墙'
  const posts = wallPosts.value
  if (posts.length === 0) return '来留下第一句话吧'
  let sub = `已有 ${posts.length} 条足迹`
  let latest = -Infinity
  for (const p of posts) {
    const t = parseWallTime(p?.created_at)
    if (!Number.isNaN(t) && t > latest) latest = t
  }
  if (latest !== -Infinity) {
    const diff = Date.now() - latest
    if (diff >= 0) {
      const MIN = 60 * 1000
      const HOUR = 60 * MIN
      if (diff < HOUR) sub += ` · 最新 ${Math.max(1, Math.floor(diff / MIN))} 分钟前`
      else if (diff < 24 * HOUR) sub += ` · 最新 ${Math.floor(diff / HOUR)} 小时前`
    }
  }
  return sub
})

function openWall() {
  void loadWall()
  openOverlaySheet('wall')
}

function fmtWallTime(v: unknown): string {
  if (typeof v !== 'string' || !v) return ''
  const dt = new Date(v)
  if (Number.isNaN(dt.getTime())) return ''
  const now = new Date()
  const sameDay = dt.toDateString() === now.toDateString()
  const mm = String(dt.getMonth() + 1).padStart(2, '0')
  const dd = String(dt.getDate()).padStart(2, '0')
  const hh = String(dt.getHours()).padStart(2, '0')
  const mi = String(dt.getMinutes()).padStart(2, '0')
  return sameDay ? `${hh}:${mi}` : `${mm}-${dd} ${hh}:${mi}`
}

/* ---------- 每日抽一签 ---------- */
const fortune = ref<Record<string, unknown> | null>(null)
const fortuneLoading = ref(false)
const fortuneDrawing = ref(false)

async function loadFortune() {
  fortuneLoading.value = true
  try {
    fortune.value = await panelApi.fortuneToday()
  } catch {
    fortune.value = null
  } finally {
    fortuneLoading.value = false
  }
}

async function drawFortune() {
  if (fortuneDrawing.value) return
  fortuneDrawing.value = true
  try {
    fortune.value = await panelApi.fortuneDraw()
  } catch (e) {
    useToast().error(humanizeError(e))
  } finally {
    fortuneDrawing.value = false
  }
}

const fortuneDrew = computed(() => fortune.value?.drew === true)

const fortuneSub = computed(() => {
  if (fortune.value === null) return '抽签亭'
  return fortuneDrew.value ? `今日：${String(fortune.value?.level ?? '')}` : '每日一签，试试手气'
})

function openFortune() {
  void loadFortune()
  openOverlaySheet('fortune')
}

/* ---------- 建筑锚点绑定 ---------- */
const anchorSubs = computed<WorldAnchorSubs>(() => ({
  cinema: cinemaSub.value,
  parcels: parcelsSub.value,
  lighthouse: lighthouseSub.value,
  collector: collectorSub.value,
  post: postSub.value,
  stele: steleSub.value,
  board: boardSub.value,
  wall: wallSub.value,
  fortune: fortuneSub.value,
}))

const anchorHandlers: WorldAnchorHandlers = {
  cinema: openCinema,
  parcels: openParcels,
  lighthouse: goServer,
  shop: goShop,
  collector: goCollector,
  post: openLetter,
  gamehall: goGame,
  stele: stele.openStele,
  board: openBoard,
  wall: openWall,
  fortune: openFortune,
}

/* ---------- 设施 pin（分区抽屉） ---------- */
const selectedKey = ref<string | null>(null)

function selectFacility(f: GameFacility) {
  selectedKey.value = f.key
  void router.push(f.path)
}

const openRegionKey = computed(() => (overlay.value === 'region' ? regionKey.value : null))

const regionGroup = computed(() => gameFacilityGroups.find((g) => g.key === regionKey.value) ?? null)

const regionFacilities = computed(() =>
  gameFacilities
    .filter((f) => f.group === regionKey.value && f.enabled !== false)
    .sort((a, b) => a.order - b.order),
)

const regionLead = computed(() => regionGroup.value?.sub ?? '')

function onRegionClick(key: string) {
  if (overlay.value === 'region' && regionKey.value === key) {
    closeOverlay()
    return
  }
  lastTrigger = document.activeElement as HTMLElement | null
  regionKey.value = key
  overlay.value = 'region'
  nextTick(() => {
    const closeBtn = sheetEl.value?.querySelector<HTMLElement>('.w-sheet-close')
    try { (closeBtn ?? sheetEl.value)?.focus() } catch { /* 静默 */ }
  })
}

function selectFromRegion(f: GameFacility) {
  closeOverlay()
  selectFacility(f)
}

/** 设施副标题：guideTip ?? desc（与旧桌面锚点文案口径一致，不编造） */
function subFor(f: GameFacility): string {
  return f.guideTip || f.desc
}

/**
 * 设施状态点：只有签到有完成数据源（checked_today）。
 * 其余设施无状态数据 → 返回 undefined，不渲染状态点（不编造完成态）。
 */
function statusFor(f: GameFacility): 'todo' | 'done' | undefined {
  if (f.key === 'checkin') return quest.checkinDone.value ? 'done' : 'todo'
  return undefined
}

/* ---------- 弹层 ---------- */
function openParcels() {
  lastTrigger = document.activeElement as HTMLElement | null
  overlay.value = 'parcels'
  nextTick(() => {
    try { sheetEl.value?.focus() } catch { /* 静默 */ }
  })
}

function openLetter() {
  if (!hasLetter.value) return
  markLetterRead()
  lastTrigger = document.activeElement as HTMLElement | null
  overlay.value = 'letter'
  nextTick(() => {
    try { sheetEl.value?.focus() } catch { /* 静默 */ }
  })
}

function openOverlaySheet(kind: 'stele' | 'board' | 'wall' | 'fortune') {
  lastTrigger = document.activeElement as HTMLElement | null
  overlay.value = kind
  nextTick(() => {
    try { sheetEl.value?.focus() } catch { /* 静默 */ }
  })
}

function closeOverlay() {
  overlay.value = 'none'
  regionKey.value = null
  nextTick(() => {
    if (lastTrigger && document.contains(lastTrigger)) {
      lastTrigger.focus()
    }
    lastTrigger = null
  })
}

const sheetTitle = computed(() => {
  switch (overlay.value) {
    case 'parcels': return '📦 今日封存'
    case 'letter': return '✉️ 每日一封'
    case 'stele': return '🏆 观影碑'
    case 'board': return '📢 服主告示牌'
    case 'wall': return '💬 到此一游'
    case 'fortune': return '🎋 每日一签'
    case 'region': return regionGroup.value ? `${regionGroup.value.icon} ${regionGroup.value.title}` : '营地分区'
    default: return ''
  }
})

const sheetTouchY = ref<number | null>(null)
function onSheetTouchStart(e: TouchEvent) {
  if (e.touches.length === 1) sheetTouchY.value = e.touches[0].clientY
}
function onSheetTouchEnd(e: TouchEvent) {
  if (sheetTouchY.value === null) return
  const dy = e.changedTouches[0].clientY - sheetTouchY.value
  sheetTouchY.value = null
  if (dy > 90 && window.matchMedia('(max-width: 1023px)').matches) closeOverlay()
}

/* ---------- 拆包裹 ---------- */
async function onUnseal(id: string | number) {
  const key = String(id)
  if (unsealingIds.value.has(key)) return
  unsealingIds.value.add(key)
  try {
    await panel.unseal(id)
  } catch (e) {
    useToast().error(humanizeError(e))
  } finally {
    unsealingIds.value.delete(key)
  }
}

/* ---------- 跳转 ---------- */
function toClassic() {
  setMode('classic')
}

function goLedger() {
  void router.push('/game/ledger')
}

function goAccount() {
  void router.push('/game/account')
}

function goServer() {
  void router.push('/server')
}

function goShop() {
  void router.push('/shop')
}

function goCollector() {
  void router.push('/collector')
}

function goGame() {
  // 已在 /game 页内时再 push('/game') 等于死按钮，改开签到页（用户拍板 2026-10-16）
  if (router.currentRoute.value.path === '/game') {
    void router.push('/game/checkin')
    return
  }
  void router.push('/game')
}

function openCinema() {
  // 用户拍板（2026-10-16）：梦幻影院改去站内经典发现页，不再新开 Emby 网页版
  setMode('classic')
  // 在 /game 页时需要回首页；在首页则原地切视图即可
  if (router.currentRoute.value.path !== '/') {
    void router.push('/')
  }
}

/* ---------- 拉取 ---------- */
async function loadMember() {
  try {
    memberRaw.value = (await memberApi.info()) as unknown as Record<string, unknown>
  } catch {
    memberRaw.value = null
  }
}

function focusableInSheet(): HTMLElement[] {
  const root = sheetEl.value
  if (!root) return []
  return [...root.querySelectorAll<HTMLElement>('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')]
    .filter((el) => !el.hasAttribute('disabled') && el.tabIndex !== -1)
}

function onKeydown(e: KeyboardEvent) {
  if (overlay.value === 'none') return
  if (e.key === 'Escape') {
    e.preventDefault()
    closeOverlay()
    return
  }
  if (e.key !== 'Tab' || overlay.value !== 'region') return
  const nodes = focusableInSheet()
  if (nodes.length === 0) return
  const first = nodes[0]
  const last = nodes[nodes.length - 1]
  const active = document.activeElement
  if (e.shiftKey && (active === first || active === sheetEl.value)) {
    e.preventDefault()
    last.focus()
  } else if (!e.shiftKey && active === last) {
    e.preventDefault()
    first.focus()
  }
}

watch(letterDateKey, () => refreshLetterRead())
watch(userId, () => refreshLetterRead())

onMounted(() => {
  void panel.loadHome()
  void panel.loadCollector()
  void quest.loadCheckin()
  void loadMember()
  void stele.loadLeaderboard()
  void stele.loadSteleTop()
  void loadWall()
  void loadFortune()
  refreshLetterRead()
  window.addEventListener('keydown', onKeydown)
})

onUnmounted(() => {
  window.removeEventListener('keydown', onKeydown)
})
</script>

<style scoped>
.world-root {
  height: 100dvh;
  box-sizing: border-box;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: flex-start;
  overflow: auto;
  padding: 12px 0 20px;
  background: var(--paper-canvas);
  font-family: "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
  color: var(--paper-ink-deep);
}
.world-col {
  position: relative;
  /* 首页地图与 /game 同一画布宽：min(1500px, 94vw)，按 1568:882 等比放大，不再缩成居中小卡 */
  width: min(1500px, 94vw);
  margin: 0 auto;
  flex: 0 0 auto;
  container-type: inline-size;
}
/* 问候/顶栏叠在地图上。定位参照这层，比画布边框外缘再进 1px，和原先叠在 .world 内缘时对齐。 */
.map-frame {
  position: relative;
}
.world-scroll {
  min-width: 0;
}
.map-scroll-hint {
  display: none;
}
.world {
  position: relative;
  width: 100%;
  aspect-ratio: 1568 / 882;
  overflow: hidden;
  background: var(--paper-canvas);
  border: 1px solid var(--paper-hairline);
  border-radius: 18px;
}
/* 顶部「今日探险」胶囊（纸卡配方，语义不变） */
.game-goalbar {
  width: min(1500px, 94vw);
  margin: 0 auto 10px;
  background: var(--paper-card-soft);
  border: 1px solid var(--paper-hairline);
  border-radius: 12px;
  padding: 8px 12px;
}
.m-goal-text {
  font-size: 12px;
  font-weight: 500;
  color: var(--paper-ink-soft);
}
.m-goal-text b { color: var(--paper-teal); }
.m-goal-bar {
  margin-top: 6px;
  height: 3px;
  border-radius: 99px;
  background: var(--paper-hairline);
  overflow: hidden;
}
.m-goal-bar i {
  display: block;
  height: 100%;
  background: var(--paper-teal);
  border-radius: 99px;
  transition: width var(--au-med);
}
.world-root.is-game {
  height: auto;
  display: block;
  overflow: visible;
  padding: 16px 0;
}
.world-root.is-game .world-col {
  width: min(1500px, 94vw);
}
/* HUD */
.hud {
  position: absolute;
  top: 1px;
  left: 1px;
  right: 1px;
  height: 4.17cqw;
  display: flex;
  align-items: center;
  gap: 0.97cqw;
  padding: 0 1.53cqw;
  background: linear-gradient(180deg, rgba(250, 248, 245, .95), rgba(250, 248, 245, .75) 75%, transparent);
  z-index: 30;
}
.hud .brand {
  font-weight: 500;
  font-size: 1.32cqw;
}
.hud .brand b {
  color: var(--paper-teal);
}
.hud .worldname {
  background: var(--paper-ink-deep);
  color: var(--paper-card-soft);
  border-radius: 999px;
  padding: 0.35cqw 0.97cqw;
  font-size: 0.94cqw;
  font-weight: 500;
  letter-spacing: 0.07cqw;
}
.hud .right {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 0.69cqw;
}
.hud .pill {
  font-size: 0.90cqw;
  color: var(--paper-ink-soft);
  background: var(--paper-card-soft);
  border: 1px solid var(--paper-hairline);
  border-radius: 999px;
  padding: 0.35cqw 0.90cqw;
  font-family: inherit;
  cursor: default;
}
.hud button.pill { cursor: pointer; }
.hud .pill .lv {
  color: var(--paper-teal);
  font-weight: 500;
}
.hud .avatar {
  width: 2.22cqw;
  height: 2.22cqw;
  min-width: 32px;
  min-height: 32px;
  border-radius: 10px;
  background: var(--paper-teal);
  color: #fff;
  border: none;
  font-size: 1.0cqw;
  cursor: pointer;
  font-family: inherit;
}
/* 问候卡 */
.hello {
  position: absolute;
  left: calc(1.53cqw + 1px);
  top: calc(5.42cqw + 1px);
  z-index: 30;
  background: var(--paper-card-soft);
  border: 1px solid var(--paper-hairline);
  border-radius: 1.11cqw;
  padding: 0.97cqw 1.25cqw;
  max-width: 22.92cqw;
}
.hello .d {
  font-size: 0.83cqw;
  letter-spacing: 0.14cqw;
  color: var(--paper-ink-soft);
}
.hello h2 {
  font-size: 1.46cqw;
  font-weight: 500;
  margin: 0.21cqw 0 0.14cqw;
}
.hello p {
  font-size: 0.90cqw;
  color: var(--paper-ink-soft);
  line-height: 1.55;
  margin: 0.14cqw 0;
}
.hello-line {
  font-size: 12px;
  color: var(--paper-ink-soft);
  letter-spacing: 1px;
  margin-top: 4px;
}
.hello p b {
  color: var(--paper-teal);
}
/* 经典视图切换 */
.classic {
  position: absolute;
  right: calc(1.53cqw + 1px);
  top: calc(5.42cqw + 1px);
  z-index: 30;
  background: var(--paper-card-soft);
  border: 1px solid var(--paper-hairline);
  color: var(--paper-ink-soft);
  border-radius: 999px;
  padding: 0.49cqw 1.04cqw;
  font-size: 0.90cqw;
  font-weight: 500;
  font-family: inherit;
  cursor: pointer;
  transition: transform var(--au-fast), border-color var(--au-fast);
}
.classic:hover {
  transform: translateY(-2px);
  border-color: var(--paper-teal-border);
}
/* 弹层外壳 */
.w-overlay {
  position: fixed;
  inset: 0;
  background: rgba(39, 37, 30, .45);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 80;
  padding: 24px;
}
.w-sheet {
  background: var(--paper-card-soft);
  border: 1px solid var(--paper-hairline);
  border-radius: 16px;
  width: min(560px, calc(100vw - 48px));
  max-height: 84vh;
  max-height: 84dvh;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: 20px;
  color: var(--paper-ink-deep);
  font-family: "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
}
.w-sheet.is-stele {
  background: radial-gradient(120% 90% at 50% 0%, var(--paper-card-soft) 0%, var(--paper-canvas) 70%, var(--paper-canvas) 100%);
  position: relative;
}
.w-grip {
  display: none;
}
.w-sheet-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
}
.w-sheet-title {
  font-size: 16px;
  font-weight: 500;
}
.w-sheet-close {
  background: none;
  border: 1px solid var(--paper-hairline);
  border-radius: 999px;
  width: 30px;
  height: 30px;
  cursor: pointer;
  color: var(--paper-ink-soft);
  font-size: 13px;
  line-height: 1;
}
.w-sheet.is-region {
  width: min(760px, calc(100vw - 48px));
}
.w-sheet-close:focus-visible,
.classic:focus-visible,
.hud .pill:focus-visible,
.hud .avatar:focus-visible {
  outline: 2px solid var(--paper-teal);
  outline-offset: 2px;
}
@media (max-width: 1023px) {
  /* 窄屏：整页可滚，地图单独横滑。画布保持桌面那套 cqw 比例（约 1340px，小字 ≥11px），
     标签间距不用另算，也不会挤成一团或盖住房子。 */
  .world-root {
    display: block;
    height: auto;
    min-height: 0;
    overflow: visible;
    padding: 8px 12px calc(16px + var(--au-dock-space, 0px));
  }
  .world-root.is-game {
    padding: 0 12px 8px;
    overflow: visible;
  }
  .world-root.is-game .world-col,
  .world-col {
    width: 100%;
    min-width: 0;
    container-type: normal;
  }
  .game-goalbar {
    width: 100%;
  }
  .map-frame {
    display: flex;
    flex-direction: column;
    align-items: stretch;
    min-width: 0;
    max-width: 100%;
  }
  .hud,
  .hello,
  .classic {
    position: static;
    top: auto;
    left: auto;
    right: auto;
    height: auto;
    max-width: none;
  }
  .hud {
    flex-wrap: wrap;
    gap: 8px;
    padding: 4px 0 8px;
    background: transparent;
  }
  .hud .brand { font-size: 16px; }
  .hud .worldname {
    font-size: 12px;
    letter-spacing: 0.04em;
    padding: 4px 10px;
  }
  .hud .right {
    margin-left: auto;
    flex-wrap: wrap;
    justify-content: flex-end;
    gap: 6px;
  }
  .hud .pill {
    font-size: 12px;
    padding: 6px 10px;
  }
  .hud button.pill { min-height: 44px; }
  .hud .avatar {
    width: 44px;
    height: 44px;
    min-width: 44px;
    min-height: 44px;
    font-size: 16px;
  }
  .hello {
    margin: 0 0 8px;
    padding: 12px 14px;
    border-radius: 16px;
  }
  .hello .d { font-size: 12px; letter-spacing: 0.04em; }
  .hello h2 { font-size: 16px; margin: 4px 0 2px; }
  .hello p { font-size: 14px; }
  .classic {
    align-self: flex-end;
    margin: 0 0 8px;
    min-height: 44px;
    padding: 8px 14px;
    font-size: 12px;
  }
  .world-scroll {
    order: 5;
    overflow-x: auto;
    overflow-y: hidden;
    max-width: 100%;
    overscroll-behavior-x: contain;
    touch-action: pan-x pan-y;
    scrollbar-width: thin;
    border-radius: 16px;
  }
  .world {
    width: 1340px;
    max-width: none;
    container-type: inline-size;
    border-radius: 16px;
  }
  .map-scroll-hint {
    display: block;
    order: 4;
    margin: 0 0 8px;
    font-size: 12px;
    line-height: 1.33;
    color: var(--paper-ink-soft);
  }
  .w-grip {
    display: block;
    width: 40px;
    height: 4px;
    border-radius: 99px;
    background: var(--paper-hairline);
    margin: 0 auto 12px;
  }
  .w-overlay {
    padding: 0;
    align-items: flex-end;
  }
  .w-sheet {
    width: 100%;
    max-height: 78dvh;
    border-radius: 16px 16px 0 0;
    border-bottom: none;
    overflow-y: auto;
    padding-bottom: calc(20px + env(safe-area-inset-bottom));
  }
  .w-sheet-head {
    position: sticky;
    top: 0;
    z-index: 2;
    background: var(--paper-card-soft);
    padding-bottom: 8px;
  }
  .w-sheet-close {
    width: 44px;
    height: 44px;
  }
}
@media (prefers-reduced-motion: reduce) {
  .classic,
  .m-goal-bar i {
    transition: none;
  }
  .classic:hover {
    transform: none;
  }
}
</style>

<style>
/* ---------- 昼夜天色（跨组件，作用于 WorldCanvas 内元素） ---------- */
/* 滤镜只调 brightness（禁 sepia），时长与动效规范一致 */
.dp-dawn .scene { filter: brightness(.97); }
.dp-day .scene { filter: none; }
.dp-dusk .scene { filter: brightness(.9); }
.dp-night .scene { filter: brightness(.45); }
.night-veil { display: none; }
.dp-night .night-veil {
  display: block;
  position: absolute;
  inset: 0;
  z-index: 5;
  pointer-events: none;
  background: linear-gradient(180deg, rgba(26, 38, 92, .72) 0%, rgba(26, 38, 92, .66) 60%, rgba(26, 38, 92, .70) 100%);
}
.veil-moon { position: absolute; overflow: visible; filter: drop-shadow(0 0 9px rgba(247, 240, 207, .55)); }
.veil-moon.moon-d { left: 27.4%; top: 7%; width: 46px; height: 46px; }
.veil-moon.moon-m { display: none; }
.world-root.dp-night .mark .name {
  background: rgba(39, 37, 30, .88);
  color: var(--paper-card-soft);
  border-color: rgba(250, 248, 245, .16);
}
.world-root.dp-night .mark .art-svg { filter: brightness(.86); }
.dp-night .world::after {
  /* 星空点缀（纯 CSS，不新增图片） */
  content: "";
  position: absolute;
  inset: 0;
  pointer-events: none;
  background:
    radial-gradient(circle at 12% 14%, rgba(255, 246, 220, .95) 0 2px, transparent 3px),
    radial-gradient(circle at 28% 8%, rgba(255, 246, 220, .8) 0 1.5px, transparent 2.5px),
    radial-gradient(circle at 45% 18%, rgba(255, 246, 220, .9) 0 2px, transparent 3px),
    radial-gradient(circle at 62% 10%, rgba(255, 246, 220, .7) 0 1.5px, transparent 2.5px),
    radial-gradient(circle at 78% 22%, rgba(255, 246, 220, .85) 0 2px, transparent 3px),
    radial-gradient(circle at 92% 8%, rgba(255, 246, 220, .9) 0 2px, transparent 3px),
    radial-gradient(circle at 8% 32%, rgba(255, 246, 220, .6) 0 1.5px, transparent 2.5px),
    radial-gradient(circle at 55% 30%, rgba(255, 246, 220, .55) 0 1.5px, transparent 2.5px);
}
.dp-night .world::before {
  /* 夜色罩：地图整体压暗，纸色建筑依然可辨 */
  content: "";
  position: absolute;
  inset: 0;
  z-index: 20;
  pointer-events: none;
  background: rgba(20, 28, 52, .12);
}
.dp-night .lighthouse .pin {
  /* 夜晚灯塔亮灯 */
  filter: drop-shadow(0 0 10px rgba(255, 214, 120, .85));
}
</style>
