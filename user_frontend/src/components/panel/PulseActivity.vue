<template>
  <div class="v4-activity" aria-live="off">
    <span :key="idx" class="v4-activity-line">{{ current }}</span>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'

const props = withDefaults(defineProps<{ items?: string[]; intervalMs?: number }>(), {
  items: () => [],
  intervalMs: 3200, // 设计稿 3.2s 一条
})

const idx = ref(0)
let timer = 0
const motionMq = window.matchMedia('(prefers-reduced-motion: reduce)')

const current = computed(() => {
  // items 为空走不到这里：DiscoveryView 只在非 loading/error 时渲染，
  // 且 activityTexts 的空兜底是 ['暂无动态']，长度恒 ≥ 1
  return props.items[idx.value % props.items.length]
})

function tick() {
  if (props.items.length > 1) idx.value = (idx.value + 1) % props.items.length
}

// 列表变化时回到第一条
watch(() => props.items, () => { idx.value = 0 }, { deep: true })

function startTick() {
  if (timer) return
  timer = window.setInterval(tick, props.intervalMs)
}
function stopTick() {
  window.clearInterval(timer)
  timer = 0
}
/** R28：用户在页面打开期间切换系统"减少动态"设置时即时启停轮播 */
function onMotionChange(e: { matches: boolean }) {
  if (e.matches) stopTick()
  else startTick()
}

onMounted(() => {
  // 减少动态偏好：只显示第一条，不轮播；监听先注册，避免页内关闭后轮播永不启动
  motionMq.addEventListener('change', onMotionChange)
  if (motionMq.matches) return
  startTick()
})
onUnmounted(() => {
  stopTick()
  motionMq.removeEventListener('change', onMotionChange)
})
</script>

<style scoped>
.v4-activity {
  font-size: 12px;
  color: var(--line-deep, #C9BFA9);
  margin-top: 8px;
  min-height: 40px;
  line-height: 1.6;
}
.v4-activity-line {
  display: inline-block;
  animation: v4-fadein 0.4s ease;
}
@keyframes v4-fadein {
  from { opacity: 0; transform: translateY(6px); }
  to { opacity: 1; transform: none; }
}
</style>
