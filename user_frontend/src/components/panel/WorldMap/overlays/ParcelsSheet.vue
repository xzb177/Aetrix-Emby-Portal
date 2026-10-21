<template>
  <div class="sheet-parcels">
          <p class="w-sheet-empty" v-if="parcelsLoading && parcelList.length === 0">正在搬包裹…</p>
          <p class="w-sheet-empty" v-else-if="parcelList.length === 0">今天还没有新封存，明天再来拆 📦</p>
          <div class="w-parcel-grid" v-else>
            <ParcelBox
              v-for="p in parcelList"
              :key="String(p.id)"
              :parcel="p"
              :busy="unsealingIds.has(String(p.id))"
              @unseal="onUnseal"
              @play="openPicker(p)"
            />
          </div>
          <PlayerSchemeSheet v-model:open="pickerOpen" :title="pickerTitle" />
          </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import ParcelBox from '@/components/panel/ParcelBox.vue'
import PlayerSchemeSheet from '@/components/panel/PlayerSchemeSheet.vue'
import type { ParcelItem } from '@/composables/usePanelApi'

/** 封存包裹弹层：逻辑随 sheet 搬家（数据仍归 index 持有，行为不变） */
defineProps<{
  parcelList: ParcelItem[]
  parcelsLoading: boolean
  unsealingIds: Set<string>
  onUnseal: (id: string | number) => void
}>()

const pickerOpen = ref(false)
const pickerTitle = ref('')

function openPicker(p: ParcelItem) {
  pickerTitle.value = p.name || '神秘包裹'
  pickerOpen.value = true
}
</script>

<style scoped>
.w-sheet-empty {
  font-size: 13.5px;
  color: var(--paper-ink-soft);
  padding: 12px 0;
}
.w-sheet-foot {
  margin-top: 12px;
  font-size: 11px;
  color: var(--paper-ink-soft);
  text-align: center;
  letter-spacing: .08em;
}

.w-parcel-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
}
/* 面单卡原为横滑条设计、定高 216px，在弹层里内容稀疏显空 → 弹层内改自然高度紧凑排布 */
.w-parcel-grid { align-items: flex-start; }

.w-parcel-grid .v4-parcel { min-height: 0; }

.w-parcel-grid .v4-parcel-waybill { flex: 0 1 auto; justify-content: flex-start; }



@media (prefers-reduced-motion: reduce) {
  .sheet-parcels * {
    animation: none !important;
    transition: none !important;
  }
}
</style>
