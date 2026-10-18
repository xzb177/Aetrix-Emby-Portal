<script setup lang="ts">
/**
 * 数据瓦片（暗房影院）：小标签 + 大数字 + 一行脚注，可整块点进明细页。
 *
 * 配色口径（同用户端资产卡）：数字一律走正文色，**颜色只用来提示「有没有要看的」**——
 * tone=warn / danger 时数字与图标染警示色、描边加一档；ok / info 只染图标；accent 让数字
 * 走琥珀（全页最多一两个，比如「累计营收」）。
 * 传 to 就渲染成 RouterLink（悬停描边提亮），否则是静态 div。用法见 ./README.md。
 */
import { computed } from 'vue'
import { RouterLink, type RouteLocationRaw } from 'vue-router'

const props = withDefaults(
  defineProps<{
    label: string
    value: string | number
    /** 跟在数字后面的小字（「台 · 可用 2」「/ 120」） */
    suffix?: string
    /** 数字下面的一行脚注，允许换行 */
    hint?: string
    /** 标签前的图标（lucide 组件本身） */
    icon?: unknown
    tone?: 'plain' | 'ok' | 'info' | 'warn' | 'danger' | 'accent'
    /** 点进去的明细页（带筛选的深链也行，如 /tickets?status=open） */
    to?: RouteLocationRaw
    /** 大号图标布局：图标放左侧方块里（仪表盘顶部 KPI 用） */
    layout?: 'stack' | 'icon-left'
    /** 悬停提示 */
    title?: string
  }>(),
  { suffix: '', hint: '', icon: undefined, tone: 'plain', to: undefined, layout: 'stack', title: undefined },
)

const tag = computed(() => (props.to ? RouterLink : 'div'))
</script>

<template>
  <component
    :is="tag"
    :to="to"
    class="au-stat"
    :class="[`tone-${tone}`, `layout-${layout}`, { 'is-link': !!to }]"
    :title="title"
  >
    <span v-if="icon && layout === 'icon-left'" class="au-stat__badge">
      <component :is="icon" :size="16" />
    </span>
    <span class="au-stat__body">
      <span class="au-stat__label">
        <component :is="icon" v-if="icon && layout === 'stack'" :size="13" class="au-stat__icon" />
        {{ label }}
        <slot name="label-extra" />
      </span>
      <span class="au-stat__value">
        <slot name="value">{{ value }}</slot><span v-if="suffix" class="au-stat__suffix">{{ suffix }}</span>
      </span>
      <span v-if="hint || $slots.hint" class="au-stat__hint">
        <slot name="hint">{{ hint }}</slot>
      </span>
    </span>
  </component>
</template>

<style scoped>
.au-stat {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  min-width: 0;
  padding: 16px 18px;
  background: var(--au-surface);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-lg);
  color: var(--au-text);
  text-decoration: none;
  /* ui/beautify-20261018：顶缘高光；动效只走 transform / border-color / box-shadow */
  box-shadow: var(--au-card-highlight);
  transition: transform var(--au-fast) var(--au-ease), border-color var(--au-fast) var(--au-ease),
    background var(--au-fast) var(--au-ease), box-shadow var(--au-fast) var(--au-ease);
}

.au-stat.is-link { cursor: pointer; }
.au-stat.is-link:hover {
  border-color: var(--au-border-strong);
  background: var(--au-surface-2);
  /* ui/beautify-20261018：悬停抬升（与验收「无阴影」行的口径冲突见 PR 正文，暂按任务条文实现） */
  transform: translateY(-2px);
  box-shadow: inset 0 1px 0 rgba(245, 235, 220, 0.06), 0 4px 16px rgba(0, 0, 0, 0.12);
}
.au-stat.is-link:focus-visible { outline: 2px solid var(--au-border-focus); outline-offset: 2px; }

.au-stat__badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  flex: 0 0 34px;
  border-radius: var(--au-r-md);
  background: var(--au-surface-2);
  border: 1px solid var(--au-border);
  color: var(--au-text-3);
}

.au-stat__body { display: flex; flex-direction: column; min-width: 0; flex: 1; }

.au-stat__label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  font-weight: 500;
  letter-spacing: 0.04em;
  color: var(--au-text-3);
}

.au-stat__icon { color: var(--au-text-4); flex-shrink: 0; }

.au-stat__value {
  margin-top: 6px;
  font-size: 1.75rem;
  font-weight: 700;
  line-height: 1.1;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
  color: var(--au-text);
  overflow-wrap: anywhere;
}

.au-stat__suffix {
  margin-left: 4px;
  font-size: 13px;
  font-weight: 500;
  letter-spacing: 0;
  color: var(--au-text-3);
}

.au-stat__hint {
  margin-top: 6px;
  font-size: 12px;
  line-height: 1.45;
  color: var(--au-text-3);
  overflow-wrap: anywhere;
}

/* 状态包：颜色只用来提示「有没有要看的」，不当装饰 */
.tone-ok .au-stat__badge,
.tone-ok .au-stat__icon { color: var(--au-success); }
.tone-ok .au-stat__badge { background: var(--au-success-soft); border-color: var(--au-success-border); }
.tone-info .au-stat__badge,
.tone-info .au-stat__icon { color: var(--au-info); }
.tone-info .au-stat__badge { background: var(--au-info-soft); border-color: var(--au-info-border); }
.tone-accent .au-stat__value { color: var(--au-primary); }
.tone-accent .au-stat__badge,
.tone-accent .au-stat__icon { color: var(--au-primary); }
.au-stat.tone-warn { border-color: var(--au-warning-border); }
.tone-warn .au-stat__value,
.tone-warn .au-stat__icon,
.tone-warn .au-stat__badge { color: var(--au-warning); }
.tone-warn .au-stat__badge { background: var(--au-warning-soft); border-color: var(--au-warning-border); }
.au-stat.tone-danger { border-color: var(--au-danger-border); }
.tone-danger .au-stat__value,
.tone-danger .au-stat__icon,
.tone-danger .au-stat__badge { color: var(--au-danger); }
.tone-danger .au-stat__badge { background: var(--au-danger-soft); border-color: var(--au-danger-border); }

@media (max-width: 640px) {
  .au-stat { padding: 14px; gap: 10px; }
  .au-stat__value { font-size: 1.375rem; }
}
</style>
