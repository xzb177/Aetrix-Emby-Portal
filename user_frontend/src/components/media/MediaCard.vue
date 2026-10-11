<script setup lang="ts">
import { computed, ref, watch } from 'vue'

interface Props {
  title: string
  year?: string
  poster: string
  href: string
  overview?: string
  genres?: string[]
  quality?: string
  unplayedCount?: number
  watched?: boolean
}

const props = defineProps<Props>()

const emit = defineEmits<{
  (event: 'poster-error'): void
}>()

const loaded = ref(false)
const topGenres = computed(() => (props.genres || []).slice(0, 2))

watch(
  () => props.poster,
  () => {
    loaded.value = false
  },
  { flush: 'sync' },
)
</script>

<template>
  <a
    class="media-card"
    :href="href"
    :title="title"
    tabindex="0"
  >
    <span class="media-frame">
      <span
        v-if="!loaded && poster"
        class="media-skeleton"
        aria-hidden="true"
      />

      <img
        v-if="poster"
        :src="poster"
        :alt="title"
        loading="lazy"
        decoding="async"
        :class="{ loaded }"
        @load="loaded = true"
        @error="emit('poster-error')"
      />

      <span v-else class="poster-type" aria-hidden="true">
        <span class="poster-type-title">{{ title }}</span>
        <span v-if="year" class="poster-type-year">{{ year }}</span>
      </span>

      <span v-if="quality" class="badge-quality">{{ quality }}</span>
      <span v-if="unplayedCount" class="badge-unplayed">
        {{ unplayedCount }}
      </span>
      <span v-if="watched" class="badge-watched" aria-label="已看">✓</span>

      <span class="hover-info" aria-hidden="true">
        <span v-if="overview" class="hover-overview">{{ overview }}</span>

        <span v-if="topGenres.length" class="hover-tags">
          <span v-for="g in topGenres" :key="g" class="hover-tag">
            {{ g }}
          </span>
        </span>

        <span class="hover-actions">
          <span class="hover-play">▶ 播放</span>
          <span class="hover-detail">详情</span>
        </span>
      </span>
    </span>

    <span class="media-name">{{ title }}</span>
  </a>
</template>

<style scoped>
.media-card {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  min-width: 0;
  color: inherit;
  text-decoration: none;
  scroll-snap-align: start;
  border-radius: var(--au-r-sm);
  -webkit-tap-highlight-color: transparent;
}

.media-frame {
  position: relative;
  display: block;
  flex-shrink: 0;
  box-sizing: border-box;
  width: 100%;
  aspect-ratio: 2 / 3;
  overflow: hidden;
  background: var(--au-surface-2);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-sm);
  transition:
    transform var(--au-fast) var(--au-ease),
    box-shadow var(--au-fast) var(--au-ease),
    border-color var(--au-fast) var(--au-ease);
}

.media-card:hover .media-frame,
.media-card:focus-visible .media-frame {
  transform: translateY(-6px);
  border-color: var(--au-primary-border);
  box-shadow: 0 0 24px var(--au-primary-soft);
}

.media-card:focus-visible {
  outline: 2px solid var(--au-primary);
  outline-offset: 2px;
}

.media-frame img {
  position: absolute;
  inset: 0;
  display: block;
  width: 100%;
  height: 100%;
  object-fit: cover;
  opacity: 0;
  transition: opacity 0.4s ease;
}

.media-frame img.loaded {
  opacity: 1;
}

.media-skeleton {
  position: absolute;
  inset: 0;
  background: linear-gradient(
    90deg,
    transparent,
    rgba(255, 255, 255, 0.06),
    transparent
  );
  background-size: 200% 100%;
  animation: media-shimmer 1.5s linear infinite;
  pointer-events: none;
}

@keyframes media-shimmer {
  from {
    background-position: 200% 0;
  }

  to {
    background-position: -200% 0;
  }
}

.poster-type {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.75rem;
  box-sizing: border-box;
  padding: 1.25rem;
  text-align: center;
  background:
    radial-gradient(
      ellipse at 50% 25%,
      var(--au-primary-soft),
      transparent 70%
    ),
    linear-gradient(160deg, var(--au-surface-3), var(--au-surface-2) 65%, var(--au-surface));
}

.poster-type-title {
  display: -webkit-box;
  overflow: hidden;
  max-width: 100%;
  color: var(--au-text);
  font-family:
    "Noto Serif SC", "Songti SC", "STSong", "SimSun", Georgia, serif;
  font-size: 1.25rem;
  font-weight: 600;
  line-height: 1.45;
  overflow-wrap: anywhere;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 5;
}

.poster-type-year {
  color: var(--au-text-3);
  font-size: 12px;
  line-height: 1.5;
  letter-spacing: 0.15em;
}

.badge-quality,
.badge-unplayed,
.badge-watched {
  position: absolute;
  z-index: 2;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  box-sizing: border-box;
  font-size: 10px;
  font-weight: 700;
  line-height: 1.4;
  white-space: nowrap;
  pointer-events: none;
}

.badge-quality {
  top: 8px;
  left: 8px;
  padding: 2px 8px;
  color: var(--au-warning);
  background: rgba(0, 0, 0, 0.55);
  border: 1px solid rgba(255, 255, 255, 0.5);
  border-radius: var(--au-r-full);
  -webkit-backdrop-filter: blur(4px);
  backdrop-filter: blur(4px);
}

.badge-unplayed {
  top: 8px;
  right: 8px;
  min-width: 20px;
  padding: 3px 7px;
  color: var(--au-on-primary);
  background: var(--au-primary);
  border-radius: var(--au-r-full);
}

.badge-watched {
  top: 8px;
  right: 8px;
  width: 20px;
  height: 20px;
  color: var(--au-on-primary);
  background: var(--au-success);
  border-radius: 50%;
  font-size: 12px;
}

.badge-unplayed + .badge-watched {
  top: 34px;
}

.hover-info {
  position: absolute;
  inset: 0;
  z-index: 1;
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  gap: 0.5rem;
  box-sizing: border-box;
  padding: 0.75rem;
  background: linear-gradient(
    to top,
    rgba(4, 8, 16, 0.94) 0%,
    rgba(4, 8, 16, 0.6) 45%,
    transparent 72%
  );
  opacity: 0;
  transform: translateY(8px);
  transition:
    opacity 0.28s cubic-bezier(0.22, 1, 0.36, 1),
    transform 0.28s cubic-bezier(0.22, 1, 0.36, 1);
}

.media-card:hover .hover-info,
.media-card:focus-visible .hover-info {
  opacity: 1;
  transform: translateY(0);
}

.hover-overview {
  display: -webkit-box;
  overflow: hidden;
  color: var(--au-text-2);
  font-size: 12px;
  line-height: 1.5;
  overflow-wrap: anywhere;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.hover-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  min-width: 0;
}

.hover-tag {
  display: inline-block;
  box-sizing: border-box;
  max-width: 100%;
  overflow: hidden;
  padding: 2px 8px;
  color: var(--au-text-2);
  background: var(--au-on-image);
  border-radius: var(--au-r-full);
  font-size: 10px;
  line-height: 1.4;
  text-overflow: ellipsis;
  white-space: nowrap;
  -webkit-backdrop-filter: blur(4px);
  backdrop-filter: blur(4px);
}

.hover-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}

.hover-play,
.hover-detail {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  box-sizing: border-box;
  padding: 6px 12px;
  border-radius: var(--au-r-full);
  font-size: 12px;
  font-weight: 700;
  line-height: 1.4;
  white-space: nowrap;
  cursor: pointer;
}

.hover-play {
  color: var(--au-on-primary);
  background: var(--au-primary);
}

.hover-detail {
  color: var(--au-text);
  background: transparent;
  border: 1px solid rgba(255, 255, 255, 0.4);
}

.media-name {
  display: block;
  min-width: 0;
  overflow: hidden;
  color: var(--au-text-2);
  font-size: 13px;
  line-height: 1.5;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.media-card:hover .media-name,
.media-card:focus-visible .media-name {
  color: var(--au-text);
}

@media (hover: none) {
  .hover-info {
    display: none;
  }

  .media-card:active .media-frame {
    transform: scale(0.97);
    transition: none;
  }
}

@media (prefers-reduced-motion: reduce) {
  .media-card,
  .media-card *,
  .media-card::before,
  .media-card::after,
  .media-card *::before,
  .media-card *::after {
    transition: none;
    animation: none;
  }

  .hover-info,
  .media-card:hover .hover-info,
  .media-card:focus-visible .hover-info {
    opacity: 1;
    transform: none;
  }
}
</style>