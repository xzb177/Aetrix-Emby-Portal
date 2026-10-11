<script setup lang="ts">
/**
 * 底部导航坞（≤768px 专用）
 *
 * 借鉴纸片人控制台的移动端形态：拇指区常驻 Tab 栏——毛玻璃底 + 1px 顶边 +
 * 当前项品牌色软底胶囊高亮。条目与顶栏主导航完全同源（primaryNav），
 * 「一个语义只有一个入口」：≥769px 整个坞不渲染，顶栏仍是唯一导航；
 * ≤768px 顶栏退成单行（品牌 + 资产 pill + 账号），主导航交给这里。
 *
 * 登录页与播放页沿用 App.vue 的 showChrome 口径，同样不渲染。
 */
import { RouterLink, useRoute } from 'vue-router'
import { primaryNav } from '@/config/navigation'

const route = useRoute()

function isActive(path: string) {
  if (path === '/') return route.path === '/'
  return route.path.startsWith(path)
}
</script>

<template>
  <nav class="app-dock" aria-label="底部导航">
    <RouterLink
      v-for="item in primaryNav"
      :key="item.path"
      :to="item.path"
      class="dock-item"
      :class="{ 'dock-item-active': isActive(item.path) }"
      :aria-current="isActive(item.path) ? 'page' : undefined"
    >
      <span class="dock-ic">
        <component :is="item.icon" :size="20" :stroke-width="isActive(item.path) ? 2.2 : 1.8" />
      </span>
      <span class="dock-label">{{ item.name }}</span>
    </RouterLink>
  </nav>
</template>

<style scoped>
.app-dock {
  position: fixed;
  left: 0;
  right: 0;
  bottom: 0;
  /* 40：高于页面内容、低于顶栏下拉菜单（60）。三者同在一个顶栏堆叠上下文
     （header 的 z-index:50 + backdrop-filter）里比拼，之前坞挂 50 且 DOM 靠后，
     把下拉菜单的下缘盖住——移动端外观切换「点不到」的根源 */
  z-index: 40;
  display: none;
  align-items: stretch;
  justify-content: space-around;
  padding: 0 0.5rem env(safe-area-inset-bottom);
  /* 暗房影院：页面底色 0.96 + 模糊 + 发丝线（与顶栏同一配方），滚过去的字不再透出来 */
  background: var(--au-chrome);
  -webkit-backdrop-filter: saturate(1.4) blur(16px);
  backdrop-filter: saturate(1.4) blur(16px);
  border-top: 1px solid var(--au-border);
}

.dock-item {
  flex: 1;
  max-width: 96px;
  /* 触控区 ≥48px（图标 26px + 标签 + 内边距，实测约 55px，这里显式兜底） */
  min-height: 48px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 2px;
  padding: 0.375rem 0 0.25rem;
  text-decoration: none;
  color: var(--au-text-3);
  /* 按压反馈：纯 CSS :active 缩放，零 JS、零布局抖动。
     reduced-motion 下 --neo-scale-press=1、--neo-duration-fast=0ms，自动无缩放 */
  transition:
    color var(--au-fast) var(--au-ease),
    transform var(--neo-duration-fast, 150ms) var(--neo-ease-default, cubic-bezier(0.4, 0, 0.2, 1));
}

.dock-item:active {
  transform: scale(0.96);
}

.dock-ic {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 44px;
  height: 26px;
  border-radius: var(--au-r-full);
  transition: background var(--au-fast) var(--au-ease);
}

.dock-label {
  font-size: 0.8125rem;
  font-weight: 600;
  letter-spacing: 0.02em;
}

.dock-item-active { color: var(--au-text); }
.dock-item-active .dock-ic { color: var(--au-primary); }

/* 当前页：图标下方一颗琥珀指示点（与顶栏导航同一语言），不再铺色块 */
.dock-ic { position: relative; }
.dock-item-active .dock-ic::after {
  content: '';
  position: absolute;
  bottom: -3px;
  left: 50%;
  width: 4px;
  height: 4px;
  margin-left: -2px;
  border-radius: 50%;
  background: var(--au-primary);
}

@media (max-width: 768px) {
  .app-dock { display: flex; }
}
</style>
