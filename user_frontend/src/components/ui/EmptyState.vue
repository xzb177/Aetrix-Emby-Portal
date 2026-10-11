<template>
  <div
    :class="['empty-state', `empty-state--${variant}`, `empty-state--${resolvedSize}`]"
    :role="announce ? 'status' : undefined"
    :aria-live="announce ? 'polite' : undefined"
  >
    <div class="empty-state__illustration">
      <slot name="illustration">
        <!-- 默认插图 -->
        <svg
          v-if="variant === 'default'"
          viewBox="0 0 200 200"
          fill="none"
          xmlns="http://www.w3.org/2000/svg"
          class="empty-state__svg"
        >
          <circle cx="100" cy="100" r="80" fill="currentColor" fill-opacity="0.05"/>
          <path
            d="M100 60C94.4772 60 90 64.4772 90 70V85H75C69.4772 85 65 89.4772 65 95V130C65 135.523 69.4772 140 75 140H125C130.523 140 135 135.523 135 130V95C135 89.4772 130.523 85 125 85H110V70C110 64.4772 105.523 60 100 60ZM100 70C101.105 70 102 70.8946 102 72V85H98V72C98 70.8946 98.8946 70 100 70ZM75 95H125V130H75V95Z"
            fill="currentColor"
            fill-opacity="0.3"
          />
        </svg>

        <!-- 搜索空状态 -->
        <svg
          v-else-if="variant === 'search'"
          viewBox="0 0 200 200"
          fill="none"
          xmlns="http://www.w3.org/2000/svg"
          class="empty-state__svg"
        >
          <circle cx="80" cy="80" r="40" fill="currentColor" fill-opacity="0.05"/>
          <path
            d="M135 125L110 100M95 95C95 109.357 83.3574 121 69 121C54.6426 121 43 109.357 43 95C43 80.6426 54.6426 69 69 69C83.3574 69 95 80.6426 95 95Z"
            stroke="currentColor"
            stroke-width="4"
            stroke-linecap="round"
            stroke-opacity="0.3"
          />
        </svg>

        <!-- 错误空状态 -->
        <svg
          v-else-if="variant === 'error'"
          viewBox="0 0 200 200"
          fill="none"
          xmlns="http://www.w3.org/2000/svg"
          class="empty-state__svg"
        >
          <circle cx="100" cy="100" r="70" fill="currentColor" fill-opacity="0.05"/>
          <path
            d="M100 65C98.3431 65 97 66.3431 97 68V97H68C66.3431 97 65 98.3431 65 100C65 101.657 66.3431 103 68 103H97V132C97 133.657 98.3431 135 100 135C101.657 135 103 133.657 103 132V103H132C133.657 103 135 101.657 135 100C135 98.3431 133.657 97 132 97H103V68C103 66.3431 101.657 65 100 65Z"
            fill="currentColor"
            fill-opacity="0.3"
          />
        </svg>

        <!-- 网络错误 -->
        <svg
          v-else-if="variant === 'network'"
          viewBox="0 0 200 200"
          fill="none"
          xmlns="http://www.w3.org/2000/svg"
          class="empty-state__svg"
        >
          <circle cx="100" cy="100" r="70" fill="currentColor" fill-opacity="0.05"/>
          <path
            d="M100 60C93.5 60 88.5 65 88.5 71.5V88.5H71.5C65 88.5 60 93.5 60 100V128.5C60 135 65 140 71.5 140H128.5C135 140 140 135 140 128.5V100C140 93.5 135 88.5 128.5 88.5H111.5V71.5C111.5 65 106.5 60 100 60ZM100 68C101.5 68 102.5 69 102.5 70.5V88.5H97.5V70.5C97.5 69 98.5 68 100 68ZM71.5 95.5H128.5V128.5H71.5V95.5Z"
            fill="currentColor"
            fill-opacity="0.3"
          />
        </svg>

        <!-- 成功空状态 -->
        <svg
          v-else-if="variant === 'success'"
          viewBox="0 0 200 200"
          fill="none"
          xmlns="http://www.w3.org/2000/svg"
          class="empty-state__svg empty-state__svg--success"
        >
          <circle cx="100" cy="100" r="70" fill="currentColor" fill-opacity="0.1"/>
          <path
            d="M100 55C74.6 55 54 75.6 54 101C54 126.4 74.6 147 100 147C125.4 147 146 126.4 146 101C146 75.6 125.4 55 100 55ZM100 62C121.5 62 139 79.5 139 101C139 122.5 121.5 140 100 140C78.5 140 61 122.5 61 101C61 79.5 78.5 62 100 62ZM92.5 95.5L88 100L92.5 104.5L108.5 120.5L113 116L108.5 111.5L96 99L100.5 94.5L108.5 86.5L104 82L99.5 86.5L92.5 93.5V95.5Z"
            fill="currentColor"
            fill-opacity="0.5"
          />
        </svg>
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

.empty-state__illustration {
  margin-bottom: 1.25rem;
}

.empty-state--compact .empty-state__illustration {
  margin-bottom: 0.75rem;
}

.empty-state__svg {
  width: 120px;
  height: 120px;
  color: var(--au-text-3); /* SYS-1：幽灵变量 --text-quaternary 已删除，改用 Aurora */
}

.empty-state--compact .empty-state__svg {
  width: 48px;
  height: 48px;
}

.empty-state__svg--success {
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
.empty-state__illustration {
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
  .empty-state__illustration,
  .empty-state__content {
    animation: none;
  }
}
</style>