<template>
  <div class="v4-radar-box">
    <h3 v-if="activeCount > 0">服务器在忙 · {{ activeCount }} 个任务在线</h3>
    <!-- R39-11：没任务时说人话，不说"0 个任务在线" -->
    <h3 v-else>服务器待命 · 暂无在线任务</h3>
    <!-- R39-10：canvas 装饰图加 role="img"，读屏按图片念已有 aria-label -->
    <canvas ref="cv" class="v4-radar-cv" role="img" aria-label="任务雷达扫描图"></canvas>
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
let ctx: CanvasRenderingContext2D | null = null // 缓存：不每帧调 getContext
// R41：标签宽度缓存——只在任务列表变化时重算 measureText，不再每帧逐个测
let labelWidthKey = ''
let labelWidths: number[] = []
function cachedLabelWidths(c: CanvasRenderingContext2D, labels: string[]): number[] {
  const key = labels.join('\u0001')
  if (labelWidthKey !== key) {
    labelWidthKey = key
    labelWidths = labels.map((s) => (s ? c.measureText(s).width : 0))
  }
  return labelWidths
}
// R41：扫描线渐变只在半径变化时重建，不再每帧 createLinearGradient
let scanGrad: CanvasGradient | null = null
let scanGradR = -1
function cachedScanGrad(c: CanvasRenderingContext2D, r: number): CanvasGradient {
  if (!scanGrad || scanGradR !== r) {
    scanGrad = c.createLinearGradient(0, 0, r, 0)
    scanGrad.addColorStop(0, 'rgba(155,224,138,0)')
    scanGrad.addColorStop(1, 'rgba(155,224,138,.8)')
    scanGradR = r
  }
  return scanGrad
}
// R41：R 无下限会导致 ctx.arc 负半径抛错杀死 rAF；宽高 ≤ 0 时直接跳过本帧
function radarRadius(w: number, h: number): number {
  return Math.max(0, Math.min(w, h) / 2 - 12)
}
/** 角速度：0.045 rad/帧 × 60fps —— 高刷屏也用时间增量，转速一致 */
const ANGULAR_SPEED = 0.045 * 60
// R28-4：reduced-motion 改为响应式——页面打开期间用户改系统设置也能即时启停动画
const motionMq = window.matchMedia('(prefers-reduced-motion: reduce)')
const reducedMotion = ref(motionMq.matches)

/** 固定角度布局（确定性，不闪烁），按任务数均分 */
function targetPos(i: number, n: number, cx: number, cy: number, r: number) {
  const a = (i / Math.max(n, 1)) * Math.PI * 2 - Math.PI / 2
  const rr = r * (0.45 + 0.4 * ((i * 37) % 10) / 10)
  return { x: cx + Math.cos(a) * rr, y: cy + Math.sin(a) * rr, label: tasks.value[i]?.name || '' }
}

/**
 * R28：任务标签防重叠绘制——活跃任务优先画标签，与已绘制标签包围盒
 * 重叠的次要标签跳过（375px 下任务多时不再糊成一团）。
 * alphaFor(i) 返回该标签的透明度（静态模式传固定值）。
 */
function drawLabels(
  ctx: CanvasRenderingContext2D,
  n: number,
  cx: number,
  cy: number,
  r: number,
  alphaFor: (i: number) => number,
) {
  ctx.font = '10px sans-serif'
  ctx.textAlign = 'center'
  // R41：宽度走缓存——tasks 不变时不重复 measureText
  const widths = cachedLabelWidths(
    ctx,
    Array.from({ length: n }, (_, i) => tasks.value[i]?.name || ''),
  )
  const drawn: Array<{ x0: number; x1: number; y0: number; y1: number }> = []
  const order = Array.from({ length: n }, (_, i) => i).sort(
    (a, b) => Number(!!tasks.value[b]?.active) - Number(!!tasks.value[a]?.active),
  )
  for (const i of order) {
    const p = targetPos(i, n, cx, cy, r)
    if (!p.label) continue
    const w = widths[i]
    const box = { x0: p.x - w / 2 - 3, x1: p.x + w / 2 + 3, y0: p.y - 20, y1: p.y - 4 }
    const hit = drawn.some((b) => box.x0 < b.x1 && box.x1 > b.x0 && box.y0 < b.y1 && box.y1 > b.y0)
    if (hit) continue
    drawn.push(box)
    ctx.fillStyle = `rgba(237,230,214,${alphaFor(i)})`
    ctx.fillText(p.label, p.x, p.y - 8)
  }
}

function drawStatic() {
  const c = cv.value
  if (!c) return
  const dpr = Math.min(window.devicePixelRatio || 1, 2)
  const w = c.clientWidth
  const h = 190
  // R41：宽高为 0（如元素被隐藏）时跳过本帧，避免负半径杀死 rAF
  if (w <= 0 || h <= 0) return
  c.width = w * dpr
  c.height = h * dpr
  if (!ctx) ctx = c.getContext('2d')
  if (!ctx) return
  ctx.scale(dpr, dpr)
  const cx = w / 2
  const cy = h / 2
  const R = radarRadius(w, h)
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
  }
  drawLabels(ctx, n, cx, cy, R, () => 0.85)
}

function tick(ts: number) {
  const c = cv.value
  if (!c) return
  const dpr = Math.min(window.devicePixelRatio || 1, 2)
  const w = c.clientWidth
  const h = 190
  // R41：宽高为 0（如元素被隐藏）时跳过本帧，避免负半径杀死 rAF
  if (w <= 0 || h <= 0) {
    raf = requestAnimationFrame(tick)
    return
  }
  if (c.width !== Math.round(w * dpr)) {
    c.width = Math.round(w * dpr)
    c.height = Math.round(h * dpr)
  }
  if (!ctx) ctx = c.getContext('2d')
  if (!ctx) return
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
  ctx.clearRect(0, 0, w, h)
  const cx = w / 2
  const cy = h / 2
  const R = radarRadius(w, h)
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
  // 扫描线（R41：渐变走缓存，只在半径变化时重建）
  ctx.save()
  ctx.translate(cx, cy)
  ctx.rotate(angle)
  ctx.strokeStyle = cachedScanGrad(ctx, R)
  ctx.lineWidth = 3
  ctx.beginPath()
  ctx.moveTo(0, 0)
  ctx.lineTo(R, 0)
  ctx.stroke()
  ctx.restore()
  // 任务光点：扫过时高亮
  const n = tasks.value.length
  const alphas: number[] = new Array(n)
  for (let i = 0; i < n; i++) {
    const p = targetPos(i, n, cx, cy, R)
    const ta = (i / Math.max(n, 1)) * Math.PI * 2 - Math.PI / 2
    let d = (angle - ta) % (Math.PI * 2)
    if (d < 0) d += Math.PI * 2
    const alpha = Math.max(0.25, 1 - d / 2.5)
    alphas[i] = alpha
    const active = tasks.value[i]?.active
    ctx.fillStyle = active ? `rgba(255,217,138,${alpha})` : `rgba(155,224,138,${alpha * 0.5})`
    ctx.beginPath()
    ctx.arc(p.x, p.y, 4, 0, Math.PI * 2)
    ctx.fill()
  }
  drawLabels(ctx, n, cx, cy, R, (i) => alphas[i])
  angle += lastTs > 0 ? ANGULAR_SPEED * Math.min((ts - lastTs) / 1000, 0.1) : 0
  lastTs = ts
  raf = requestAnimationFrame(tick)
}

function onVis() {
  if (document.hidden) {
    cancelAnimationFrame(raf)
    raf = 0
  } else if (!raf && !reducedMotion.value) {
    lastTs = 0 // 从后台回来时丢弃旧时间戳，避免跳帧
    raf = requestAnimationFrame(tick)
  }
}

/** 用户在页面打开期间切换系统"减少动态"设置：即时启停雷达动画（R28-4 模式） */
function onMotionChange(e: { matches: boolean }) {
  reducedMotion.value = e.matches
  if (e.matches) {
    cancelAnimationFrame(raf)
    raf = 0
    document.removeEventListener('visibilitychange', onVis)
    drawStatic()
  } else {
    document.addEventListener('visibilitychange', onVis)
    lastTs = 0
    raf = requestAnimationFrame(tick)
  }
}

onMounted(() => {
  window.addEventListener('resize', drawStatic)
  motionMq.addEventListener('change', onMotionChange)
  // 减少动态偏好：不启动动画，但画一帧静态雷达，不留空白
  if (reducedMotion.value) {
    drawStatic()
    return
  }
  document.addEventListener('visibilitychange', onVis)
  raf = requestAnimationFrame(tick)
})

onBeforeUnmount(() => {
  cancelAnimationFrame(raf)
  motionMq.removeEventListener('change', onMotionChange)
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
  color: var(--line-deep, #c9bfa9);
  padding: 5px 10px;
  border-radius: var(--paper-r-lg, 12px);
  border: 1px solid rgba(255, 255, 255, 0.15);
}
.blip i {
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: var(--paper-r-full, 50%);
  background: var(--pulse-green, #9be08a);
  margin-right: 5px;
}
.blip i.idle {
  background: rgba(155, 224, 138, 0.35);
}
</style>
