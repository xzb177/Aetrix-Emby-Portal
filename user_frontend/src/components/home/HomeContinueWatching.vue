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
  <section v-if="cards.length" class="resume au-anim-up">
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
.resume {
  content-visibility: auto;
  contain-intrinsic-size: auto 140px;
}
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
}
.resume-thumb {
  flex: 0 0 96px;
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
</style>
