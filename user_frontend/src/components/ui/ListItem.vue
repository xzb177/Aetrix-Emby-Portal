<script setup lang="ts">
/**
 * ListItem - 列表项组件
 *
 * 用于列表中的单个条目，支持图标、标题、描述、右侧内容和点击。
 *
 * @props
 * - icon: 左侧图标 URL
 * - title: 主标题
 * - description: 副标题/描述文字
 * - value: 右侧显示的值
 * - clickable: 是否可点击
 * - active: 是否处于激活状态
 * - divider: 是否显示底部分隔线
 */

interface Props {
  icon?: string
  title?: string
  description?: string
  value?: string
  clickable?: boolean
  active?: boolean
  divider?: boolean
}

defineProps<Props>()
</script>

<template>
  <div
    class="ui-list-item"
    :class="{
      'ui-list-item--clickable': clickable,
      'ui-list-item--active': active
    }"
  >
    <div v-if="icon || $slots.icon" class="ui-list-item__icon">
      <slot name="icon">
        <img decoding="async" loading="lazy" v-if="icon" :src="icon" alt="" class="ui-list-item__icon-img">
      </slot>
    </div>

    <div class="ui-list-item__content">
      <div v-if="title || $slots.title" class="ui-list-item__title">
        <slot name="title">{{ title }}</slot>
      </div>
      <div v-if="description || $slots.description" class="ui-list-item__description">
        <slot name="description">{{ description }}</slot>
      </div>
    </div>

    <div v-if="value || $slots.value" class="ui-list-item__value">
      <slot name="value">{{ value }}</slot>
    </div>

    <div v-if="clickable" class="ui-list-item__arrow">
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
        <path d="M6 3L11 8L6 13" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
    </div>

    <div v-if="divider" class="ui-list-item__divider"></div>
  </div>
</template>

<style scoped>
.ui-list-item {
  display: flex;
  align-items: center;
  padding: var(--neo-space-4) 0;
  position: relative;
}

.ui-list-item--clickable {
  cursor: pointer;
  user-select: none;
  transition: background-color var(--neo-duration-fast) var(--neo-ease-default);
}

.ui-list-item--clickable:active {
  opacity: 0.7;
}

.ui-list-item__icon {
  width: 40px;
  height: 40px;
  border-radius: var(--neo-radius-xs);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  margin-right: var(--neo-space-3);
  background: var(--neo-bg-surface-1);
}

.ui-list-item__icon-img {
  width: 24px;
  height: 24px;
}

.ui-list-item__content {
  flex: 1;
  min-width: 0;
}

.ui-list-item__title {
  font-size: var(--neo-font-size-md);
  font-weight: var(--neo-font-weight-medium);
  line-height: var(--neo-line-height-normal);
  color: var(--neo-text-primary);
}

.ui-list-item__description {
  font-size: var(--neo-font-size-sm);
  font-weight: var(--neo-font-weight-normal);
  line-height: var(--neo-line-height-normal);
  color: var(--neo-text-tertiary);
  margin-top: 2px;
}

.ui-list-item__value {
  font-size: var(--neo-font-size-md);
  font-weight: var(--neo-font-weight-normal);
  color: var(--neo-text-tertiary);
  margin-right: var(--neo-space-2);
  flex-shrink: 0;
}

.ui-list-item__arrow {
  color: var(--neo-text-tertiary);
  margin-left: var(--neo-space-2);
  flex-shrink: 0;
}

.ui-list-item__divider {
  position: absolute;
  bottom: 0;
  left: 56px;
  right: 0;
  height: 1px;
  background: var(--neo-divider);
}

.ui-list-item--active .ui-list-item__title {
  color: var(--neo-primary);
}
</style>
