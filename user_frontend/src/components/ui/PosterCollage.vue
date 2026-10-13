<script setup lang="ts">
/**
 * 登录页海报拼贴动态背景。
 *
 * - 桌面 3 列 / 移动 2 列，左右分布，中间留空给表单
 * - 每列纵向缓慢无缝滚动（内容复制一份，translateY(-50%) 循环），相邻列反向
 * - 纯 CSS 动画，只动 transform；prefers-reduced-motion 时关闭
 * - aria-hidden + pointer-events: none，不拦截表单交互
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import type { LoginPoster } from '@/api/site'

const props = defineProps<{
  posters: LoginPoster[]
  /** EA 海报直链基地址（后端 /api/site/login-posters 下发，不在前端硬编码） */
  embyBaseUrl: string
}>()

/** 断点 < 768 为移动：窗口缩放/旋转时更新，海报按新列数重分列 */
const isMobile = ref(
  typeof window !== 'undefined' ? window.innerWidth < 768 : false,
)
function onResize() {
  isMobile.value = window.innerWidth < 768
}
onMounted(() => {
  onResize()
  window.addEventListener('resize', onResize)
})
onUnmounted(() => {
  window.removeEventListener('resize', onResize)
})

/** 列数：桌面 3，移动 2 */
const columnCount = computed(() => (isMobile.value ? 2 : 3))

/** 按 i % 列数 分列 */
const columns = computed((): LoginPoster[][] => {
  const cols: LoginPoster[][] = []
  for (let i = 0; i < columnCount.value; i++) cols.push([])
  props.posters.forEach((p, i) => {
    cols[i % columnCount.value].push(p)
  })
  return cols
})

/** 每列时长错开，避免机械同步 */
const durations = ['60s', '85s', '70s']

function posterSrc(id: string): string {
  // 直连 EA 公开图片端点（基地址由后端下发；登录页未登录时 embyBaseRef 为空，不能用 imageUrl()）
  return `${props.embyBaseUrl}/emby/Items/${encodeURIComponent(id)}/Images/Primary?maxWidth=320`
}

/** 海报加载失败（404/裂图）时隐藏该图：两轮复制用同一 URL，要么都成功要么都隐藏，无缝滚动对称性不受影响 */
function hideBrokenPoster(e: Event): void {
  const t = e.target as HTMLImageElement | null
  if (t) t.style.display = 'none'
}
</script>

<template>
  <div class="poster-collage" aria-hidden="true">
    <div
      v-for="(col, ci) in columns"
      :key="ci"
      class="collage-col"
      :class="{ reverse: ci % 2 === 1 }"
      :style="{ animationDuration: durations[ci % durations.length] }"
    >
      <template v-for="round in 2" :key="round">
        <img
          v-for="p in col"
          :key="`${round}-${p.id}`"
          class="collage-poster"
          :src="posterSrc(p.id)"
          :alt="''"
          loading="lazy"
          decoding="async"
          draggable="false"
          @error="hideBrokenPoster"
        />
      </template>
    </div>
    <div class="collage-mask"></div>
  </div>
</template>

<style scoped>
.poster-collage {
  position: absolute;
  inset: 0;
  z-index: 0;
  overflow: hidden;
  pointer-events: none;
  display: flex;
  justify-content: space-between;
  padding: 0 clamp(16px, 4vw, 64px);
  gap: 14px;
}

.collage-col {
  display: flex;
  flex-direction: column;
  gap: 14px;
  width: 160px;
  flex-shrink: 0;
  animation-name: collage-drift;
  animation-timing-function: linear;
  animation-iteration-count: infinite;
  will-change: transform;
}

.collage-col.reverse {
  animation-direction: reverse;
}

@keyframes collage-drift {
  to {
    transform: translateY(-50%);
  }
}

.collage-poster {
  width: 160px;
  aspect-ratio: 2 / 3;
  object-fit: cover;
  border-radius: 10px;
  flex-shrink: 0;
  filter: brightness(0.65) saturate(0.85);
  user-select: none;
}

.collage-mask {
  position: absolute;
  inset: 0;
  pointer-events: none;
  background:
    radial-gradient(ellipse 75% 65% at 50% 45%, var(--au-bg) 45%, transparent 100%),
    var(--au-bg);
  opacity: 0.6;
}

@media (max-width: 767px) {
  .poster-collage {
    padding: 0 12px;
    gap: 10px;
    opacity: 0.7;
  }

  .collage-col {
    width: 110px;
    gap: 10px;
    animation-duration: 100s !important;
  }

  .collage-poster {
    width: 110px;
    border-radius: 8px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .collage-col {
    animation: none;
  }
}
</style>
