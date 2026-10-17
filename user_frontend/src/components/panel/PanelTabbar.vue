<template>
  <nav class="v4-tabbar" aria-label="主导航">
    <router-link
      v-for="item in primaryNav"
      :key="item.path"
      :to="item.path"
      :class="{ on: activePath === item.path }"
      :aria-current="activePath === item.path ? 'page' : undefined"
      :aria-label="item.name"
    >
      <span class="ic"><component :is="item.icon" :size="22" :stroke-width="activePath === item.path ? 2.2 : 1.8" /></span>{{ item.name }}
    </router-link>
  </nav>
</template>

<script setup lang="ts">
/**
 * v4 底部导航（≤768px 唯一一套，由 App.vue 全局常驻渲染）。
 * 条目与桌面顶栏完全同源（src/config/navigation.ts 的 primaryNav），
 * 激活判定走 resolveActiveNavPath（最长前缀匹配）。
 * 样式全部来自 v4-paper.css 的 .v4-tabbar；桌面 ≥769px 由 CSS 隐藏。
 * iOS 安全区在 CSS 层处理：padding-bottom: calc(10px + env(safe-area-inset-bottom))。
 */
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { primaryNav, resolveActiveNavPath } from '@/config/navigation'

const route = useRoute()
const activePath = computed(() => resolveActiveNavPath(route.path))
</script>
