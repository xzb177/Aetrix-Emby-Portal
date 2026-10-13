<template>
  <div :class="['skeleton', `skeleton--${variant}`, { 'skeleton--pulse': pulse }]"></div>
</template>

<script setup lang="ts">
export type SkeletonVariant = 'text' | 'circle' | 'rect' | 'rounded'

export interface SkeletonProps {
  variant?: SkeletonVariant
  pulse?: boolean
}

withDefaults(defineProps<SkeletonProps>(), {
  variant: 'text',
  pulse: true
})
</script>

<style scoped>
.skeleton {
  background: linear-gradient(
    90deg,
    var(--skeleton-base) 0%,
    var(--skeleton-highlight) 50%,
    var(--skeleton-base) 100%
  );
  background-size: 200% 100%;
  display: inline-block;
}

.skeleton--pulse {
  animation: shimmer 1.5s ease-in-out infinite;
}

/* R27：尊重减少动态偏好 —— shimmer 停掉，只保留静态底色 */
@media (prefers-reduced-motion: reduce) {
  .skeleton,
  .skeleton--pulse {
    animation: none;
  }
}

.skeleton--text {
  height: 1em;
  width: 100%;
  border-radius: 4px;
}

.skeleton--circle {
  border-radius: 50%;
  aspect-ratio: 1 / 1;
}

.skeleton--rect {
  border-radius: 4px;
}

.skeleton--rounded {
  border-radius: 8px;
}

@keyframes shimmer {
  0% {
    background-position: -200% 0;
  }
  100% {
    background-position: 200% 0;
  }
}
</style>
