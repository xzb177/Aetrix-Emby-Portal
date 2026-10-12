<script setup lang="ts">
/**
 * 追新日历 · 选中日的条目列表
 *
 * 纯展示组件：拿到哪天、那天的数据、是不是在加载，就画出来。
 * 点条目用 Rex 深链打开（逻辑在 utils/rexDeepLink，与之前一致）。
 */
import { CalendarDays, ChevronRight, Film, Sparkles } from 'lucide-vue-next'
import type { CalendarDay, CalendarItem } from '@/api/calendar'
import { posterUrl } from '@/api/emby'
import { rexDeepLink } from '@/utils/rexDeepLink'
import { WEEK_LABELS } from '@/composables/useCalendarMonth'

defineProps<{
  selectedDate: string
  selectedDay: CalendarDay | null
  loading: boolean
}>()

function posterOf(item: CalendarItem): string {
  return posterUrl(item, 160)
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
</script>

<template>
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
</template>

<style scoped>
/* ---------- 当日列表 ---------- */
.day-panel { margin-top: 1rem; }

.cal-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  margin-bottom: 0.875rem;
  /* M1：header 是 div 不是 button，不吃 mobile 规则；这里声明高度防抖动 */
  min-height: 24px;
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
  /* M1：显式最小高度，不吃 mobile.css 给 a 的 44px 规则，也不被它撑变形 */
  min-height: 80px;
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
  font-size: var(--cal-day-title-size);
  font-weight: 600;
  color: var(--au-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.day-sub {
  font-size: var(--cal-day-sub-size);
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
  /* 标签行固定高度：有没有评分/库名，行高都不变 */
  min-height: 20px;
}

.day-tags .au-badge {
  font-size: var(--cal-day-sub-size);
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
  font-size: var(--cal-day-sub-size);
  color: var(--au-text-4);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 8rem;
}

.day-rate {
  font-size: var(--cal-day-sub-size);
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

.cal-note {
  margin: 0.875rem 0 0;
  font-size: var(--cal-day-sub-size);
  color: var(--au-text-3);
}

/* ---------- 窄屏 ---------- */
@media (max-width: 768px) {
  .day-list { grid-template-columns: 1fr; }
}

@media (prefers-reduced-motion: reduce) {
  .day-item, .day-go { transition: none; }
  .day-item:hover { transform: none; }
}
</style>
