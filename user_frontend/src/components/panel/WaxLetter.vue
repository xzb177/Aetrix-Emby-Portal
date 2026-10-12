<template>
  <div
    class="v4-letter"
    :class="{ open: isOpen }"
    role="button"
    tabindex="0"
    :aria-label="isOpen ? '昨日回顾信件，点击合上' : '每日一封，点击拆信'"
    @click="toggle"
    @keydown="onKey"
  >
    <div v-if="!isOpen" class="v4-letter-env">
      <div class="v4-letter-wax">封</div>
      <p>来自昨日 {{ letterTime }} 的你<br /><span class="v4-letter-hint">点一下，拆信</span></p>
    </div>
    <div v-else class="v4-letter-content">
      <div class="v4-kv"><span>昨日观影</span><b>{{ letter.watch_duration || '—' }}</b></div>
      <div class="v4-kv"><span>看完</span><b>{{ watchedText }}</b></div>
      <div class="v4-kv" v-if="letter.taste_shift"><span>口味偏移</span><span>{{ letter.taste_shift }}</span></div>
      <div class="v4-kv"><span>今日推荐位</span><span>已为你留好 1 个</span></div>
      <p v-if="letter.teaser" class="v4-letter-ps">P.S. {{ letter.teaser }}</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

const props = withDefaults(defineProps<{ letter?: Record<string, unknown> }>(), {
  letter: () => ({}),
})

const isOpen = ref(false)

/** 拆开/合上切换 */
function toggle() {
  isOpen.value = !isOpen.value
}
function onKey(e: KeyboardEvent) {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault()
    toggle()
  }
}

/** 信封上的时间：昨日日期 */
const letterTime = computed(() => {
  const raw = props.letter.date as string | undefined
  if (raw) {
    const d = new Date(raw)
    if (!Number.isNaN(d.getTime())) return `${d.getMonth() + 1}.${d.getDate()}`
  }
  const y = new Date()
  y.setDate(y.getDate() - 1)
  return `${y.getMonth() + 1}.${y.getDate()}`
})

/** 看完的条目：数组取前 3，否则按数量兜底 */
const watchedText = computed(() => {
  const w = props.letter.watched as string[] | undefined
  if (Array.isArray(w) && w.length) {
    const shown = w.slice(0, 3).join('、')
    return w.length > 3 ? `${shown} 等` : shown
  }
  const n = props.letter.watch_count as number | undefined
  if (typeof n === 'number' && n > 0) return `${n} 部作品`
  return '还没看，今晚补上'
})
</script>

<style scoped>
/* 取自设计稿 home.html .letter */
.v4-letter {
  background: #FFFDF6;
  border: 1.5px solid var(--line);
  border-radius: 10px;
  overflow: hidden;
  cursor: pointer;
}
.v4-letter-env {
  padding: 18px;
  text-align: center;
  transition: opacity 0.3s;
}
.v4-letter-wax {
  width: 52px;
  height: 52px;
  border-radius: 50%;
  background: var(--red);
  color: #fff;
  margin: 0 auto 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 22px;
  font-weight: 800;
  box-shadow: 0 3px 0 #a33319;
  font-family: Georgia, serif;
}
.v4-letter-env p { font-size: 13px; color: var(--ink2); margin: 0; }
.v4-letter-hint { font-size: 11px; }
.v4-letter-content {
  padding: 18px;
  font-size: 13px;
  line-height: 2;
  animation: v4-fadein 0.4s ease;
}
.v4-letter-content b { color: var(--red); }
.v4-letter-ps {
  margin: 10px 0 0;
  color: var(--ink2);
  font-size: 12px;
}
.v4-kv {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  border-bottom: 1px dotted var(--line);
  padding: 6px 0;
}
.v4-kv span:first-child { color: var(--ink2); flex-shrink: 0; }
.v4-kv b, .v4-kv span:last-child { text-align: right; }
@keyframes v4-fadein {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: none; }
}
</style>
