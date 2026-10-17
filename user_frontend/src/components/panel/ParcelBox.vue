<template>
  <!-- 桌面三栏货架：字排封面变体（C档第2批；移动端与世界地图仍用默认盲盒变体） -->
  <div
    v-if="props.variant === 'cover'"
    class="d3-parcel d3-hover"
    :class="{ 'is-busy': busy, 'no-link': isOpen && !parcel.deep_link }"
    :role="isOpen ? undefined : 'button'"
    :tabindex="isOpen ? undefined : '0'"
    :aria-label="isOpen ? undefined : '待拆封存，点击开封'"
    @click="onTap"
    @keydown="onKey"
  >
    <div class="d3-cover" :class="coverTone">
      <div class="d3-cov-k">{{ coverKicker }}</div>
      <div class="d3-cov-t">{{ parcel.name || '神秘包裹' }}</div>
    </div>
    <div class="d3-parcel-body">
      <div style="min-width:0">
        <div class="d3-parcel-name">{{ parcel.name || '神秘包裹' }}</div>
        <div class="d3-parcel-meta">{{ coverMeta }}</div>
      </div>
      <span class="d3-tag-sealed" :class="{ 'is-open': isOpen }">{{ isOpen ? '已拆封' : '待拆' }}</span>
    </div>
  </div>
  <div
    v-else
    class="v4-parcel"
    :class="{ opened: isOpen, 'is-busy': busy, 'no-link': isOpen && !parcel.deep_link, 'just-opened': justOpened }"
    :role="isOpen ? undefined : 'button'"
    :tabindex="isOpen ? undefined : '0'"
    :aria-label="isOpen ? undefined : '待拆封存，点击开封'"
    @click="onTap"
    @keydown="onKey"
  >
    <!-- 印章：未拆=封，开封=剧/影 -->
    <div class="v4-parcel-stamp" aria-hidden="true" @animationend="justOpened = false">{{ stampText }}</div>
    <div class="v4-parcel-waybill">
      <template v-if="!isOpen">
        <div class="v4-parcel-seal">✦</div>
        <div class="v4-parcel-lbl">待拆</div>
        <div class="v4-parcel-meta">{{ metaLine }}</div>
      </template>
      <template v-else>
        <!-- R37 P3-C2：名缺省说人话「神秘包裹」，不甩「未知」 -->
        <b class="v4-parcel-name">{{ parcel.name || '神秘包裹' }}</b>
        <div class="v4-parcel-divider" aria-hidden="true"></div>
        <span class="v4-parcel-sub">{{ bodySub }}</span>
        <!-- R37 P3-C3：时间缺省直接隐藏时间行，不甩「时间未知」 -->
        <span v-if="arrivedText" class="v4-parcel-time">{{ arrivedText }}</span>
      </template>
    </div>
    <div class="v4-parcel-foot">
      <span v-if="!isOpen" class="v4-parcel-hint">点一下开封</span>
      <a
        v-else-if="parcel.deep_link"
        class="v4-parcel-go"
        :href="parcel.deep_link"
        target="_blank"
        rel="noopener"
        aria-label="去播放器看"
        @click.stop
      >去看 →</a>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { ParcelItem } from '@/composables/usePanelApi'

const props = withDefaults(
  defineProps<{ parcel: ParcelItem; busy?: boolean; variant?: 'parcel' | 'cover' }>(),
  { variant: 'parcel' },
)
const emit = defineEmits<{ (e: 'unseal', id: string | number): void }>()

const isOpen = computed(() => !!props.parcel.unsealed)

/* P-3 拆封仪式感：开封瞬间给印章一个「盖下去」的动效（一次性，动画结束自清） */
const justOpened = ref(false)
watch(isOpen, (now, before) => {
  if (now && !before) justOpened.value = true
})

/** 类型标签：白名单映射 series→剧集、movie→电影，其余统一收敛为"影片"（不直接展示后端原文） */
const typeLabel = computed(() => {
  const t = String(props.parcel.item_type || props.parcel.type || '')
  if (t === 'series') return '剧集'
  if (t === 'movie') return '电影'
  return '影片'
})
/** 印章字：未拆=封，开封后剧集=剧、电影=影 */
const stampText = computed(() => {
  if (!isOpen.value) return '封'
  const t = String(props.parcel.item_type || props.parcel.type || '')
  if (t === 'series') return '剧'
  if (t === 'movie') return '影'
  return '✦'
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
/* ---------- cover 变体（桌面三栏字排封面） ---------- */
/** 双色调按 id 稳定散列，保证同一包裹每次色调一致 */
const coverTone = computed(() => {
  const s = String(props.parcel.id ?? '')
  let h = 0
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0
  return `d3-tone-${'abcd'[h % 4]}`
})
/** 封面英文 kicker：只映射后端真实给出的类型，不编造细分 */
const coverKicker = computed(() => {
  const t = String(props.parcel.item_type || props.parcel.type || '')
  if (t === 'series') return 'TV SERIES'
  if (t === 'movie') return 'FILM'
  return 'FEATURE'
})
/** cover 卡副标题行：类型 · 集数/年份（复用面单口径，不编新字段） */
const coverMeta = computed(() => {
  const parts = [typeLabel.value]
  const eps = props.parcel.episode_count
  if (typeof eps === 'number' && eps > 0) parts.push(`${eps}集`)
  const y = props.parcel.production_year
  if (y) parts.push(String(y))
  if (arrivedText.value) parts.push(arrivedText.value)
  return parts.join(' · ')
})
/** 开封后的副标题行：类型 · 年份（面单第二行，短） */
const bodySub = computed(() => {
  const parts = [typeLabel.value]
  const eps = props.parcel.episode_count
  if (typeof eps === 'number' && eps > 0) parts.push(`全${eps}集`)
  else if (props.parcel.production_year) parts.push(String(props.parcel.production_year))
  return parts.join(' · ')
})
/** 到货时间：今天 HH:MM / 昨天 / M-D；缺省返回空串，由模板隐藏时间行（R37 P3-C3） */
const arrivedText = computed(() => {
  const raw = props.parcel.arrived_at || props.parcel.added_at
  if (!raw) return ''
  const d = new Date(raw)
  if (Number.isNaN(d.getTime())) return ''
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

function goPlayer() {
  const link = props.parcel.deep_link
  if (typeof link === 'string' && link) window.open(link, '_blank', 'noopener')
}

function onKey(e: KeyboardEvent) {
  if ((e.target as HTMLElement).closest('a')) return
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault()
    onTap()
  }
}

function onTap() {
  if (isOpen.value) {
    // R29：开封但无跳转链接时整卡不可点，直接 return
    if (!props.parcel.deep_link) return
    // 已开封：整卡点进播放器（与"去看 →"同一跳转，新开标签页）
    goPlayer()
    return
  }
  if (props.busy) return
  // busy 由父组件的请求生命周期控制（请求完成前防连点），这里只做事件转发
  emit('unseal', props.parcel.id)
}
</script>

<style scoped>
/* 竖版文字面单卡：快递包裹面单气质，不放海报图 */
.v4-parcel {
  flex: 0 0 auto;
  width: 132px;
  min-height: 216px;
  scroll-snap-align: start;
  background: var(--paper2);
  border: 1.5px solid var(--ink2);
  border-radius: var(--paper-r-md, 10px);
  padding: 14px 10px 10px;
  position: relative;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 8px;
  transition: transform 0.15s ease;
}
.v4-parcel:not(.opened) { border-style: dashed; }
.v4-parcel:active { transform: scale(0.96); }
/* R29：开封但无跳转链接时整卡不可点 */
.v4-parcel.no-link { cursor: default; }
.v4-parcel.is-busy { pointer-events: none; opacity: 0.75; }
/* 印章：右上角旋转小圆章 */
.v4-parcel-stamp {
  position: absolute;
  top: 8px;
  right: 8px;
  width: 30px;
  height: 30px;
  border-radius: var(--paper-r-full, 50%);
  border: 1.5px solid var(--red);
  color: var(--red);
  font-size: 13px;
  font-weight: 800;
  display: flex;
  align-items: center;
  justify-content: center;
  transform: rotate(-10deg);
  opacity: 0.85;
  pointer-events: none;
}
.v4-parcel-waybill {
  flex: 1;
  display: flex;
  flex-direction: column;
  justify-content: center;
  min-height: 0;
}
/* 未拆态：火漆封印居中 */
.v4-parcel:not(.opened) .v4-parcel-waybill { align-items: center; text-align: center; }
.v4-parcel-seal {
  width: 38px;
  height: 38px;
  border-radius: var(--paper-r-full, 50%);
  background: var(--red);
  color: var(--on-dark, #fff);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 20px;
  box-shadow: 0 2px 0 var(--red-deep, #a33319);
  margin: 0 auto;
}
.v4-parcel-lbl { font-size: 12px; font-weight: 700; margin-top: 6px; }
.v4-parcel-meta { font-size: 11px; color: var(--ink2); }
/* 开封态：面单排版 */
.v4-parcel.opened {
  border-color: var(--green);
  /* R39-04：原硬编码 #F1F4EC 与 --paper(#F6F1E7) 均为暖白，收敛到 token */
  background: var(--paper);
  animation: v4-pop 0.35s ease;
}
.v4-parcel-name {
  font-size: 15px;
  font-weight: 800;
  line-height: 1.4;
  padding-right: 28px; /* 给印章让位 */
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  animation: v4-fadein 0.4s ease;
}
.v4-parcel-divider {
  border-top: 1px dashed var(--ink2);
  margin: 8px 0;
}
.v4-parcel-sub { color: var(--ink2); font-size: 11px; }
.v4-parcel-time { color: var(--ink2); font-size: 11px; }
.v4-parcel-foot { margin-top: auto; min-height: 18px; }
.v4-parcel-hint { font-size: 11px; color: var(--ink2); }
.v4-parcel-go {
  display: inline-flex;
  align-items: center;
  min-height: 44px;
  font-size: 12px;
  font-weight: 700;
  color: var(--red);
  text-decoration: none;
  animation: v4-fadein 0.4s ease 0.1s both;
}
.v4-parcel.just-opened .v4-parcel-stamp {
  animation: stamp-slam .45s cubic-bezier(.2, 1.6, .4, 1) both;
}
@keyframes stamp-slam {
  0% { transform: rotate(6deg) scale(2.1); opacity: 0; }
  60% { transform: rotate(-13deg) scale(.94); opacity: 1; }
  100% { transform: rotate(-10deg) scale(1); opacity: 1; }
}
.v4-parcel.just-opened {
  animation: parcel-nudge .3s ease-out;
}
@keyframes parcel-nudge {
  0% { transform: translateY(0); }
  40% { transform: translateY(-2px); }
  100% { transform: translateY(0); }
}
@media (prefers-reduced-motion: reduce) {
  .v4-parcel.just-opened,
  .v4-parcel.just-opened .v4-parcel-stamp { animation: none; }
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

/* R27：减少动态偏好下装饰动画/过渡停掉（本地守卫） */
@media (prefers-reduced-motion: reduce) {
  .v4-parcel { transition: none; }
  .v4-parcel:active { transform: none; }
  .v4-parcel.opened { animation: none; transition: none; }
  .v4-parcel-name { animation: none; transition: none; }
  .v4-parcel-go { animation: none; transition: none; }
}
</style>
