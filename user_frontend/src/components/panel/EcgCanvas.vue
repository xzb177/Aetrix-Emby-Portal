<template>
  <canvas ref="cv" class="v4-ecg" aria-hidden="true"></canvas>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'

const cv = ref<HTMLCanvasElement | null>(null)
let raf = 0
let last = 0
let x = 0
const data: number[] = new Array(200).fill(64)
const STEP_MS = 90 // 与设计稿 setInterval(90ms) 同节奏

function draw(ts: number) {
  const el = cv.value
  if (!el) return
  // 页面不可见时暂停（性能纪律）
  if (document.hidden) {
    raf = requestAnimationFrame(draw)
    return
  }
  if (ts - last >= STEP_MS) {
    last = ts
    data.shift()
    // 心跳波形：每 90 拍一次大搏动（取自设计稿）
    const beat = x % 90 < 8 ? Math.random() * 50 : Math.random() * 8
    data.push(64 - beat - Math.random() * 4)
    x++
    const ctx = el.getContext('2d')
    if (ctx) {
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
        if (i === 0) ctx.moveTo(px, py)
        else ctx.lineTo(px, py)
      })
      ctx.stroke()
    }
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

onMounted(() => {
  // 减少动态偏好：不启动动画
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
  size()
  window.addEventListener('resize', size)
  raf = requestAnimationFrame(draw)
})
onUnmounted(() => {
  cancelAnimationFrame(raf)
  window.removeEventListener('resize', size)
})
</script>

<style scoped>
.v4-ecg {
  width: 100%;
  height: 64px;
  display: block;
}
</style>
