<template>
  <Teleport to="body">
    <Transition name="modal-backdrop">
      <div
        v-if="modelValue"
        class="modal-backdrop"
        @click="handleBackdropClick"
      />
    </Transition>

    <Transition name="modal-content">
      <div
        v-if="modelValue"
        :class="['modal-container', { 'modal-container--centered': centered }]"
        role="dialog"
        aria-modal="true"
      >
        <div
          :class="['modal-content-wrapper', `modal--${size}`]"
          @click.stop
        >
          <div v-if="$slots.header" class="modal-header">
            <slot name="header">
              <h3 class="modal-title">{{ title }}</h3>
            </slot>
            <button
              v-if="closable"
              type="button"
              class="modal-close"
              @click="handleClose"
              aria-label="关闭"
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <line x1="18" y1="6" x2="6" y2="18"/>
                <line x1="6" y1="6" x2="18" y2="18"/>
              </svg>
            </button>
          </div>

          <div class="modal-body" :class="{ 'modal-body--no-padding': noPadding }">
            <slot />
          </div>

          <div v-if="$slots.footer" class="modal-footer">
            <slot name="footer" />
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
export type ModalSize = 'sm' | 'md' | 'lg' | 'xl' | 'full'

export interface ModalProps {
  modelValue: boolean
  title?: string
  size?: ModalSize
  centered?: boolean
  closable?: boolean
  closeOnBackdrop?: boolean
  noPadding?: boolean
}

const props = withDefaults(defineProps<ModalProps>(), {
  size: 'md',
  centered: true,
  closable: true,
  closeOnBackdrop: true,
  noPadding: false
})

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  close: []
  open: []
}>()

// ==================== 滚动锁定 ====================
// 锁定背景滚动
const lockScroll = () => {
  document.body.style.overflow = 'hidden'
  document.body.style.position = 'fixed'
  document.body.style.width = '100%'
}

// 解锁背景滚动
const unlockScroll = () => {
  document.body.style.overflow = ''
  document.body.style.position = ''
  document.body.style.width = ''
}

// 监听 modelValue 变化，控制滚动锁定
watch(() => props.modelValue, (isOpen) => {
  if (isOpen) {
    lockScroll()
    emit('open')
  } else {
    unlockScroll()
  }
})

// ==================== 关闭逻辑 ====================
const handleClose = () => {
  emit('update:modelValue', false)
  emit('close')
}

const handleBackdropClick = () => {
  if (props.closeOnBackdrop) {
    handleClose()
  }
}

// ==================== ESC 键关闭 ====================
const handleKeydown = (e: KeyboardEvent) => {
  if (e.key === 'Escape' && props.modelValue && props.closable) {
    handleClose()
  }
}

if (typeof window !== 'undefined') {
  onMounted(() => {
    document.addEventListener('keydown', handleKeydown)
  })
  onUnmounted(() => {
    document.removeEventListener('keydown', handleKeydown)
    // 组件卸载时确保解锁滚动
    unlockScroll()
  })
}
</script>

<script lang="ts">
import { watch } from 'vue'
import { onMounted, onUnmounted } from 'vue'
export default {
  name: 'Modal'
}
</script>

<style scoped>
/* ==================== 遮罩层 ==================== */
.modal-backdrop {
  position: fixed;
  inset: 0;
  z-index: var(--neo-z-overlay, 80);
  background: var(--au-scrim);
}

/* 减少 prefers-reduced-motion 的动效 */
@media (prefers-reduced-motion: reduce) {
  .modal-backdrop {
    backdrop-filter: none;
  }
}

/* 暗房影院：遮罩是纯黑场，不做毛玻璃 */

/* ==================== 容器 ==================== */
.modal-container {
  position: fixed;
  inset: 0;
  z-index: var(--neo-z-modal, 100);
  overflow-y: auto;
  display: flex;
  padding: 1rem;
  /* Safe Area 支持 */
  padding-bottom: max(1rem, env(safe-area-inset-bottom, 0px));
}

.modal-container--centered {
  align-items: center;
  justify-content: center;
}

/* ==================== 内容卡片 ==================== */
.modal-content-wrapper {
  position: relative;
  width: 100%;
  background: var(--au-surface);
  border: 1px solid var(--au-border-strong);
  border-radius: var(--neo-radius-lg, 18px);
  box-shadow: var(--neo-shadow-lg, 0 8px 32px rgba(0, 0, 0, 0.6));
  display: flex;
  flex-direction: column;
  max-height: calc(100vh - 2rem);
  max-height: calc(100dvh - 2rem); /* R27：移动端地址栏收放高度不跳 */
}

/* ==================== 尺寸变体 ==================== */
.modal--sm { max-width: 400px; }
.modal--md { max-width: 540px; }
.modal--lg { max-width: 720px; }
.modal--xl { max-width: 960px; }
.modal--full {
  max-width: 100%;
  height: 100%;
  max-height: 100vh;
  max-height: 100dvh; /* R27：移动端地址栏收放高度不跳 */
  border-radius: 0;
}

/* ==================== 头部 ==================== */
.modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 1.25rem 1.5rem;
  border-bottom: 1px solid var(--neo-border-subtle);
  flex-shrink: 0;
}

.modal-title {
  font-size: 1.125rem;
  font-weight: 700;
  color: var(--neo-text-primary);
  margin: 0;
}

.modal-close {
  width: 32px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: none;
  border-radius: var(--neo-radius-xs, 8px);
  color: var(--neo-text-tertiary);
  cursor: pointer;
  transition: all var(--neo-duration-fast, 150ms) var(--neo-ease-default);
  flex-shrink: 0;
}

.modal-close:hover {
  background: var(--neo-bg-surface-hover);
  color: var(--neo-text-primary);
}

.modal-close:active {
  transform: scale(var(--neo-scale-press, 0.98));
}

.modal-close svg {
  width: 18px;
  height: 18px;
}

/* ==================== 内容区 ==================== */
.modal-body {
  padding: 1.5rem;
  overflow-y: auto;
  flex: 1;
}

.modal-body--no-padding {
  padding: 0;
}

/* 隐藏滚动条但保留功能 */
.modal-body::-webkit-scrollbar {
  width: 4px;
}

.modal-body::-webkit-scrollbar-track {
  background: transparent;
}

.modal-body::-webkit-scrollbar-thumb {
  background: var(--neo-border-subtle);
  border-radius: 2px;
}

.modal-body::-webkit-scrollbar-thumb:hover {
  background: var(--neo-border-default);
}

/* ==================== 底部 ==================== */
.modal-footer {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 0.75rem;
  padding: 1rem 1.5rem;
  border-top: 1px solid var(--neo-border-subtle);
  flex-shrink: 0;
}

/* ==================== 动画 ==================== */
/* 背景进入/离开动画 */
.modal-backdrop-enter-active,
.modal-backdrop-leave-active {
  transition: opacity var(--neo-duration-normal, 200ms) var(--neo-ease-default);
}

.modal-backdrop-enter-from,
.modal-backdrop-leave-to {
  opacity: 0;
}

/* 内容进入/离开动画 */
.modal-content-enter-active {
  transition: all var(--neo-duration-slow, 300ms) cubic-bezier(0.16, 1, 0.3, 1);
}

.modal-content-leave-active {
  transition: all var(--neo-duration-normal, 200ms) ease-in;
}

.modal-content-enter-from {
  opacity: 0;
  transform: scale(0.95) translateY(10px);
}

.modal-content-leave-to {
  opacity: 0;
  transform: scale(0.95);
}

/* ==================== 动效降级 ==================== */
@media (prefers-reduced-motion: reduce) {
  .modal-close:active {
    transform: none;
  }

  .modal-backdrop-enter-active,
  .modal-backdrop-leave-active,
  .modal-content-enter-active,
  .modal-content-leave-active {
    transition: opacity 0.1s;
  }

  .modal-content-enter-from,
  .modal-content-leave-to {
    transform: none;
  }
}

/* ==================== 响应式 ==================== */
@media (max-width: 640px) {
  .modal-container {
    padding: 0;
    padding-bottom: env(safe-area-inset-bottom, 0px);
    align-items: flex-end;
  }

  .modal-content-wrapper {
    max-height: 85vh;
    border-radius: var(--neo-radius-lg, 18px) var(--neo-radius-lg, 18px) 0 0;
  }

  .modal--sm,
  .modal--md,
  .modal--lg,
  .modal--xl {
    max-width: 100%;
  }

  .modal-content-enter-from {
    transform: translateY(100%);
  }
}
</style>
