<template>
  <div class="world-marks" aria-hidden="false">
    <section
      v-for="z in zones"
      :key="z.key"
      class="zone"
      :style="{ left: z.left, top: z.top, width: z.width }"
      :aria-label="z.title"
    >
      <p class="zone-label">
        <span class="zone-en">{{ z.en }}</span>
        <span class="zone-cn">{{ z.title }}</span>
      </p>
      <div class="zone-grid">
        <FacilityPin
          v-for="f in z.facilities"
          :key="f.key"
          :facility="f"
          :sub="subFor(f)"
          :status="statusFor(f)"
          :selected="selectedKey === f.key"
          @select="selectFacility"
        />
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { gameFacilities, gameFacilityGroups, type GameFacility } from '@/config/game'
import FacilityPin from './FacilityPin.vue'

/**
 * 分区设施层：v-for gameFacilities 按 group 分区渲染，区内 CSS grid 自动排布。
 * 禁止任何 left/top !important 硬编码定位 —— 分区位置是分区容器的静态布局，
 * 设施卡在区内永远走 grid 流。
 */
const props = defineProps<{
  /** 设施副标题（与桌面锚点同源的数据流） */
  subFor: (f: GameFacility) => string
  /** 设施状态点：有数据才返回 todo/done，无数据返回 undefined（不编造） */
  statusFor: (f: GameFacility) => 'todo' | 'done' | undefined
  selectedKey: string | null
  selectFacility: (f: GameFacility) => void
}>()

interface Zone {
  key: string
  en: string
  title: string
  left: string
  top: string
  width: string
  facilities: GameFacility[]
}

const ZONE_LAYOUT: Record<string, { en: string; left: string; top: string; width: string }> = {
  daily: { en: 'CAMP GATE', left: '13%', top: '2%', width: '21%' },
  account: { en: 'ACCOUNT HUT', left: '37%', top: '2%', width: '11%' },
  social: { en: 'SIGNAL HILL', left: '63%', top: '2%', width: '23%' },
  asset: { en: 'LEDGER HUT', left: '78%', top: '63%', width: '20%' },
  style: { en: 'LIFE CAMP', left: '2%', top: '63%', width: '17%' },
}

const zones = computed<Zone[]>(() =>
  gameFacilityGroups
    .map((g) => ({
      key: g.key,
      en: ZONE_LAYOUT[g.key]?.en ?? g.key.toUpperCase(),
      title: g.title,
      left: ZONE_LAYOUT[g.key]?.left ?? '2%',
      top: ZONE_LAYOUT[g.key]?.top ?? '2%',
      width: ZONE_LAYOUT[g.key]?.width ?? '20%',
      facilities: gameFacilities
        .filter((f) => f.group === g.key && f.enabled !== false)
        .sort((a, b) => a.order - b.order),
    }))
    .filter((z) => z.facilities.length > 0),
)
</script>

<style scoped>
.world-marks {
  position: absolute;
  inset: 0;
  z-index: 22;
  pointer-events: none;
}
.zone {
  position: absolute;
  pointer-events: auto;
  border: 1.5px dashed var(--paper-hairline);
  border-radius: 48%;
  padding: 18px 16px 14px;
  background: rgba(253, 251, 250, 0.55);
}
.zone-label {
  margin: 0 0 8px;
  text-align: center;
  font-size: 12px;
  line-height: 1.33;
  letter-spacing: 0.08em;
  color: var(--paper-ink-soft);
}
.zone-en {
  display: block;
  font-weight: 500;
}
.zone-cn {
  display: block;
  font-weight: 400;
  letter-spacing: 0.2em;
}
.zone-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(128px, 1fr));
  gap: 10px;
  justify-items: stretch;
}
@media (prefers-reduced-motion: reduce) {
  .zone {
    background: rgba(253, 251, 250, 0.8);
  }
}
</style>
