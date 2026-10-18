import { computed, onMounted, onUnmounted, ref } from 'vue'

/**
 * 昼夜：响应式分钟时钟 + daypart。
 * 从 WorldMap.vue 拆出（原内联逻辑，行为不变）：
 * 页面开着跨时段时，天色与问候语自动刷新。
 */
export type Daypart = 'dawn' | 'day' | 'dusk' | 'night'

export function useDaypart() {
  const nowTick = ref<number>(Date.now())
  let clockTimer: ReturnType<typeof setInterval> | undefined

  const daypart = computed<Daypart>(() => {
    const h = new Date(nowTick.value).getHours()
    if (h >= 5 && h < 9) return 'dawn'
    if (h >= 9 && h < 17) return 'day'
    if (h >= 17 && h < 19) return 'dusk'
    return 'night'
  })

  onMounted(() => {
    clockTimer = setInterval(() => {
      nowTick.value = Date.now()
    }, 60_000)
  })

  onUnmounted(() => {
    if (clockTimer !== undefined) {
      clearInterval(clockTimer)
      clockTimer = undefined
    }
  })

  return { nowTick, daypart }
}
