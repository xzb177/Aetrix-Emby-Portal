<template>
  <div class="v4-radar-box">
    <h3>BACKGROUND OPS · {{ activeCount }} 个任务在线</h3>
    <canvas ref="cv" class="v4-radar-cv" aria-label="任务雷达扫描图"></canvas>
    <div class="blips">
      <div v-for="t in tasks" :key="t.key" class="blip">
        <i :class="{ idle: !t.active }"></i>{{ t.name }} · {{ t.active ? '进行中' : '待命' }}
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import type { RadarTask } from '@/composables/usePanelApi'

const props = defineProps<{ tasks: RadarTask[] }>()
const cv = ref<HTMLCanvasElement | null>(null)

const tasks = computed(() => props.tasks || [])
const activeCount = computed(() => tasks.value.filter((t) => t.active).length)

let raf = 0
let angle = 0
let lastTs = 0
/** 角速度：0.045 rad/帧 × 60fps —— 高刷屏也用时间增量，转速一致 */
const ANGULAR_SPEED = 0.045 * 60
const reduced = typeof window !== 'undefined'
  && typeof window.matchMedia === 'function'
  && window.matchMedia('(prefers-reduced-motion: reduce)').matches

/** 固定角度布局（确定性，不闪烁），按任务数均分 */
function targetPos(i: number, n: number, cx: number, cy: number, r: number) {
  const a = (i / Math.max(n, 1)) * Math.PI * 2 - Math.PI / 2
  const rr = r * (0.45 + 0.4 * ((i * 37) % 10) / 10)
  return { x: cx + Math.cos(a) * rr, y: cy + Math.sin(a) * rr, label: tasks.value[i]?.name || '' }
}

function drawStatic() {
  const c = cv.value
  if (!c) return
  const dpr = Math.min(window.devicePixelRatio || 1, 2)
  const w = c.clientWidth
  const h = 190
  c.width = w * dpr
  c.height = h * dpr
  const ctx = c.getContext('2d')
  if (!ctx) return
  ctx.scale(dpr, dpr)
  const cx = w / 2
  const cy = h / 2
  const R = Math.min(w, h) / 2 - 12
  ctx.strokeStyle = 'rgba(155,224,138,.25)'
  ctx.lineWidth = 1.5
  ;[0.33, 0.66, 1].forEach((k) => {
    ctx.beginPath()
    ctx.arc(cx, cy, R * k, 0, Math.PI * 2)
    ctx.stroke()
  })
  ctx.beginPath()
  ctx.moveTo(cx - R, cy)
  ctx.lineTo(cx + R, cy)
  ctx.moveTo(cx, cy - R)
  ctx.lineTo(cx, cy + R)
  ctx.stroke()
  const n = tasks.value.length
  for (let i = 0; i < n; i++) {
    const p = targetPos(i, n, cx, cy, R)
    const active = tasks.value[i]?.active
    ctx.fillStyle = active ? 'rgba(255,217,138,.9)' : 'rgba(155,224,138,.35)'
    ctx.beginPath()
    ctx.arc(p.x, p.y, 4, 0, Math.PI * 2)
    ctx.fill()
    ctx.fillStyle = 'rgba(237,230,214,.85)'
    ctx.font = '10px sans-serif'
    ctx.textAlign = 'center'
    ctx.fillText(p.label, p.x, p.y - 8)
  }
}

function tick(ts: number) {
  const c = cv.value
  if (!c) return
  const dpr = Math.min(window.devicePixelRatio || 1, 2)
  const w = c.clientWidth
  const h = 190
  if (c.width !== Math.round(w * dpr)) {
    c.width = Math.round(w * dpr)
    c.height = Math.round(h * dpr)
  }
  const ctx = c.getContext('2d')
  if (!ctx) return
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  ctx.clearRect(0, 0, w, h)
  const cx = w / 2
  const cy = h / 2
  const R = Math.min(w, h) / 2 - 12
  ctx.strokeStyle = 'rgba(155,224,138,.25)'
  ctx.lineWidth = 1.5
  ;[0.33, 0.66, 1].forEach((k) => {
    ctx.beginPath()
    ctx.arc(cx, cy, R * k, 0, Math.PI * 2)
    ctx.stroke()
  })
  ctx.beginPath()
  ctx.moveTo(cx - R, cy)
  ctx.lineTo(cx + R, cy)
  ctx.moveTo(cx, cy - R)
  ctx.lineTo(cx, cy + R)
  ctx.stroke()
  // 扫描线
  ctx.save()
  ctx.translate(cx, cy)
  ctx.rotate(angle)
  const grad = ctx.createLinearGradient(0, 0, R, 0)
  grad.addColorStop(0, 'rgba(155,224,138,0)')
  grad.addColorStop(1, 'rgba(155,224,138,.8)')
  ctx.strokeStyle = grad
  ctx.lineWidth = 3
  ctx.beginPath()
  ctx.moveTo(0, 0)
  ctx.lineTo(R, 0)
  ctx.stroke()
  ctx.restore()
  // 任务光点：扫过时高亮
  const n = tasks.value.length
  for (let i = 0; i < n; i++) {
    const p = targetPos(i, n, cx, cy, R)
    const ta = (i / Math.max(n, 1)) * Math.PI * 2 - Math.PI / 2
    let d = (angle - ta) % (Math.PI * 2)
    if (d < 0) d += Math.PI * 2
    const alpha = Math.max(0.25, 1 - d / 2.5)
    const active = tasks.value[i]?.active
    ctx.fillStyle = active ? `rgba(255,217,138,${alpha})` : `rgba(155,224,138,${alpha * 0.5})`
    ctx.beginPath()
    ctx.arc(p.x, p.y, 4, 0, Math.PI * 2)
    ctx.fill()
    ctx.fillStyle = `rgba(237,230,214,${alpha})`
    ctx.font = '10px sans-serif'
    ctx.textAlign = 'center'
    ctx.fillText(p.label, p.x, p.y - 8)
  }
  angle += lastTs > 0 ? ANGULAR_SPEED * Math.min((ts - lastTs) / 1000, 0.1) : 0
  lastTs = ts
  raf = requestAnimationFrame(tick)
}

function onVis() {
  if (document.hidden) {
    cancelAnimationFrame(raf)
    raf = 0
  } else if (!raf && !reduced) {
    lastTs = 0 // 从后台回来时丢弃旧时间戳，避免跳帧
    raf = requestAnimationFrame(tick)
  }
}

onMounted(() => {
  window.addEventListener('resize', drawStatic)
  if (reduced) {
    drawStatic()
    return
  }
  document.addEventListener('visibilitychange', onVis)
  raf = requestAnimationFrame(tick)
})

onBeforeUnmount(() => {
  cancelAnimationFrame(raf)
  document.removeEventListener('visibilitychange', onVis)
  window.removeEventListener('resize', drawStatic)
})
</script>

<style scoped>
/* 取自设计稿 .radar-box / .blips */
.v4-radar-box {
  background: var(--ink);
  border-radius: 14px;
  padding: 14px;
  position: relative;
}
.v4-radar-box h3 {
  color: #ede6d6;
  font-size: 13px;
  letter-spacing: 2px;
  margin: 0 0 6px;
}
.v4-radar-cv {
  width: 100%;
  height: 190px;
  display: block;
}
.blips {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 10px;
}
.blip {
  font-size: 11px;
  background: rgba(255, 255, 255, 0.08);
  color: #c9bfa9;
  padding: 5px 10px;
  border-radius: 12px;
  border: 1px solid rgba(255, 255, 255, 0.15);
}
.blip i {
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #9be08a;
  margin-right: 5px;
}
.blip i.idle {
  background: rgba(155, 224, 138, 0.35);
}
</style>
