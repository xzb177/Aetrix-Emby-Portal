<template>
  <canvas ref="cv" class="v4-ecg" aria-hidden="true"></canvas>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'

const cv = ref<HTMLCanvasElement | null>(null)
let raf = 0
let last = 0
let x = 0
let ctx: CanvasRenderingContext2D | null = null // 缓存：不每帧调 getContext
const data: number[] = new Array(200).fill(64)
const STEP_MS = 90 // 与设计稿 setInterval(90ms) 同节奏
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches

/** 静态心跳波形：一段基线 + 周期性搏动（减少动态偏好时画一帧，不留白） */
function fillStaticWave() {
  for (let i = 0; i < data.length; i++) {
    const ph = i % 90
    const beat = ph < 8 ? 40 * Math.sin((ph / 8) * Math.PI) : Math.sin(i / 6) * 3
    data[i] = 64 - beat - 2
  }
}

function paint() {
  const el = cv.value
  if (!el) return
  if (!ctx) ctx = el.getContext('2d')
  if (!ctx) return
  const w = el.width
  const h = el.height
  ctx.clearRect(0, 0, w, h)
  ctx.strokeStyle = '#9BE08A'
  ctx.lineWidth = 3 * (w / (el.clientWidth || w)) // 按 DPR 换算回 CSS 像素观感
  ctx.lineJoin = 'round'
  ctx.beginPath()
  data.forEach((v, i) => {
    const px = (i / (data.length - 1)) * w
    const py = (v / 64) * h
    if (i === 0) ctx!.moveTo(px, py)
    else ctx!.lineTo(px, py)
  })
  ctx.stroke()
}

function draw(ts: number) {
  if (ts - last >= STEP_MS) {
    last = ts
    data.shift()
    // 心跳波形：每 90 拍一次大搏动（取自设计稿）
    const beat = x % 90 < 8 ? Math.random() * 50 : Math.random() * 8
    data.push(64 - beat - Math.random() * 4)
    x++
    paint()
  }
  raf = requestAnimationFrame(draw)
}

function size() {
  const el = cv.value
  if (!el) return
  // 设备像素比 ×2（方案 §2.2）
  const dpr = Math.min(window.devicePixelRatio || 1, 2)
  const rect = el.getBoundingClientRect()
  el.width = Math.max(1, Math.round(rect.width * dpr))
  el.height = Math.max(1, Math.round(rect.height * dpr))
}

function onResize() {
  size()
  if (reducedMotion) paint() // 静态模式 resize 后重画一帧
}

function onVis() {
  if (document.hidden) {
    cancelAnimationFrame(raf)
    raf = 0
  } else if (!raf && !reducedMotion) {
    last = 0 // 从后台回来时丢弃旧时间戳，避免跳帧
    raf = requestAnimationFrame(draw)
  }
}

onMounted(() => {
  size()
  window.addEventListener('resize', onResize)
  // 减少动态偏好：不启动动画，但画一帧静态波形，不留空白
  if (reducedMotion) {
    fillStaticWave()
    paint()
    return
  }
  document.addEventListener('visibilitychange', onVis)
  raf = requestAnimationFrame(draw)
})
onUnmounted(() => {
  cancelAnimationFrame(raf)
  document.removeEventListener('visibilitychange', onVis)
  window.removeEventListener('resize', onResize)
})
</script>

<style scoped>
.v4-ecg {
  width: 100%;
  height: 64px;
  display: block;
}
</style>
