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

const current = computed(() => {
  if (!props.items.length) return '正在感知服务器…'
  return props.items[idx.value % props.items.length]
})

function tick() {
  if (props.items.length > 1) idx.value = (idx.value + 1) % props.items.length
}

// 列表变化时回到第一条
watch(() => props.items, () => { idx.value = 0 }, { deep: true })

onMounted(() => {
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
  timer = window.setInterval(tick, props.intervalMs)
})
onUnmounted(() => { window.clearInterval(timer) })
</script>

<style scoped>
.v4-activity {
  font-size: 12px;
  color: #C9BFA9;
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
