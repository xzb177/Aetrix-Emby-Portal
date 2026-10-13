<template>
  <div
    class="v4-parcel"
    :class="{ opened: isOpen, 'is-busy': busy }"
    role="button"
    tabindex="0"
    :aria-label="isOpen ? parcel.name : '待拆封存，点击开封'"
    @click="onTap"
    @keydown="onKey"
  >
    <div v-if="!isOpen" class="v4-parcel-hideable">
      <div class="v4-parcel-seal">✦</div>
      <div class="v4-parcel-lbl">待拆</div>
      <div class="v4-parcel-meta">{{ metaLine }}</div>
    </div>
    <div v-else class="v4-parcel-reveal">
      <b>{{ parcel.name || '未知条目' }}</b><br />
      <span class="v4-parcel-sub">{{ subLine }}</span><br />
      <span class="v4-parcel-time">{{ arrivedText }}</span>
    </div>
    <a
      v-if="isOpen && parcel.deep_link"
      class="v4-parcel-go"
      :href="parcel.deep_link"
      @click.stop
    >去播放器看 →</a>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import type { ParcelItem } from '@/composables/usePanelApi'

const props = defineProps<{ parcel: ParcelItem }>()
const emit = defineEmits<{ (e: 'unseal', id: string | number): void }>()

const busy = ref(false)
const isOpen = computed(() => !!props.parcel.unsealed)

/** 类型标签：电影 / 电视剧 */
const typeLabel = computed(() => {
  const t = String(props.parcel.item_type || props.parcel.type || '')
  if (t === 'series') return '剧集'
  if (t === 'movie') return '电影'
  return props.parcel.media_type || '影片'
})
/** 未拆时的小字：类型 · 集数/清晰度 */
const metaLine = computed(() => {
  const parts = [typeLabel.value]
  const eps = props.parcel.episode_count
  if (typeof eps === 'number' && eps > 0) parts.push(`${eps}集`)
  const res = props.parcel.video_resolution || props.parcel.resolution
  if (res) parts.push(String(res))
  else if (props.parcel.production_year) parts.push(String(props.parcel.production_year))
  return parts.join(' · ')
})
/** 拆开后的副标题行 */
const subLine = computed(() => {
  const parts = [typeLabel.value]
  const eps = props.parcel.episode_count
  if (typeof eps === 'number' && eps > 0) parts.push(`全 ${eps} 集`)
  const res = props.parcel.video_resolution || props.parcel.resolution
  if (res) parts.push(String(res))
  else if (props.parcel.production_year) parts.push(String(props.parcel.production_year))
  return parts.join(' · ') + ' · 已入库'
})
/** 到货时间：今天 HH:MM / 昨天 / M-D */
const arrivedText = computed(() => {
  const raw = props.parcel.arrived_at || props.parcel.added_at
  if (!raw) return '刚刚到货'
  const d = new Date(raw)
  if (Number.isNaN(d.getTime())) return '刚刚到货'
  const now = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`
  const sameDay = d.toDateString() === now.toDateString()
  if (sameDay) return `今天 ${hm} 到货`
  const yd = new Date(now)
  yd.setDate(yd.getDate() - 1)
  if (d.toDateString() === yd.toDateString()) return `昨天 ${hm} 到货`
  return `${d.getMonth() + 1}月${d.getDate()}日 ${hm} 到货`
})

function onKey(e: KeyboardEvent) {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault()
    onTap()
  }
}

function onTap() {
  if (isOpen.value || busy.value) return
  busy.value = true
  try {
    emit('unseal', props.parcel.id)
  } finally {
    // optimistic 更新由 store 完成；busy 只防连点，短暂释放
    window.setTimeout(() => { busy.value = false }, 600)
  }
}
</script>

<style scoped>
/* 取自设计稿 home.html .parcel（PR-3 填实） */
.v4-parcel {
  flex: 1;
  min-width: 0;
  background: var(--paper2);
  border: 1.5px dashed var(--ink2);
  border-radius: 10px;
  padding: 14px 8px;
  text-align: center;
  cursor: pointer;
  transition: transform 0.15s ease, border-color 0.25s ease, background 0.25s ease;
  position: relative;
  min-height: 132px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
}
.v4-parcel:active { transform: scale(0.94); }
.v4-parcel.is-busy { pointer-events: none; opacity: 0.75; }
.v4-parcel-seal {
  width: 38px;
  height: 38px;
  border-radius: 50%;
  background: var(--red);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 20px;
  box-shadow: 0 2px 0 #a33319;
  margin: 0 auto;
}
.v4-parcel-lbl { font-size: 12px; font-weight: 700; margin-top: 6px; }
.v4-parcel-meta { font-size: 11px; color: var(--ink2); }
/* 开封态：取自设计稿 .parcel.opened */
.v4-parcel.opened {
  border-style: solid;
  border-color: var(--green);
  background: #F1F4EC;
  cursor: default;
  animation: v4-pop 0.35s ease;
}
.v4-parcel.opened:active { transform: none; }
.v4-parcel.opened .v4-parcel-seal { background: var(--green); box-shadow: 0 2px 0 #35593f; }
.v4-parcel-reveal { font-size: 12px; line-height: 1.7; animation: v4-fadein 0.4s ease; }
.v4-parcel-reveal b { font-size: 13px; }
.v4-parcel-sub { color: var(--ink2); font-size: 11px; }
.v4-parcel-time { color: var(--ink2); font-size: 11px; }
.v4-parcel-go {
  display: inline-flex;
  align-items: center;
  min-height: 44px;
  margin-top: 6px;
  background: var(--ink);
  color: #fff;
  font-size: 11px;
  padding: 10px 18px;
  border-radius: 16px;
  text-decoration: none;
  animation: v4-fadein 0.4s ease 0.1s both;
}
@keyframes v4-pop {
  0% { transform: scale(0.92); }
  60% { transform: scale(1.03); }
  100% { transform: scale(1); }
}
@keyframes v4-fadein {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: none; }
}
.v4-parcel:focus-visible {
  outline: 2px solid var(--red);
  outline-offset: 2px;
}
</style>
