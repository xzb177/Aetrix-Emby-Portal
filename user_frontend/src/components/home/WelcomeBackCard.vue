<!--
  「好久不见」欢迎卡（装修第二期 U2）：纯展示组件。
  - 标题固定"好久不见"；正文由调用方按继续观看第一条生成（或通用文案）；
  - 主行动"继续观看"复用首页现有的 Rex deep link（href prop），不另写路由、不新增请求；
  - 无 href 时不渲染按钮，不给死按钮。
  - 入场只动 opacity；prefers-reduced-motion 下直接呈现。
-->
<template>
  <section class="welcome-back au-anim-up" aria-label="欢迎回来">
    <div class="welcome-back__body">
      <p class="au-eyebrow welcome-back__eyebrow">好久不见</p>
      <p class="welcome-back__message">{{ message }}</p>
    </div>
    <a
      v-if="href"
      :href="href"
      class="au-btn au-btn-primary au-btn-sm welcome-back__cta"
    >
      继续观看
    </a>
  </section>
</template>

<script setup lang="ts">
defineProps<{
  /** 正文：按继续观看第一条生成（"上次看到《剧名》第 X 集，接着看吧"），无则通用文案 */
  message: string
  /** 继续观看的跳转地址（首页现有的 Rex deep link）；为空则不显示按钮 */
  href: string
}>()
</script>

<style scoped>
.welcome-back {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--au-space-md);
  padding: var(--au-space-md) var(--au-space-lg);
  background: var(--au-surface);
  border: 1px solid var(--au-border);
  border-radius: var(--au-r-lg);
  /* 暗房影院：左侧一根琥珀发丝线，不铺色（与公告条幅同一语言） */
  border-left: 2px solid var(--au-primary);
  /* 入场只动透明度，不位移、不阻塞 */
  animation: welcome-back-in var(--au-dur-fast) ease-out;
}

.welcome-back__body {
  min-width: 0;
}

.welcome-back__eyebrow {
  margin: 0 0 var(--au-space-xs);
}

.welcome-back__message {
  margin: 0;
  font-size: 0.9375rem;
  line-height: 1.55;
  color: var(--au-text-2);
}

.welcome-back__cta {
  flex-shrink: 0;
}

@keyframes welcome-back-in {
  from { opacity: 0; }
  to { opacity: 1; }
}

@media (prefers-reduced-motion: reduce) {
  .welcome-back {
    animation: none;
  }
}
</style>
