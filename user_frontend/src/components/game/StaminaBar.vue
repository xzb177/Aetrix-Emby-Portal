<template>
  <!-- 探险体力条：仅公益服用户可见（vitality !== null），v4 纸色风格 -->
  <div v-if="visible" class="stamina-bar" :class="{ 'is-low': isLow, 'is-empty': isEmpty }" role="status" :aria-label="`${text.title} ${vitality}${text.unit}`">
    <span class="v4-stamp stamina-stamp">{{ text.title }}</span>
    <div class="stamina-track">
      <div class="stamina-fill" :style="{ transform: `scaleX(${pct / 100})` }"></div>
      <div class="stamina-hearts" aria-hidden="true">
        <span v-for="i in maxHearts" :key="i" class="heart" :class="{ lit: i <= litHearts }">❤️</span>
      </div>
    </div>
    <div class="stamina-meta">
      <b>{{ vitality }}</b><span class="unit">/{{ max }}{{ text.unit }}</span>
      <span v-if="isEmpty" class="stamina-warn">{{ text.empty }} · {{ text.emptyHint }}</span>
      <span v-else-if="isLow" class="stamina-warn">{{ text.low }}</span>
      <span v-else class="stamina-ok">{{ text.full }}</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, onMounted } from 'vue'
import { vitalityApi, type VitalityStatus } from '@/api/economy'
import { staminaText, STAMINA_LOW_RATIO } from '@/config/stamina'
import { useUserStore } from '@/stores/user'

const text = staminaText
const userStore = useUserStore()

const vitality = ref<number | null>(null)
const max = ref(0)

const visible = computed(() => userStore.isLoggedIn && vitality.value !== null)
const pct = computed(() => (max.value > 0 && vitality.value !== null ? Math.min(100, Math.round((vitality.value / max.value) * 100)) : 0))
const maxHearts = computed(() => Math.min(10, Math.max(1, max.value || 1)))
/** 点亮的心数按体力比例折算（max>10 时心数封顶 10，直接按 vitality 点亮会全亮失真） */
const litHearts = computed(() => (max.value > 0 && vitality.value !== null ? Math.round((vitality.value / max.value) * maxHearts.value) : 0))
const isEmpty = computed(() => vitality.value === 0)
const isLow = computed(() => !isEmpty.value && vitality.value !== null && max.value > 0 && vitality.value / max.value < STAMINA_LOW_RATIO)

async function load() {
  if (!userStore.isLoggedIn) {
    vitality.value = null
    return
  }
  try {
    const st: VitalityStatus = await vitalityApi.status()
    vitality.value = st.vitality
    const m = Number(st.max)
    max.value = Number.isFinite(m) && m > 0 ? m : 0
  } catch (err) {
    // R41：catch 不再静默吞错 —— warn 留痕方便排查；行为不变（仍置 null 隐藏整条）
    console.warn('[stamina] 加载失败', err)
    // 非公益服用户（403）或拿不到就不显示，不打扰
    vitality.value = null
  }
}

/** 供父组件在报到成功后调用，刷新体力并返回最新值 */
async function refresh(): Promise<number | null> {
  await load()
  return vitality.value
}

/** 父组件判断用：体力条当前是否可见（决定外层 section 要不要渲染，不留幽灵间隙） */
function isShown(): boolean {
  return visible.value
}

/** 父组件判断用：当前体力数值（报到前后对比，只在数值真涨了才提示"体力已恢复"） */
function current(): number | null {
  return vitality.value
}

defineExpose({ refresh, load, isShown, current })

onMounted(load)
</script>

<style scoped>
.stamina-bar {
  /* R41：配色变量定义已收敛到 styles/v4-paper.css（令牌契约要求定义出现在被引入的样式表里），
     这里只引用变量；v4 永远浅色，值与原来一字不差，视觉零变化 */
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  margin-bottom: 12px;
  background: var(--paper, #f7f1e3);
  border: 1px solid var(--line, #e2d6b8);
  border-radius: var(--paper-r-md, 10px);
  box-shadow: 0 1px 0 rgba(0, 0, 0, 0.04);
}
.stamina-stamp {
  flex: none;
}
.stamina-track {
  position: relative;
  flex: 1;
  height: 22px;
  background: rgba(0, 0, 0, 0.06);
  border-radius: 11px;
  overflow: hidden;
}
.stamina-fill {
  position: absolute;
  inset: 0 auto 0 0;
  width: 100%;
  /* 用 transform 做填充动画：width 是 layout 属性会触发重排，掉帧；
     scaleX 只走合成层，60fps。JS 里按比例设 scaleX(0~1) 代替 width。 */
  transform-origin: left center;
  background: linear-gradient(90deg, var(--stb-fill-from), var(--stb-fill-to));
  border-radius: 11px;
  transition: transform 0.4s ease;
}
@media (prefers-reduced-motion: reduce) {
  .stamina-fill {
    transition: none;
  }
}
.stamina-bar.is-low .stamina-fill {
  background: linear-gradient(90deg, var(--stb-fill-low-from), var(--stb-fill-low-to));
}
.stamina-bar.is-empty .stamina-fill {
  background: var(--stb-fill-empty);
}
.stamina-hearts {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: space-around;
  padding: 0 6px;
  font-size: 13px;
  pointer-events: none;
}
.heart {
  filter: grayscale(1);
  opacity: 0.35;
}
.heart.lit {
  filter: none;
  opacity: 1;
}
.stamina-meta {
  flex: none;
  font-size: 13px;
  display: flex;
  align-items: baseline;
  gap: 4px;
}
.stamina-meta .unit {
  color: var(--stb-unit);
}
.stamina-warn {
  color: var(--stb-warn);
  font-weight: 600;
}
.stamina-ok {
  color: var(--stb-ok);
}
.stamina-bar.is-empty {
  border-color: var(--stb-empty-border);
  background: var(--stb-empty-bg);
}
</style>
