<script setup lang="ts">
import { RouterLink } from 'vue-router'
import { Home, ArrowLeft } from 'lucide-vue-next'
</script>

<template>
  <div class="nf-page">
    <div class="nf-inner">
      <p class="au-eyebrow nf-eyebrow">胶片未找到</p>
      <h1 class="nf-code">404</h1>
      <h2 class="nf-title">这一卷胶片不在片库里</h2>
      <p class="nf-desc">
        抱歉，您访问的页面不存在或已被移除
      </p>
      <div class="nf-actions">
        <button class="au-btn au-btn-ghost" @click="$router.back()">
          <ArrowLeft :size="16" />
          <span>返回</span>
        </button>
        <RouterLink to="/" class="au-btn au-btn-primary">
          <Home :size="16" />
          <span>首页</span>
        </RouterLink>
      </div>
    </div>
  </div>
</template>

<style scoped>
.nf-page {
  min-height: 100vh;
  min-height: 100dvh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 5rem 1rem calc(3rem + var(--au-dock-space));
}

.nf-inner {
  text-align: center;
  animation: au-fade-up var(--au-fade-in) var(--au-ease) both;
}

.nf-eyebrow {
  margin: 0 0 0.75rem;
}

.nf-code {
  margin: 0 0 0.75rem;
  font-size: clamp(4.5rem, 16vw, 8rem);
  font-weight: 700;
  line-height: 1;
  font-variant-numeric: tabular-nums;
  color: var(--au-text);
}

.nf-title {
  margin: 0 0 0.75rem;
  font-size: 1.375rem;
  font-weight: 700;
  color: var(--au-text);
}

.nf-desc {
  max-width: 28rem;
  margin: 0 auto 2rem;
  font-size: 0.875rem;
  color: var(--au-text-3);
}

.nf-actions {
  display: flex;
  justify-content: center;
  gap: 0.75rem;
}

@media (prefers-reduced-motion: reduce) {
  .nf-inner { animation: none; }
}
</style>
