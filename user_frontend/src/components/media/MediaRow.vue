<script setup lang="ts">
interface Props {
  loading?: boolean
  skeletonCount?: number
}

withDefaults(defineProps<Props>(), {
  loading: false,
  skeletonCount: 6,
})
</script>

<template>
  <div class="media-row" :class="{ 'is-loading': loading }">
    <template v-if="loading">
      <div
        v-for="i in skeletonCount"
        :key="i"
        class="media-card-skeleton"
        aria-hidden="true"
      >
        <span class="media-skeleton-frame" />
        <span class="media-skeleton-line" style="width: 60%" />
        <span class="media-skeleton-line" style="width: 40%" />
      </div>
    </template>
    <slot v-else />
  </div>
</template>

<style scoped>
.media-row {
  display: flex;
  gap: 0.75rem;
  overflow-x: auto;
  overscroll-behavior-x: contain;
  scroll-snap-type: x proximity;
  scrollbar-width: none;
  margin: -0.75rem calc(var(--gutter, 1rem) * -1) 0;
  padding: 0.75rem var(--gutter, 1rem) 0.25rem;
  scroll-padding-inline: var(--gutter, 1rem);
}

.media-row::-webkit-scrollbar {
  display: none;
}

/* 槽位内的 MediaCard 宽度（与骨架卡保持一致） */
.media-row :deep(.media-card) {
  flex: 0 0 auto;
  width: 120px;
}

@media (min-width: 1024px) {
  /* 桌面宽屏：--au-bp-lg 的补集（>1024px），值与断点表同源 */
  .media-row :deep(.media-card) {
    width: 144px;
  }

  .media-card-skeleton {
    width: 144px;
  }
}

@media (--au-bp-md) {
  .media-row :deep(.media-card) {
    width: 104px;
  }

  .media-card-skeleton {
    width: 104px;
  }
}

.media-card-skeleton {
  display: flex;
  flex: 0 0 auto;
  flex-direction: column;
  gap: 0.5rem;
  width: 120px;
  scroll-snap-align: start;
}

.media-skeleton-frame,
.media-skeleton-line {
  display: block;
  background: linear-gradient(
    90deg,
    rgba(128, 128, 128, 0.12) 25%,
    rgba(128, 128, 128, 0.24) 50%,
    rgba(128, 128, 128, 0.12) 75%
  );
  background-size: 200% 100%;
  animation: media-row-shimmer 1.5s linear infinite;
}

.media-skeleton-frame {
  width: 100%;
  aspect-ratio: 2 / 3;
  border-radius: var(--au-r-sm);
}

.media-skeleton-line {
  height: 12px;
  border-radius: 6px;
}

@keyframes media-row-shimmer {
  from {
    background-position: 200% 0;
  }

  to {
    background-position: -200% 0;
  }
}

@media (prefers-reduced-motion: reduce) {
  .media-skeleton-frame,
  .media-skeleton-line {
    animation: none;
  }
}
</style>