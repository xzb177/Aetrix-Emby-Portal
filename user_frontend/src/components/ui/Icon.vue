<template>
  <span
    :class="['ui-icon', `ui-icon--${size}`, { 'ui-icon--spin': spin }]"
    :style="colorStyle"
    v-html="svgContent"
  />
</template>

<script setup lang="ts">
import { computed } from 'vue'

export type IconSize = 'xs' | 'sm' | 'md' | 'lg' | 'xl' | '2xl'

export interface IconProps {
  name: string
  size?: IconSize
  color?: string
  spin?: boolean
}

const props = withDefaults(defineProps<IconProps>(), {
  size: 'md',
  spin: false
})

// 图标尺寸规范（单位：px）
const iconSizes: Record<IconSize, number> = {
  xs: 14,
  sm: 16,
  md: 20,
  lg: 24,
  xl: 32,
  '2xl': 48
}

const currentSize = computed(() => iconSizes[props.size])

const colorStyle = computed(() => {
  if (!props.color) return {}
  return { color: props.color }
})

// 图标库（SVG paths）
const icons: Record<string, string> = {
  // 导航图标
  'home': '<path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><polyline points="9 22 9 12 15 12 15 22" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
  'user': '<path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><circle cx="12" cy="7" r="4" fill="none" stroke="currentColor" stroke-width="2"/>',
  'settings': '<circle cx="12" cy="12" r="3" fill="none" stroke="currentColor" stroke-width="2"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',

  // 操作图标
  'search': '<circle cx="11" cy="11" r="8" fill="none" stroke="currentColor" stroke-width="2"/><line x1="21" y1="21" x2="16.65" y2="16.65" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  'close': '<line x1="18" y1="6" x2="6" y2="18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><line x1="6" y1="6" x2="18" y2="18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  'check': '<polyline points="20 6 9 17 4 12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
  'plus': '<line x1="12" y1="5" x2="12" y2="19" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><line x1="5" y1="12" x2="19" y2="12" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  'minus': '<line x1="5" y1="12" x2="19" y2="12" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  'edit': '<path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
  'delete': '<polyline points="3 6 5 6 21 6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
  'refresh': '<polyline points="23 4 23 10 17 10" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><polyline points="1 20 1 14 7 14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',

  // 状态图标
  'success': '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><polyline points="22 4 12 14.01 9 11.01" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
  'error': '<circle cx="12" cy="12" r="10" fill="none" stroke="currentColor" stroke-width="2"/><line x1="15" y1="9" x2="9" y2="15" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><line x1="9" y1="9" x2="15" y2="15" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  'warning': '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><line x1="12" y1="9" x2="12" y2="13" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><line x1="12" y1="17" x2="12.01" y2="17" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  'info': '<circle cx="12" cy="12" r="10" fill="none" stroke="currentColor" stroke-width="2"/><line x1="12" y1="16" x2="12" y2="12" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><line x1="12" y1="8" x2="12.01" y2="8" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',

  // 箭头图标
  'arrow-up': '<line x1="12" y1="19" x2="12" y2="5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><polyline points="5 12 12 5 19 12" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
  'arrow-down': '<line x1="12" y1="5" x2="12" y2="19" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><polyline points="19 12 12 19 5 12" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
  'arrow-left': '<line x1="19" y1="12" x2="5" y2="12" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><polyline points="12 19 5 12 12 5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
  'arrow-right': '<line x1="5" y1="12" x2="19" y2="12" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><polyline points="12 5 19 12 12 19" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
  'chevron-down': '<polyline points="6 9 12 15 18 9" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
  'chevron-up': '<polyline points="18 15 12 9 6 15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',

  // 文件图标
  'file': '<path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><polyline points="13 2 13 9 20 9" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
  'folder': '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',

  // 媒体图标
  'image': '<rect x="3" y="3" width="18" height="18" rx="2" ry="2" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="8.5" cy="8.5" r="1.5" fill="currentColor"/><polyline points="21 15 16 10 5 21" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
  'play': '<polygon points="5 3 19 12 5 21 5 3" fill="currentColor"/>',
  'pause': '<rect x="6" y="4" width="4" height="16" fill="currentColor"/><rect x="14" y="4" width="4" height="16" fill="currentColor"/>',

  // 通信图标
  'mail': '<path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z" fill="none" stroke="currentColor" stroke-width="2"/><polyline points="22,6 12,13 2,6" fill="none" stroke="currentColor" stroke-width="2"/>',
  'bell': '<path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><path d="M13.73 21a2 2 0 0 1-3.46 0" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',

  // 加载图标
  'loader': '<line x1="12" y1="2" x2="12" y2="6" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><line x1="12" y1="18" x2="12" y2="22" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><line x1="4.93" y1="4.93" x2="7.76" y2="7.76" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><line x1="16.24" y1="16.24" x2="19.07" y2="19.07" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><line x1="2" y1="12" x2="6" y2="12" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><line x1="18" y1="12" x2="22" y2="12" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><line x1="4.93" y1="19.07" x2="7.76" y2="16.24" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><line x1="16.24" y1="7.76" x2="19.07" y2="4.93" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
}

const svgContent = computed(() => {
  const iconPath = icons[props.name]
  if (!iconPath) {
    console.warn(`Icon "${props.name}" not found`)
    return ''
  }
  return `<svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">${iconPath}</svg>`
})
</script>

<style scoped>
.ui-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  stroke: currentColor;
}

.ui-icon--xs { width: 14px; height: 14px; }
.ui-icon--sm { width: 16px; height: 16px; }
.ui-icon--md { width: 20px; height: 20px; }
.ui-icon--lg { width: 24px; height: 24px; }
.ui-icon--xl { width: 32px; height: 32px; }
.ui-icon--2xl { width: 48px; height: 48px; }

.ui-icon :deep(svg) {
  width: 100%;
  height: 100%;
}

.ui-icon--spin :deep(svg) {
  animation: spin 1s linear infinite;
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

@media (prefers-reduced-motion: reduce) {
  .ui-icon--spin :deep(svg) {
    animation: none;
  }
}
</style>
