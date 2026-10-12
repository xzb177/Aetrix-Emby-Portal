<script setup lang="ts">
import { ChevronRight } from 'lucide-vue-next'
import type { Component } from 'vue'

export interface TodoRow {
  key: string
  to: string
  icon: Component
  label: string
  sub: string
  value: string
  hot: boolean
}

defineProps<{
  loading: boolean
  rows: TodoRow[]
}>()
</script>

<template>
  <!-- 进行中：自己提交的求片 / 工单处理到哪了 -->
  <div class="program-head">
    <p class="program-eyebrow">等候区</p>
    <div class="program-row">
      <h2 class="program-title">等片中</h2>
    </div>
  </div>
  <section v-if="loading" class="todo-card" aria-hidden="true">
    <div class="au-skeleton sk-panel-row"></div>
    <div class="au-skeleton sk-panel-row short"></div>
  </section>
  <section v-else class="todo-card au-anim-up">
    <RouterLink v-for="t in rows" :key="t.key" :to="t.to" class="todo-row">
      <component :is="t.icon" :size="16" class="todo-icon" />
      <span class="todo-body">
        <span class="todo-label">{{ t.label }}</span>
        <span class="todo-sub">{{ t.sub }}</span>
      </span>
      <span v-if="t.value" class="todo-value" :class="{ hot: t.hot }">{{ t.value }}</span>
      <ChevronRight :size="14" class="todo-arrow" />
    </RouterLink>
  </section>
</template>

<style scoped>
/* 从 HomeView 整体迁移（template + script + style）。
   区块头排版（.program-*）已上浮为 L2 全局类（aurora.css），本组件直接复用。 */
.todo-card {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  background: var(--au-surface);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-lg);
}

section.todo-card[aria-hidden='true'] { padding: 1rem; }

/* 等片中区块对全局入场动画的真实覆盖：骨架态不需要 gap（从 HomeView 迁移，§2.5 红线②） */
.todo-card.au-anim-up { gap: 0; }

.todo-row {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0.875rem 1rem;
  color: inherit;
  text-decoration: none;
  transition: background var(--au-fast) var(--au-ease);
}

.todo-row + .todo-row { border-top: 1px solid var(--au-border); }
.todo-row:first-child { border-radius: var(--au-r-lg) var(--au-r-lg) 0 0; }
.todo-row:last-child { border-radius: 0 0 var(--au-r-lg) var(--au-r-lg); }
.todo-row:hover { background: var(--au-surface-2); }

.todo-icon {
  flex-shrink: 0;
  color: var(--au-text-3);
}

.todo-body {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 0.125rem;
  min-width: 0;
}

.todo-label {
  font-size: 0.9375rem;
  font-weight: 600;
  color: var(--au-text);
}

.todo-sub {
  font-size: 0.8125rem;
  color: var(--au-text-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.todo-value {
  font-family: var(--au-font-serif);
  font-size: 1.25rem;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  color: var(--au-text-3);
}

.todo-value.hot { color: var(--au-primary); }

.todo-arrow {
  flex-shrink: 0;
  color: var(--au-text-4);
}

.sk-panel-row { height: 44px; }
.sk-panel-row.short { width: 60%; }
</style>
