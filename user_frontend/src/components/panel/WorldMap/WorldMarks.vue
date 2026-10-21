<template>
  <div class="region-rail" role="group" aria-label="营地分区">
    <button
      v-for="z in zones"
      :key="z.key"
      type="button"
      class="region-btn"
      :class="{ 'is-open': openRegionKey === z.key }"
      :aria-expanded="openRegionKey === z.key"
      aria-haspopup="dialog"
      :aria-controls="openRegionKey === z.key ? 'region-sheet' : undefined"
      :aria-label="`${z.title}，${z.sub}，${z.facilities.length} 个入口`"
      @click="openRegion(z.key)"
    >
      <span class="region-en">{{ z.en }}</span>
      <span class="region-cn">{{ z.title }}</span>
      <span class="region-sub">{{ z.sub }} · {{ z.facilities.length }}</span>
    </button>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { gameFacilities, gameFacilityGroups, type GameFacility } from '@/config/game'

/**
 * 分区入口：地图上只留地标。设施纸卡收到分区抽屉里，
 * 这里只放一排可键盘操作的分区按钮（Enter/Space 由 button 原生打开）。
 */
defineProps<{
  openRegionKey: string | null
  openRegion: (key: string) => void
}>()

interface Zone {
  key: string
  en: string
  title: string
  sub: string
  facilities: GameFacility[]
}

const ZONE_EN: Record<string, string> = {
  daily: 'CAMP GATE',
  account: 'ACCOUNT HUT',
  social: 'SIGNAL HILL',
  asset: 'LEDGER HUT',
  style: 'LIFE CAMP',
}

const zones = computed<Zone[]>(() =>
  gameFacilityGroups
    .map((g) => ({
      key: g.key,
      en: ZONE_EN[g.key] ?? g.key.toUpperCase(),
      title: g.title,
      sub: g.sub,
      facilities: gameFacilities
        .filter((f) => f.group === g.key && f.enabled !== false)
        .sort((a, b) => a.order - b.order),
    }))
    .filter((z) => z.facilities.length > 0),
)
</script>

<style scoped>
.region-rail {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 8px;
  margin-top: 12px;
}
.region-btn {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  justify-content: center;
  gap: 2px;
  min-height: 44px;
  padding: 10px 12px;
  background: var(--paper-card-soft, #fdfbfa);
  border: 1px solid var(--paper-hairline, #d1d1cd);
  border-radius: 12px;
  cursor: pointer;
  font-family: "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
  text-align: left;
  color: var(--paper-ink-deep, #27251e);
  transition: transform var(--au-fast, 0.16s);
}
.region-btn:hover {
  transform: translateY(-2px);
}
.region-btn:focus-visible {
  outline: 2px solid var(--paper-teal, #016a71);
  outline-offset: 2px;
}
.region-btn.is-open .region-cn {
  color: var(--paper-teal, #016a71);
}
.region-en {
  font-size: 11px;
  line-height: 1.33;
  font-weight: 500;
  letter-spacing: 0.08em;
  color: var(--paper-ink-soft, #6d675c);
}
.region-cn {
  font-size: 14px;
  line-height: 1.43;
  font-weight: 500;
}
.region-sub {
  font-size: 12px;
  line-height: 1.33;
  font-weight: 400;
  color: var(--paper-ink-soft, #6d675c);
}
@media (prefers-reduced-motion: reduce) {
  .region-btn {
    transition: none;
  }
  .region-btn:hover {
    transform: none;
  }
}
</style>
