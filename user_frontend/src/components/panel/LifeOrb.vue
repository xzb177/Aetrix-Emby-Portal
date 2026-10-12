<template>
  <div class="v4-organism">
    <div class="orb" :class="orbMood" aria-hidden="true"></div>
    <h3>{{ statusLine }}</h3>
    <div class="cond" v-html="conditionHtml"></div>
    <div class="vitals">
      <div>
        <b>{{ vitalCells[0].value }}</b>
        <span>{{ vitalCells[0].label }}</span>
      </div>
      <div>
        <b>{{ vitalCells[1].value }}</b>
        <span>{{ vitalCells[1].label }}</span>
      </div>
      <div>
        <b>{{ vitalCells[2].value }}</b>
        <span>{{ vitalCells[2].label }}</span>
      </div>
      <div>
        <b>{{ vitalCells[3].value }}</b>
        <span>{{ vitalCells[3].label }}</span>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { ServerVitals } from '@/composables/usePanelApi'

const props = defineProps<{ vitals: ServerVitals }>()

const backlog = computed(() => props.vitals.backlog ?? 0)
const level = computed(() => String(props.vitals.level ?? '').toLowerCase())
const status = computed(() => String(props.vitals.status ?? '').toLowerCase())

/** 光球情绪：健康绿 / 亚健康黄 / 趴窝灰 */
const orbMood = computed(() => {
  if (level.value === 'down' || status.value === 'down') return 'tired'
  if (level.value === 'degraded' || status.value === 'degraded') return 'busy'
  return ''
})

/** 拟人化状态行：全部来自真实数据，不许造假 */
const statusLine = computed(() => {
  if (level.value === 'down' || status.value === 'down') return '状态：趴窝了 😴'
  if (backlog.value > 0) return '状态：消化中 😋'
  return '状态：吃饱了，晒太阳 😌'
})

const conditionHtml = computed(() => {
  const n = backlog.value
  if (n > 0) {
    return `刚吃下 <b>${n}</b> 条待刮削，正在慢慢消化<br>心跳正常 · 胃口很好`
  }
  return '肠胃清空，一身轻松<br>心跳正常 · 体温正常 · 随时待命'
})

const vitalCells = computed(() => {
  const st = status.value || level.value || '—'
  const stText =
    st === 'healthy' || st === 'up' ? '在线' :
    st === 'degraded' ? '有点累' :
    st === 'down' ? '趴窝' : String(props.vitals.status || props.vitals.level || '—')
  return [
    { value: stText, label: '状态' },
    { value: backlog.value > 0 ? `${backlog.value}` : '0', label: '待消化' },
    { value: String(props.vitals.level || '—'), label: '健康等级' },
    { value: String(props.vitals.version || '—'), label: '版本' },
  ]
})
</script>

<style scoped>
/* 取自设计稿 .organism / .orb / .vitals */
.v4-organism {
  margin: 14px 0 0;
  background: #fffdf6;
  border: 1.5px solid var(--ink);
  border-radius: 16px;
  padding: 20px;
  text-align: center;
  position: relative;
  overflow: hidden;
}
.orb {
  width: 110px;
  height: 110px;
  margin: 6px auto;
  border-radius: 50%;
  background: radial-gradient(circle at 35% 30%, #b8e0a8, #4a7c59 70%);
  animation: v4-breathe 4s ease-in-out infinite;
  position: relative;
  box-shadow: 0 0 40px rgba(74, 124, 89, 0.35);
}
.orb.busy {
  background: radial-gradient(circle at 35% 30%, #f5dfa0, #c99b3f 70%);
  box-shadow: 0 0 40px rgba(201, 155, 63, 0.35);
}
.orb.tired {
  background: radial-gradient(circle at 35% 30%, #d8d2c2, #8a8478 70%);
  box-shadow: 0 0 24px rgba(138, 132, 120, 0.3);
  animation-duration: 7s;
}
@media (prefers-reduced-motion: reduce) {
  .orb {
    animation: none;
  }
}
@keyframes v4-breathe {
  0%,
  100% {
    transform: scale(1);
  }
  50% {
    transform: scale(1.08);
  }
}
.orb::after {
  content: "";
  position: absolute;
  inset: 18px;
  border-radius: 50%;
  background: radial-gradient(circle at 40% 35%, rgba(255, 255, 255, 0.7), transparent 60%);
}
.v4-organism h3 {
  font-size: 15px;
  margin: 10px 0 0;
}
.v4-organism .cond {
  font-size: 12px;
  color: var(--ink2);
  margin-top: 4px;
  line-height: 1.7;
}
.vitals {
  display: flex;
  margin-top: 14px;
  border-top: 1px dotted var(--line);
  padding-top: 12px;
}
.vitals div {
  flex: 1;
  min-width: 0;
}
.vitals b {
  font-size: 15px;
  display: block;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.vitals span {
  font-size: 10px;
  color: var(--ink2);
}
.vitals div + div {
  border-left: 1px dotted var(--line);
}
</style>
