<template>
  <div
    :class="['empty-state', `empty-state--${variant}`, `empty-state--${resolvedSize}`]"
    :role="announce ? 'status' : undefined"
    :aria-live="announce ? 'polite' : undefined"
  >
    <div class="empty-state__icon" aria-hidden="true">
      <slot name="illustration">
        <component :is="variantIcon" :size="iconSize" :stroke-width="1.75" />
      </slot>
    </div>

    <div class="empty-state__content">
      <h3 v-if="title || $slots.title" class="empty-state__title">
        <slot name="title">{{ title }}</slot>
      </h3>

      <p v-if="description || $slots.description" class="empty-state__description">
        <slot name="description">{{ description }}</slot>
      </p>

      <div v-if="$slots.action" class="empty-state__action">
        <slot name="action"></slot>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { CircleAlert, CircleCheck, Inbox, Search, WifiOff } from 'lucide-vue-next'
import type { Component } from 'vue'

export interface EmptyStateProps {
  variant?: 'default' | 'search' | 'error' | 'network' | 'success'
  title?: string
  description?: string
  /** @deprecated 用 size="compact" 代替 */
  compact?: boolean
  size?: 'regular' | 'compact'
  /** 为 true 时以 role="status" + aria-live="polite" 播报 */
  announce?: boolean
}

const props = withDefaults(defineProps<EmptyStateProps>(), {
  variant: 'default',
  compact: false,
  size: undefined,
  announce: false
})

// size 优先；未传 size 时用 legacy compact 兜底
const resolvedSize = computed<'regular' | 'compact'>(() =>
  props.size ?? (props.compact ? 'compact' : 'regular')
)

const variantIcon = computed<Component>(() => {
  switch (props.variant) {
    case 'search': return Search
    case 'error': return CircleAlert
    case 'network': return WifiOff
    case 'success': return CircleCheck
    default: return Inbox
  }
})

const iconSize = computed(() => (resolvedSize.value === 'compact' ? 18 : 28))
</script>

<style scoped>
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 3rem 1.5rem;
  text-align: center;
  color: var(--text-tertiary);
}

.empty-state--compact {
  padding: 1.5rem 1rem;
}

.empty-state__icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 0.75rem;
  color: var(--au-text-3);
}

.empty-state--compact .empty-state__icon {
  margin-bottom: 0.5rem;
}

.empty-state--success .empty-state__icon {
  color: var(--brand-primary);
}

.empty-state__content {
  max-width: 320px;
}

.empty-state__title {
  font-size: 1.125rem;
  font-weight: 600;
  color: var(--text-secondary);
  margin: 0 0 0.5rem 0;
}

.empty-state__description {
  font-size: 0.875rem;
  line-height: 1.5;
  color: var(--text-tertiary);
  margin: 0 0 1.5rem 0;
}

.empty-state__action {
  display: flex;
  gap: 0.75rem;
  justify-content: center;
  flex-wrap: wrap;
}

/* 入场动画：只 opacity + translateY，短促不挡交互 */
.empty-state__icon {
  animation: empty-fade-in 0.3s ease-out;
}

.empty-state__content {
  animation: empty-fade-up 0.3s ease-out 0.05s both;
}

@keyframes empty-fade-in {
  from { opacity: 0; }
  to { opacity: 1; }
}

@keyframes empty-fade-up {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: translateY(0); }
}

/* reduced-motion：直接呈现，无位移无渐变 */
@media (prefers-reduced-motion: reduce) {
  .empty-state__icon,
  .empty-state__content {
    animation: none;
  }
}
</style>