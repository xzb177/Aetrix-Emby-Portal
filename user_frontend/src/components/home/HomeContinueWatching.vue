<script setup lang="ts">
export interface ResumeCard {
  key: string
  href: string
  name: string
  thumb: string
  title: string
  meta: string
  percent: number | null
}

defineProps<{
  cards: ResumeCard[]
}>()

const emit = defineEmits<{
  (e: 'thumb-error', card: ResumeCard): void
}>()
</script>

<template>
  <section v-if="cards.length" class="au-anim-up">
    <div class="program-head">
      <p class="program-eyebrow">继续放映</p>
      <div class="program-row">
        <h2 class="program-title">上次看到一半的</h2>
      </div>
    </div>
    <div class="resume-list">
      <a
        v-for="card in cards"
        :key="card.key"
        :href="card.href"
        class="resume-card"
        :title="`在 Rex 里打开：${card.name}`"
      >
        <span class="resume-thumb">
          <img
            v-if="card.thumb"
            :src="card.thumb"
            alt=""
            loading="lazy"
            decoding="async"
            @error="emit('thumb-error', card)"
          />
          <span v-else class="poster-type resume-type" aria-hidden="true">
            <span class="poster-type-title">{{ card.name }}</span>
          </span>
        </span>
        <span class="resume-main">
          <span class="resume-title">{{ card.title }}</span>
          <span v-if="card.meta" class="resume-meta">{{ card.meta }}</span>
          <span v-if="card.percent !== null" class="resume-progress">
            <span
              class="resume-bar"
              role="progressbar"
              :aria-valuenow="card.percent"
              aria-valuemin="0"
              aria-valuemax="100"
              :aria-label="`已看 ${card.percent}%`"
            >
              <span class="resume-fill" :style="{ width: card.percent + '%' }"></span>
            </span>
            <span class="resume-pct">{{ card.percent }}%</span>
          </span>
        </span>
      </a>
    </div>
  </section>
</template>

<style scoped>
.resume-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.resume-card {
  display: flex;
  gap: 12px;
  padding: 8px;
  border-radius: var(--au-r-sm);
  color: inherit;
  text-decoration: none;
  background: var(--au-surface);
  border: 1px solid var(--au-border);
  /* P3-9：悬停高亮 + 按压回弹的过渡（旧 HomeView.vue 第 1896-1916 行） */
  transition:
    border-color var(--au-fast) var(--au-ease),
    transform 120ms ease-out;
}
.resume-card:hover { border-color: var(--au-border-strong); }
.resume-card:hover .resume-thumb img { filter: brightness(1.08); }
/* 按压回弹（60fps 三档按压：卡片级用 0.98，和海报 0.97 区分层级） */
.resume-card:active { transform: scale(0.98); }
/* P3-10：缩略图按卡片 40% 自适应、最大 220px（旧第 1918-1925 行） */
.resume-thumb {
  flex: 0 0 auto;
  width: min(40%, 220px);
  aspect-ratio: 16 / 9;
  border-radius: 6px;
  overflow: hidden;
  background: var(--au-surface-2);
  position: relative;
}
.resume-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  transition: filter var(--au-fast) var(--au-ease);
}
/* P2-24：没有海报时的排版占位卡——暗色暖底（两套主题都暗，像一张片名卡）+ 衬线片名居中（旧第 1862-1880 行） */
.poster-type {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.5rem;
  width: 100%;
  height: 100%;
  padding: 0.75rem;
  background:
    radial-gradient(120% 80% at 50% 0%, rgba(232, 168, 74, 0.12), transparent 60%),
    var(--au-always-dark-surface-2);
  text-align: center;
}
.poster-type-title {
  display: -webkit-box;
  -webkit-line-clamp: 4;
  -webkit-box-orient: vertical;
  overflow: hidden;
  font-family: var(--au-font-serif);
  font-size: 0.9375rem;
  font-weight: 600;
  line-height: 1.35;
  color: var(--au-always-light);
  overflow-wrap: anywhere;
}
/* 横版占位卡：沿用海报行的排版占位，片名收到两行（旧第 1934-1938 行） */
.resume-type { padding: 0.5rem 0.625rem; }
.resume-type .poster-type-title {
  -webkit-line-clamp: 2;
  font-size: 0.875rem;
}
.resume-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 4px; }
.resume-title { font-weight: 600; font-size: 14px; }
.resume-meta { font-size: 12px; color: var(--au-text-3); }
.resume-progress { display: flex; align-items: center; gap: 8px; margin-top: auto; }
.resume-bar {
  flex: 1;
  height: 4px;
  background: var(--au-surface-2);
  border-radius: 2px;
  overflow: hidden;
}
.resume-fill { display: block; height: 100%; background: var(--au-primary); }
.resume-pct { font-size: 11px; color: var(--au-text-3); }

/* 从 HomeView 迁移（#706 抽取遗漏，home-refactor 修复）：
   排版占位卡类在 template 中使用，定义残留在 HomeView scoped（scoped 不穿透 → 样式失效）。 */
/* 没有海报：排版占位卡。暗色暖底（两套主题都暗，像一张片名卡）+ 衬线片名居中 + 年份 */
.poster-type {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.5rem;
  width: 100%;
  height: 100%;
  padding: 0.75rem;
  background:
    radial-gradient(120% 80% at 50% 0%, rgba(232, 168, 74, 0.12), transparent 60%),
    var(--au-always-dark-surface-2);
  text-align: center;
}

.poster-type-title {
  display: -webkit-box;
  -webkit-line-clamp: 4;
  -webkit-box-orient: vertical;
  overflow: hidden;
  font-family: var(--au-font-serif);
  font-size: 0.9375rem;
  font-weight: 600;
  line-height: 1.35;
  color: var(--au-always-light);
  overflow-wrap: anywhere;
}

/* 横版占位卡：沿用海报行的排版占位，片名收到两行 */
.resume-type { padding: 0.5rem 0.625rem; }
.resume-type .poster-type-title {
  -webkit-line-clamp: 2;
  font-size: 0.875rem;
}

/* 手机：剧照占卡片约 40% 宽，文字区收紧（从 HomeView 迁移） */
@media (--au-bp-sm) {
  .resume-card {
    gap: 0.75rem;
    padding: 0.5rem;
  }

  .resume-thumb { width: 40%; }

  .resume-title { font-size: 0.875rem; }

  .resume-meta { font-size: 0.75rem; }

  .resume-progress { margin-top: 0.25rem; }
}
</style>
