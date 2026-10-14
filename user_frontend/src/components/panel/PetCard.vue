<template>
  <div v-if="pet" class="v4-pet">
    <div class="evo" role="img" :aria-label="`馆灵：${pet.name}`">{{ pet.icon }}</div>
    <h3>{{ pet.name }} · {{ stageLabel }}</h3>
    <p>
      已经陪你看了 {{ hoursText }}<br />
      {{ flavorText }}
    </p>
    <div
      class="evo-bar"
      role="progressbar"
      :aria-valuenow="progressPct"
      aria-valuemin="0"
      aria-valuemax="100"
      :aria-label="`进化进度 ${progressPct}%`"
    >
      <i :style="{ '--evo-p': evoScale }"></i>
    </div>
    <div class="next">{{ nextText }}</div>
    <div class="evo-steps">
      <span
        v-for="(s, i) in pet.stages"
        :key="s.name"
        :class="{ on: i <= pet.stage }"
      >{{ s.icon }} {{ s.name }}</span>
    </div>
  </div>
  <div v-else class="v4-pet">
    <div class="v4-skeleton" style="height:200px" aria-hidden="true"></div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { PetData } from '@/composables/usePanelApi'

const props = defineProps<{ pet: PetData }>()

/** 阶段文案：优先用后端给的阶段名，拿不到再按索引给中文阶段名 */
const STAGE_LABELS = ['雏形', '成长期', '成熟期', '蜕变期', '完全体']

const stageLabel = computed(() => props.pet.stages?.[props.pet.stage]?.name || STAGE_LABELS[Math.min(props.pet.stage, STAGE_LABELS.length - 1)] || '成长期')

/** 时长文案：已看时长用 round，剩余用 ceil（避免"0 分钟"） */
function formatDuration(hours: number, mode: 'round' | 'ceil' = 'round'): string {
  const v = Number.isFinite(hours) ? Math.max(0, hours) : 0
  const r = mode === 'ceil' ? Math.ceil : Math.round
  if (v >= 1) return `${r(v)} 小时`
  return `${Math.max(1, r(v * 60))} 分钟`
}

const hoursText = computed(() => formatDuration(props.pet.hours))

/** 文案：下一阶段还差多少 */
const nextText = computed(() => {
  const p = props.pet
  // 有下一形态：再分「进度可算」与「进度算不出来」两路，不谎称已满级
  if (p.next_name) {
    // R48A-5：hours 非法时降级不展示剩余量
    if (p.next_hours != null && Number.isFinite(p.hours)) {
      const remain = Math.max(0, p.next_hours - p.hours)
      // R48A-53：差值已归零说明马上进化，不说"再看 0 分钟"
      if (remain <= 0) return '马上就能进化啦'
      return `再看 ${formatDuration(remain, 'ceil')}，进化为「${p.next_name}」`
    }
    // next_name 存在但 hours/next_hours 脏数据或未下发：如实说明进度暂不可算
    return `进化为「${p.next_name}」（进度计算中）`
  }
  return '已达最高形态 · 它是你的了 🐉'
})

/** 趣味文案：根据当前阶段变化 */
const flavorText = computed(() => {
  const h = new Date().getHours()
  const night = h >= 23 || h < 5
  if (props.pet.stage <= 0) return '它刚睁开眼睛，好奇地看着你的片库'
  if (night) return '夜深了，它陪你守着这盏小灯'
  return '你看的每一分钟，都在喂养它'
})

/** 进化进度百分比：钳制 0–100，NaN→0；aria 与刻度共用 */
const progressPct = computed(() => {
  const v = Number.isFinite(props.pet.progress) ? props.pet.progress : 0
  return Math.min(100, Math.max(0, Math.round(v * 100)))
})

const evoScale = computed(() => progressPct.value / 100)
</script>

<style scoped>
/* 取自设计稿 .pet / .evo / .evo-bar / .evo-steps */
.v4-pet {
  background: var(--paper-card);
  border: 1.5px solid var(--line);
  border-radius: 14px;
  padding: 18px;
  text-align: center;
}
.v4-pet .evo {
  font-size: 56px;
  margin: 6px 0;
  animation: v4-bob 3s ease-in-out infinite;
}
@media (prefers-reduced-motion: reduce) {
  .v4-pet .evo {
    animation: none;
  }
  /* R48A-30：减少动态偏好下进度条过渡也停掉 */
  .evo-bar i {
    transition: none;
  }
}
@keyframes v4-bob {
  0%,
  100% {
    transform: translateY(0);
  }
  50% {
    transform: translateY(-8px);
  }
}
.v4-pet h3 {
  font-size: 15px;
  margin: 0;
}
.v4-pet p {
  font-size: 12px;
  color: var(--ink2);
  margin-top: 6px;
  line-height: 1.8;
}
.evo-bar {
  height: 10px;
  background: var(--paper2);
  border-radius: 6px;
  margin: 14px 4px 6px;
  overflow: hidden;
}
.evo-bar i {
  display: block;
  height: 100%;
  background: linear-gradient(90deg, var(--gold), var(--red));
  border-radius: 6px;
  width: 100%;
  transform: scaleX(var(--evo-p, 0));
  transform-origin: left center;
  transition: transform 1s ease;
}
.v4-pet .next {
  font-size: 12px;
  color: var(--ink2);
}
.evo-steps {
  display: flex;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
  font-size: 12px;
  color: var(--ink2);
}
.evo-steps .on {
  color: var(--red);
  font-weight: 700;
}
</style>
