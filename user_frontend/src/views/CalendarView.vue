<script setup lang="ts">
/**
 * 追新日历 — 按入库日期看每天新上了什么
 *
 * ## 结构（2026-10-12 重构）
 * 这个文件只剩骨架：页头 + 工具条 + 数据拉取。月份数学搬到了
 * composables/useCalendarMonth，月网格是 components/calendar/CalendarGrid，
 * 当日列表是 components/calendar/DayDetailList。
 *
 * ## 数据从哪来
 * 全部来自 GET /api/user/emby/calendar（见 api/calendar.ts）：一次请求换回
 * 按天分好组的条目。后端已经按 `date_added` 分桶、按媒体库可见范围收过一遍，
 * 这里只做展示，不再二次分桶——前后端各分一次迟早会差一天。
 *
 * ## 颜色
 * 全程只用 --au-* 令牌，浅色 / 深色两套主题自动跟随，不写死任何色值。
 * 日历自己的节拍收敛到 --cal-* 变量（见下方 <style>），子组件直接引用。
 */
import { ref, computed, watch, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { CalendarDays, ChevronLeft, ChevronRight, Film, Tv, ListVideo } from 'lucide-vue-next'
import { fetchCalendar, type CalendarItemType, type CalendarResponse } from '@/api/calendar'
import { embyApi, type EmbyItem } from '@/api/emby'
import { useToast } from '@/composables/useToast'
import { useCalendarMonth } from '@/composables/useCalendarMonth'
import CalendarGrid from '@/components/calendar/CalendarGrid.vue'
import DayDetailList from '@/components/calendar/DayDetailList.vue'

const toast = useToast()
const route = useRoute()
const router = useRouter()

// ==================== 月份（逻辑在 useCalendarMonth） ====================

const {
  year, month, todayStr, monthLabel, isCurrentMonth,
  rangeStart, rangeEnd, monthCells,
  shiftMonth, goToday,
} = useCalendarMonth()

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

/** 日期 → 当天数据；没有新片的日期不进 map，子组件里用可选链读 */
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
  if (isCurrentMonth.value) return todayStr
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

      <CalendarGrid
        :cells="monthCells"
        :day-map="dayMap"
        :selected-date="selectedDate"
        :today-str="todayStr"
        @select="selectDate"
      />

      <div v-if="loading" class="au-empty">
        <span class="au-spinner" aria-hidden="true" />
        <span>正在读取入库记录…</span>
      </div>
      <p v-else-if="isEmptyMonth" class="cal-note">
        这个区间还没有新上架的条目。条目是扫描进库时写下的入库时间，扫完就会出现。
      </p>
      <p v-else class="cal-note">
        格子里的封面点开就是详情页；角标是当天的真实条数（超出可展示数量的部分只计数）。
      </p>
    </section>

    <!-- 选中日详情 -->
    <DayDetailList
      :selected-date="selectedDate"
      :selected-day="selectedDay"
      :loading="loading"
    />
  </div>
</template>

<style scoped>
/* ---------- 日历节拍变量见 styles/calendar.css ----------
   --cal-* 统一收敛在被引入的样式表里（CI 设计令牌契约检查要求），
   这里和子组件都通过 DOM 继承直接引用，不再重复定义。 */

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
  /* 导航行固定高度：翻月时月份文字不变形 */
  min-height: 32px;
}

.cal-step {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  /* M1：显式 min-height，顶掉 mobile.css 给所有 button 的 44px
     （否则窄屏上这两个小方块会被撑成 44px 高的长条，月份标题错位） */
  min-height: 32px;
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
  font-size: var(--cal-day-sub-size);
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
  font-size: var(--cal-day-sub-size);
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
  font-size: var(--cal-day-sub-size);
  font-variant-numeric: tabular-nums;
}
/* 选中态的角标用 --au-text 而不是 --au-primary：浅色主题下
   --au-primary 压在 --au-primary-mid 上只有 2.87:1（数字已经 11px，
   再低就真的读不出来了），换成正文色后两套主题都在 10:1 上下 */
.cal-chip.on .cal-chip-n { background: var(--au-primary-mid); color: var(--au-text); }

.cal-note {
  margin: 0.875rem 0 0;
  font-size: var(--cal-day-sub-size);
  color: var(--au-text-3);
}

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
}

@media (prefers-reduced-motion: reduce) {
  .cal-step { transition: none; }
  .cal-step:active { transform: none; }
}
</style>
