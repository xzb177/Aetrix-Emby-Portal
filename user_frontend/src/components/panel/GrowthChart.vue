<template>
  <div class="v4-growth">
    <div class="bars">
      <div v-for="d in days" :key="d.date" class="bar-col">
        <div class="bar-val">{{ d.count }}</div>
        <div class="bar-track">
          <div class="bar-fill" :style="{ height: d.height + '%' }"></div>
        </div>
        <div class="bar-day">{{ d.weekday }}</div>
        <div class="bar-date">{{ d.date }}</div>
      </div>
    </div>
    <p class="growth-foot">{{ footText }}</p>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { GrowthDay } from '@/composables/usePanelApi'

const props = defineProps<{ growth: GrowthDay[] }>()

const days = computed(() => {
  const list = (props.growth || []).slice(-7)
  const max = Math.max(1, ...list.map((d) => d.count || 0))
  return list.map((d) => ({
    ...d,
    height: Math.max(4, Math.round(((d.count || 0) / max) * 100)),
  }))
})

const total = computed(() => (props.growth || []).reduce((s, d) => s + (d.count || 0), 0))

/** 全部来自真实数据，不许造假 */
const footText = computed(() => {
  const t = total.value
  if (t === 0) return '这 7 天静悄悄，一部新片也没长出来'
  return `过去 7 天，一共长了 ${t} 部新片`
})
</script>

<style scoped>
.v4-growth {
  background: var(--paper-card);
  border: 1.5px solid var(--line);
  border-radius: 14px;
  padding: 16px 12px 12px;
}
.bars {
  display: flex;
  align-items: flex-end;
  justify-content: space-around;
  min-height: 120px;
}
.bar-col {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  min-width: 0;
}
.bar-val {
  font-size: 13px;
  font-weight: 800;
  color: var(--ink);
  margin-bottom: 4px;
}
.bar-track {
  width: 22px;
  height: 80px;
  background: var(--paper);
  border: 1px solid var(--line);
  border-radius: 6px;
  display: flex;
  align-items: flex-end;
  overflow: hidden;
}
.bar-fill {
  width: 100%;
  background: linear-gradient(180deg, var(--green), color-mix(in srgb, var(--green) 75%, #000));
  border-radius: 0 0 4px 4px;
  transition: height 0.5s ease;
}
.bar-day {
  font-size: 11px;
  color: var(--ink2);
  margin-top: 6px;
}
.bar-date {
  font-size: 10px;
  color: var(--ink2);
  opacity: 0.7;
}
.growth-foot {
  text-align: center;
  font-size: 12px;
  color: var(--ink2);
  margin: 10px 0 0;
}
@media (prefers-reduced-motion: reduce) {
  .bar-fill {
    transition: none;
  }
}
</style>
