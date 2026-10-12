<template>
  <nav class="v4-tabbar" aria-label="面板导航">
    <router-link to="/" exact-active-class="on" aria-label="发现">
      <span class="ic">🧭</span>发现
    </router-link>
    <router-link to="/collector" active-class="on" aria-label="收藏家">
      <span class="ic">🏅</span>收藏家
    </router-link>
    <router-link to="/server" active-class="on" aria-label="服务器">
      <span class="ic">🌱</span>服务器
    </router-link>
  </nav>
</template>

<script setup lang="ts">
// v4 面板底部导航：样式全部来自 v4-paper.css（设计稿原值）
// iOS 安全区在 CSS 层处理：padding-bottom: calc(10px + env(safe-area-inset-bottom))
</script>
