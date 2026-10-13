<script setup lang="ts">
import { onMounted } from 'vue'
import { Check, X, Info, TriangleAlert, type LucideIcon } from 'lucide-vue-next'

export interface ToastMessage {
  id: number
  type: 'success' | 'error' | 'warning' | 'info'
  message: string
  duration?: number
}

const props = defineProps<{
  messages: ToastMessage[]
}>()

const emit = defineEmits<{
  (e: 'remove', id: number): void
}>()

// 自动移除消息
onMounted(() => {
  props.messages.forEach(msg => {
    const duration = msg.duration ?? 3000
    if (duration > 0) {
      setTimeout(() => {
        emit('remove', msg.id)
      }, duration)
    }
  })
})

// 状态图标：lucide（与全站统一，不再用 Unicode 字符）
const iconMap: Record<ToastMessage['type'], LucideIcon> = {
  success: Check,
  error: X,
  warning: TriangleAlert,
  info: Info,
}

const getIcon = (type: ToastMessage['type']): LucideIcon => iconMap[type] || Info

// 获取样式类
const getToastClass = (type: string) => {
  const classes = {
    success: 'toast-success',
    error: 'toast-error',
    warning: 'toast-warning',
    info: 'toast-info'
  }
  return classes[type as keyof typeof classes] || classes.info
}
</script>

<template>
  <Teleport to="body">
    <div class="toast-container" role="status" aria-live="polite">
      <TransitionGroup name="toast">
        <div
          v-for="msg in messages"
          :key="msg.id"
          class="toast"
          :class="getToastClass(msg.type)"
        >
          <span class="toast-icon" aria-hidden="true">
            <component :is="getIcon(msg.type)" :size="12" :stroke-width="2.75" />
          </span>
          <span class="toast-message">{{ msg.message }}</span>
          <button
            type="button"
            class="toast-close"
            aria-label="关闭提示"
            @click="emit('remove', msg.id)"
          >
            <X :size="15" />
          </button>
        </div>
      </TransitionGroup>
    </div>
  </Teleport>
</template>

<style scoped>
.toast-container {
  position: fixed;
  /* 贴在吸顶导航下方，并让开刘海区 */
  top: calc(74px + env(safe-area-inset-top, 0px));
  left: 0;
  right: 0;
  margin: 0 auto;
  width: max-content;
  max-width: calc(100vw - 32px);
  z-index: var(--z-tooltip, 700);
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  pointer-events: none;
}

.toast {
  pointer-events: auto;
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0.875rem 1rem;
  min-width: 280px;
  max-width: 90vw;
  /* 暗房影院：实色表面 + 强边框 + 克制阴影，不再有毛玻璃 */
  background: var(--au-surface-3);
  border: 1px solid var(--au-border-strong);
  border-radius: var(--au-r-md);
  box-shadow: var(--au-shadow-2);
}

.toast-icon {
  width: 20px;
  height: 20px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  flex-shrink: 0;
}

.toast-message {
  flex: 1;
  font-size: 0.875rem;
  line-height: 1.5;
  color: var(--au-text);
  word-break: break-word;
}

.toast-close {
  /* 触摸区 44px：负 margin 抵消放大，视觉占位与原来 24px 一致 */
  width: 44px;
  height: 44px;
  margin: -10px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: none;
  background: transparent;
  color: var(--au-text-3);
  border-radius: var(--au-r-sm);
  cursor: pointer;
  transition: background-color var(--au-fast) var(--au-ease),
              color var(--au-fast) var(--au-ease),
              transform var(--au-fast) var(--au-ease);
  flex-shrink: 0;
}

.toast-close:hover {
  background: var(--au-surface-2);
  color: var(--au-text);
}

.toast-close:active {
  transform: scale(0.9);
}

.toast-close:focus-visible {
  outline: 2px solid var(--au-primary);
  outline-offset: 1px;
}

/* 四种状态 = 四支语义色（底色与图标取同一支，不再手写 rgba） */
.toast-success { border-color: var(--au-success-border); }

.toast-success .toast-icon {
  background: var(--au-success-soft);
  color: var(--au-success);
}

.toast-error { border-color: var(--au-danger-border); }

.toast-error .toast-icon {
  background: var(--au-danger-soft);
  color: var(--au-danger);
}

.toast-warning { border-color: var(--au-warning-border); }

.toast-warning .toast-icon {
  background: var(--au-warning-soft);
  color: var(--au-warning);
}

.toast-info { border-color: var(--au-info-border); }

.toast-info .toast-icon {
  background: var(--au-info-soft);
  color: var(--au-info);
}

/* 过渡动画
   只做「淡入 + 轻微下移」，不碰水平位移：容器已经用 translateX(-50%) 居中了，
   子项再写一次 translateX(-50%) 会让每条通知先从左侧半个身位滑回来。 */
.toast-enter-active {
  transition: opacity var(--au-med) var(--au-ease),
              transform var(--au-med) var(--au-ease);
}

.toast-leave-active {
  transition: opacity var(--au-fast) var(--au-ease),
              transform var(--au-fast) var(--au-ease);
}

.toast-enter-from {
  opacity: 0;
  transform: translateY(-12px);
}

.toast-leave-to {
  opacity: 0;
  transform: translateY(-8px);
}

.toast-move {
  transition: transform var(--au-med) var(--au-ease);
}

/* 移动端适配 */
@media (max-width: 640px) {
  .toast-container {
    top: calc(66px + env(safe-area-inset-top, 0px));
    left: 1rem;
    right: 1rem;
    transform: none;
  }

  .toast {
    min-width: auto;
    width: 100%;
  }
}

/* 减少动态效果：只保留透明度变化 */
@media (prefers-reduced-motion: reduce) {
  .toast-enter-from,
  .toast-leave-to {
    transform: none;
  }

  .toast-close:active {
    transform: none;
  }
}
</style>
