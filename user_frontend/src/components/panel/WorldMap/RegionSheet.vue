<template>
  <div class="region-grid">
    <p class="region-lead">{{ lead }}</p>
    <FacilityPin
      v-for="f in facilities"
      :key="f.key"
      :facility="f"
      :sub="subFor(f)"
      :status="statusFor(f)"
      :selected="selectedKey === f.key"
      @select="emit('select', f)"
    />
  </div>
</template>

<script setup lang="ts">
import type { GameFacility } from '@/config/game'
import FacilityPin from './FacilityPin.vue'

/**
 * 分区抽屉里的设施纸卡。
 * 卡在纸面上排开，不压在地图上；副标题在抽屉里完整显示，不再被地图裁切。
 */
defineProps<{
  lead: string
  facilities: GameFacility[]
  subFor: (f: GameFacility) => string
  statusFor: (f: GameFacility) => 'todo' | 'done' | undefined
  selectedKey: string | null
}>()

const emit = defineEmits<{ select: [f: GameFacility] }>()
</script>

<style scoped>
.region-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 12px;
}
.region-lead {
  grid-column: 1 / -1;
  margin: 0 0 4px;
  font-size: 14px;
  line-height: 1.43;
  font-weight: 400;
  color: var(--paper-ink-soft, #6d675c);
}
/* 抽屉里副标题完整可读；地图外的卡不再被 overflow 裁成一行 */
.region-grid :deep(.pin-sub) {
  display: block;
  -webkit-line-clamp: unset;
  overflow: visible;
}
</style>
