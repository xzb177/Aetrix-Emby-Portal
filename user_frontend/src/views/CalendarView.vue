<script setup lang="ts">
/**
 * 追新日历 — 按入库日期看每天新上了什么
 *
 * ## 数据从哪来
 * 全部来自 GET /api/user/emby/calendar（见 api/calendar.ts）：一次请求换回
 * 按天分好组的条目。后端已经按 `date_added` 分桶、按媒体库可见范围收过一遍，
 * 这里只做展示，不再二次分桶——前后端各分一次迟早会差一天。
 *
 * ## 为什么日期格子里画封面而不是只写数字
 * 「今天上了 3 部」回答不了「是哪 3 部」。格子直接铺小封面，扫一眼就知道；
 * 数量多的那天画不下就只留计数（后端把当天的**真实**条数放在 count 上，
 * 不受单日详情上限影响）。
 *
 * ## 颜色
 * 全程只用 --au-* 令牌，浅色 / 深色两套主题自动跟随，不写死任何色值。
 */
import { ref, computed, watch, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  CalendarDays, ChevronLeft, ChevronRight, Film, Tv, ListVideo, Sparkles,
} from 'lucide-vue-next'
import {
  fetchCalendar, type CalendarItem, type CalendarItemType, type CalendarResponse,
} from '@/api/calendar'
import { embyApi, posterUrl, type EmbyItem } from '@/api/emby'
import { rexDeepLink } from '@/utils/rexDeepLink'
import { useToast } from '@/composables/useToast'
import EmptyState from '@/components/ui/EmptyState.vue'

const toast = useToast()
const route = useRoute()
const router = useRouter()

// ==================== 月份 ====================

const today = new Date()
const year = ref(today.getFullYear())
const month = ref(today.getMonth()) // 0-11

const monthLabel = computed(() => `${year.value} 年 ${month.value + 1} 月`)
const isCurrentMonth = computed(
  () => year.value === today.getFullYear() && month.value === today.getMonth(),
)

/** 该月的 YYYY-MM-DD（本地日期，不用 toISOString —— 那是 UTC，会差一天） */
function isoOf(d: Date): string {
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${m}-${day}`
}

const rangeStart = computed(() => isoOf(new Date(year.value, month.value, 1)))
const rangeEnd = computed(() => isoOf(new Date(year.value, month.value + 1, 0)))

const WEEK_LABELS = ['日', '一', '二', '三', '四', '五', '六']

/** 日历格子：先按 1 号的星期补空位，再填 1..daysInMonth（与签到页同一套算法） */
interface Cell { date: string; day: number; inMonth: boolean }

const monthCells = computed<Cell[]>(() => {
  const first = new Date(year.value, month.value, 1)
  const lead = first.getDay()
  const total = new Date(year.value, month.value + 1, 0).getDate()
  const cells: Cell[] = []
  for (let i = 0; i < lead; i++) {
    cells.push({ date: '', day: 0, inMonth: false })
  }
  for (let d = 1; d <= total; d++) {
    cells.push({ date: isoOf(new Date(year.value, month.value, d)), day: d, inMonth: true })
  }
  return cells
})

// ==================== 筛选 ====================

const ALL_TYPES: CalendarItemType[] = ['movie', 'series', 'episode']

const TYPE_TABS: { key: CalendarItemType; label: string; icon: typeof Film }[] = [
  { key: 'movie', label: '电影', icon: Film },
  { key: 'series', label: '剧集', icon: Tv },
  { key: 'episode', label: '单集', icon: ListVideo },
]

/** 媒体库下拉直接用 Views 的 Id（guid），后端两种标识都收 */
const libraries = ref<EmbyItem[]>([])
const libraryId = ref('')

/** 永远非空：全部取消勾选时回到「全选」，避免出现「什么都没选 = 什么都不显示」 */
const selectedTypes = ref<CalendarItemType[]>([...ALL_TYPES])

function toggleType(key: CalendarItemType) {
  const has = selectedTypes.value.includes(key)
  const next = has
    ? selectedTypes.value.filter((t) => t !== key)
    : ALL_TYPES.filter((t) => t === key || selectedTypes.value.includes(t))
  selectedTypes.value = next.length ? next : [...ALL_TYPES]
}

const allTypesSelected = computed(() => selectedTypes.value.length === ALL_TYPES.length)

// ==================== 数据 ====================

const loading = ref(true)
const payload = ref<CalendarResponse | null>(null)

/**
 * 请求序号：连续翻月 / 换库时，先发的请求可能后到。
 *
 * 不做这个护栏的话会出现「标题写着 10 月、格子画的是 9 月」——因为后发的
 * 10 月请求先回来、先渲染，随后 9 月那份才落地并覆盖它。接口不慢的时候几乎
 * 不会发生，但在弱网 / 冷缓存下必现，而且看起来像「日期错乱」很难自查。
 */
let latestRequest = 0

/** 日期 → 当天数据；没有新片的日期不进 map，模板里用可选链读 */
const dayMap = computed(() => {
  const map = new Map<string, CalendarResponse['days'][number]>()
  for (const day of payload.value?.days || []) map.set(day.date, day)
  return map
})

const totalCount = computed(() => payload.value?.total ?? 0)

const isEmptyMonth = computed(() => !loading.value && totalCount.value === 0)

// ==================== 选中日 ====================

/** 选中日期用路由 query 承载：日历页可以被收藏 / 分享到某一天 */
const selectedDate = ref('')

/** 切月后默认落在「今天」所在的那格（本月）或当月 1 号 */
function defaultDayOfMonth(): string {
  if (isCurrentMonth.value) return isoOf(today)
  return rangeStart.value
}

/**
 * 路由上带的 day 只在**本月**时才认：直接打开 `/calendar?day=2026-09-30`
 * 而当月是 10 月，那一格根本不存在，认下来下方永远是一片空。
 */
function initialDay(): string {
  const fromQuery = typeof route.query.day === 'string' ? route.query.day : ''
  if (/^\d{4}-\d{2}-\d{2}$/.test(fromQuery)
    && fromQuery >= rangeStart.value && fromQuery <= rangeEnd.value) {
    return fromQuery
  }
  return defaultDayOfMonth()
}

const selectedDay = computed(() => dayMap.value.get(selectedDate.value) || null)

function selectDate(date: string) {
  selectedDate.value = date
  // replace 而不是 push：翻月时也会调这里，用 push 会在历史里堆一串同页记录
  router.replace({ query: { ...route.query, day: date } })
}

function shiftMonth(delta: number) {
  const next = new Date(year.value, month.value + delta, 1)
  year.value = next.getFullYear()
  month.value = next.getMonth()
}

function goToday() {
  year.value = today.getFullYear()
  month.value = today.getMonth()
}

function isToday(date: string): boolean {
  return date === isoOf(today)
}

// ==================== 条目展示 ====================

function posterOf(item: CalendarItem): string {
  return posterUrl(item, 160)
}

/** 日历格子里的封面点：没图就不给 background-image，退回底色 + 描边 */
function dotStyle(item: CalendarItem): Record<string, string> | undefined {
  const url = posterOf(item)
  return url ? { backgroundImage: `url(${url})` } : undefined
}

function typeLabel(item: CalendarItem): string {
  if (item.Type === 'Movie') return '电影'
  if (item.Type === 'Series') return '剧集'
  return '单集'
}

function typeClass(item: CalendarItem): string {
  if (item.Type === 'Movie') return 'is-movie'
  if (item.Type === 'Series') return 'is-series'
  return 'is-episode'
}

/** 单集标题写成「剧名 第 N 集」：光写「第 3 集」用户认不出是哪部 */
function titleOf(item: CalendarItem): string {
  if (item.Type === 'Episode' && item.SeriesName) return item.SeriesName
  return item.Name
}

function subtitleOf(item: CalendarItem): string {
  if (item.Type === 'Episode') {
    const no = item.IndexNumber ? `第 ${item.IndexNumber} 集` : ''
    return [item.SeriesName ? item.Name : '', no].filter(Boolean).join(' · ')
  }
  return [item.ProductionYear, item.Genres?.slice(0, 2).join(' / ')].filter(Boolean).join(' · ')
}

function weekdayOf(date: string): string {
  const d = new Date(`${date}T00:00:00`)
  return Number.isNaN(d.getTime()) ? '' : `星期${WEEK_LABELS[d.getDay()]}`
}

// ==================== 拉取 ====================

async function load() {
  const reqId = ++latestRequest
  loading.value = true
  try {
    const res = await fetchCalendar({
      start: rangeStart.value,
      end: rangeEnd.value,
      libraryId: libraryId.value || undefined,
      itemTypes: allTypesSelected.value ? [] : selectedTypes.value,
    })
    // 已经有更新的请求在飞：这份是过期结果，丢掉（否则会把新月份覆盖成旧月份）
    if (reqId !== latestRequest) return
    payload.value = res
  } catch (err: any) {
    if (reqId !== latestRequest) return
    payload.value = null
    const detail = err?.response?.data?.detail
    toast.error(typeof detail === 'string' ? detail : '日历加载失败，请稍后重试')
  } finally {
    // 同理：只有最后一次请求才能收 loading，否则会把新请求的骨架屏提前收掉
    if (reqId === latestRequest) loading.value = false
  }
}

onMounted(async () => {
  // 库列表失败不该拖垮日历：筛选框降级成「全部媒体库」，页面照常用
  embyApi.getViews().then((items) => { libraries.value = items }).catch(() => {})
  selectedDate.value = initialDay()
  await load()
})

// 翻月 / 换筛选都重新取；selectedDate 要跟着月份走，
// 否则留在上个月的日期会让下方列表空着（用户看着像坏了）。
// 走 selectDate 而不是直接赋值，地址栏上的 day 也不会变成上个月的死日期。
watch([year, month, libraryId, selectedTypes], async () => {
  selectDate(defaultDayOfMonth())
  await load()
}, { deep: true })
</script>

<template>
  <div class="au-page calendar-view">
    <header class="page-head">
      <div>
        <h1 class="page-title">
          <CalendarDays :size="22" />
          追新日历
        </h1>
        <p class="page-sub">
          按入库时间看每天新上了什么 · {{ monthLabel }}
          <template v-if="!loading && totalCount > 0"> · 共 {{ totalCount }} 条</template>
        </p>
      </div>
      <div class="head-actions">
        <button
          class="au-btn au-btn-ghost au-btn-sm"
          :disabled="isCurrentMonth"
          @click="goToday"
        >
          回到本月
        </button>
      </div>
    </header>

    <!-- 月历主体 -->
    <section class="au-card au-card-pad au-anim-up">
      <div class="cal-toolbar">
        <div class="cal-nav">
          <button class="cal-step" aria-label="上个月" @click="shiftMonth(-1)">
            <ChevronLeft :size="18" />
          </button>
          <span class="cal-month">{{ monthLabel }}</span>
          <button class="cal-step" aria-label="下个月" @click="shiftMonth(1)">
            <ChevronRight :size="18" />
          </button>
        </div>

        <div class="cal-filters">
          <select v-model="libraryId" class="au-input cal-select" aria-label="按媒体库筛选">
            <option value="">全部媒体库</option>
            <option v-for="lib in libraries" :key="lib.Id" :value="lib.Id">{{ lib.Name }}</option>
          </select>

          <div class="cal-types" role="group" aria-label="按类型筛选">
            <button
              v-for="tab in TYPE_TABS"
              :key="tab.key"
              class="cal-chip"
              :class="{ on: selectedTypes.includes(tab.key) }"
              :aria-pressed="selectedTypes.includes(tab.key)"
              @click="toggleType(tab.key)"
            >
              <component :is="tab.icon" :size="14" />
              <span>{{ tab.label }}</span>
              <span v-if="payload?.types?.[tab.key]" class="cal-chip-n">
                {{ payload.types[tab.key] }}
              </span>
            </button>
          </div>
        </div>
      </div>

      <div class="cal-week">
        <span v-for="w in WEEK_LABELS" :key="w">{{ w }}</span>
      </div>

      <div class="cal-grid">
        <button
          v-for="(cell, idx) in monthCells"
          :key="cell.date || `pad-${idx}`"
          class="cal-cell"
          :class="{
            pad: !cell.inMonth,
            today: isToday(cell.date),
            picked: cell.date === selectedDate,
            filled: !!dayMap.get(cell.date)?.count,
          }"
          :disabled="!cell.inMonth"
          :aria-current="isToday(cell.date) ? 'date' : undefined"
          :aria-label="cell.date ? `${cell.date} 新增 ${dayMap.get(cell.date)?.count || 0} 条` : ''"
          @click="selectDate(cell.date)"
        >
          <template v-if="cell.inMonth">
            <span class="cal-daynum">{{ cell.day }}</span>
            <span v-if="dayMap.get(cell.date)?.count" class="cal-dots">
              <span
                v-for="item in (dayMap.get(cell.date)?.items || []).slice(0, 3)"
                :key="item.Id"
                class="cal-dot"
                :style="dotStyle(item)"
              />
            </span>
            <span v-if="(dayMap.get(cell.date)?.count || 0) > 0" class="cal-count">
              {{ dayMap.get(cell.date)?.count }}
            </span>
          </template>
        </button>
      </div>

      <div v-if="loading" class="au-empty">
        <span class="au-spinner" aria-hidden="true" />
        <span>正在读取入库记录…</span>
      </div>
      <EmptyState
        v-else-if="isEmptyMonth"
        title="这个时间段暂时没有内容"
        description="条目是扫描进库时写下的入库时间，扫完就会出现，可以切换日期看看"
      />
      <p v-else class="cal-note">
        格子里的封面点开就是详情页；角标是当天的真实条数（超出可展示数量的部分只计数）。
      </p>
    </section>

    <!-- 选中日详情 -->
    <section class="au-card au-card-pad au-anim-up day-panel">
      <header class="cal-head">
        <h3>
          <Sparkles :size="16" />
          <template v-if="selectedDate">{{ selectedDate }} {{ weekdayOf(selectedDate) }}</template>
          <template v-else>选择一天</template>
        </h3>
        <span v-if="selectedDay" class="au-badge au-badge-cyan">{{ selectedDay.count }} 条新上</span>
      </header>

      <div v-if="loading" class="au-empty">
        <span class="au-spinner" aria-hidden="true" />
      </div>

      <div v-else-if="!selectedDate" class="au-empty">
        <CalendarDays :size="28" />
        <span>点上面任意一天，看看那天新上了什么</span>
      </div>

      <div v-else-if="!selectedDay" class="au-empty">
        <CalendarDays :size="28" />
        <span>这一天没有新上架的条目</span>
      </div>

      <ul v-else class="day-list">
        <li v-for="item in selectedDay.items" :key="item.Id">
          <a :href="rexDeepLink(item)" class="day-item" :title="`在 Rex 里打开：${titleOf(item)}`">
            <span class="day-poster">
              <img
                v-if="posterOf(item)"
                :src="posterOf(item)"
                :alt="titleOf(item)"
                loading="lazy"
                decoding="async"
              />
              <Film v-else :size="18" aria-hidden="true" />
            </span>
            <span class="day-meta">
              <span class="day-title">{{ titleOf(item) }}</span>
              <span class="day-sub">{{ subtitleOf(item) }}</span>
              <span class="day-tags">
                <span class="au-badge" :class="`tag-${typeClass(item)}`">{{ typeLabel(item) }}</span>
                <span v-if="item.LibraryName" class="day-lib">{{ item.LibraryName }}</span>
                <span v-if="item.CommunityRating" class="day-rate">{{ item.CommunityRating.toFixed(1) }}</span>
              </span>
            </span>
            <ChevronRight :size="16" class="day-go" aria-hidden="true" />
          </a>
        </li>
      </ul>

      <p v-if="selectedDay && selectedDay.count > selectedDay.items.length" class="cal-note">
        当天共 {{ selectedDay.count }} 条，这里只列出最近入库的 {{ selectedDay.items.length }} 条。
      </p>
      <p v-if="selectedDay" class="cal-note">
        点条目会用 <strong>Rex</strong> 打开：有 TMDB 编号的直接跳到那部作品，没有的按名字交给
        Rex 搜索。需要先装好 Rex 客户端。
      </p>
    </section>
  </div>
</template>

<style scoped>
/* ---------- 工具条 ---------- */
.cal-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem 1rem;
  flex-wrap: wrap;
  margin-bottom: 1rem;
}

.cal-nav {
  display: flex;
  align-items: center;
  gap: 0.375rem;
}

.cal-step {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-sm);
  background: var(--au-surface-2);
  color: var(--au-text-2);
  cursor: pointer;
  transition: background var(--au-fast) var(--au-ease),
    color var(--au-fast) var(--au-ease), transform var(--au-fast) var(--au-ease);
}
.cal-step:hover { background: var(--au-surface-3); color: var(--au-primary); }
.cal-step:active { transform: scale(0.94); }

.cal-month {
  min-width: 6.5rem;
  text-align: center;
  font-family: var(--au-font-serif);
  font-size: 1.0625rem;
  font-weight: 700;
  color: var(--au-text);
  font-variant-numeric: tabular-nums;
}

.cal-filters {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  flex-wrap: wrap;
}

.cal-select {
  width: auto;
  min-width: 8.5rem;
  height: 34px;
  font-size: 0.8125rem;
  cursor: pointer;
}

.cal-types {
  display: flex;
  gap: 0.25rem;
  padding: 3px;
  border-radius: var(--au-r-full);
  background: var(--au-surface-2);
  border: 1px solid var(--au-border);
}

.cal-chip {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 0.3125rem;
  height: 28px;
  /* 分段器里的小胶囊：不吃 mobile.css 给所有 button 的 44px 最小高度（那会把整条撑成一排大药丸） */
  min-height: 0;
  padding: 0 0.625rem;
  border: 0;
  border-radius: var(--au-r-full);
  background: transparent;
  color: var(--au-text-3);
  font-size: 0.8125rem;
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
  transition: background var(--au-fast) var(--au-ease), color var(--au-fast) var(--au-ease);
}
.cal-chip:hover { color: var(--au-text); }
.cal-chip.on {
  background: var(--au-primary-soft);
  color: var(--au-primary);
  box-shadow: inset 0 0 0 1px var(--au-primary-border);
}
.cal-chip svg { opacity: 0.85; }

.cal-chip-n {
  padding: 0 0.3125rem;
  border-radius: var(--au-r-full);
  background: var(--au-surface-3);
  color: var(--au-text-3);
  font-size: 0.8125rem;
  font-variant-numeric: tabular-nums;
}
/* 选中态的角标用 --au-text 而不是 --au-primary：浅色主题下
   --au-primary 压在 --au-primary-mid 上只有 2.87:1（数字已经 11px，
   再低就真的读不出来了），换成正文色后两套主题都在 10:1 上下 */
.cal-chip.on .cal-chip-n { background: var(--au-primary-mid); color: var(--au-text); }

/* ---------- 网格 ---------- */
.cal-week {
  display: grid;
  grid-template-columns: repeat(7, 1fr);
  gap: 0.3125rem;
  margin-bottom: 0.375rem;
}
.cal-week span {
  text-align: center;
  font-size: 0.8125rem;
  font-weight: 600;
  color: var(--au-text-4);
}

.cal-grid {
  display: grid;
  grid-template-columns: repeat(7, 1fr);
  gap: 0.3125rem;
}

.cal-cell {
  position: relative;
  aspect-ratio: 1 / 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 3px;
  padding: 2px;
  border: 1px solid transparent;
  border-radius: var(--au-r-sm);
  background: var(--au-surface);
  color: var(--au-text-2);
  cursor: pointer;
  transition: background var(--au-fast) var(--au-ease),
    border-color var(--au-fast) var(--au-ease), transform var(--au-fast) var(--au-ease);
}
.cal-cell:not(:disabled):hover {
  background: var(--au-surface-2);
  border-color: var(--au-border-strong);
}
.cal-cell:not(:disabled):active { transform: scale(0.96); }
.cal-cell.pad { background: transparent; cursor: default; }

/* 有新片的那天底色抬一档：整个月一眼能看出哪几天有更新，
   不必逐格去读角标。写在 .today / .picked 之前，让那两个状态盖住它 */
.cal-cell.filled { background: var(--au-surface-2); }

.cal-daynum {
  font-size: 0.8125rem;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  line-height: 1;
}

.cal-dots {
  display: flex;
  gap: 2px;
  justify-content: center;
}

.cal-dot {
  width: 9px;
  height: 9px;
  border-radius: var(--au-r-full);
  background: var(--au-surface-3);
  background-size: cover;
  background-position: center;
  box-shadow: 0 0 0 1px var(--au-border);
}

.cal-count {
  position: absolute;
  top: 2px;
  right: 3px;
  min-width: 16px;
  padding: 0 4px;
  border-radius: var(--au-r-full);
  background: var(--au-primary-soft);
  color: var(--au-primary);
  font-size: 0.8125rem;
  font-weight: 700;
  line-height: 16px;
  text-align: center;
  font-variant-numeric: tabular-nums;
}

.cal-cell.today {
  border-color: var(--au-primary-border);
  color: var(--au-text);
  font-weight: 700;
}
.cal-cell.today .cal-daynum { color: var(--au-primary); }

.cal-cell.picked {
  background: var(--au-primary-soft);
  border-color: var(--au-primary);
  box-shadow: 0 0 0 1px var(--au-primary-border);
}

.cal-note {
  margin: 0.875rem 0 0;
  font-size: 0.8125rem;
  color: var(--au-text-3);
}

/* ---------- 当日列表 ---------- */
.day-panel { margin-top: 1rem; }

.cal-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  margin-bottom: 0.875rem;
}
.cal-head h3 {
  display: flex;
  align-items: center;
  gap: 0.4375rem;
  margin: 0;
  font-size: 0.9375rem;
  font-weight: 700;
  color: var(--au-text);
  font-variant-numeric: tabular-nums;
}
.cal-head svg { color: var(--au-primary); }

.day-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 0.5rem;
}

.day-item {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  height: 100%;
  padding: 0.5rem;
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-md);
  background: var(--au-surface-2);
  text-decoration: none;
  color: var(--au-text);
  transition: background var(--au-fast) var(--au-ease),
    border-color var(--au-fast) var(--au-ease), transform var(--au-fast) var(--au-ease);
}
.day-item:hover {
  background: var(--au-surface-3);
  border-color: var(--au-primary-border);
}
.day-item:active { transform: translateY(0); }

.day-poster {
  flex: none;
  width: 46px;
  height: 64px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--au-r-sm);
  background: var(--au-surface-3);
  color: var(--au-text-4);
  overflow: hidden;
}
.day-poster img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.day-meta {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.day-title {
  font-size: 0.8125rem;
  font-weight: 600;
  color: var(--au-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.day-sub {
  font-size: 0.8125rem;
  color: var(--au-text-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.day-tags {
  display: flex;
  align-items: center;
  gap: 0.375rem;
  flex-wrap: wrap;
  margin-top: 2px;
}

.day-tags .au-badge {
  font-size: 0.8125rem;
  padding: 0 0.4375rem;
  height: 20px;
  display: inline-flex;
  align-items: center;
  border-radius: var(--au-r-full);
  font-weight: 600;
}
.tag-movie { background: var(--au-info-soft); color: var(--au-info); }
.tag-series { background: var(--au-violet-soft); color: var(--au-violet); }
.tag-episode { background: var(--au-success-soft); color: var(--au-success); }

.day-lib {
  font-size: 0.8125rem;
  color: var(--au-text-4);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 8rem;
}

.day-rate {
  font-size: 0.8125rem;
  font-weight: 700;
  color: var(--au-warning);
  font-variant-numeric: tabular-nums;
}

.day-go {
  flex: none;
  color: var(--au-text-4);
  transition: transform var(--au-fast) var(--au-ease), color var(--au-fast) var(--au-ease);
}
.day-item:hover .day-go { color: var(--au-primary); transform: translateX(2px); }

/* ---------- 窄屏 ---------- */
@media (max-width: 768px) {
  .cal-toolbar { gap: 0.625rem; }
  .cal-nav { width: 100%; justify-content: space-between; }
  .cal-month { flex: 1; }
  .cal-types { width: 100%; }
  .cal-chip { flex: 1; height: 32px; }
  .cal-filters { width: 100%; }
  .cal-select { flex: 1; min-width: 0; }

  /* 类型组独占一行：与库下拉各占一半时，三个 chip 加上角标会被挤到换行 */
  .cal-types { flex: 1 0 100%; justify-content: space-between; }
  .cal-chip { flex: 1; justify-content: center; padding: 0 0.375rem; min-width: 0; }
  .cal-chip span { overflow: hidden; text-overflow: ellipsis; }

  /* 格子从 1:1 收成固定高度：方形格在 7 列窄屏上只剩 40px，
     封面缩到 9px 圆点都看不清，改成「角标 + 圆点」的高度即可 */
  .cal-cell { aspect-ratio: auto; min-height: 46px; }
  .cal-dot { width: 7px; height: 7px; }

  .day-list { grid-template-columns: 1fr; }
}

@media (prefers-reduced-motion: reduce) {
  .cal-cell, .day-item, .cal-step, .day-go { transition: none; }
  .cal-cell:not(:disabled):active,
  .day-item:active,
  .day-item:hover { transform: none; }
}
</style>