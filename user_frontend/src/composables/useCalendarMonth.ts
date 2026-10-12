/**
 * 追新日历的月份数学：今天是哪月、格子怎么排、上下月怎么翻。
 *
 * 从 CalendarView.vue 里搬出来，不带任何网络请求——纯日期计算，
 * 方便以后签到页想复用同一套「补齐 7 格」算法时直接拿走。
 */
import { ref, computed } from 'vue'

/** 日历格子：date 为空表示占位格（前后补齐用），不可点 */
export interface CalCell {
  date: string
  day: number
  inMonth: boolean
}

export const WEEK_LABELS = ['日', '一', '二', '三', '四', '五', '六']

/** YYYY-MM-DD（本地日期，不用 toISOString —— 那是 UTC，会差一天） */
export function isoOf(d: Date): string {
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${m}-${day}`
}

export function useCalendarMonth() {
  const today = new Date()
  const todayStr = isoOf(today)

  const year = ref(today.getFullYear())
  const month = ref(today.getMonth()) // 0-11

  const monthLabel = computed(() => `${year.value} 年 ${month.value + 1} 月`)
  const isCurrentMonth = computed(
    () => year.value === today.getFullYear() && month.value === today.getMonth(),
  )

  const rangeStart = computed(() => isoOf(new Date(year.value, month.value, 1)))
  const rangeEnd = computed(() => isoOf(new Date(year.value, month.value + 1, 0)))

  /**
   * 日历格子：先按 1 号的星期补前导空位，再填 1..daysInMonth，
   * 最后把尾部补到 7 的倍数——末行不再参差不齐，网格永远是完整矩形。
   * （与签到页同一套算法，只是这里尾部也补齐）
   */
  const monthCells = computed<CalCell[]>(() => {
    const first = new Date(year.value, month.value, 1)
    const lead = first.getDay()
    const total = new Date(year.value, month.value + 1, 0).getDate()
    const cells: CalCell[] = []
    for (let i = 0; i < lead; i++) {
      cells.push({ date: '', day: 0, inMonth: false })
    }
    for (let d = 1; d <= total; d++) {
      cells.push({ date: isoOf(new Date(year.value, month.value, d)), day: d, inMonth: true })
    }
    while (cells.length % 7 !== 0) {
      cells.push({ date: '', day: 0, inMonth: false })
    }
    return cells
  })

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
    return date === todayStr
  }

  return {
    year,
    month,
    todayStr,
    monthLabel,
    isCurrentMonth,
    rangeStart,
    rangeEnd,
    monthCells,
    shiftMonth,
    goToday,
    isToday,
  }
}
