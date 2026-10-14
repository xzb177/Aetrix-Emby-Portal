<template>
  <div class="v4-eco">
    <div ref="plantsEl" class="plants">
      <div v-for="(p, i) in plants" :key="p.library_id" class="plant">
        <div class="leaf" aria-hidden="true">{{ leafOf(i) }}</div>
        <div class="stem" :style="{ height: p.height + 'px' }"></div>
        <div class="ct">{{ fmtCount(p.count) }}</div>
        <div class="nm">{{ p.name }}</div>
      </div>
    </div>
    <p class="eco-foot">🌧️ 片库是活的，每天都在长</p>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import type { EcoItem } from '@/composables/usePanelApi'

const props = defineProps<{ eco: EcoItem[] }>()

/** 叶子 emoji：按索引确定性分配 */
const LEAVES = ['🌿', '🌱', '🌿', '🌷', '🌿', '🍀', '🌾', '🌵', '🎋', '🌺', '🍁', '🌻']
const leafOf = (i: number) => LEAVES[i % LEAVES.length]

/** 数量格式化：万 */
function fmtCount(n: number): string {
  if (n >= 10000) {
    const w = n / 10000
    return w >= 100 ? `${Math.round(w)}万` : `${w.toFixed(1)}万`
  }
  return `${n}`
}

const MAX_H = 110

const plants = computed(() => {
  const list = (props.eco || []).slice(0, 12)
  const max = Math.max(1, ...list.map((e) => e.count || 0))
  return list.map((e) => ({
    ...e,
    height: Math.max(10, Math.round(((e.count || 0) / max) * MAX_H)),
  }))
})

const plantsEl = ref<HTMLElement | null>(null)
/** 内容超出可横滑时才显示右侧渐隐，避免无滚动时误导 */
function updateScrollHint() {
  const el = plantsEl.value
  if (!el) return
  el.classList.toggle('can-scroll', el.scrollWidth > el.clientWidth + 1)
}
onMounted(() => {
  updateScrollHint()
  window.addEventListener('resize', updateScrollHint)
})
onUnmounted(() => {
  window.removeEventListener('resize', updateScrollHint)
})
watch(plants, () => {
  requestAnimationFrame(updateScrollHint)
})
</script>

<style scoped>
/* 取自设计稿 .eco / .plants / .plant */
.v4-eco {
  background: var(--paper-card);
  border: 1.5px solid var(--line);
  border-radius: 14px;
  padding: 16px;
}
.plants {
  display: flex;
  align-items: flex-end;
  justify-content: space-around;
  min-height: 130px;
  border-bottom: 2px solid var(--ink);
  padding-bottom: 0;
  margin-bottom: 8px;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  gap: 4px;
}
/* 右侧渐隐边缘：仅可横滑时显示，提示还能滑 */
.plants.can-scroll {
  mask-image: linear-gradient(90deg, #000 92%, transparent);
  -webkit-mask-image: linear-gradient(90deg, #000 92%, transparent);
}
.plant {
  text-align: center;
  min-width: 44px;
  flex: 1;
}
.plant .stem {
  width: 10px;
  margin: 0 auto;
  border-radius: 5px 5px 0 0;
  background: linear-gradient(180deg, #7cb87c, var(--green, #4a7c59));
}
.plant .leaf {
  font-size: 14px;
  margin-bottom: -4px;
}
.plant .nm {
  font-size: 10px;
  margin-top: 6px;
  color: var(--ink2);
  line-height: 1.4;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.plant .ct {
  font-size: 10px;
  font-weight: 700;
}
.eco-foot {
  font-size: 11px;
  color: var(--ink2);
  text-align: center;
  margin: 8px 0 0;
}
</style>
