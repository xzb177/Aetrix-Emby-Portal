<script setup lang="ts">
// 放映指南：纯静态 3 个 RouterLink，无数据、无事件
</script>

<template>
  <!-- 帮助中心：纯文字列表 + 发丝分隔线，不再有图标方块 -->
  <div class="program-head">
    <p class="program-eyebrow">服务台</p>
    <div class="program-row">
      <h2 class="program-title">放映指南</h2>
    </div>
  </div>
  <nav class="help-list" aria-label="帮助中心">
    <RouterLink to="/profile" class="help-row">
      <span class="help-row-body">
        <strong>连接播放器</strong>
        <em>Infuse / Forward 等客户端的服务器地址、账号与一键导入</em>
      </span>
      <span class="help-row-arrow" aria-hidden="true">›</span>
    </RouterLink>
    <RouterLink to="/game/wishlist" class="help-row">
      <span class="help-row-body">
        <strong>求片</strong>
        <em>库里没有想看的？去许愿池提交，入库后通知你</em>
      </span>
      <span class="help-row-arrow" aria-hidden="true">›</span>
    </RouterLink>
    <RouterLink to="/game/signal" class="help-row">
      <span class="help-row-body">
        <strong>联系客服</strong>
        <em>遇到问题发求救信号，客服会尽快回复</em>
      </span>
      <span class="help-row-arrow" aria-hidden="true">›</span>
    </RouterLink>
  </nav>
</template>

<style scoped>
/* 从 HomeView 整体迁移（template + style，本区块无 script 逻辑）。
   区块头排版（.program-*）已上浮为 L2 全局类（aurora.css），本组件直接复用。 */
.help-list {
  display: flex;
  flex-direction: column;
  border-top: 1px solid var(--au-border);
}

.help-row {
  display: flex;
  align-items: center;
  gap: 1rem;
  padding: 1rem 0.25rem;
  border-bottom: 1px solid var(--au-border);
  color: inherit;
  text-decoration: none;
}

.help-row-body {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  min-width: 0;
}

.help-row-body strong {
  font-size: 0.9375rem;
  font-weight: 600;
  color: var(--au-text);
  transition: color var(--au-fast) var(--au-ease);
}

.help-row-body em {
  font-style: normal;
  font-size: 0.8125rem;
  line-height: 1.5;
  color: var(--au-text-3);
}

.help-row-arrow {
  flex-shrink: 0;
  font-size: 1.25rem;
  line-height: 1;
  color: var(--au-text-4);
  transition: color var(--au-fast) var(--au-ease);
}

.help-row:hover .help-row-body strong,
.help-row:hover .help-row-arrow { color: var(--au-primary); }
</style>
