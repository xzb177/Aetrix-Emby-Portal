<script setup lang="ts">
import { Megaphone, X } from 'lucide-vue-next'
import type { Component } from 'vue'
import type { Announcement } from '@/api'

export interface BannerFallback {
  icon: Component
  title: string
  text: string
}

defineProps<{
  loading: boolean
  banners: Announcement[]
  fallbackBanner: BannerFallback | null
}>()

defineEmits<{
  (e: 'close'): void
}>()
</script>

<template>
  <!-- 说明条幅：置顶公告（可关闭）优先；无公告时按账号状态兜底（公益服 / 未开通）。
       加载中不渲染，避免先出兜底再跳成公告。暗房影院：左侧一根琥珀发丝线，不铺色 -->
  <Transition name="banner">
    <div v-if="!loading && banners.length" class="notice-banner">
      <Megaphone :size="16" class="banner-icon" />
      <div class="banner-body">
        <strong class="banner-title">{{ banners[0].title }}</strong>
        <p class="banner-text">{{ banners[0].content }}</p>
      </div>
      <button class="banner-close" title="关闭" @click="$emit('close')">
        <X :size="14" />
      </button>
    </div>
    <div v-else-if="!loading && fallbackBanner" class="notice-banner">
      <component :is="fallbackBanner.icon" :size="16" class="banner-icon" />
      <div class="banner-body">
        <strong class="banner-title">{{ fallbackBanner.title }}</strong>
        <p class="banner-text">{{ fallbackBanner.text }}</p>
      </div>
    </div>
  </Transition>
</template>

<style scoped>
/* 从 HomeView 整体迁移（template + script + style） */
.notice-banner {
  display: flex;
  align-items: flex-start;
  gap: 0.75rem;
  margin-bottom: 1.25rem;
  padding: 0.875rem 1rem;
  background: var(--au-surface);
  border: 1px solid var(--au-border);
  border-left: 2px solid var(--au-primary);
  border-radius: var(--au-r-sm);
}

.banner-icon {
  flex-shrink: 0;
  margin-top: 0.125rem;
  color: var(--au-primary);
}

.banner-body {
  flex: 1;
  min-width: 0;
}

.banner-title {
  display: block;
  font-size: 0.875rem;
  font-weight: 600;
  color: var(--au-text);
}

.banner-text {
  margin: 0.25rem 0 0;
  font-size: 0.8125rem;
  line-height: 1.6;
  color: var(--au-text-2);
  white-space: pre-line;
}

.banner-close {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border: none;
  border-radius: var(--au-r-sm);
  background: transparent;
  color: var(--au-text-3);
  cursor: pointer;
}

.banner-close:hover {
  background: var(--au-surface-2);
  color: var(--au-text);
}

.banner-enter-active,
.banner-leave-active {
  transition: opacity var(--au-med) var(--au-ease);
}

.banner-enter-from,
.banner-leave-to {
  opacity: 0;
}
</style>
