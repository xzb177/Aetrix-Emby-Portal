<template>
  <div class="v4-sky">
    <h3>CONSTELLATION · {{ unlockedCount }} 颗已点亮</h3>
    <svg ref="svgEl" viewBox="0 0 335 230" role="img" aria-label="成就星图"></svg>
    <div v-if="tipVisible" class="v4-star-tip" v-html="tipHtml"></div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import type { Achievement } from '@/composables/usePanelApi'

const props = defineProps<{
  achievements: Achievement[]
}>()

const svgEl = ref<SVGSVGElement | null>(null)
const tipVisible = ref(false)
const tipHtml = ref('')
let tipTimer: ReturnType<typeof setTimeout> | null = null

const NS = 'http://www.w3.org/2000/svg'
const W = 335
const H = 230

/** 确定性伪随机（固定种子，布局稳定不闪烁） */
function mulberry32(seed: number) {
  return function () {
    seed |= 0
    seed = (seed + 0x6d2b79f5) | 0
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

interface StarNode extends Achievement {
  x: number
  y: number
}

const nodes = computed<StarNode[]>(() => {
  const rnd = mulberry32(20261013)
  const placed: Array<{ x: number; y: number }> = []
  return props.achievements.map((a) => {
    // 泊松式散点：拒绝太近的点，保证星星不重叠
    let x = 30
    let y = 30
    for (let tries = 0; tries < 60; tries++) {
      x = 28 + rnd() * (W - 56)
      y = 24 + rnd() * (H - 48)
      if (placed.every((p) => Math.hypot(p.x - x, p.y - y) > 52)) break
    }
    placed.push({ x, y })
    return { ...a, x, y }
  })
})

const unlockedCount = computed(() => nodes.value.filter((n) => n.unlocked).length)

function showTip(html: string) {
  tipHtml.value = html
  tipVisible.value = true
  if (tipTimer) clearTimeout(tipTimer)
  tipTimer = setTimeout(() => {
    tipVisible.value = false
  }, 2600)
}

/** escape：成就名/描述来自后端 seed，不信任直接插 HTML */
function esc(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c] as string))
}

/** 星星点击/回车：已解锁看成就名，未解锁看提示 */
function onStar(n: StarNode) {
  if (n.unlocked) {
    showTip(`✨ <b>${esc(n.name)}</b> · ${esc(n.desc || '')}`)
  } else {
    showTip('🌫️ <b>???</b> · 还没发生的事')
  }
}

function render() {
  const svg = svgEl.value
  if (!svg) return
  svg.replaceChildren()
  const list = nodes.value
  // 连线：只连连续的已解锁星（取自设计稿逻辑）
  for (let i = 1; i < list.length; i++) {
    const a = list[i - 1]
    const b = list[i]
    if (!a.unlocked || !b.unlocked) continue
    const l = document.createElementNS(NS, 'line')
    l.setAttribute('x1', String(a.x))
    l.setAttribute('y1', String(a.y))
    l.setAttribute('x2', String(b.x))
    l.setAttribute('y2', String(b.y))
    l.setAttribute('stroke', 'rgba(255,217,138,.25)')
    l.setAttribute('stroke-width', '1')
    svg.appendChild(l)
  }
  // 星星
  list.forEach((n) => {
    const c = document.createElementNS(NS, 'circle')
    c.setAttribute('cx', String(n.x))
    c.setAttribute('cy', String(n.y))
    c.setAttribute('r', n.unlocked ? '6' : '4')
    c.setAttribute('fill', n.unlocked ? '#FFD98A' : '#4a463d')
    c.setAttribute('tabindex', '0')
    c.style.cursor = 'pointer'
    if (n.unlocked) c.style.filter = 'drop-shadow(0 0 6px #FFD98A)'
    c.addEventListener('click', (e) => {
      e.stopPropagation()
      onStar(n)
    })
    c.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault()
        onStar(n)
      }
    })
    const t = document.createElementNS(NS, 'title')
    t.textContent = n.unlocked ? n.name : '???'
    c.appendChild(t)
    svg.appendChild(c)
  })
}

onMounted(render)
watch(nodes, render)
onUnmounted(() => {
  if (tipTimer) clearTimeout(tipTimer)
})
</script>

<style scoped>
/* 取自设计稿 .sky / #stars / .star-tip */
.v4-sky {
  background: var(--ink);
  border-radius: 14px;
  padding: 8px 8px 4px;
  position: relative;
}
.v4-sky h3 {
  color: #ede6d6;
  font-size: 13px;
  letter-spacing: 2px;
  padding: 10px 10px 0;
  margin: 0;
}
.v4-sky svg {
  width: 100%;
  height: 230px;
  display: block;
}
.v4-star-tip {
  position: absolute;
  left: 50%;
  transform: translateX(-50%);
  bottom: 12px;
  max-width: 88%;
  white-space: normal;
  text-align: center;
  background: #fffdf6;
  border-radius: 10px;
  padding: 8px 14px;
  font-size: 12px;
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.3);
  animation: v4-fadein 0.25s ease;
}
.v4-star-tip :deep(b) {
  color: var(--red);
}
@keyframes v4-fadein {
  from {
    opacity: 0;
    transform: translate(-50%, 6px);
  }
  to {
    opacity: 1;
    transform: translate(-50%, 0);
  }
}
</style>
