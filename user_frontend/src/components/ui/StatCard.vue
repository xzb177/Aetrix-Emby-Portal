<script setup lang="ts">
/**
 * StatCard - 统计卡片组件
 *
 * 用于展示关键指标数据，包含图标、数值、标签和趋势。
 *
 * @props
 * - icon: 图标组件
 * - value: 数值（支持带单位的字符串）
 * - label: 标签文字
 * - trend: 趋势文字（如 '+12%'）
 * - trendUp: 趋势是否向上（绿色为正，红色为负）
 * - color: 主题颜色 ('success' | 'warning' | 'danger' | 'info')
 */

interface Props {
  icon?: string
  value: string | number
  label: string
  trend?: string
  trendUp?: boolean
  color?: 'success' | 'warning' | 'danger' | 'info'
}

const props = withDefaults(defineProps<Props>(), {
  color: 'success',
  trendUp: true
})
</script>

<template>
  <div class="ui-stat-card">
    <div v-if="icon" class="ui-stat-card__icon" :class="`ui-stat-card__icon--${color}`">
      <img decoding="async" loading="lazy" v-if="icon" :src="icon" alt="" class="ui-stat-card__icon-img">
    </div>
    <div class="ui-stat-card__content">
      <div class="ui-stat-card__value">{{ value }}</div>
      <div class="ui-stat-card__label">{{ label }}</div>
    </div>
    <div v-if="trend" class="ui-stat-card__trend" :class="{ 'ui-stat-card__trend--up': trendUp }">
      {{ trend }}
    </div>
  </div>
</template>

<style scoped>
.ui-stat-card {
  display: flex;
  align-items: center;
  gap: var(--neo-space-3);
  background: var(--neo-bg-surface-1);
  border: 1px solid var(--neo-border-default);
  border-radius: var(--neo-radius-lg);
  padding: var(--neo-space-4);
  box-shadow: var(--neo-shadow-sm);
  transition: background-color var(--neo-duration-fast) var(--neo-ease-default);
}

.ui-stat-card:active {
  background: var(--neo-bg-surface-hover);
}

/* 图标容器 */
.ui-stat-card__icon {
  width: 40px;
  height: 40px;
  border-radius: var(--neo-radius-xs);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.ui-stat-card__icon--success { background: var(--neo-success-bg); }
.ui-stat-card__icon--warning { background: var(--neo-warning-bg); }
.ui-stat-card__icon--danger { background: var(--neo-danger-bg); }
.ui-stat-card__icon--info { background: var(--neo-info-bg); }

.ui-stat-card__icon-img {
  width: 24px;
  height: 24px;
}

/* 内容区域 */
.ui-stat-card__content {
  flex: 1;
  min-width: 0;
}

.ui-stat-card__value {
  font-size: var(--neo-font-size-3xl);
  font-weight: var(--neo-font-weight-semibold);
  line-height: var(--neo-line-height-tight);
  color: var(--neo-text-primary);
}

.ui-stat-card__label {
  font-size: var(--neo-font-size-sm);
  font-weight: var(--neo-font-weight-normal);
  line-height: var(--neo-line-height-normal);
  color: var(--neo-text-tertiary);
  margin-top: 2px;
}

/* 趋势 */
.ui-stat-card__trend {
  font-size: var(--neo-font-size-sm);
  font-weight: var(--neo-font-weight-medium);
  color: var(--neo-danger);
  flex-shrink: 0;
}

.ui-stat-card__trend--up {
  color: var(--neo-success);
}
</style>
