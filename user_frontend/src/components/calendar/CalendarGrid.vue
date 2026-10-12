<script setup lang="ts">
/**
 * 追新日历 · 月网格
 *
 * ## 布局为什么是「三行式」
 * 之前格子是「日期数字 + 封面圆点 + 右上角 absolute 角标」：
 * 1. 有圆点的格子和没圆点的格子，日期数字不在同一条水平线上
 *    （flex 居中，有内容的格子把数字顶上去了）——这就是用户说的「错位」；
 * 2. absolute 角标在窄屏小格子里会溢出格子角，被裁掉一半。
 *
 * 现在每格固定三行：r1 日期+角标（文档流，不再 absolute）、r2 封面圆点
 * （没内容也占位，高度不变）、r3 结构占位。三行的 min-height 都是
 * --cal-* 变量，全月所有格子的每一行都对齐。
 *
 * ## M1 防御
 * mobile.css 会给所有 button 加 min-height: 44px（≤768px），这里每个
 * button 都显式声明自己的 min-height/height，不吃那条全局规则。
 */
import { WEEK_LABELS, type CalCell } from '@/composables/useCalendarMonth'
import type { CalendarDay, CalendarItem } from '@/api/calendar'
import { posterUrl } from '@/api/emby'

const props = defineProps<{
  cells: CalCell[]
  dayMap: Map<string, CalendarDay>
  selectedDate: string
  todayStr: string
}>()

const emit = defineEmits<{
  select: [date: string]
}>()

function countOf(date: string): number {
  return props.dayMap.get(date)?.count || 0
}

function itemsOf(date: string): CalendarItem[] {
  return props.dayMap.get(date)?.items || []
}

/** 格子里的封面点：没图就不给 background-image，退回底色 + 描边 */
function dotStyle(item: CalendarItem): Record<string, string> | undefined {
  const url = posterUrl(item, 160)
  return url ? { backgroundImage: `url(${url})` } : undefined
}
</script>

<template>
  <div class="cal-week" aria-hidden="true">
    <span v-for="w in WEEK_LABELS" :key="w">{{ w }}</span>
  </div>

  <div class="cal-grid" role="grid" aria-label="月历">
    <button
      v-for="(cell, idx) in cells"
      :key="cell.date || `pad-${idx}`"
      class="cal-cell"
      :class="{
        pad: !cell.inMonth,
        today: cell.date === todayStr,
        picked: cell.date === selectedDate,
        filled: countOf(cell.date) > 0,
      }"
      :disabled="!cell.inMonth"
      :aria-current="cell.date === todayStr ? 'date' : undefined"
      :aria-label="cell.date ? `${cell.date} 新增 ${countOf(cell.date)} 条` : ''"
      @click="emit('select', cell.date)"
    >
      <template v-if="cell.inMonth">
        <!-- r1：日期 + 角标（文档流，角标不再 absolute 飞角上） -->
        <span class="cal-r1">
          <span class="cal-daynum">{{ cell.day }}</span>
          <span v-if="countOf(cell.date) > 0" class="cal-count">{{ countOf(cell.date) }}</span>
        </span>
        <!-- r2：封面圆点；没内容也占着高度，保证日期行全月对齐 -->
        <span class="cal-r2">
          <span v-if="itemsOf(cell.date).length" class="cal-dots">
            <span
              v-for="item in itemsOf(cell.date).slice(0, 3)"
              :key="item.Id"
              class="cal-dot"
              :style="dotStyle(item)"
            />
          </span>
        </span>
        <!-- r3：结构占位行，吸收剩余空间 -->
        <span class="cal-r3" aria-hidden="true" />
      </template>
    </button>
  </div>
</template>

<style scoped>
/* ---------- 星期头 ---------- */
.cal-week {
  display: grid;
  grid-template-columns: repeat(7, 1fr);
  gap: var(--cal-gap);
  margin-bottom: var(--cal-week-mb);
}
.cal-week span {
  text-align: center;
  font-size: var(--cal-week-size);
  font-weight: 600;
  color: var(--au-text-4);
  /* 与格子日期数字同字号体系，视觉基线一致 */
  line-height: 1;
  min-height: var(--cal-week-size);
}

/* ---------- 网格 ---------- */
.cal-grid {
  display: grid;
  grid-template-columns: repeat(7, 1fr);
  gap: var(--cal-gap);
}

.cal-cell {
  position: relative;
  aspect-ratio: 1 / 1;
  /* M1：显式高度声明，不吃 mobile.css 的 button 44px 最小高度 */
  min-height: var(--cal-cell-min-h);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--cal-row-gap);
  padding: var(--cal-cell-pad);
  border: 1px solid transparent;
  border-radius: var(--cal-cell-radius);
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

/* ---------- 三行：r1 日期+角标 / r2 圆点 / r3 占位 ---------- */
.cal-r1 {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  /* 固定行高：全月所有格子的日期行在同一水平线 */
  min-height: var(--cal-row1-h);
}

.cal-daynum {
  font-size: var(--cal-daynum-size);
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  line-height: 1;
}

.cal-count {
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--au-r-full);
  background: var(--au-primary-soft);
  color: var(--au-primary);
  font-size: var(--cal-count-size);
  font-weight: 700;
  line-height: 1;
  font-variant-numeric: tabular-nums;
}

.cal-r2 {
  display: flex;
  align-items: center;
  justify-content: center;
  /* 没圆点也占着：日期行才不会上下乱跳 */
  min-height: var(--cal-row2-h);
}

.cal-dots {
  display: flex;
  gap: var(--cal-dot-gap);
  justify-content: center;
}

.cal-dot {
  width: var(--cal-dot-size);
  height: var(--cal-dot-size);
  border-radius: var(--au-r-full);
  background: var(--au-surface-3);
  background-size: cover;
  background-position: center;
  box-shadow: 0 0 0 1px var(--au-border);
}

.cal-r3 {
  flex: 1 1 auto;
  min-height: 0;
}

/* ---------- 状态 ---------- */
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

/* ---------- 窄屏 ---------- */
@media (max-width: 768px) {
  /* 格子从 1:1 收成固定高度：方形格在 7 列窄屏上只剩 40px，
     封面缩到圆点都看不清，改成「日期+角标+圆点」的高度即可 */
  .cal-cell { aspect-ratio: auto; }
}

@media (prefers-reduced-motion: reduce) {
  .cal-cell { transition: none; }
  .cal-cell:not(:disabled):active { transform: none; }
}
</style>
